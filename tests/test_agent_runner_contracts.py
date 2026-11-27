"""Real A2A Client/TaskRunner ↔ ASGI Agent contracts, without LLM/MCP work."""

from contextlib import asynccontextmanager
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from uuid import uuid4

from a2a.helpers import new_data_part
from a2a.server.agent_execution import AgentExecutor
from a2a.server.context import ServerCallContext
from a2a.server.tasks import TaskUpdater
from a2a.types import TaskState
from google.protobuf.json_format import MessageToDict
import httpx

from agents.core.config import AgentSettings
from agents.main import create_app
from orchestrator.a2a import A2AAgentClient
from orchestrator.application.a2a_tasks import (
    A2ATaskRunner, TaskPollingPolicy, TaskRunDisposition,
)
from orchestrator.domain import AgentRole, WorkflowRun, WorkflowStep


class InterruptedExecutor(AgentExecutor):
    """Publish a question and return; later approved calls eventually finish."""

    def __init__(self, state, *, final_attempt=2):
        self.state = state
        self.final_attempt = final_attempt
        self.calls = []

    async def execute(self, context, event_queue):
        metadata = context.metadata
        self.calls.append((context.task_id, context.context_id, metadata["attempt"]))
        updater = TaskUpdater(event_queue, context.task_id, context.context_id)
        if metadata["attempt"] < self.final_attempt:
            await updater.update_status(
                self.state,
                message=updater.new_agent_message(parts=[new_data_part(
                    {"question": "Explicit operator input is required"},
                    media_type="application/json",
                )]),
                metadata=metadata,
            )
        else:
            await updater.update_status(TaskState.TASK_STATE_COMPLETED, metadata=metadata)

    async def cancel(self, context, event_queue):
        await TaskUpdater(event_queue, context.task_id, context.context_id).update_status(
            TaskState.TASK_STATE_CANCELED,
        )


class AgentRunnerContractTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        environment = patch.dict(os.environ, {}, clear=True)
        environment.start()
        self.addCleanup(environment.stop)
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "planner.sqlite3"
        self.settings = AgentSettings(
            role="PLANNER", database_path=self.path,
            bearer_token="DUMMY_RUNNER_OPERATOR_TOKEN", _env_file=None,
        )
        self.run = WorkflowRun(scenario_id=uuid4(), request_text="Plan fixture work")
        self.step = WorkflowStep(
            run_id=self.run.run_id, agent_role=AgentRole.PLANNER,
            requirement_ids=[uuid4()], input_artifact_ids=[uuid4()], code_version=1,
        )

    @asynccontextmanager
    async def client_for(self, executor):
        accepted_requests = []

        async def capture(request):
            if request.url.path == "/message:send":
                accepted_requests.append(json.loads(await request.aread()))

        app = create_app(self.settings, executor=executor)
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url=self.settings.agent_base_url,
                event_hooks={"request": [capture]},
            ) as http_client:
                async with A2AAgentClient(
                    self.settings.agent_base_url, httpx_client=http_client,
                    headers={"Authorization": "Bearer DUMMY_RUNNER_OPERATOR_TOKEN"},
                ) as client:
                    await client.resolve_agent_card()
                    runner = A2ATaskRunner(
                        client, policy=TaskPollingPolicy(interval_seconds=0.001, timeout_seconds=2),
                    )
                    yield app, http_client, runner, accepted_requests

    async def continue_waiting(self, runner, waiting, state):
        kwargs = {
            "agent_id": "planner", "payload": {"request": "Operator approved continuation"},
            "agent_context": waiting.agent_context,
        }
        if state == TaskState.TASK_STATE_AUTH_REQUIRED:
            return await runner.continue_after_auth(
                self.run, waiting.step, authentication_configured=True, **kwargs,
            )
        return await runner.continue_after_input(self.run, waiting.step, **kwargs)

    async def assert_real_two_continuations(self, state):
        executor = InterruptedExecutor(state)
        async with self.client_for(executor) as (app, http_client, runner, requests):
            waiting = await runner.submit_and_wait(
                self.run, self.step, agent_id="planner", payload={"request": "Plan fixture work"},
            )
            again = await self.continue_waiting(runner, waiting, state)
            finished = await self.continue_waiting(runner, again, state)
            self.assertEqual([waiting.step.attempt, again.step.attempt, finished.step.attempt], [0, 1, 2])
            self.assertEqual(finished.disposition, TaskRunDisposition.COMPLETED)
            self.assertEqual([call[2] for call in executor.calls], [0, 1, 2])
            self.assertEqual(len({call[:2] for call in executor.calls}), 1)
            self.assertEqual(finished.step.a2a_task_id, waiting.step.a2a_task_id)
            self.assertEqual(finished.step.agent_context_id, waiting.step.agent_context_id)
            self.assertEqual(len(finished.task.history), 5)
            self.assertEqual(MessageToDict(finished.task.metadata)["attempt"], 2)
            self.assertEqual([body["metadata"]["attempt"] for body in requests], [0, 1, 2])
            for previous, current in zip(requests, requests[1:]):
                self.assertNotEqual(previous["message"]["messageId"], current["message"]["messageId"])
                self.assertEqual(
                    {key: value for key, value in previous["metadata"].items() if key != "attempt"},
                    {key: value for key, value in current["metadata"].items() if key != "attempt"},
                )
            # Replay any old accepted call after the latest attempt completed.
            for body in tuple(requests):
                replay = await http_client.post("/message:send", json=body)
                self.assertEqual(replay.status_code, 200, replay.text)
                self.assertEqual(replay.json()["task"]["metadata"]["attempt"], 2)
                self.assertEqual(replay.json()["task"]["id"], finished.task.id)
            self.assertEqual(len(executor.calls), 3)
            stored = await app.state.task_store.get(finished.task.id, ServerCallContext())
            self.assertEqual(MessageToDict(stored.task.metadata)["attempt"], 2)
            self.assertNotIn("DUMMY_RUNNER_OPERATOR_TOKEN", str(MessageToDict(stored.task)))

    async def test_existing_runner_input_continues_same_task_with_attempt_one_and_two(self):
        await self.assert_real_two_continuations(TaskState.TASK_STATE_INPUT_REQUIRED)

    async def test_existing_runner_auth_continues_same_task_with_attempt_one_and_two(self):
        await self.assert_real_two_continuations(TaskState.TASK_STATE_AUTH_REQUIRED)

    async def test_existing_runner_resumes_after_agent_restart_and_does_not_replay_old_messages(self):
        initial = InterruptedExecutor(TaskState.TASK_STATE_INPUT_REQUIRED, final_attempt=1)
        async with self.client_for(initial) as (_, _, runner, _):
            waiting = await runner.submit_and_wait(
                self.run, self.step, agent_id="planner", payload={"request": "Plan fixture work"},
            )
        resumed_executor = InterruptedExecutor(TaskState.TASK_STATE_INPUT_REQUIRED, final_attempt=1)
        async with self.client_for(resumed_executor) as (_, _, runner, requests):
            observed = await runner.resume_polling(
                self.run, waiting.step, agent_id="planner", agent_context=waiting.agent_context,
            )
            self.assertEqual(observed.disposition, TaskRunDisposition.WAITING_INPUT)
            self.assertEqual(resumed_executor.calls, [])
            completed = await self.continue_waiting(runner, observed, TaskState.TASK_STATE_INPUT_REQUIRED)
            self.assertEqual(completed.disposition, TaskRunDisposition.COMPLETED)
            self.assertEqual(completed.step.attempt, 1)
            self.assertEqual(completed.task.id, waiting.task.id)
            self.assertEqual(len(requests), 1)
            self.assertEqual(requests[0]["metadata"]["attempt"], 1)
        self.assertEqual(len(initial.calls), 1)
        self.assertEqual(len(resumed_executor.calls), 1)


if __name__ == "__main__":
    unittest.main()
