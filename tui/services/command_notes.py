"""Facts about specific commands that the Click tree does not carry (ADR 023,
T1.5 and Verification notes, item 2). Plain Python, no Textual.
"""
from tui.services.command_model import CommandSpec

_CHAIN_WARNING = "This is one stage of a chain. Running it alone skips the other stages. Use `{chain}` instead."
_SCAN_WARNING = (_CHAIN_WARNING + " Run directly it also uses the wrong folder: {wrong} instead of {right} "
                 "(a known issue, ADR 023 Verification notes).")

# command path -> why it should not be run on its own
_NOT_DIRECT = {
    ("s5", "homeostat-scan"): _SCAN_WARNING.format(chain="s2 homeostat run", wrong="intelligence/", right="homeostat/"),
    ("s5", "homeostat"): _CHAIN_WARNING.format(chain="s2 homeostat run"),
    ("s5", "homeostat-render"): _CHAIN_WARNING.format(chain="s2 homeostat run"),
    ("s1d", "newsletter-scan"): _SCAN_WARNING.format(chain="s2 newsletter run", wrong="intelligence/", right="newsletter/"),
    ("s1d", "newsletter"): _CHAIN_WARNING.format(chain="s2 newsletter run"),
    ("s1d", "newsletter-track"): _CHAIN_WARNING.format(chain="s2 newsletter run"),
    ("ui",): "This opens the terminal UI itself, which you are already in.",
}

_UPDATE_WARNING = ("This rewrites the stored sync hash in prompts/s5/policy_evaluation_task.txt. Continue only "
                   "after reviewing that prompt against the Editorial Policy section of docs/vsm.md.")


def not_run_directly(spec: CommandSpec):
    """Why this command has no Run button in the explorer, or None."""
    return _NOT_DIRECT.get(tuple(spec.path))


def extra_confirmation(spec: CommandSpec, values: dict):
    """A warning that forces the run modal to ask before running, or None."""
    if spec.path == ["s5", "check-policy-sync"] and values.get("do_update"):
        return _UPDATE_WARNING
    return None
