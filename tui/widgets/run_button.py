"""A button that starts a run. Disabled while any run is in progress (ADR 023,
Decision 2: one run at a time), and while its screen says the action is not
possible, with the reason as a tooltip.
"""
from textual.widgets import Button

BUSY_REASON = "Another command is running"


class RunButton(Button):
    def __init__(self, label: str = "Run", *, blocked_reason: str = None, **kwargs):
        super().__init__(label, variant="primary", **kwargs)
        self._blocked_reason = blocked_reason

    def on_mount(self) -> None:
        self.watch(self.app, "busy", lambda _busy: self._refresh_enabled(), init=True)

    @property
    def blocked_reason(self):
        return self._blocked_reason

    @blocked_reason.setter
    def blocked_reason(self, reason) -> None:
        self._blocked_reason = reason
        if self.is_mounted:
            self._refresh_enabled()

    def _refresh_enabled(self) -> None:
        busy = bool(getattr(self.app, "busy", False))
        self.disabled = busy or bool(self._blocked_reason)
        self.tooltip = BUSY_REASON if busy else self._blocked_reason
