"""Retain ownership of cleanup under asyncio and AnyIO level cancellation."""

import asyncio

import anyio


async def await_owned(task, *, cancel_on_interrupt=False):
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        if cancel_on_interrupt and not task.done():
            task.cancel()
        with anyio.CancelScope(shield=True):
            while not task.done():
                try:
                    await asyncio.shield(task)
                except asyncio.CancelledError:
                    continue
                except Exception:
                    break
        try:
            task.result()
        except (asyncio.CancelledError, Exception):
            pass
        raise
