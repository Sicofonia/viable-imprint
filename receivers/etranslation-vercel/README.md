# eTranslation receiver (ADR 022)

A small Vercel Function that catches eTranslation's asynchronous deliveries so the
pipeline CLI can pick them up. eTranslation's REST v2 API has no way to fetch a finished
translation: it only *pushes* the result to an HTTP(S) URL, FTP or SFTP. This function is
that URL, and a private Vercel Blob store holds the result until the CLI collects it.

It is deployed once and then left alone. The pipeline never imports or runs this code; it
only makes HTTPS calls to it (`providers/translation/etranslation.py`). Design and
reasoning: `docs/adr/022-etranslation-provider.md`, Decision 2.

## What it does

One path, `/api/etranslation`, three verbs, every one of them requiring `?token=<secret>`:

| Verb | Called by | Does |
|---|---|---|
| `POST ?kind=delivery\|success\|failure` | eTranslation | Stores the JSON body at `etranslation/<requestId>/<kind>.json`. Overwrites, so a duplicate delivery is harmless. Always answers `200` when stored. |
| `GET ?requestId=N` | the pipeline | `{"status":"pending"}`, `{"status":"delivered","delivery":{…}}` or `{"status":"failed","failure":{…}}`. |
| `DELETE ?requestId=N` | the pipeline | Removes everything stored for that request, once the CLI has saved the result locally. |

The secret is compared in constant time against `RECEIVER_SECRET`; with no secret
configured the function refuses everything. eTranslation cannot attach headers to its
callbacks, which is why the secret travels in the query string. Treat it accordingly: it
can show up in Vercel's logs, so never paste a log or a URL containing it anywhere.

## One-time setup

1. **Plan.** Vercel's Hobby plan is non-commercial only; this runs on a **Pro** team.
2. **Vercel CLI 50.20 or newer** (private Blob stores need it): `npm i -g vercel@latest`,
   then `vercel login`.
3. **Create a private Blob store** (dashboard → Storage → Blob → *Private*, an EU region).
   Neither setting can be changed afterwards. Its Base URL should contain
   `.private.blob.vercel-storage.com`.
4. **Create the project.** In this folder run `vercel link`. Answer **No** to connecting a
   Git repository (we deploy by hand), and accept the detected defaults. The project name
   sets the URL, `<name>.vercel.app`.
5. **Connect the store to the project** (store → Projects → Connect to Project →
   Production). Vercel adds `BLOB_STORE_ID` and the OIDC settings itself; there is no
   token to copy and the function needs no credentials in code.
6. **Set the secret.** Generate one with `openssl rand -hex 32`. Add it in the project's
   Settings → Environment Variables as `RECEIVER_SECRET` (Production, Sensitive), and put
   the *same* value in the repo-root `.env` as `ETRANSLATION_RECEIVER_SECRET`.
7. **Deploy** (next section), then put the URL in `config.yaml` as
   `translation.etranslation.receiver_url`: `https://<name>.vercel.app/api/etranslation`.

## Deploying and redeploying

**Deploy from a copy outside the git repository.** `vercel deploy` attaches the repo's git
author to the deployment, and Vercel blocks a deployment whose commit author is not a
member of the team ("the commit author doesn't have permission to create deployments for
this project"). If your git identity differs from the Vercel account's (it does here), the
deploy is blocked. A folder with no git repo has no author to check:

```
# first time
mkdir -p ~/vercel-deploy
cp -r receivers/etranslation-vercel ~/vercel-deploy/etranslation-receiver
rm -rf ~/vercel-deploy/etranslation-receiver/node_modules ~/vercel-deploy/etranslation-receiver/.env.local
cd ~/vercel-deploy/etranslation-receiver
git rev-parse --is-inside-work-tree     # must say: fatal: not a git repository
vercel deploy --prod

# after changing code in the repo
cp -r receivers/etranslation-vercel/api receivers/etranslation-vercel/lib ~/vercel-deploy/etranslation-receiver/
cd ~/vercel-deploy/etranslation-receiver && vercel deploy --prod
```

The copy keeps the `.vercel/` link, so it deploys to the same project. The repo folder is
the source of truth; the copy is disposable.

**Settings-only changes** (rotating `RECEIVER_SECRET`, say) need a new deployment to take
effect but no new code, so the dashboard's *Redeploy* is enough. A dashboard redeploy
re-runs the *same source* as the deployment it started from, so it cannot ship a code
change.

## Testing a deployment

Offline, no network: `npm install && npm test`.

Against the real deployment, one command at a time in the same shell, from the repo root
(this reads the secret from `.env` so it is never typed or pasted):

```
S=$(grep '^ETRANSLATION_RECEIVER_SECRET=' .env | cut -d= -f2)
U=https://<name>.vercel.app/api/etranslation
curl -s -o /dev/null -w "no token: %{http_code}\n" "$U?requestId=1"        # 401
curl -s "$U?token=$S&requestId=1"                                          # {"status":"pending"}
curl -s -X POST "$U?token=$S&kind=delivery" -d '{"requestId":999001,"result":"aG9sYQ=="}'   # {"stored":true}
curl -s "$U?token=$S&requestId=999001"                                     # {"status":"delivered",...}
curl -s -X DELETE "$U?token=$S&requestId=999001"                           # {"deleted":true}
curl -s "$U?token=$S&requestId=999001"                                     # {"status":"pending"}
```

## When something fails

`vercel logs --environment production --since 1h --status-code 500 --expand` (run from
this folder or its deploy copy). Redact `token=…` before sharing any output. Two failures
have already happened and are worth recognizing:

- `Invalid URL` at `new URL(...)`: the handler was exported with `export default`, which
  Vercel invokes the Node way (`req.url` is only a path). The named `GET`/`POST`/`DELETE`
  exports in `api/etranslation.js` are what make it receive a standard Web `Request`.
- `result.stream.text is not a function`: `@vercel/blob`'s `get()` returns a plain
  `ReadableStream`, whatever the docs' examples suggest. `lib/stream.js` handles it.

Blob contents never need cleaning up by hand: the CLI deletes a request's files once it
has the result saved locally. If a run is abandoned, the leftovers are a few files in the
store, visible in the Vercel dashboard.
