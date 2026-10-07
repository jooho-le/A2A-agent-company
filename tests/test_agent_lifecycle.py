"""17번 SQLite Agent lifecycle 계약. 실제 LLM/MCP 없이 재시작·중복·재개를 검사한다."""

import asyncio
from contextlib import asynccontextmanager
from copy import deepcopy
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from uuid import uuid4

from a2a.helpers import new_data_part, new_task
from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.cluster.task_store import ConcurrentTaskModificationError, VersionedTaskStore
from a2a.server.context import ServerCallContext
from a2a.server.events import EventQueue
from a2a.server.tasks import TaskUpdater
from a2a.types import Task, TaskState
from a2a.utils.constants import A2A_JSON_MEDIA_TYPE, VERSION_HEADER
from google.protobuf.json_format import MessageToDict
import httpx

from agents.core.config import AgentSettings
from agents.main import create_app
from orchestrator.a2a import A2AWorkflowMetadata, build_send_message_request


class RecordingExecutor(AgentExecutor):
    """선택한 SDK 상태를 게시하고 execute/cancel 횟수만 기록하는 테스트 실행기."""

    def __init__(self, *, outcome=TaskState.TASK_STATE_COMPLETED, block=False, artifact=False):
        self.outcome = outcome
        self.block = block
        self.artifact = artifact
        self.execute_count = 0
        self.cancel_count = 0
        self.task_ids = []
        self.context_ids = []
        self.wait = asyncio.Event()

    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        self.execute_count += 1
        self.task_ids.append(context.task_id)
        self.context_ids.append(context.context_id)
        updater = TaskUpdater(event_queue, context.task_id, context.context_id)
        if context.current_task is None:
            task = new_task(
                task_id=context.task_id, context_id=context.context_id,
                state=TaskState.TASK_STATE_SUBMITTED,
                history=[context.message] if context.message is not None else [],
            )
            task.metadata.update(context.metadata)
            await event_queue.enqueue_event(task)
        if self.block:
            await updater.update_status(TaskState.TASK_STATE_WORKING, metadata=context.metadata)
            await self.wait.wait()
        if self.artifact:
            await updater.add_artifact(
                [new_data_part({"fixture": "not a product validation result"}, media_type="application/json")],
                artifact_id=f"fixture::{context.task_id}", name="fixture-report.json",
            )
        await updater.update_status(self.outcome, metadata=context.metadata)

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        self.cancel_count += 1
        metadata = context.metadata
        if not metadata and context.current_task is not None:
            metadata = MessageToDict(context.current_task.metadata)
        updater = TaskUpdater(event_queue, context.task_id, context.context_id)
        await updater.update_status(TaskState.TASK_STATE_CANCELED, metadata=metadata)


class AgentLifecycleTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        environment = patch.dict(os.environ, {}, clear=True)
        environment.start()
        self.addCleanup(environment.stop)
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.database_path = Path(self.temporary.name) / "planner.sqlite3"
        self.metadata = A2AWorkflowMetadata(
            runId=uuid4(), workflowStepId=uuid4(), scenarioId=uuid4(),
            attempt=0, requirementIds=(uuid4(),),
        )

    @asynccontextmanager
    async def client_for(self, *, executor=None, database_path=None):
        settings = AgentSettings(
            role="PLANNER", database_path=database_path or self.database_path, _env_file=None,
        )
        app = create_app(settings, executor=executor)
        # Each restart test exits the previous lifespan before opening this DB again.
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url=settings.agent_base_url,
            ) as client:
                yield app, client

    @staticmethod
    def headers():
        return {VERSION_HEADER: "1.0", "Content-Type": A2A_JSON_MEDIA_TYPE}

    def wire(self, *, metadata=None, context_id=None, task_id=None, payload=None):
        return MessageToDict(build_send_message_request(
            payload or {"request": "Perform fixture work"}, metadata or self.metadata,
            context_id=context_id, task_id=task_id,
        ))

    async def send(self, client, body):
        response = await client.post("/message:send", json=body, headers=self.headers())
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()["task"]

    async def poll(self, client, task_id, state):
        for _ in range(50):
            response = await client.get(f"/tasks/{task_id}", headers=self.headers())
            self.assertEqual(response.status_code, 200, response.text)
            task = response.json()
            if task["status"]["state"] == state:
                return task
            await asyncio.sleep(0)
        self.fail(f"Expected {state}; observed {task['status']['state']}")

    def assert_error(self, response, status=400):
        self.assertEqual(response.status_code, status, response.text)
        self.assertEqual(response.json()["error"]["code"], status)

    async def test_task_metadata_artifact_and_ids_survive_a_closed_app_restart(self):
        executor = RecordingExecutor(artifact=True)
        body = self.wire()
        async with self.client_for(executor=executor) as (_, client):
            submitted = await self.send(client, body)
            before = await self.poll(client, submitted["id"], "TASK_STATE_COMPLETED")
        self.assertTrue(self.database_path.exists())
        fresh = RecordingExecutor()
        async with self.client_for(executor=fresh) as (app, client):
            self.assertIsInstance(app.state.task_store, VersionedTaskStore)
            after = await self.poll(client, submitted["id"], "TASK_STATE_COMPLETED")
        self.assertEqual(before, after)
        self.assertEqual(after["metadata"], self.metadata.to_a2a_json())
        self.assertEqual(after["artifacts"][0]["name"], "fixture-report.json")
        self.assertEqual(executor.execute_count, 1)
        self.assertEqual(fresh.execute_count, 0)

    async def test_identical_message_replay_returns_terminal_task_without_reexecution(self):
        executor = RecordingExecutor()
        body = self.wire()
        async with self.client_for(executor=executor) as (_, client):
            submitted = await self.send(client, body)
            completed = await self.poll(client, submitted["id"], "TASK_STATE_COMPLETED")
            repeated = await self.send(client, deepcopy(body))
        self.assertEqual(repeated, completed)
        self.assertEqual(executor.execute_count, 1)

    async def test_duplicate_message_remains_idempotent_after_restart(self):
        body = self.wire()
        first = RecordingExecutor()
        async with self.client_for(executor=first) as (_, client):
            submitted = await self.send(client, body)
            completed = await self.poll(client, submitted["id"], "TASK_STATE_COMPLETED")
        second = RecordingExecutor()
        async with self.client_for(executor=second) as (_, client):
            repeated = await self.send(client, body)
        self.assertEqual(repeated, completed)
        self.assertEqual(first.execute_count, 1)
        self.assertEqual(second.execute_count, 0)

    async def test_concurrent_duplicate_messages_claim_one_task_and_one_execution(self):
        executor = RecordingExecutor(block=True)
        body = self.wire()
        async with self.client_for(executor=executor) as (_, client):
            responses = await asyncio.gather(*(
                client.post("/message:send", json=deepcopy(body), headers=self.headers())
                for _ in range(4)
            ))
            for response in responses:
                self.assertEqual(response.status_code, 200, response.text)
            tasks = [response.json()["task"] for response in responses]
            self.assertEqual(len({task["id"] for task in tasks}), 1)
            self.assertEqual(len({task["contextId"] for task in tasks}), 1)
            await self.poll(client, tasks[0]["id"], "TASK_STATE_WORKING")
            self.assertEqual(executor.execute_count, 1)

    async def test_same_message_id_with_changed_payload_is_a_conflict(self):
        executor = RecordingExecutor()
        body = self.wire()
        changed = deepcopy(body)
        changed["message"]["parts"][0]["data"]["request"] = "Different fixture work"
        async with self.client_for(executor=executor) as (_, client):
            submitted = await self.send(client, body)
            await self.poll(client, submitted["id"], "TASK_STATE_COMPLETED")
            response = await client.post("/message:send", json=changed, headers=self.headers())
            self.assert_error(response)
        self.assertEqual(executor.execute_count, 1)

    async def test_same_message_id_with_changed_workflow_metadata_is_a_conflict(self):
        body = self.wire()
        changed = deepcopy(body)
        changed["metadata"]["workflowStepId"] = str(uuid4())
        executor = RecordingExecutor()
        async with self.client_for(executor=executor) as (_, client):
            submitted = await self.send(client, body)
            await self.poll(client, submitted["id"], "TASK_STATE_COMPLETED")
            response = await client.post("/message:send", json=changed, headers=self.headers())
            self.assert_error(response)
        self.assertEqual(executor.execute_count, 1)

    async def test_distinct_message_ids_create_distinct_tasks(self):
        executor = RecordingExecutor()
        first = self.wire()
        second = deepcopy(first)
        second["message"]["messageId"] = str(uuid4())
        async with self.client_for(executor=executor) as (_, client):
            one = await self.send(client, first)
            two = await self.send(client, second)
            await self.poll(client, one["id"], "TASK_STATE_COMPLETED")
            await self.poll(client, two["id"], "TASK_STATE_COMPLETED")
        self.assertNotEqual(one["id"], two["id"])
        self.assertEqual(executor.execute_count, 2)

    async def test_context_binding_survives_restart_for_a_new_step_in_the_same_run(self):
        async with self.client_for(executor=RecordingExecutor()) as (_, client):
            one = await self.send(client, self.wire())
            await self.poll(client, one["id"], "TASK_STATE_COMPLETED")
        metadata = self.metadata.model_copy(update={"workflow_step_id": uuid4(), "attempt": 1})
        second = RecordingExecutor()
        async with self.client_for(executor=second) as (_, client):
            two = await self.send(client, self.wire(metadata=metadata, context_id=one["contextId"]))
            finished = await self.poll(client, two["id"], "TASK_STATE_COMPLETED")
        self.assertNotEqual(one["id"], two["id"])
        self.assertEqual(one["contextId"], two["contextId"])
        self.assertEqual(finished["metadata"], metadata.to_a2a_json())
        self.assertEqual(second.execute_count, 1)

    async def test_persisted_context_rejects_another_run_or_scenario(self):
        async with self.client_for(executor=RecordingExecutor()) as (_, client):
            one = await self.send(client, self.wire())
            await self.poll(client, one["id"], "TASK_STATE_COMPLETED")
        second = RecordingExecutor()
        async with self.client_for(executor=second) as (_, client):
            for field in ("run_id", "scenario_id"):
                with self.subTest(field=field):
                    metadata = self.metadata.model_copy(update={field: uuid4()})
                    response = await client.post(
                        "/message:send", json=self.wire(metadata=metadata, context_id=one["contextId"]),
                        headers=self.headers(),
                    )
                    self.assert_error(response)
        self.assertEqual(second.execute_count, 0)

    async def test_interrupted_task_can_resume_after_restart_with_a_new_message_id(self):
        for state in (TaskState.TASK_STATE_INPUT_REQUIRED, TaskState.TASK_STATE_AUTH_REQUIRED):
            with self.subTest(state=TaskState.Name(state)):
                database = Path(self.temporary.name) / f"{TaskState.Name(state)}.sqlite3"
                original_body = self.wire()
                initial = RecordingExecutor(outcome=state)
                async with self.client_for(executor=initial, database_path=database) as (_, client):
                    one = await self.send(client, original_body)
                    interrupted = await self.poll(client, one["id"], TaskState.Name(state))
                    repeated = await self.send(client, original_body)
                    self.assertEqual(repeated, interrupted)
                    self.assertEqual(initial.execute_count, 1)
                continuation = self.wire(
                    context_id=one["contextId"], task_id=one["id"], payload={"userInput": "Continue fixture work"},
                )
                resumed = RecordingExecutor()
                async with self.client_for(executor=resumed, database_path=database) as (_, client):
                    two = await self.send(client, continuation)
                    completed = await self.poll(client, two["id"], "TASK_STATE_COMPLETED")
                    replay = await self.send(client, original_body)
                    self.assertEqual(replay, completed)
                self.assertEqual(two["id"], one["id"])
                self.assertEqual(two["contextId"], one["contextId"])
                self.assertEqual(completed["metadata"], self.metadata.to_a2a_json())
                self.assertEqual(resumed.execute_count, 1)

    async def test_new_message_cannot_resume_a_working_task(self):
        executor = RecordingExecutor(block=True)
        async with self.client_for(executor=executor) as (_, client):
            one = await self.send(client, self.wire())
            await self.poll(client, one["id"], "TASK_STATE_WORKING")
            response = await client.post(
                "/message:send", json=self.wire(context_id=one["contextId"], task_id=one["id"]),
                headers=self.headers(),
            )
            self.assert_error(response)
            self.assertEqual(executor.execute_count, 1)

    async def test_active_shutdown_restart_is_failed_without_automatic_reexecution(self):
        first = RecordingExecutor(block=True)
        body = self.wire()
        async with self.client_for(executor=first) as (_, client):
            one = await self.send(client, body)
            await self.poll(client, one["id"], "TASK_STATE_WORKING")
        fresh = RecordingExecutor()
        async with self.client_for(executor=fresh) as (app, client):
            failed = await self.poll(client, one["id"], "TASK_STATE_FAILED")
            self.assertEqual(failed["status"]["message"]["parts"][0]["data"]["code"],
                             "AGENT_EXECUTION_INTERRUPTED")
            self.assertEqual(failed["metadata"], self.metadata.to_a2a_json())
            self.assertFalse(failed.get("artifacts"))
            replay = await self.send(client, body)
            self.assertEqual(replay, failed)
            revisions = await app.state.task_store.revisions(one["id"])
            self.assertIn(TaskState.TASK_STATE_WORKING, [task.status.state for task in revisions])
            self.assertEqual(revisions[-1].status.state, TaskState.TASK_STATE_FAILED)
        self.assertEqual(first.execute_count, 1)
        self.assertEqual(fresh.execute_count, 0)

    async def test_terminal_http_operations_cannot_change_task_or_revision_history(self):
        executor = RecordingExecutor()
        async with self.client_for(executor=executor) as (app, client):
            one = await self.send(client, self.wire())
            completed = await self.poll(client, one["id"], "TASK_STATE_COMPLETED")
            before = await app.state.task_store.revisions(one["id"])
            response = await client.post(f"/tasks/{one['id']}:cancel", headers=self.headers())
            self.assert_error(response)
            response = await client.post(
                "/message:send", json=self.wire(context_id=one["contextId"], task_id=one["id"]),
                headers=self.headers(),
            )
            self.assert_error(response)
            after = await app.state.task_store.revisions(one["id"])
            self.assertEqual([MessageToDict(task) for task in before], [MessageToDict(task) for task in after])
            self.assertEqual(await self.poll(client, one["id"], "TASK_STATE_COMPLETED"), completed)
        self.assertEqual(executor.execute_count, 1)

    async def test_store_itself_rejects_rewriting_a_terminal_task_at_the_current_version(self):
        async with self.client_for(executor=RecordingExecutor()) as (app, client):
            one = await self.send(client, self.wire())
            await self.poll(client, one["id"], "TASK_STATE_COMPLETED")
            context = ServerCallContext()
            stored = await app.state.task_store.get(one["id"], context)
            changed = Task()
            changed.CopyFrom(stored.task)
            changed.status.state = TaskState.TASK_STATE_WORKING
            with self.assertRaises(ConcurrentTaskModificationError):
                await app.state.task_store.save(
                    changed, event=None, prev=stored.task, prev_version=stored.version, context=context,
                )
            after = await app.state.task_store.get(one["id"], context)
            self.assertEqual(after.version, stored.version)
            self.assertEqual(MessageToDict(after.task), MessageToDict(stored.task))

    async def test_confirmed_cancellation_and_original_message_replay_survive_restart(self):
        first = RecordingExecutor(block=True)
        body = self.wire()
        async with self.client_for(executor=first) as (_, client):
            one = await self.send(client, body)
            await self.poll(client, one["id"], "TASK_STATE_WORKING")
            response = await client.post(f"/tasks/{one['id']}:cancel", headers=self.headers())
            self.assertEqual(response.status_code, 200, response.text)
            canceled = response.json()
            self.assertEqual(canceled["status"]["state"], "TASK_STATE_CANCELED")
        second = RecordingExecutor()
        async with self.client_for(executor=second) as (_, client):
            self.assertEqual(await self.poll(client, one["id"], "TASK_STATE_CANCELED"), canceled)
            replay = await self.send(client, body)
            self.assertEqual(replay, canceled)
            response = await client.post(f"/tasks/{one['id']}:cancel", headers=self.headers())
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.json(), canceled)
        self.assertEqual(first.cancel_count, 1)
        self.assertEqual(second.execute_count, 0)
        self.assertEqual(second.cancel_count, 0)

    async def test_raw_secrets_do_not_persist_in_task_history_or_database(self):
        body = self.wire()
        body["message"]["parts"][0]["data"].update(
            password="DUMMY_PERSISTENT_PASSWORD", apiKey="DUMMY_PERSISTENT_API_KEY",
        )
        async with self.client_for(executor=RecordingExecutor()) as (_, client):
            one = await self.send(client, body)
            await self.poll(client, one["id"], "TASK_STATE_COMPLETED")
        fresh = RecordingExecutor()
        async with self.client_for(executor=fresh) as (app, client):
            task = await self.poll(client, one["id"], "TASK_STATE_COMPLETED")
            replay = await self.send(client, body)
            revisions = await app.state.task_store.revisions(one["id"])
            outputs = (
                json.dumps(task), json.dumps(replay), json.dumps([MessageToDict(row) for row in revisions]),
            )
            for output in outputs:
                self.assertNotIn("DUMMY_PERSISTENT_PASSWORD", output)
                self.assertNotIn("DUMMY_PERSISTENT_API_KEY", output)
        database_bytes = self.database_path.read_bytes()
        self.assertNotIn(b"DUMMY_PERSISTENT_PASSWORD", database_bytes)
        self.assertNotIn(b"DUMMY_PERSISTENT_API_KEY", database_bytes)
        self.assertEqual(fresh.execute_count, 0)


if __name__ == "__main__":
    unittest.main()
