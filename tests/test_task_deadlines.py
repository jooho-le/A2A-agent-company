"""Deadline regressions; no LLM, stdio child, Docker or listener is started."""

import asyncio
from contextlib import asynccontextmanager
from threading import Event
import unittest
from unittest.mock import patch

import anyio

from agents.core.deadlines import _TaskDeadline, task_deadline
from agents.platform.composition import _host_call


class TaskDeadlineTests(unittest.IsolatedAsyncioTestCase):
    async def test_native_scope_is_used_when_available(self):
        sentinel = object()
        with patch.object(asyncio, "timeout", return_value=sentinel, create=True) as native:
            self.assertIs(task_deadline(1), sentinel)
        native.assert_called_once_with(1)

    async def test_missing_timeout_api_uses_same_task_through_anyio_context(self):
        events, task = [], asyncio.current_task()

        @asynccontextmanager
        async def transport():
            async with anyio.create_task_group():
                events.append(("enter", asyncio.current_task()))
                try:
                    yield
                finally:
                    await asyncio.sleep(0)
                    events.append(("exit", asyncio.current_task()))

        with patch.dict(asyncio.__dict__):
            asyncio.__dict__.pop("timeout", None)
            deadline = task_deadline(1)
            self.assertIsInstance(deadline, _TaskDeadline)
            async with deadline:
                async with transport():
                    events.append(("body", asyncio.current_task()))
        self.assertEqual([name for name, _ in events], ["enter", "body", "exit"])
        self.assertTrue(all(owner is task for _, owner in events))
        self.assertFalse(deadline.expired())

    async def test_body_timeout_is_one_shot_and_async_cleanup_finishes(self):
        events, task = [], asyncio.current_task()

        @asynccontextmanager
        async def transport():
            events.append(("enter", asyncio.current_task()))
            try:
                yield
            finally:
                await asyncio.sleep(.02)
                events.append(("drained", asyncio.current_task()))

        deadline = _TaskDeadline(.005)
        with self.assertRaises(TimeoutError):
            async with deadline:
                async with transport():
                    await asyncio.sleep(1)
        self.assertTrue(deadline.expired())
        self.assertEqual([name for name, _ in events], ["enter", "drained"])
        self.assertTrue(all(owner is task for _, owner in events))
        if hasattr(task, "cancelling"):
            self.assertEqual(task.cancelling(), 0)

    async def test_deadline_cancels_anyio_group_without_task_scope_mismatch(self):
        finished = asyncio.Event()

        async def child():
            try:
                await asyncio.sleep(1)
            finally:
                finished.set()

        deadline = _TaskDeadline(.005)
        with self.assertRaises(TimeoutError):
            async with deadline:
                async with anyio.create_task_group() as group:
                    group.start_soon(child)
                    await asyncio.sleep(1)
        self.assertTrue(finished.is_set())
        self.assertTrue(deadline.expired())

    async def test_native_transport_timeout_is_not_scope_expiration(self):
        error, deadline = TimeoutError("inert transport timeout"), _TaskDeadline(1)
        with self.assertRaises(TimeoutError) as caught:
            async with deadline:
                raise error
        self.assertIs(caught.exception, error)
        self.assertFalse(deadline.expired())

    async def test_external_cancel_is_preserved_and_cleanup_drains(self):
        entered, cleaned = asyncio.Event(), asyncio.Event()
        deadline = _TaskDeadline(1)

        async def operation():
            async with deadline:
                try:
                    entered.set()
                    await asyncio.sleep(1)
                finally:
                    await asyncio.sleep(.005)
                    cleaned.set()

        task = asyncio.create_task(operation())
        await entered.wait()
        task.cancel("inert caller cancellation")
        with self.assertRaises(asyncio.CancelledError) as caught:
            await task
        self.assertEqual(caught.exception.args, ("inert caller cancellation",))
        self.assertTrue(cleaned.is_set())
        self.assertFalse(deadline.expired())

    async def test_concurrent_external_cancel_is_not_reclassified_as_timeout(self):
        entered, cleaning = asyncio.Event(), asyncio.Event()
        deadline = _TaskDeadline(.005)

        async def operation():
            async with deadline:
                try:
                    entered.set()
                    await asyncio.sleep(1)
                finally:
                    cleaning.set()
                    await asyncio.sleep(1)

        task = asyncio.create_task(operation())
        await entered.wait()
        await cleaning.wait()
        task.cancel("inert concurrent caller cancellation")
        with self.assertRaises(asyncio.CancelledError) as caught:
            await task
        self.assertEqual(caught.exception.args, ("inert concurrent caller cancellation",))
        self.assertTrue(deadline.expired())

    async def test_exiting_cancels_timer_and_does_not_cancel_later_work(self):
        deadline = _TaskDeadline(.01)
        async with deadline:
            await asyncio.sleep(0)
        await asyncio.sleep(.02)
        self.assertFalse(deadline.expired())
        self.assertIsNone(deadline._handle)

    async def test_sync_host_worker_is_drained_before_deadline_error_escapes(self):
        finished = Event()

        def worker(_argument):
            Event().wait(.02)
            finished.set()
            return "inert late result"

        deadline = _TaskDeadline(.005)
        with self.assertRaises(TimeoutError):
            async with deadline:
                await _host_call(worker, None)
        self.assertTrue(deadline.expired())
        self.assertTrue(finished.is_set())

    async def test_immediate_expiration_waits_for_checkpoint(self):
        deadline = _TaskDeadline(0)
        with self.assertRaises(TimeoutError):
            async with deadline:
                self.assertFalse(deadline.expired())
                await asyncio.sleep(.01)
        self.assertTrue(deadline.expired())

    async def test_scope_cannot_be_reentered(self):
        deadline = _TaskDeadline(1)
        async with deadline:
            await asyncio.sleep(0)
        with self.assertRaisesRegex(RuntimeError, "TASK_DEADLINE_ALREADY_ENTERED"):
            async with deadline:
                self.fail("scope cannot be reentered")

    async def test_python_310_task_without_cancel_counter_apis_times_out(self):
        class Python310Task:
            def __init__(self, task):
                self._task = task

            def cancel(self, message):
                return self._task.cancel(message)

        async def operation():
            owner = asyncio.current_task()
            with patch("agents.core.deadlines.asyncio.current_task", return_value=Python310Task(owner)):
                deadline = _TaskDeadline(.005)
                with self.assertRaises(TimeoutError):
                    async with deadline:
                        await asyncio.sleep(1)
                self.assertTrue(deadline.expired())

        await asyncio.create_task(operation())

    async def test_python_310_task_without_cancel_counter_apis_preserves_external_cancel(self):
        class Python310Task:
            def __init__(self, task):
                self._task = task

            def cancel(self, message):
                return self._task.cancel(message)

        entered, cleaned = asyncio.Event(), asyncio.Event()
        deadline = _TaskDeadline(1)

        async def operation():
            owner = asyncio.current_task()
            with patch("agents.core.deadlines.asyncio.current_task", return_value=Python310Task(owner)):
                async with deadline:
                    try:
                        entered.set()
                        await asyncio.sleep(1)
                    finally:
                        await asyncio.sleep(.005)
                        cleaned.set()

        task = asyncio.create_task(operation())
        await entered.wait()
        task.cancel("inert Python 3.10 caller cancellation")
        with self.assertRaises(asyncio.CancelledError) as caught:
            await task
        self.assertEqual(caught.exception.args, ("inert Python 3.10 caller cancellation",))
        self.assertTrue(cleaned.is_set())
        self.assertFalse(deadline.expired())


if __name__ == "__main__":
    unittest.main()
