"""Terminal UI for the pipeline (ADR 023). Self-contained and optional: nothing
outside this package and `tests/tui/` imports it, except the lazy import in
`pipeline.py`'s `ui` command. Importing the package itself needs no Textual;
only `app`, `screens` and `widgets` do. `services/` never does.
"""
