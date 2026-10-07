"""Durable Agent storage without LLM, MCP, HTTP servers, or real product code."""

import asyncio
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4

from a2a.helpers import new_data_part
from a2a.server.cluster.task_store import ConcurrentTaskModificationError
from a2a.server.cluster.version import TaskVersion
from a2a.server.context import ServerCallContext
from a2a.types import Artifact, ListTasksRequest, Message, Role, Task, TaskState
from a2a.utils.errors import InvalidParamsError, TaskNotFoundError, UnsupportedOperationError
from google.protobuf.json_format import MessageToDict

from agents.api.sqlite_task_store import (
    AgentStoreLeaseError, AgentStoreRoleError, AgentTaskStoreError, SQLiteAgentTaskStore,
)
from orchestrator.a2a.requests import A2AWorkflowMetadata, build_send_message_request
from orchestrator.domain.states import AgentRole


def task_copy(task):
    copied = Task()
    copied.CopyFrom(task)
    return copied


class AgentTaskStoreTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "agents" / "planner.sqlite3"
        self.store = SQLiteAgentTaskStore(self.path, AgentRole.PLANNER)
        self.context = ServerCallContext()
        self.metadata = A2AWorkflowMetadata(
            run_id=uuid4(), workflow_step_id=uuid4(), scenario_id=uuid4(), attempt=0,
        )
        await self.store.start()
        self.addAsyncCleanup(self.store.aclose)

    def request(self, payload=None, *, metadata=None, task_id=None, context_id=None, message_id=None):
        return build_send_message_request(
            payload or {"request": "Plan signup"}, metadata or self.metadata,
            task_id=task_id, context_id=context_id, message_id=message_id,
        )

    async def claim(self, request=None):
        return await self.store.claim_message(request or self.request(), self.context)

    async def write_state(self, task_id, state, *, message=None, artifact=None):
        stored = await self.store.get(task_id, self.context)
        updated = task_copy(stored.task)
        updated.status.state = state
        if message is not None:
            updated.status.message.CopyFrom(message)
        if artifact is not None:
            updated.artifacts.append(artifact)
        await self.store.save(
            updated, event=None, prev=stored.task, prev_version=stored.version, context=self.context,
        )
        return await self.store.get(task_id, self.context)

    def agent_message(self, task, payload):
        return Message(
            message_id=str(uuid4()), task_id=task.id, context_id=task.context_id,
            role=Role.ROLE_AGENT, parts=[new_data_part(payload, media_type="application/json")],
        )

    async def test_constructor_creates_nothing_and_unstarted_operations_fail(self):
        path = Path(self.directory.name) / "unstarted" / "developer.sqlite3"
        store = SQLiteAgentTaskStore(path, AgentRole.DEVELOPER)
        self.assertFalse(path.parent.exists())
        with self.assertRaises(AgentTaskStoreError):
            await store.get("missing", self.context)
        with self.assertRaises(AgentTaskStoreError):
            await store.claim_message(self.request(), self.context)

    async def test_claim_preallocates_server_ids_metadata_and_sanitized_history(self):
        request = self.request({"request": "Plan signup", "password": "DUMMY_STORE_PASSWORD"})
        claim = await self.claim(request)
        self.assertFalse(claim.duplicate)
        self.assertNotEqual(claim.task.id, str(self.metadata.workflow_step_id))
        self.assertEqual(claim.task.status.state, TaskState.TASK_STATE_SUBMITTED)
        self.assertEqual(len(claim.task.history), 1)
        history = claim.task.history[0]
        self.assertEqual(history.message_id, request.message.message_id)
        self.assertEqual(history.task_id, claim.task.id)
        self.assertEqual(history.context_id, claim.task.context_id)
        self.assertNotIn("DUMMY_STORE_PASSWORD", str(MessageToDict(claim.task)))
        stored = await self.store.get(claim.task.id, self.context)
        self.assertEqual(stored.version, TaskVersion(1))
        self.assertEqual(len(await self.store.revisions(claim.task.id)), 1)
        # A returned proto is a copy, not a mutable view of persistence.
        claim.task.status.state = TaskState.TASK_STATE_COMPLETED
        self.assertEqual((await self.store.get(stored.task.id, self.context)).task.status.state, TaskState.TASK_STATE_SUBMITTED)

    async def test_same_message_id_is_idempotent_even_after_terminal_completion(self):
        request = self.request()
        first = await self.claim(request)
        await self.write_state(first.task.id, TaskState.TASK_STATE_COMPLETED)
        repeated = await self.claim(request)
        self.assertTrue(repeated.duplicate)
        self.assertEqual(repeated.task.id, first.task.id)
        self.assertEqual(repeated.task.status.state, TaskState.TASK_STATE_COMPLETED)
        self.assertEqual(len(await self.store.revisions(first.task.id)), 2)

    async def test_same_message_id_different_payload_or_metadata_is_rejected(self):
        request = self.request()
        first = await self.claim(request)
        changed = self.request({"request": "Change requirements"}, message_id=uuid4())
        changed.message.message_id = request.message.message_id
        with self.assertRaises(InvalidParamsError):
            await self.claim(changed)
        changed = self.request()
        changed.message.message_id = request.message.message_id
        changed.metadata.update({"runId": str(uuid4())})
        with self.assertRaises(InvalidParamsError):
            await self.claim(changed)
        self.assertEqual(len(await self.store.revisions(first.task.id)), 1)

    async def test_concurrent_claims_execute_only_one_reservation(self):
        request = self.request()
        claims = await asyncio.gather(*(self.claim(request) for _ in range(8)))
        self.assertEqual(sum(not claim.duplicate for claim in claims), 1)
        self.assertEqual(len({claim.task.id for claim in claims}), 1)
        self.assertEqual(len(await self.store.revisions(claims[0].task.id)), 1)

    async def test_existing_context_can_create_new_step_but_not_another_run(self):
        first = await self.claim()
        metadata = self.metadata.model_copy(update={"workflow_step_id": uuid4(), "attempt": 1})
        second = await self.claim(self.request(metadata=metadata, context_id=first.task.context_id))
        self.assertNotEqual(first.task.id, second.task.id)
        self.assertEqual(first.task.context_id, second.task.context_id)
        alien = metadata.model_copy(update={"run_id": uuid4()})
        with self.assertRaises(InvalidParamsError):
            await self.claim(self.request(metadata=alien, context_id=first.task.context_id))
        with self.assertRaises(InvalidParamsError):
            await self.claim(self.request(context_id="unknown-context"))

    async def test_explicit_task_id_must_exist(self):
        with self.assertRaises(TaskNotFoundError):
            await self.claim(self.request(task_id="client-chosen-task"))

    async def test_unknown_task_with_unknown_context_still_returns_task_not_found(self):
        with self.assertRaises(TaskNotFoundError):
            await self.claim(self.request(task_id="unknown-task", context_id="unknown-context"))

    async def test_task_event_cannot_reference_another_task(self):
        first = await self.claim()
        current = await self.store.get(first.task.id, self.context)
        event = task_copy(current.task)
        event.id = "another-task"
        with self.assertRaises(InvalidParamsError):
            await self.store.save(
                current.task, event=event, prev=current.task,
                prev_version=current.version, context=self.context,
            )
        self.assertEqual(len(await self.store.revisions(first.task.id)), 1)

    async def test_input_and_auth_resume_preserve_status_message_and_input_once(self):
        for state in (TaskState.TASK_STATE_INPUT_REQUIRED, TaskState.TASK_STATE_AUTH_REQUIRED):
            with self.subTest(state=state):
                initial = await self.claim()
                prompt = self.agent_message(initial.task, {"request": "Provide input"})
                await self.write_state(initial.task.id, state, message=prompt)
                followup = self.request(
                    {"inputData": {"answer": "Proceed"}},
                    task_id=initial.task.id, context_id=initial.task.context_id,
                    metadata=self.metadata.model_copy(update={"attempt": 1}),
                )
                resumed = await self.claim(followup)
                self.assertEqual(resumed.task.id, initial.task.id)
                self.assertEqual(resumed.task.status.state, TaskState.TASK_STATE_SUBMITTED)
                self.assertEqual(MessageToDict(resumed.task.metadata)["attempt"], 1)
                self.assertFalse(resumed.task.status.HasField("message"))
                self.assertEqual(len(resumed.task.history), 3)
                self.assertEqual(resumed.task.history[1].message_id, prompt.message_id)
                self.assertEqual(resumed.task.history[2].message_id, followup.message.message_id)
                repeated = await self.claim(followup)
                self.assertTrue(repeated.duplicate)
                self.assertEqual(len(repeated.task.history), 3)

    async def test_new_followup_is_blocked_for_active_and_terminal_tasks(self):
        first = await self.claim()
        followup = self.request(task_id=first.task.id, context_id=first.task.context_id)
        with self.assertRaises(UnsupportedOperationError):
            await self.claim(followup)
        await self.write_state(first.task.id, TaskState.TASK_STATE_COMPLETED)
        with self.assertRaises(UnsupportedOperationError):
            await self.claim(followup)

    async def test_normal_stale_cas_write_is_rejected(self):
        first = await self.claim()
        old = await self.store.get(first.task.id, self.context)
        await self.write_state(first.task.id, TaskState.TASK_STATE_WORKING)
        stale = task_copy(old.task)
        stale.status.state = TaskState.TASK_STATE_COMPLETED
        with self.assertRaises(ConcurrentTaskModificationError):
            await self.store.save(stale, event=None, prev=old.task, prev_version=old.version, context=self.context)
        self.assertEqual(len(await self.store.revisions(first.task.id)), 2)

    async def test_stale_cancel_preserves_latest_artifacts_and_history(self):
        first = await self.claim()
        old = await self.store.get(first.task.id, self.context)
        newer = task_copy(old.task)
        newer.status.state = TaskState.TASK_STATE_WORKING
        newer.history.append(self.agent_message(newer, {"progress": "Saved"}))
        newer.artifacts.append(Artifact(
            artifact_id="artifact:password=opaque",
            parts=[new_data_part({"password": "DUMMY_ARTIFACT_PASSWORD"}, media_type="application/json")],
        ))
        await self.store.save(newer, event=None, prev=old.task, prev_version=old.version, context=self.context)
        canceled = task_copy(old.task)
        canceled.status.state = TaskState.TASK_STATE_CANCELED
        await self.store.save(canceled, event=None, prev=old.task, prev_version=old.version, context=self.context)
        final = (await self.store.get(first.task.id, self.context)).task
        self.assertEqual(final.status.state, TaskState.TASK_STATE_CANCELED)
        self.assertEqual(len(final.history), 2)
        self.assertEqual(final.artifacts[0].artifact_id, "artifact:password=opaque")
        self.assertNotIn("DUMMY_ARTIFACT_PASSWORD", str(MessageToDict(final)))

    async def test_terminal_tasks_cannot_be_changed_even_with_current_version(self):
        for state in (TaskState.TASK_STATE_COMPLETED, TaskState.TASK_STATE_FAILED,
                      TaskState.TASK_STATE_REJECTED, TaskState.TASK_STATE_CANCELED):
            with self.subTest(state=state):
                first = await self.claim()
                current = await self.write_state(first.task.id, state)
                updated = task_copy(current.task)
                updated.status.state = TaskState.TASK_STATE_WORKING
                with self.assertRaises(ConcurrentTaskModificationError):
                    await self.store.save(updated, event=None, prev=current.task, prev_version=current.version, context=self.context)
                updated.status.state = TaskState.TASK_STATE_CANCELED
                with self.assertRaises(ConcurrentTaskModificationError):
                    await self.store.save(updated, event=None, prev=current.task, prev_version=current.version, context=self.context)
                self.assertEqual((await self.store.revisions(first.task.id))[-1].status.state, state)

    async def test_context_metadata_and_history_cannot_be_rewritten(self):
        first = await self.claim()
        current = await self.store.get(first.task.id, self.context)
        for change in ("context", "metadata", "history"):
            with self.subTest(change=change):
                updated = task_copy(current.task)
                if change == "context":
                    updated.context_id = str(uuid4())
                elif change == "metadata":
                    updated.metadata.update({"runId": str(uuid4())})
                else:
                    del updated.history[:]
                with self.assertRaises(InvalidParamsError):
                    await self.store.save(updated, event=None, prev=current.task, prev_version=current.version, context=self.context)

    async def test_interrupted_failure_is_allowed_but_direct_restart_is_not(self):
        first = await self.claim()
        current = await self.write_state(first.task.id, TaskState.TASK_STATE_AUTH_REQUIRED)
        for state in (TaskState.TASK_STATE_SUBMITTED, TaskState.TASK_STATE_WORKING):
            updated = task_copy(current.task)
            updated.status.state = state
            with self.assertRaises(InvalidParamsError):
                await self.store.save(updated, event=None, prev=current.task, prev_version=current.version, context=self.context)
        final = await self.write_state(first.task.id, TaskState.TASK_STATE_FAILED)
        self.assertEqual(final.task.status.state, TaskState.TASK_STATE_FAILED)

    async def test_clean_shutdown_records_failed_active_task_without_replay(self):
        request = self.request()
        initial = await self.claim(request)
        await self.write_state(initial.task.id, TaskState.TASK_STATE_WORKING)
        await self.store.aclose()
        reopened = SQLiteAgentTaskStore(self.path, AgentRole.PLANNER)
        await reopened.start()
        self.addAsyncCleanup(reopened.aclose)
        current = (await reopened.get(initial.task.id, self.context)).task
        self.assertEqual(current.status.state, TaskState.TASK_STATE_FAILED)
        self.assertEqual(current.status.message.role, Role.ROLE_AGENT)
        self.assertEqual(MessageToDict(current.status.message.parts[0].data)["code"], "AGENT_EXECUTION_INTERRUPTED")
        duplicate = await reopened.claim_message(request, self.context)
        self.assertTrue(duplicate.duplicate)
        self.assertEqual(duplicate.task.status.state, TaskState.TASK_STATE_FAILED)

    async def test_shutdown_preserves_interrupted_tasks_and_context_for_restart(self):
        first = await self.claim()
        await self.write_state(first.task.id, TaskState.TASK_STATE_INPUT_REQUIRED)
        await self.store.aclose()
        reopened = SQLiteAgentTaskStore(self.path, AgentRole.PLANNER)
        await reopened.start()
        self.addAsyncCleanup(reopened.aclose)
        current = (await reopened.get(first.task.id, self.context)).task
        self.assertEqual(current.status.state, TaskState.TASK_STATE_INPUT_REQUIRED)
        followup = self.request(
            task_id=first.task.id, context_id=first.task.context_id,
            metadata=self.metadata.model_copy(update={"attempt": 1}),
        )
        resumed = await reopened.claim_message(followup, self.context)
        self.assertEqual(resumed.task.id, first.task.id)
        self.assertEqual(MessageToDict(resumed.task.metadata)["attempt"], 1)

    async def test_continuation_only_advances_attempt_by_one_without_changing_identity(self):
        metadata = self.metadata.model_copy(update={"attempt": 2})
        first = await self.claim(self.request(metadata=metadata))
        await self.write_state(first.task.id, TaskState.TASK_STATE_INPUT_REQUIRED)
        before = await self.store.get(first.task.id, self.context)
        for attempt in (0, 1, 2, 4):
            with self.subTest(attempt=attempt), self.assertRaises(InvalidParamsError):
                await self.claim(self.request(
                    metadata=metadata.model_copy(update={"attempt": attempt}),
                    task_id=first.task.id, context_id=first.task.context_id,
                ))
        for field, value in (
            ("run_id", uuid4()), ("workflow_step_id", uuid4()), ("scenario_id", uuid4()),
            ("requirement_ids", (uuid4(),)), ("code_version", 1),
            ("project_artifact_ids", (uuid4(),)),
        ):
            with self.subTest(field=field), self.assertRaises(InvalidParamsError):
                await self.claim(self.request(
                    metadata=metadata.model_copy(update={"attempt": 3, field: value}),
                    task_id=first.task.id, context_id=first.task.context_id,
                ))
        with patch("agents.api.sqlite_task_store.resolve_user_scope", return_value="another-owner"):
            with self.assertRaises(TaskNotFoundError):
                await self.claim(self.request(
                    metadata=metadata.model_copy(update={"attempt": 3}),
                    task_id=first.task.id, context_id=first.task.context_id,
                ))
        after = await self.store.get(first.task.id, self.context)
        self.assertEqual(after.version, before.version)
        self.assertEqual(after.task, before.task)
        with sqlite3.connect(self.path) as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM agent_message_receipts").fetchone()[0], 1)

    async def test_two_continuations_persist_counter_binding_history_and_receipts_atomically(self):
        original_request = self.request()
        first = await self.claim(original_request)
        accepted = [original_request]
        for attempt, state in ((1, TaskState.TASK_STATE_INPUT_REQUIRED), (2, TaskState.TASK_STATE_AUTH_REQUIRED)):
            await self.write_state(first.task.id, state)
            request = self.request(
                {"answer": f"Continuation {attempt}"},
                task_id=first.task.id, context_id=first.task.context_id,
                metadata=self.metadata.model_copy(update={"attempt": attempt}),
            )
            claim = await self.claim(request)
            accepted.append(request)
            self.assertEqual(claim.task.id, first.task.id)
            self.assertEqual(claim.task.context_id, first.task.context_id)
            self.assertEqual(MessageToDict(claim.task.metadata)["attempt"], attempt)
            with sqlite3.connect(self.path) as connection:
                binding, payload = connection.execute(
                    "SELECT metadata_json, payload_json FROM agent_tasks WHERE task_id=?", (first.task.id,),
                ).fetchone()
                self.assertEqual(json.loads(binding)["attempt"], attempt)
                self.assertEqual(json.loads(payload)["metadata"]["attempt"], attempt)
                self.assertEqual(connection.execute("SELECT COUNT(*) FROM agent_message_receipts").fetchone()[0], attempt + 1)
                self.assertEqual(connection.execute("SELECT COUNT(*) FROM agent_task_history").fetchone()[0], attempt + 1)
            for request in accepted:
                replay = await self.claim(request)
                self.assertTrue(replay.duplicate)
                self.assertEqual(MessageToDict(replay.task.metadata)["attempt"], attempt)
        revisions = await self.store.revisions(first.task.id)
        self.assertEqual([MessageToDict(task.metadata)["attempt"] for task in revisions], [0, 0, 1, 1, 2])
        await self.write_state(first.task.id, TaskState.TASK_STATE_COMPLETED)
        await self.store.aclose()
        reopened = SQLiteAgentTaskStore(self.path, AgentRole.PLANNER)
        await reopened.start()
        self.addAsyncCleanup(reopened.aclose)
        for request in accepted:
            replay = await reopened.claim_message(request, self.context)
            self.assertTrue(replay.duplicate)
            self.assertEqual(replay.task.status.state, TaskState.TASK_STATE_COMPLETED)
            self.assertEqual(MessageToDict(replay.task.metadata)["attempt"], 2)
            self.assertEqual(len(replay.task.history), 3)

    async def test_sdk_save_cannot_advance_or_revert_the_approved_continuation_attempt(self):
        first = await self.claim()
        old = await self.write_state(first.task.id, TaskState.TASK_STATE_INPUT_REQUIRED)
        resumed = await self.claim(self.request(
            task_id=first.task.id, context_id=first.task.context_id,
            metadata=self.metadata.model_copy(update={"attempt": 1}),
        ))
        stale = task_copy(old.task)
        stale.status.state = TaskState.TASK_STATE_WORKING
        with self.assertRaises(ConcurrentTaskModificationError):
            await self.store.save(stale, event=None, prev=old.task, prev_version=old.version, context=self.context)
        current = await self.store.get(first.task.id, self.context)
        for attempt in (0, 2):
            updated = task_copy(current.task)
            updated.metadata.update({"attempt": attempt})
            with self.subTest(attempt=attempt), self.assertRaises(InvalidParamsError):
                await self.store.save(updated, event=None, prev=current.task, prev_version=current.version, context=self.context)
        valid = task_copy(resumed.task)
        valid.status.state = TaskState.TASK_STATE_WORKING
        await self.store.save(valid, event=None, prev=current.task, prev_version=current.version, context=self.context)
        self.assertEqual(MessageToDict((await self.store.get(first.task.id, self.context)).task.metadata)["attempt"], 1)

    async def test_stale_cancel_preserves_latest_approved_attempt_history_and_artifacts(self):
        first = await self.claim()
        old = await self.write_state(first.task.id, TaskState.TASK_STATE_INPUT_REQUIRED)
        resumed = await self.claim(self.request(
            task_id=first.task.id, context_id=first.task.context_id,
            metadata=self.metadata.model_copy(update={"attempt": 1}),
        ))
        artifact = Artifact(artifact_id=str(uuid4()), parts=[new_data_part({"result": "New evidence"})])
        latest = await self.write_state(resumed.task.id, TaskState.TASK_STATE_WORKING, artifact=artifact)
        canceled = task_copy(old.task)
        canceled.status.state = TaskState.TASK_STATE_CANCELED
        await self.store.save(canceled, event=None, prev=old.task, prev_version=old.version, context=self.context)
        stored = await self.store.get(first.task.id, self.context)
        self.assertEqual(stored.task.status.state, TaskState.TASK_STATE_CANCELED)
        self.assertEqual(MessageToDict(stored.task.metadata)["attempt"], 1)
        self.assertEqual(stored.task.history, latest.task.history)
        self.assertEqual(stored.task.artifacts, latest.task.artifacts)
        with sqlite3.connect(self.path) as connection:
            binding = connection.execute(
                "SELECT metadata_json FROM agent_tasks WHERE task_id=?", (first.task.id,),
            ).fetchone()[0]
            self.assertEqual(json.loads(binding)["attempt"], 1)

    async def test_cancel_cannot_use_unapproved_attempt_or_another_identity(self):
        first = await self.claim()
        await self.write_state(first.task.id, TaskState.TASK_STATE_INPUT_REQUIRED)
        await self.claim(self.request(
            task_id=first.task.id, context_id=first.task.context_id,
            metadata=self.metadata.model_copy(update={"attempt": 1}),
        ))
        current = await self.store.get(first.task.id, self.context)
        for field, value in (("attempt", 2), ("runId", str(uuid4()))):
            invalid = task_copy(current.task)
            invalid.status.state = TaskState.TASK_STATE_CANCELED
            invalid.metadata.update({field: value})
            with self.subTest(field=field), self.assertRaises(InvalidParamsError):
                await self.store.save(
                    invalid, event=None, prev=current.task, prev_version=current.version, context=self.context,
                )
        invalid = task_copy(first.task)
        invalid.context_id = "another-context"
        invalid.status.state = TaskState.TASK_STATE_CANCELED
        with self.assertRaises(InvalidParamsError):
            await self.store.save(
                invalid, event=None, prev=current.task, prev_version=current.version, context=self.context,
            )
        stored = await self.store.get(first.task.id, self.context)
        self.assertEqual(stored.version, current.version)
        self.assertEqual(stored.task, current.task)

    async def test_failed_receipt_insert_rolls_back_continuation_binding_history_and_revision(self):
        first = await self.claim()
        await self.write_state(first.task.id, TaskState.TASK_STATE_INPUT_REQUIRED)
        before = await self.store.get(first.task.id, self.context)
        revisions = await self.store.revisions(first.task.id)
        with sqlite3.connect(self.path) as connection:
            connection.execute(
                "CREATE TRIGGER reject_new_receipts BEFORE INSERT ON agent_message_receipts "
                "BEGIN SELECT RAISE(ABORT, 'fixture receipt failure'); END"
            )
        with self.assertRaises(sqlite3.IntegrityError):
            await self.claim(self.request(
                task_id=first.task.id, context_id=first.task.context_id,
                metadata=self.metadata.model_copy(update={"attempt": 1}),
            ))
        after = await self.store.get(first.task.id, self.context)
        self.assertEqual(after.version, before.version)
        self.assertEqual(after.task, before.task)
        self.assertEqual(await self.store.revisions(first.task.id), revisions)
        with sqlite3.connect(self.path) as connection:
            binding = connection.execute(
                "SELECT metadata_json FROM agent_tasks WHERE task_id=?", (first.task.id,),
            ).fetchone()[0]
            self.assertEqual(json.loads(binding)["attempt"], 0)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM agent_message_receipts").fetchone()[0], 1)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM agent_task_history").fetchone()[0], 1)

    async def test_two_distinct_messages_cannot_reserve_the_same_continuation_attempt(self):
        first = await self.claim()
        await self.write_state(first.task.id, TaskState.TASK_STATE_AUTH_REQUIRED)
        requests = [self.request(
            task_id=first.task.id, context_id=first.task.context_id,
            metadata=self.metadata.model_copy(update={"attempt": 1}),
        ) for _ in range(2)]
        results = await asyncio.gather(*(self.claim(request) for request in requests), return_exceptions=True)
        self.assertEqual(sum(isinstance(result, UnsupportedOperationError) for result in results), 1)
        self.assertEqual(sum(not isinstance(result, Exception) for result in results), 1)
        with sqlite3.connect(self.path) as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM agent_message_receipts").fetchone()[0], 2)
        self.assertEqual(MessageToDict((await self.store.get(first.task.id, self.context)).task.metadata)["attempt"], 1)

    async def test_database_role_and_foreign_schema_are_guarded(self):
        alien = SQLiteAgentTaskStore(self.path, AgentRole.SECURITY)
        with self.assertRaises(AgentStoreRoleError):
            await alien.start()
        path = Path(self.directory.name) / "foreign.sqlite3"
        with sqlite3.connect(path) as connection:
            connection.execute("CREATE TABLE user_data(value TEXT)")
        foreign = SQLiteAgentTaskStore(path, AgentRole.PLANNER)
        with self.assertRaises(AgentStoreRoleError):
            await foreign.start()
        with sqlite3.connect(path) as connection:
            tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertEqual(tables, {"user_data"})

    async def test_alive_lease_owner_is_not_stolen_and_changed_token_blocks_reads(self):
        other = SQLiteAgentTaskStore(self.path, AgentRole.PLANNER)
        with self.assertRaises(AgentStoreLeaseError):
            await other.start()
        token = self.store._lease_token
        with sqlite3.connect(self.path) as connection:
            connection.execute("UPDATE agent_process_lease SET token='changed'")
        try:
            with self.assertRaises(AgentStoreLeaseError):
                await self.store.get("missing", self.context)
        finally:
            with sqlite3.connect(self.path) as connection:
                connection.execute("UPDATE agent_process_lease SET token=?", (token,))

    async def test_dead_owner_recovery_records_failed_without_executing_request(self):
        request = self.request()
        first = await self.claim(request)
        with sqlite3.connect(self.path) as connection:
            connection.execute("UPDATE agent_process_lease SET owner_pid=99999999")
        self.store._lease_token = None  # Simulate a dead Python service, not a graceful close.
        reopened = SQLiteAgentTaskStore(self.path, AgentRole.PLANNER)
        with patch("agents.api.sqlite_task_store.os.kill", side_effect=ProcessLookupError):
            await reopened.start()
        self.addAsyncCleanup(reopened.aclose)
        duplicate = await reopened.claim_message(request, self.context)
        self.assertTrue(duplicate.duplicate)
        self.assertEqual(duplicate.task.status.state, TaskState.TASK_STATE_FAILED)

    async def test_unknown_owner_lease_is_not_stolen(self):
        token = self.store._lease_token
        with sqlite3.connect(self.path) as connection:
            connection.execute("UPDATE agent_process_lease SET owner_pid=NULL")
        try:
            with self.assertRaises(AgentStoreLeaseError):
                await SQLiteAgentTaskStore(self.path, AgentRole.PLANNER).start()
        finally:
            with sqlite3.connect(self.path) as connection:
                connection.execute("UPDATE agent_process_lease SET token=?, owner_pid=?", (token, os.getpid()))

    async def test_append_only_audit_rows_have_sql_guards(self):
        await self.claim()
        with sqlite3.connect(self.path) as connection:
            for table in ("agent_store_identity", "agent_contexts", "agent_task_revisions", "agent_task_history", "agent_message_receipts"):
                for operation in (f"UPDATE {table} SET rowid=rowid", f"DELETE FROM {table}"):
                    with self.subTest(table=table, operation=operation):
                        with self.assertRaises(sqlite3.IntegrityError):
                            connection.execute(operation)
                        connection.rollback()

    async def test_db_payload_and_receipt_do_not_contain_secrets(self):
        first = await self.claim(self.request({"request": "Plan signup", "apiKey": "DUMMY_RECEIPT_KEY"}))
        artifact = Artifact(artifact_id="opaque-artifact", parts=[new_data_part(
            {"password": "DUMMY_OUTPUT_PASSWORD"}, media_type="application/json",
        )])
        await self.write_state(first.task.id, TaskState.TASK_STATE_COMPLETED, artifact=artifact)
        with sqlite3.connect(self.path) as connection:
            text = " ".join(str(row) for row in connection.iterdump())
        self.assertNotIn("DUMMY_RECEIPT_KEY", text)
        self.assertNotIn("DUMMY_OUTPUT_PASSWORD", text)

    async def test_list_and_delete_are_safely_unsupported(self):
        with self.assertRaises(UnsupportedOperationError):
            await self.store.list(ListTasksRequest(), self.context)
        with self.assertRaises(UnsupportedOperationError):
            await self.store.delete("missing", self.context)

    async def test_concurrent_initial_roles_cannot_share_a_new_database(self):
        path = Path(self.directory.name) / "race.sqlite3"
        stores = [SQLiteAgentTaskStore(path, role) for role in (AgentRole.PLANNER, AgentRole.SECURITY)]

        def start_store(store):
            try:
                asyncio.run(store.start())
                return True
            except AgentStoreRoleError:
                return False

        results = await asyncio.gather(*(asyncio.to_thread(start_store, store) for store in stores))
        self.assertEqual(results.count(True), 1)
        winner = stores[results.index(True)]
        self.addAsyncCleanup(winner.aclose)
        with sqlite3.connect(path) as connection:
            role, version = connection.execute("SELECT role,schema_version FROM agent_store_identity").fetchone()
        self.assertEqual(role, winner.role.value)
        self.assertEqual(version, 1)


if __name__ == "__main__":
    unittest.main()
