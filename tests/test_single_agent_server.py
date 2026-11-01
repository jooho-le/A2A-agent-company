"""Single A2A identity/storage contracts; fixtures do not execute products."""

import asyncio
from contextlib import asynccontextmanager
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from uuid import uuid4

from a2a.server.context import ServerCallContext
from a2a.types import AgentCard
from a2a.utils.constants import A2A_JSON_MEDIA_TYPE, VERSION_HEADER
from a2a.utils.errors import InvalidParamsError, UnsupportedOperationError
from google.protobuf.json_format import MessageToDict, ParseDict
import httpx

from agents.api.sqlite_task_store import SQLiteAgentTaskStore, AgentStoreRoleError, AgentStoreLeaseError, AgentTaskStoreError
from agents.core.config import AgentSettings
from agents.core.contracts import AGENT_PORTS
from agents.core.identity import SingleAgentIdentity
from agents.single.__main__ import main
from agents.single.app import create_app
from orchestrator.a2a import A2AWorkflowMetadata, build_send_message_request, validate_agent_card
from orchestrator.domain.states import AgentRole
from test_agent_server import CompletedExecutor


class SingleAgentServerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        env = patch.dict(os.environ, {}, clear=True)
        env.start()
        self.addCleanup(env.stop)
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name) / "single.sqlite3"
        self.settings = AgentSettings(role="SINGLE_AGENT", database_path=self.path, _env_file=None)
        self.metadata = A2AWorkflowMetadata(run_id=uuid4(), workflow_step_id=uuid4(), scenario_id=uuid4(), attempt=0)

    @asynccontextmanager
    async def client(self, *, executor=None, settings=None):
        settings = settings or self.settings
        app = create_app(settings, executor=executor)
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=settings.agent_base_url) as client:
                yield app, client

    def wire(self, *, metadata=None, context_id=None):
        return MessageToDict(build_send_message_request(
            {"request": "Single 업무", "password": "DUMMY_SINGLE_SECRET"}, metadata or self.metadata,
            context_id=context_id,
        ))

    @staticmethod
    def headers(**extra):
        return {VERSION_HEADER: "1.0", "Content-Type": A2A_JSON_MEDIA_TYPE, **extra}

    async def poll(self, client, task_id, expected):
        for _ in range(50):
            response = await client.get(f"/tasks/{task_id}", headers=self.headers())
            self.assertEqual(response.status_code, 200)
            task = response.json()
            if task["status"]["state"] == expected:
                return task
            await asyncio.sleep(0)
        self.fail("Task did not reach expected state")

    def test_construction_and_cli_have_a_distinct_identity_without_starting_io(self):
        create_app(self.settings)
        self.assertFalse(self.path.exists())
        self.assertIs(self.settings.role, SingleAgentIdentity.SINGLE_AGENT)
        self.assertEqual(self.settings.listen_port, 8105)
        self.assertEqual(AgentSettings(role="SINGLE_AGENT", _env_file=None).task_database_path,
                         Path(".data/agents/single_agent.sqlite3"))
        self.assertEqual(len(AgentRole), 4)
        self.assertEqual([AGENT_PORTS[role] for role in AgentRole], [8101, 8102, 8103, 8104])
        with self.assertRaises(ValueError):
            create_app(AgentSettings(role="PLANNER", _env_file=None))
        with patch("agents.single.__main__.uvicorn.run") as run:
            main()
        self.assertEqual(run.call_args.args, ("agents.single.app:create_app",))
        self.assertEqual(run.call_args.kwargs["port"], 8105)

    async def test_card_health_and_bootstrap_are_honest(self):
        async with self.client() as (_, client):
            self.assertEqual((await client.get("/health")).json(),
                             {"status": "ok", "role": "SINGLE_AGENT", "executionReady": False})
            card = ParseDict((await client.get("/.well-known/agent-card.json")).json(), AgentCard())
            self.assertEqual(card.name, "SINGLE_AGENT Agent")
            self.assertEqual(validate_agent_card(card), "http://127.0.0.1:8105")
            self.assertEqual(card.supported_interfaces[0].protocol_version, "1.0")
            self.assertFalse(card.skills)
            response = await client.post("/message:send", json=self.wire(), headers=self.headers())
            self.assertEqual(response.status_code, 200)
            submitted = response.json()["task"]
            task = await self.poll(client, submitted["id"], "TASK_STATE_REJECTED")
            self.assertNotEqual(task["id"], str(self.metadata.workflow_step_id))
            self.assertFalse(task.get("artifacts"))
            self.assertNotIn("DUMMY_SINGLE_SECRET", str(task))

    async def test_auth_and_version_are_enforced_before_admission(self):
        settings = AgentSettings(role="SINGLE_AGENT", database_path=self.path, bearer_token="DUMMY_SINGLE_BEARER", _env_file=None)
        async with self.client(settings=settings) as (_, client):
            self.assertEqual((await client.get("/.well-known/agent-card.json")).status_code, 401)
            wire = self.wire()
            self.assertEqual((await client.post("/message:send", json=wire, headers=self.headers())).status_code, 401)
            authorized = self.headers(Authorization="Bearer DUMMY_SINGLE_BEARER")
            authorized[VERSION_HEADER] = "0.3"
            self.assertGreaterEqual((await client.post("/message:send", json=wire, headers=authorized)).status_code, 400)
            authorized[VERSION_HEADER] = "1.0"
            self.assertEqual((await client.post("/message:send", json=wire, headers=authorized)).status_code, 200)

    async def test_context_spans_tasks_and_restart_without_losing_admission_history(self):
        wire = self.wire()
        async with self.client(executor=CompletedExecutor()) as (_, client):
            first = (await client.post("/message:send", json=wire, headers=self.headers())).json()["task"]
            await self.poll(client, first["id"], "TASK_STATE_COMPLETED")
        async with self.client(executor=CompletedExecutor()) as (app, client):
            duplicate = (await client.post("/message:send", json=wire, headers=self.headers())).json()["task"]
            self.assertEqual(duplicate["id"], first["id"])
            next_metadata = A2AWorkflowMetadata(run_id=self.metadata.run_id, scenario_id=self.metadata.scenario_id,
                                               workflow_step_id=uuid4(), attempt=0, code_version=1)
            response = await client.post("/message:send", json=self.wire(metadata=next_metadata, context_id=first["contextId"]), headers=self.headers())
            self.assertEqual(response.status_code, 200)
            second = response.json()["task"]
            await self.poll(client, second["id"], "TASK_STATE_COMPLETED")
            self.assertEqual(first["contextId"], second["contextId"])
            self.assertNotEqual(first["id"], second["id"])
            tasks = await app.state.task_store.context_tasks(first["contextId"], next_metadata, ServerCallContext())
            self.assertEqual([task.id for task in tasks], [first["id"], second["id"]])
            wrong = A2AWorkflowMetadata(run_id=uuid4(), scenario_id=self.metadata.scenario_id, workflow_step_id=uuid4(), attempt=0)
            response = await client.post("/message:send", json=self.wire(metadata=wrong, context_id=first["contextId"]), headers=self.headers())
            self.assertGreaterEqual(response.status_code, 400)
            with self.assertRaises(InvalidParamsError):
                await app.state.task_store.context_tasks(first["contextId"], wrong, ServerCallContext())

    async def test_store_identity_lease_and_context_limits_cannot_be_bypassed(self):
        store = SQLiteAgentTaskStore(self.path, SingleAgentIdentity.SINGLE_AGENT)
        await store.start()
        try:
            claim = await store.claim_message(build_send_message_request({"request": "one"}, self.metadata), ServerCallContext())
            with self.assertRaises(AgentStoreRoleError):
                await SQLiteAgentTaskStore(self.path, AgentRole.PLANNER).start()
            with self.assertRaises(AgentStoreLeaseError):
                await SQLiteAgentTaskStore(self.path, SingleAgentIdentity.SINGLE_AGENT).start()
            for maximum in (True, 0, 1_048_577):
                with self.assertRaises(AgentTaskStoreError):
                    await store.context_tasks(claim.task.context_id, self.metadata, ServerCallContext(), max_bytes=maximum)
            with self.assertRaises(AgentTaskStoreError):
                await store.context_tasks(claim.task.context_id, self.metadata, ServerCallContext(), max_bytes=1)
        finally:
            await store.aclose()

    async def test_one_run_cannot_fork_context_or_start_parallel_stage_tasks(self):
        store = SQLiteAgentTaskStore(self.path, SingleAgentIdentity.SINGLE_AGENT)
        await store.start()
        try:
            first = await store.claim_message(build_send_message_request({"request": "one"}, self.metadata), ServerCallContext())
            next_metadata = A2AWorkflowMetadata(run_id=self.metadata.run_id, scenario_id=self.metadata.scenario_id,
                                               workflow_step_id=uuid4(), attempt=0)
            with self.assertRaises(InvalidParamsError):
                await store.claim_message(build_send_message_request({"request": "fork"}, next_metadata), ServerCallContext())
            with self.assertRaises(UnsupportedOperationError):
                await store.claim_message(build_send_message_request({"request": "parallel"}, next_metadata,
                                          context_id=first.task.context_id), ServerCallContext())
        finally:
            await store.aclose()


if __name__ == "__main__":
    unittest.main()
