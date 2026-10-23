"""Graceful interrupt for pipeline runs started by the terminal UI (ADR 023,
Decision 6 and Verification notes, item 3).

The UI cancels a run by interrupting the child `pipeline.py` process so that
`task_loader._run_recorded()` sees a `KeyboardInterrupt` and writes a `failed`
ledger entry. On macOS and Linux a SIGINT does that already. On Windows the UI
starts the child in a new process group and sends `CTRL_BREAK_EVENT`, which by
default kills the process without any Python exception, and a SIGBREAK handler
alone does not wake a blocking `time.sleep()`. `install_graceful_break()`
registers a console control handler that raises `KeyboardInterrupt` in the main
thread exactly as Ctrl+C would.

Called from `pipeline.main()` only when `VIABLE_IMPRINT_GRACEFUL_BREAK` is set,
so ordinary terminal use is unchanged.
"""
import sys

ENV_VAR = "VIABLE_IMPRINT_GRACEFUL_BREAK"

_CTRL_BREAK_EVENT = 1

# The ctypes callback must stay referenced for the life of the process, or the
# console would call into freed memory.
_handler_ref = None


def install_graceful_break() -> bool:
    """Make CTRL_BREAK raise KeyboardInterrupt in the main thread. Returns True
    when a handler was installed (Windows only; elsewhere SIGINT already works)."""
    global _handler_ref
    if sys.platform != "win32":
        return False

    import _thread
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.windll.kernel32
    handler_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.DWORD)
    kernel32.SetConsoleCtrlHandler.argtypes = [handler_type, wintypes.BOOL]
    kernel32.SetConsoleCtrlHandler.restype = wintypes.BOOL
    kernel32.SetEvent.argtypes = [wintypes.HANDLE]
    kernel32.SetEvent.restype = wintypes.BOOL

    # `time.sleep()` on Windows waits on the interpreter's own SIGINT event, so
    # setting it wakes a sleeping main thread. The accessor is a private CPython
    # function: if it is ever missing the handler still raises the interrupt, just
    # not until the main thread next runs Python code.
    try:
        ctypes.pythonapi._PyOS_SigintEvent.restype = ctypes.c_void_p
        sigint_event = ctypes.pythonapi._PyOS_SigintEvent()
    except AttributeError:
        sigint_event = None

    def handler(ctrl_type):
        if ctrl_type != _CTRL_BREAK_EVENT:
            return False  # Ctrl+C and the rest keep their normal handling
        _thread.interrupt_main()
        if sigint_event:
            kernel32.SetEvent(sigint_event)
        return True

    _handler_ref = handler_type(handler)
    return bool(kernel32.SetConsoleCtrlHandler(_handler_ref, True))
