"""ADR 022 — eTranslation (European Commission) translation provider.

eTranslation's REST v2 API is fully asynchronous and push-only: a submit call
returns only a request id, and the translation is later POSTed to a URL named
in the request. This project's CLI has no public address, so the result lands
in a small receiver deployed on Vercel (`receivers/etranslation-vercel/`) and
`translate()` polls it. From the caller's side it is still `text in, text
out`, same as every other provider.

Every chunk is sent as ONE whole-document request (the Commission's own
guidance for anything over its 5,000-character snippet limit). The default
`document_format` is "txt": the bracket markup (`[i]`, `[sc]`, `[FN: ...]`) is
sent as literal text, which a real run on this account carried through intact.
"html" is the alternative — `to_html()`/`from_html()` turn the markup into
inline tags and back — and is covered by offline tests only; no real call has
exercised it.

Verified against the Commission's published REST v2 OpenAPI spec and Dev
Corner pages, and with real calls on a 15K-character chapter (63 s and 33 s;
structure and all marker types preserved). NOT verified: a full-length book's
turnaround (`poll_interval_seconds` / `timeout_minutes` are guesses), the html
format, and resume after a real interruption. Translation quality on this
material is plain machine translation and noticeably literal — see ADR 022.
"""
import base64
import hashlib
import html
import json
import re
import time
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlencode

import click
import httpx

from providers.translation.base import TranslationProvider

API_BASE = "https://language-tools.ec.europa.eu/etranslation/api"

# Request ids from eTranslation are used verbatim as receiver pathnames; the
# receiver validates them, this only bounds how long we keep asking.
_CONSECUTIVE_POLL_FAILURES = 10
_SUBMIT_ATTEMPTS = 3

# Messages for the submit errors a user can actually act on. Every other code
# is reported verbatim with its own message.
_SUBMIT_ERRORS = {
    -11200: "eTranslation says this account's quota is exceeded (QUOTA_EXCEEDED). "
            "No volume figure is published — ask the DGT advisory team what yours is.",
    -20028: "50 document requests are already in flight for this account (the "
            "concurrency quota). Wait for some to finish, then run this again — "
            "a request already submitted for this text will be resumed, not repeated.",
    -90000: "eTranslation rejected the credentials as NOT_AUTHORIZED. Check "
            "ETRANSLATION_APP_NAME / ETRANSLATION_PASSWORD in .env.",
    -10500: "This account is not allowed to request LLM-enhanced translation "
            "(LLM_NOT_ALLOWED). Set translation.etranslation.llm_enhanced to null "
            "in config.yaml to omit the llm block entirely.",
}


# ---------------------------------------------------------------------------
# Markup conversion: this project's bracket markup <-> HTML
# ---------------------------------------------------------------------------

def to_html(text: str) -> str:
    """Cleaned text -> a small HTML document. Blank-line-separated blocks
    become <p>, single newlines inside a block become <br/> (chapter number and
    title share one block on separate lines, and `format`'s heading detection
    depends on that), [i]/[sc] become inline tags. [FN: ...] stays literal."""
    blocks = []
    for block in text.split("\n\n"):
        block = block.strip("\n")
        if not block.strip():
            continue
        body = html.escape(block, quote=False)
        body = body.replace("[i]", "<i>").replace("[/i]", "</i>")
        body = body.replace("[sc]", '<span class="sc">').replace("[/sc]", "</span>")
        blocks.append("<p>" + body.replace("\n", "<br/>") + "</p>")
    return (
        '<!DOCTYPE html>\n<html><head><meta charset="utf-8"><title>manuscript</title></head>\n'
        "<body>\n" + "\n".join(blocks) + "\n</body></html>\n"
    )


class _FromHtml(HTMLParser):
    _BLOCKS = {"p", "div", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6"}
    _IGNORED = {"head", "title", "script", "style"}

    def __init__(self, has_body: bool):
        super().__init__(convert_charrefs=True)
        self.paragraphs: list = []
        self._buf: list = []
        self._in_body = not has_body  # a bare fragment has no <body> to wait for
        self._ignored = 0
        self._spans: list = []  # per open <span>: True if it was our small-caps span

    def _flush(self):
        text = re.sub(r" *\n *", "\n", "".join(self._buf)).strip()
        if text:
            self.paragraphs.append(text)
        self._buf = []

    def handle_starttag(self, tag, attrs):
        if tag == "body":
            self._in_body = True
        elif tag in self._IGNORED:
            self._ignored += 1
        elif not self._in_body or self._ignored:
            return
        elif tag in self._BLOCKS:
            self._flush()
        elif tag == "br":
            self._buf.append("\n")
        elif tag in ("i", "em"):
            self._buf.append("[i]")
        elif tag == "span":
            classes = (dict(attrs).get("class") or "").split()
            is_sc = "sc" in classes
            self._spans.append(is_sc)
            if is_sc:
                self._buf.append("[sc]")

    def handle_startendtag(self, tag, attrs):
        # <br/> arrives here, not via handle_starttag.
        if tag == "br" and self._in_body and not self._ignored:
            self._buf.append("\n")
        else:
            self.handle_starttag(tag, attrs)
            if tag not in ("br",):
                self.handle_endtag(tag)

    def handle_endtag(self, tag):
        if tag in self._IGNORED:
            self._ignored = max(0, self._ignored - 1)
        elif not self._in_body or self._ignored:
            return
        elif tag in self._BLOCKS:
            self._flush()
        elif tag in ("i", "em"):
            self._buf.append("[/i]")
        elif tag == "span" and self._spans:
            if self._spans.pop():
                self._buf.append("[/sc]")

    def handle_data(self, data):
        if self._in_body and not self._ignored:
            self._buf.append(re.sub(r"\s+", " ", data))


def from_html(document: str) -> str:
    """Translated HTML -> this project's bracket markup, paragraphs separated
    by a blank line. Tolerant of the engine reformatting whitespace, using <em>
    for <i>, or returning a bare fragment instead of a full document."""
    parser = _FromHtml(has_body="<body" in document.lower())
    parser.feed(document)
    parser.close()
    parser._flush()
    return "\n\n".join(parser.paragraphs)


# ---------------------------------------------------------------------------
# Provider
# ---------------------------------------------------------------------------

class ETranslationProvider(TranslationProvider):
    # Not eTranslation's own limit (20 MB of base64) but the receiver's: a
    # delivery comes back through a Vercel Function, whose request/response
    # body limit is a hard 4.5 MB. 2M characters of Spanish, with accents,
    # HTML tags and base64's 4/3 overhead, stays under that. Every real
    # manuscript is one chunk; anything larger is split into several requests.
    max_chars_per_request = 2_000_000

    def __init__(self, app_name: str, password: str, receiver_url: str, receiver_secret: str,
                 *, domain: str = "GEN", document_format: str = "txt",
                 llm_enhanced=False, poll_interval_seconds: float = 30.0,
                 timeout_minutes: float = 120.0):
        self._auth = httpx.BasicAuth(app_name, password)
        self.receiver_url = receiver_url
        self._secret = receiver_secret
        self.domain = domain
        self.document_format = document_format
        self.llm_enhanced = llm_enhanced  # True / False / None (omit the llm block)
        self.poll_interval_seconds = poll_interval_seconds
        self.timeout_minutes = timeout_minutes
        # Local count of source characters translated by this task run —
        # eTranslation reports no billed-character figure of its own.
        self.usage = {"characters": 0}
        self._llm_support_checked = False
        self._state_file = None
        self._state: dict = {}
        self._reference = "viable-imprint"

    # -- optional run hooks (see TranslationProvider) -------------------

    def begin_run(self, output_dir: Path, input_stem: str, reference: str) -> None:
        """Sidecar of in-flight requests (ADR 022, Decision 6). An interrupted
        run resumes the same request ids instead of translating the book twice.
        Received translations are stored here too, so a crash after the
        receiver's copy is deleted cannot lose a finished chunk."""
        self._reference = reference
        self._state_file = Path(output_dir) / f".{input_stem}.etranslation.json"
        self._state = {}
        if self._state_file.exists():
            try:
                self._state = json.loads(self._state_file.read_text(encoding="utf-8"))["requests"]
            except (ValueError, KeyError, TypeError):
                click.echo(f"  Ignoring unreadable resume file {self._state_file.name}; starting fresh.")

    def finish_run(self) -> None:
        if self._state_file is not None:
            self._state_file.unlink(missing_ok=True)
        self._state_file, self._state = None, {}

    def _save_state(self) -> None:
        if self._state_file is None:
            return
        tmp = self._state_file.with_suffix(".tmp")
        tmp.write_text(json.dumps({"version": 1, "requests": self._state}), encoding="utf-8")
        tmp.replace(self._state_file)

    # -- the TranslationProvider contract -------------------------------

    def translate(self, text: str, source_lang: str, target_lang: str) -> str:
        source, target = source_lang.upper(), target_lang.upper()
        # A chunk's identity is everything that shapes the translation, not just
        # its text: after changing domain, LLM mode or format and re-running, an
        # interrupted request submitted with the old settings must not be resumed.
        settings = f"{source}>{target}|{self.domain}|{self.llm_enhanced}|{self.document_format}"
        key = hashlib.sha256(f"{settings}\n{text}".encode("utf-8")).hexdigest()
        entry = self._state.get(key)

        if entry and "translation" in entry:
            click.echo("  Using the translation already received for this chunk.")
            self.usage["characters"] += len(text)
            return entry["translation"]

        if entry and entry.get("request_id"):
            request_id = entry["request_id"]
            click.echo(f"  Resuming eTranslation request {request_id} "
                       f"(submitted {entry.get('submitted_at', 'earlier')})...")
        else:
            self._check_status()
            self._warn_if_llm_unsupported(source, target)
            request_id = self._submit(text, source, target)
            entry = {"request_id": request_id,
                     "submitted_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
            self._state[key] = entry
            self._save_state()
            click.echo(f"  Submitted to eTranslation as request {request_id}; waiting for delivery...")

        status, payload = self._wait(request_id)
        if status == "failed":
            # A failed request will not recover; drop it so the next run submits afresh.
            self._state.pop(key, None)
            self._save_state()
            self._delete_from_receiver(request_id)
            raise click.ClickException(
                f"eTranslation could not translate request {request_id}: "
                f"{payload.get('errorMessage', 'no message')} (code {payload.get('errorCode', '?')}). "
                "Re-running will submit it again."
            )

        translated = self._decode(payload)
        entry["translation"] = translated
        self._save_state()
        self._delete_from_receiver(request_id)
        self.usage["characters"] += len(text)
        return translated

    # -- eTranslation API ------------------------------------------------

    def _check_status(self) -> None:
        """Advisory only: a status endpoint that is itself down must not stop a run."""
        try:
            resp = httpx.request("GET", f"{API_BASE}/status", auth=self._auth, timeout=30.0)
            level = resp.json()["level"] if resp.status_code == 200 else None
        except (httpx.TransportError, ValueError, KeyError):
            level = None
        if level is None:
            click.echo("  (Could not read eTranslation's status endpoint; continuing.)")
        elif level >= 3:
            raise click.ClickException(
                "eTranslation reports its service is blocked (status level 3). Try again later."
            )
        elif level == 2:
            click.echo("  eTranslation reports critical load (level 2) — expect delays.")

    def _warn_if_llm_unsupported(self, source: str, target: str) -> None:
        """`llm_enhanced: true` on a domain with no LLM variant for the language
        pair is silently ignored by eTranslation — a real run on this account's
        GEN domain returned plain machine translation, byte-identical to a run
        without it. Warn instead of letting a no-op look like a setting that
        worked. Once per provider instance; advisory, so any failure to read
        the list is skipped, like the status check."""
        if self.llm_enhanced is not True or self._llm_support_checked:
            return
        self._llm_support_checked = True
        try:
            resp = httpx.request("GET", f"{API_BASE}/getDomains", auth=self._auth, timeout=30.0)
            data = resp.json() if resp.status_code == 200 else None
        except (httpx.TransportError, ValueError):
            return
        # The spec shows two shapes (an object keyed by domain, and a list of
        # {domain, languagePairs}); read either.
        if isinstance(data, dict):
            entries = {name: (v or {}).get("languagePairs", []) for name, v in data.items()}
        elif isinstance(data, list):
            entries = {e.get("domain"): e.get("languagePairs", []) for e in data}
        else:
            return
        if self.domain not in entries:
            return
        pair = f"{source}-{target}"
        # Variants look like EN-ES, EN-ES-GLS, EN-ES-LLM, EN-ES-LLM-GLS.
        if not any(p.startswith(pair + "-") and "LLM" in p.split("-")[2:] for p in entries[self.domain]):
            with_llm = sorted(d for d, pairs in entries.items()
                              if any(p.startswith(pair + "-") and "LLM" in p.split("-")[2:] for p in pairs))
            click.echo(
                f"  Warning: domain {self.domain} has no LLM variant for {pair}, so llm_enhanced: true is "
                "being ignored (plain machine translation). "
                + (f"Domains that do: {', '.join(with_llm)}." if with_llm else "No domain on this account does.")
            )

    def _callback(self, kind: str) -> str:
        return f"{self.receiver_url}?{urlencode({'kind': kind, 'token': self._secret})}"

    def _submit(self, text: str, source: str, target: str) -> int:
        body = to_html(text) if self.document_format == "html" else text
        payload = {
            "sourceLanguage": source,
            "targetLanguages": [target],
            "domain": self.domain,
            "callerInformation": {"externalReference": self._reference[:200]},
            "documentToTranslate": {"document": {
                "content": base64.b64encode(body.encode("utf-8")).decode("ascii"),
                "format": self.document_format,
                "filename": "manuscript",
            }},
            "deliveries": {"http": self._callback("delivery")},
            "notifications": {"success": {"http": self._callback("success")},
                              "failure": {"http": self._callback("failure")}},
        }
        if self.llm_enhanced is not None:
            payload["llm"] = {"enabled": bool(self.llm_enhanced)}

        resp = self._send("POST", f"{API_BASE}/askTranslate", json=payload, auth=self._auth, timeout=120.0)
        if resp.status_code == 200:
            try:
                return int(resp.json()["requestId"])
            except (ValueError, KeyError, TypeError):
                raise click.ClickException("eTranslation accepted the request but returned no request id.")
        if resp.status_code in (401, 403):
            raise click.ClickException(
                f"eTranslation rejected the credentials (HTTP {resp.status_code}). "
                "Check ETRANSLATION_APP_NAME / ETRANSLATION_PASSWORD in .env."
            )
        try:
            err = resp.json()
            code, message = err.get("errorCode"), err.get("errorMessage")
        except ValueError:
            code, message = None, resp.text[:200]
        raise click.ClickException(
            _SUBMIT_ERRORS.get(code) or f"eTranslation rejected the request (HTTP {resp.status_code}, "
                                        f"code {code}): {message}"
        )

    def _send(self, method: str, url: str, **kwargs) -> httpx.Response:
        """One call with a few retries on connection errors and 5xx. Never
        puts the URL in a message: the receiver's carries the shared secret."""
        for attempt in range(_SUBMIT_ATTEMPTS):
            try:
                resp = httpx.request(method, url, **kwargs)
                if resp.status_code < 500:
                    return resp
                problem = f"HTTP {resp.status_code}"
            except httpx.TransportError as e:
                problem = type(e).__name__
            if attempt < _SUBMIT_ATTEMPTS - 1:
                wait = 2 * 2 ** attempt
                click.echo(f"  eTranslation call failed ({problem}); retrying in {wait}s...")
                time.sleep(wait)
        raise click.ClickException(f"eTranslation call failed after {_SUBMIT_ATTEMPTS} attempts ({problem}).")

    # -- receiver --------------------------------------------------------

    def _wait(self, request_id: int):
        """Poll the receiver until the request is delivered or failed.
        Returns ("delivered", delivery) or ("failed", failure)."""
        started = time.monotonic()
        deadline = started + self.timeout_minutes * 60
        last_note = started
        failures = 0
        while True:
            try:
                resp = httpx.request("GET", self.receiver_url, timeout=60.0,
                                     params={"token": self._secret, "requestId": request_id})
                problem = None if resp.status_code == 200 else f"HTTP {resp.status_code}"
            except httpx.TransportError as e:
                resp, problem = None, type(e).__name__

            if resp is not None and resp.status_code == 401:
                raise click.ClickException(
                    "The receiver rejected the shared secret (401). ETRANSLATION_RECEIVER_SECRET in "
                    ".env must equal RECEIVER_SECRET on Vercel, and the receiver must have been "
                    "redeployed after any change to it."
                )
            if resp is not None and resp.status_code == 404:
                raise click.ClickException(
                    "The receiver URL answered 404. Check translation.etranslation.receiver_url in "
                    "config.yaml (it ends in /api/etranslation)."
                )

            if problem is None:
                failures = 0
                body = resp.json()
                if body.get("status") == "delivered":
                    return "delivered", body["delivery"]
                if body.get("status") == "failed":
                    return "failed", body["failure"]
            else:
                failures += 1
                if failures >= _CONSECUTIVE_POLL_FAILURES:
                    raise click.ClickException(
                        f"The receiver has been unreachable for {failures} polls in a row ({problem}). "
                        f"Request {request_id} is kept; re-run to resume it."
                    )

            now = time.monotonic()
            if now >= deadline:
                raise click.ClickException(
                    f"No delivery for request {request_id} after {self.timeout_minutes:g} minutes. "
                    "It is kept: re-running resumes waiting for it. If eTranslation never delivers, delete "
                    f"{self._state_file.name if self._state_file else 'the resume file'} to submit afresh."
                )
            if now - last_note >= 300:
                click.echo(f"  Still waiting for request {request_id} ({int((now - started) // 60)} min)...")
                last_note = now
            time.sleep(self.poll_interval_seconds)

    def _delete_from_receiver(self, request_id: int) -> None:
        try:
            httpx.request("DELETE", self.receiver_url, timeout=30.0,
                          params={"token": self._secret, "requestId": request_id})
        except httpx.TransportError:
            click.echo(f"  (Could not clear request {request_id} from the receiver; harmless, leave it.)")

    def _decode(self, delivery: dict) -> str:
        result = delivery.get("result")
        if not result:
            raise click.ClickException("eTranslation's delivery carried no translated content.")
        text = base64.b64decode(result).decode("utf-8-sig")
        fmt = (delivery.get("outputFormat") or self.document_format).lower()
        return from_html(text) if fmt in ("html", "htm", "xhtml") else text
