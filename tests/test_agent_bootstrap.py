"""Implemented role opt-in must not turn default CLI into product execution."""

import asyncio
from contextlib import asynccontextmanager
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from uuid import uuid4

from a2a.server.agent_execution import AgentExecutor
from a2a.utils.constants import A2A_JSON_MEDIA_TYPE, VERSION_HEADER
from google.protobuf.json_format import MessageToDict
import httpx

from agents.__main__ import main as cli_main
from agents.api.card import build_agent_card
from agents.core.config import AgentSettings
from agents.main import create_app
from agents.runtime.developer import DeveloperAgentExecutor
from agents.runtime.planner import PlannerAgentExecutor
from agents.runtime.qa import QAAgentExecutor
from orchestrator.a2a import A2AWorkflowMetadata, build_send_message_request
from orchestrator.domain.states import AgentRole


class InertProvider:
    name = "fixture-provider"

    def validate_configuration(self, *args):
        raise AssertionError("configuration must not run at construction/startup")

    async def complete(self, *args):
        raise AssertionError("model must not run at construction/startup")


def uncalled_factory(*args):
    raise AssertionError("Host factory must not run at construction/startup")


class ArbitraryExecutor(AgentExecutor):
    async def execute(self, *args):
        raise AssertionError("health/card must not execute tasks")

    async def cancel(self, *args):
        raise AssertionError("health/card must not cancel tasks")


class AgentBootstrapTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        environment = patch.dict(os.environ, {}, clear=True)
        environment.start()
        self.addCleanup(environment.stop)
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def settings(self, role=AgentRole.QA, **changes):
        values = {"role": role, "database_path": self.root / f"{role.value.lower()}.sqlite3", "_env_file": None}
        values.update(changes)
        return AgentSettings(**values)

    @staticmethod
    def executor(kind=QAAgentExecutor):
        values = {"provider": InertProvider(), "context_factory": uncalled_factory}
        if kind is not PlannerAgentExecutor:
            values["services_factory"] = uncalled_factory
        return kind(**values)

    @asynccontextmanager
    async def client_for(self, settings, *, executor=None, lifespan=False):
        app = create_app(settings, executor=executor)

        @asynccontextmanager
        async def inert_lifespan():
            yield

        context = app.router.lifespan_context(app) if lifespan else inert_lifespan()
        async with context:
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                         base_url=settings.agent_base_url) as client:
                yield app, client

    async def test_all_default_roles_advertise_bootstrap_without_skills(self):
        for role in AgentRole:
            with self.subTest(role=role):
                async with self.client_for(self.settings(role)) as (_, client):
                    health = await client.get("/health")
                    card = await client.get("/.well-known/agent-card.json")
                self.assertEqual(health.json(), {"status": "ok", "role": role.value, "executionReady": False})
                self.assertEqual(card.json().get("skills", []), [])
                self.assertIn("Transport-only bootstrap", card.json()["description"])
                self.assertFalse(self.settings(role).task_database_path.exists())

    async def test_model_settings_alone_do_not_enable_qa(self):
        settings = self.settings(llm_provider="fixture-provider", llm_model_id="fixture-model")
        async with self.client_for(settings) as (_, client):
            self.assertFalse((await client.get("/health")).json()["executionReady"])
            self.assertEqual((await client.get("/.well-known/agent-card.json")).json().get("skills", []), [])

    async def test_explicit_qa_executor_has_only_measured_initial_qa_skill(self):
        settings = self.settings()
        async with self.client_for(settings, executor=self.executor()) as (_, client):
            self.assertTrue((await client.get("/health")).json()["executionReady"])
            card = (await client.get("/.well-known/agent-card.json")).json()
        self.assertEqual(len(card["skills"]), 1)
        self.assertEqual(card["skills"][0]["id"], "measured-initial-qa")
        self.assertEqual(card["skills"][0]["tags"], ["qa", "unit-tests", "browser-tests", "snapshot"])
        self.assertIn("read-only Source", card["description"])
        self.assertIn("no Source edits or product verdict", card["description"])
        self.assertFalse(settings.task_database_path.exists())

    async def test_explicit_existing_roles_keep_original_skills(self):
        for role, kind, skill in (
            (AgentRole.PLANNER, PlannerAgentExecutor, "protected-task-planning"),
            (AgentRole.DEVELOPER, DeveloperAgentExecutor, "measured-initial-implementation"),
        ):
            with self.subTest(role=role):
                async with self.client_for(self.settings(role), executor=self.executor(kind)) as (_, client):
                    self.assertTrue((await client.get("/health")).json()["executionReady"])
                    card = (await client.get("/.well-known/agent-card.json")).json()
                    self.assertEqual(card["skills"][0]["id"], skill)
                    self.assertEqual(len(card["skills"]), 1)

    async def test_each_implemented_executor_rejects_every_other_server_role(self):
        for expected, kind in ((AgentRole.PLANNER, PlannerAgentExecutor),
                               (AgentRole.DEVELOPER, DeveloperAgentExecutor), (AgentRole.QA, QAAgentExecutor)):
            for role in AgentRole:
                if role is expected:
                    continue
                with self.subTest(expected=expected, role=role), \
                        self.assertRaisesRegex(ValueError, "^AGENT_EXECUTOR_ROLE_MISMATCH$"):
                    create_app(self.settings(role), executor=self.executor(kind))
                self.assertFalse(self.settings(role).task_database_path.exists())

    async def test_arbitrary_injected_executor_is_not_readiness_proof(self):
        for role in AgentRole:
            with self.subTest(role=role):
                async with self.client_for(self.settings(role), executor=ArbitraryExecutor()) as (_, client):
                    self.assertFalse((await client.get("/health")).json()["executionReady"])
                    self.assertEqual((await client.get("/.well-known/agent-card.json")).json().get("skills", []), [])

    async def test_default_qa_rejects_task_without_artifact_or_runtime_work(self):
        metadata = A2AWorkflowMetadata(runId=uuid4(), workflowStepId=uuid4(), scenarioId=uuid4(),
                                       codeVersion=1, attempt=0, requirementIds=(uuid4(),),
                                       projectArtifactIds=(uuid4(),))
        wire = MessageToDict(build_send_message_request({"request": "검증 요청", "snapshot": {}}, metadata))
        headers = {VERSION_HEADER: "1.0", "Content-Type": A2A_JSON_MEDIA_TYPE}
        async with self.client_for(self.settings(), lifespan=True) as (_, client):
            response = await client.post("/message:send", json=wire, headers=headers)
            self.assertEqual(response.status_code, 200)
            task = response.json()["task"]
            for _ in range(50):
                task = (await client.get(f"/tasks/{task['id']}", headers=headers)).json()
                if task["status"]["state"] == "TASK_STATE_REJECTED":
                    break
                await asyncio.sleep(0)
            self.assertEqual(task["status"]["state"], "TASK_STATE_REJECTED")
            self.assertEqual(task["status"]["message"]["parts"][0]["data"]["code"],
                             "AGENT_RUNTIME_NOT_CONFIGURED")
            self.assertFalse(task.get("artifacts"))
            self.assertEqual(task["metadata"], metadata.to_a2a_json())

    def test_security_cannot_advertise_an_unimplemented_executor(self):
        with self.assertRaisesRegex(ValueError, "^AGENT_EXECUTOR_ROLE_MISMATCH$"):
            build_agent_card(self.settings(AgentRole.SECURITY), execution_ready=True)

    def test_card_ready_flag_requires_native_boolean(self):
        for value in (None, 1, "true", [], {}):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, "^AGENT_EXECUTOR_ROLE_MISMATCH$"):
                build_agent_card(self.settings(), execution_ready=value)

    def test_explicit_qa_card_preserves_official_protocol_and_auth(self):
        settings = self.settings(bearer_token="fixture-bearer")
        card = build_agent_card(settings, execution_ready=True)
        self.assertEqual(card.supported_interfaces[0].url, "http://127.0.0.1:8103")
        self.assertEqual(card.supported_interfaces[0].protocol_version, "1.0")
        self.assertEqual(card.supported_interfaces[0].protocol_binding, "HTTP+JSON")
        self.assertFalse(card.capabilities.streaming)
        self.assertFalse(card.capabilities.push_notifications)
        self.assertEqual(card.security_schemes["bearerAuth"].http_auth_security_scheme.scheme, "bearer")
        self.assertEqual(len(card.security_requirements), 1)
        self.assertNotIn("fixture-bearer", str(card))

    def test_cli_still_loads_default_app_factory_not_a_role_executor(self):
        settings = self.settings()
        with patch("agents.__main__.AgentSettings", return_value=settings), \
                patch("agents.__main__.uvicorn.run") as run:
            cli_main()
        run.assert_called_once_with("agents.main:create_app", factory=True,
                                     host=settings.host, port=8103, log_level=settings.log_level.lower())


if __name__ == "__main__":
    unittest.main()
