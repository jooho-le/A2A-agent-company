"""Step-17 lifecycle safety checks, using isolated settings and temporary DBs."""

import asyncio
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from uuid import uuid4

from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.context import ServerCallContext
from a2a.types import TaskState
from a2a.utils.errors import UnsupportedOperationError
from google.protobuf.json_format import MessageToDict
import httpx
from pydantic import ValidationError

from agents.api.validation import parse_project_request
from agents.core.config import AgentSettings
from agents.main import create_app
from agents.runtime.lifecycle import SafeAgentExecutor


class CrashedExecutor(AgentExecutor):
    def __init__(self, exception=ValueError):
        self.exception = exception
        self.calls = 0

    async def execute(self, context, event_queue):
        self.calls += 1
        raise self.exception("DUMMY_UNLABELLED_EXECUTOR_SECRET")

    async def cancel(self, context, event_queue):
        raise self.exception("DUMMY_UNLABELLED_CANCEL_SECRET")


class EventQueueSpy:
    def __init__(self):
        self.events = []

    async def enqueue_event(self, event):
        self.events.append(event)


class EventlessWaitingExecutor(AgentExecutor):
    """First external await deliberately occurs before any executor event."""

    def __init__(self):
        self.wait = asyncio.Event()
        self.stopped = False

    async def execute(self, context, event_queue):
        try:
            await self.wait.wait()
        finally:
            self.stopped = True

    async def cancel(self, context, event_queue):
        pass  # SDK confirms CANCELED after terminating this worker.


def request_body():
    return {
        "message": {
            "messageId": str(uuid4()), "role": "ROLE_USER",
            "parts": [{"data": {"request": "Plan signup"}, "mediaType": "application/json"}],
        },
        "configuration": {"returnImmediately": True, "acceptedOutputModes": ["application/json"]},
        "metadata": {
            "runId": str(uuid4()), "workflowStepId": str(uuid4()),
            "scenarioId": str(uuid4()), "attempt": 0,
        },
    }


class AgentLifecycleSafetyTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        environment = patch.dict(os.environ, {}, clear=True)
        environment.start()
        self.addCleanup(environment.stop)
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "new-dir" / "planner.sqlite3"
        self.settings = AgentSettings(role="PLANNER", database_path=self.path, _env_file=None)
        self.context = RequestContext(
            call_context=ServerCallContext(), task_id=str(uuid4()), context_id=str(uuid4()),
        )

    async def test_app_construction_and_settings_have_no_database_side_effects(self):
        app = create_app(self.settings)
        self.assertFalse(self.path.parent.exists())
        self.assertEqual(self.settings.task_database_path, self.path)
        self.assertFalse(hasattr(app.state, "settings"))

    async def test_blank_or_in_memory_database_settings_are_rejected(self):
        for value in ("", " ", ".", ":memory:"):
            with self.subTest(path=value), self.assertRaises(ValidationError):
                AgentSettings(role="PLANNER", database_path=value, _env_file=None)

    async def test_database_environment_override_is_operator_configuration_only(self):
        with patch.dict(os.environ, {"AGENT_DATABASE_PATH": str(self.path)}):
            settings = AgentSettings(role="PLANNER", _env_file=None)
        self.assertEqual(settings.task_database_path, self.path)
        self.assertFalse(self.path.exists())

    async def test_executor_exception_text_is_never_forwarded_to_sdk(self):
        executor = SafeAgentExecutor(CrashedExecutor())
        for method, code in (
            (executor.execute, "AGENT_EXECUTION_FAILED"),
            (executor.cancel, "AGENT_CANCELLATION_FAILED"),
        ):
            with self.subTest(code=code), self.assertRaises(RuntimeError) as error:
                await method(self.context, EventQueueSpy())
            self.assertEqual(str(error.exception), code)
            self.assertIsNone(error.exception.__cause__)
            self.assertTrue(error.exception.__suppress_context__)

    async def test_cancellation_is_not_converted_into_success_or_product_failure(self):
        executor = SafeAgentExecutor(CrashedExecutor(asyncio.CancelledError))
        for method in (executor.execute, executor.cancel):
            with self.assertRaises(asyncio.CancelledError):
                await method(self.context, EventQueueSpy())

    async def test_wrapper_announces_submission_before_delegated_execution(self):
        executor = SafeAgentExecutor(CrashedExecutor())
        queue = EventQueueSpy()
        with self.assertRaises(RuntimeError):
            await executor.execute(self.context, queue)
        self.assertEqual(len(queue.events), 1)
        self.assertEqual(queue.events[0].status.state, TaskState.TASK_STATE_SUBMITTED)

    async def test_eventless_waiting_executor_does_not_block_send_or_cancel(self):
        executor = EventlessWaitingExecutor()
        app = create_app(self.settings, executor=executor)
        headers = {"A2A-Version": "1.0", "Content-Type": "application/a2a+json"}
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8101",
            ) as client:
                response = await asyncio.wait_for(
                    client.post("/message:send", json=request_body(), headers=headers), 2,
                )
                self.assertEqual(response.status_code, 200, response.text)
                task_id = response.json()["task"]["id"]
                canceled = await asyncio.wait_for(
                    client.post(f"/tasks/{task_id}:cancel", json={}, headers=headers), 2,
                )
                self.assertEqual(canceled.status_code, 200, canceled.text)
                self.assertEqual(canceled.json()["status"]["state"], "TASK_STATE_CANCELED")
        self.assertTrue(executor.stopped)

    async def test_eventless_waiting_executor_does_not_block_shutdown(self):
        executor = EventlessWaitingExecutor()
        app = create_app(self.settings, executor=executor)

        async def run_and_close():
            async with app.router.lifespan_context(app):
                async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8101",
                ) as client:
                    response = await client.post("/message:send", json=request_body(), headers={
                        "A2A-Version": "1.0", "Content-Type": "application/a2a+json",
                    })
                    self.assertEqual(response.status_code, 200, response.text)
        await asyncio.wait_for(run_and_close(), 2)
        self.assertTrue(executor.stopped)

    async def test_shutdown_gate_rejects_new_execution_without_creating_db(self):
        app = create_app(self.settings)
        handler = app.state.request_handler
        await handler.aclose()
        with self.assertRaises(UnsupportedOperationError):
            await handler.on_message_send(parse_project_request(request_body()), ServerCallContext())
        self.assertFalse(self.path.exists())

    async def test_failed_executor_is_persisted_without_exception_secret_and_not_replayed(self):
        executor = CrashedExecutor()
        app = create_app(self.settings, executor=executor)
        body = request_body()
        headers = {"A2A-Version": "1.0", "Content-Type": "application/a2a+json"}
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8101",
            ) as client:
                first = await client.post("/message:send", json=body, headers=headers)
                self.assertIn(first.status_code, (200, 500), first.text)
                self.assertNotIn("DUMMY_UNLABELLED_EXECUTOR_SECRET", first.text)
                replay = await client.post("/message:send", json=body, headers=headers)
                self.assertEqual(replay.status_code, 200, replay.text)
                task = replay.json()["task"]
                for _ in range(50):
                    if task["status"]["state"] == "TASK_STATE_FAILED":
                        break
                    await asyncio.sleep(0)
                    replay = await client.post("/message:send", json=body, headers=headers)
                    self.assertEqual(replay.status_code, 200, replay.text)
                    task = replay.json()["task"]
                self.assertEqual(task["status"]["state"], "TASK_STATE_FAILED")
                revisions = await app.state.task_store.revisions(task["id"])
                persisted = str([MessageToDict(item) for item in revisions])
                self.assertNotIn("DUMMY_UNLABELLED_EXECUTOR_SECRET", persisted)
                self.assertEqual(executor.calls, 1)


if __name__ == "__main__":
    unittest.main()
