"""Same-Task, one-shot deadlines on every supported Python version.

MCP stdio uses AnyIO context managers whose enter/body/exit must share a Task.
Moving their lifetime into ``wait_for`` changes that Task, while an AnyIO
level-cancel scope can repeatedly interrupt async cleanup or worker draining.
Use asyncio's native scope when available and its edge-cancel semantics on
Python 3.10, without another dependency or a new execution allowance.
"""

import asyncio


class _TaskDeadline:
    """Python 3.10 fallback; no child Task and no repeated cancellation."""

    def __init__(self, delay):
        self._delay = delay
        self._task = None
        self._handle = None
        self._expired = False
        self._cancel_requested = False
        self._cancel_count = None
        self._token = object()

    async def __aenter__(self):
        if self._task is not None:
            raise RuntimeError("TASK_DEADLINE_ALREADY_ENTERED")
        self._task = asyncio.current_task()
        if self._task is None:
            raise RuntimeError("TASK_DEADLINE_REQUIRES_TASK")
        counter = getattr(self._task, "cancelling", None)
        if callable(counter):
            self._cancel_count = counter()
        loop = asyncio.get_running_loop()
        if self._delay <= 0:
            self._handle = loop.call_soon(self._on_deadline)
        else:
            self._handle = loop.call_later(self._delay, self._on_deadline)
        return self

    def _on_deadline(self):
        self._expired = True
        # A private cancellation message distinguishes our timeout from an
        # external cancellation even on 3.10, where cancel counters are absent.
        self._cancel_requested = self._task.cancel(self._token)

    async def __aexit__(self, exception_type, exception, traceback):
        if self._handle is not None:
            self._handle.cancel()
            self._handle = None
        if not self._cancel_requested:
            return False
        owned_cancellation = (
            isinstance(exception, asyncio.CancelledError)
            and exception.args == (self._token,)
        )
        # 3.11+ may exercise the fallback when timeout() is unavailable. Remove
        # only our request and retain concurrent external cancellation counts.
        uncancel = getattr(self._task, "uncancel", None)
        if self._cancel_count is not None and callable(uncancel):
            owned_cancellation = uncancel() <= self._cancel_count and owned_cancellation
        if owned_cancellation:
            raise TimeoutError from None
        return False

    def expired(self):
        return self._expired


def task_deadline(delay):
    """Return a deadline for the *remaining* shared Run time, never restart it."""
    native_timeout = getattr(asyncio, "timeout", None)
    if callable(native_timeout):
        return native_timeout(delay)
    return _TaskDeadline(delay)
