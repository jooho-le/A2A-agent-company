"""Host HTTP contracts with real SQLite, isolated ASGI apps and inert dispatch.

No paid API, listener, Docker or generated application is executed.
"""

import asyncio
from hashlib import sha256
import json
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch
from uuid import uuid1, uuid4

import httpx
from pydantic import SecretStr

from orchestrator.core.config import Settings
from orchestrator.domain import SCN_001_ID
from orchestrator.domain.run_configuration import RunConfiguration
from orchestrator.infrastructure import SQLiteWorkflowRepository
from orchestrator.main import create_app

from agents.platform.host import HostFactoryError
from test_owned_host import _HostFixture


TOKEN = "host-service-contract-fixture-token-never-a-real-key"


class _Dispatcher:
    def __init__(self):
        self.run_ids = []

    async def dispatch_planner(self, run_id):
        self.run_ids.append(run_id)


class HostServiceAPITests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.path = self.base / "host.sqlite3"
        self.repository = SQLiteWorkflowRepository(self.path)
        self.dispatcher = _Dispatcher()
        self.configuration = RunConfiguration.model_validate({
            "startingCommitHash": "a" * 40, "startingSnapshotSha256": "b" * 64,
            "model": {"provider": "openai", "modelId": "fixture-model", "temperature": 0},
            "limits": {"runtimeBudgetMs": 60000}, "scannerProfileRef": "artifact://scanner/approved",
            "environment": {"containerImageDigest": "sha256:" + "c" * 64,
                "dependencyLockHash": "sha256:" + "d" * 64, "hardwareProfile": "fixture-local"},
        })
        self.app = self.make_app(self.repository)
        self.body = {"scenarioId": str(SCN_001_ID), "requestText": "회원가입 기능을 만들어 주세요.",
                     "configuration": self.configuration.model_dump(mode="json", by_alias=True)}

    def make_app(self, repository):
        def validate(configuration):
            if configuration != self.configuration:
                raise ValueError("Private operator configuration mismatch")
        app = create_app(repository, dispatcher=self.dispatcher,
            settings=Settings(_env_file=None, log_level="CRITICAL", database_path=str(self.path),
                              workspace_root=str(self.base / "workspaces")), submission_validator=validate)
        app.state.host_api_auth_required = True
        app.state.host_api_token = SecretStr(TOKEN)
        app.state.require_idempotency_key = True
        app.state.approved_configuration_provider = lambda: self.configuration
        return app

    async def request(self, method, path, *, app=None, headers=None, **kwargs):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app or self.app,
            raise_app_exceptions=False), base_url="http://host") as client:
            return await client.request(method, path,
                headers={"Authorization": "Bearer " + TOKEN} if headers is None else headers, **kwargs)

    async def submit(self, key, *, body=None, app=None, **kwargs):
        return await self.request("POST", "/api/v1/runs", app=app,
            headers={"Authorization": "Bearer " + TOKEN, "Idempotency-Key": str(key)},
            json=self.body if body is None else body, **kwargs)

    def count(self, table):
        with self.repository._connection() as connection:
            return connection.execute("SELECT count(*) FROM " + table).fetchone()[0]

    async def test_auth_precedes_validation_and_covers_all_host_api_routes(self):
        paths = ["/api/v1/scenarios", "/api/v1/host/approved-configuration", "/api/v1/runs",
            f"/api/v1/runs/{uuid4()}/events", f"/api/v1/runs/{uuid4()}/artifacts",
            f"/api/v1/runs/{uuid4()}/configuration", f"/api/v1/submissions/{uuid4()}"]
        for path in paths:
            response = await self.request("GET", path, headers={})
            self.assertEqual(response.status_code, 401, path)
            self.assertEqual(response.headers["www-authenticate"], "Bearer")
        response = await self.request("POST", "/api/v1/runs", headers={}, content="private-invalid-body")
        self.assertEqual(response.status_code, 401)
        self.assertNotIn("private-invalid-body", response.text)
        self.assertEqual(self.count("workflow_runs"), 0)
        self.assertEqual((await self.request("GET", "/health", headers={})).status_code, 200)
        self.assertEqual((await self.request("GET", "/api/v1/scenarios")).status_code, 200)

    async def test_jwt_wrong_and_duplicate_authorization_are_rejected(self):
        for headers in ({"Authorization": "Bearer product-jwt"}, {"Authorization": "Basic " + TOKEN},
            [("Authorization", "Bearer " + TOKEN), ("Authorization", "Bearer " + TOKEN)]):
            response = await self.request("GET", "/api/v1/scenarios", headers=headers)
            self.assertEqual(response.status_code, 401)
            self.assertNotIn(TOKEN, response.text)
        self.app.state.host_api_token = None
        self.assertEqual((await self.request("GET", "/api/v1/scenarios")).status_code, 503)

    async def test_approved_template_hash_is_stable_and_contains_only_public_configuration(self):
        path = "/api/v1/host/approved-configuration?scenarioId=" + str(SCN_001_ID)
        first = await self.request("GET", path)
        second = await self.request("GET", path)
        self.assertEqual(first.status_code, 200, first.text)
        self.assertEqual(first.json(), second.json())
        body = first.json()
        self.assertEqual(set(body), {"scenarioId", "configurationId", "configuration"})
        canonical = json.dumps(body["configuration"], ensure_ascii=False, sort_keys=True,
                               separators=(",", ":"), allow_nan=False).encode("utf-8")
        self.assertEqual(body["configurationId"], sha256(canonical).hexdigest())
        self.assertEqual(first.headers["cache-control"], "no-store")
        self.assertEqual(body["configuration"], self.body["configuration"])
        self.assertNotIn(TOKEN, first.text)
        self.assertNotIn(str(self.base), first.text)
        self.assertEqual(self.count("workflow_runs"), 0)
        self.assertEqual(self.dispatcher.run_ids, [])

    async def test_approved_endpoint_rejects_unknown_scenario_and_unavailable_or_secret_config(self):
        path = "/api/v1/host/approved-configuration?scenarioId="
        for value in ("invalid", str(uuid4())):
            self.assertEqual((await self.request("GET", path + value)).status_code, 422)
        for provider in (None, Mock(side_effect=ValueError(TOKEN)),
                lambda: self.configuration.model_copy(update={"warmup_state": "api_key=fixture-private"})):
            self.app.state.approved_configuration_provider = provider
            response = await self.request("GET", path + str(SCN_001_ID))
            self.assertEqual(response.status_code, 503, response.text)
            self.assertEqual(response.headers["cache-control"], "no-store")
            self.assertNotIn(TOKEN, response.text)
            self.assertNotIn("fixture-private", response.text)

    async def test_keyed_replay_preserves_response_and_dispatches_only_once(self):
        key = uuid4()
        first = await self.submit(key)
        second = await self.submit(key)
        self.assertEqual(first.status_code, 201, first.text)
        self.assertEqual(second.status_code, 200, second.text)
        self.assertEqual(first.json(), second.json())
        self.assertEqual(first.headers["idempotency-replayed"], "false")
        self.assertEqual(second.headers["idempotency-replayed"], "true")
        self.assertEqual(first.headers["location"], second.headers["location"])
        self.assertEqual(len(self.dispatcher.run_ids), 1)
        for table, count in (("workflow_runs", 1), ("workflow_steps", 1), ("run_configurations", 1),
                             ("workspaces", 1), ("run_submissions", 1), ("trace_events", 3)):
            self.assertEqual(self.count(table), count, table)

    async def test_same_key_different_original_text_conflicts_even_after_redaction(self):
        key = uuid4()
        first_body = {**self.body, "requestText": "password=first-private 개발 요청"}
        self.assertEqual((await self.submit(key, body=first_body)).status_code, 201)
        changed = {**first_body, "requestText": "password=second-private 개발 요청"}
        response = await self.submit(key, body=changed)
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["detail"], "IDEMPOTENCY_KEY_CONFLICT")
        with self.repository._connection() as connection:
            stored = str(connection.execute("SELECT payload_json FROM workflow_runs").fetchall()[0][0])
            stored += str(connection.execute("SELECT payload_json FROM run_submissions").fetchall()[0][0])
        self.assertNotIn("first-private", stored)
        self.assertNotIn("second-private", stored)
        self.assertEqual(self.count("workflow_runs"), 1)

    async def test_json_object_key_order_and_formatting_do_not_create_another_run(self):
        key = uuid4()
        first = await self.submit(key)
        reversed_body = dict(reversed(list(self.body.items())))
        # Explicit content type preserves the same JSON request contract.
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url="http://host") as client:
            second = await client.post("/api/v1/runs", headers={"Authorization": "Bearer " + TOKEN,
                "Idempotency-Key": str(key), "Content-Type": "application/json"},
                content=json.dumps(reversed_body, ensure_ascii=True, indent=4))
        self.assertEqual(second.status_code, 200, second.text)
        self.assertEqual(first.json(), second.json())

    async def test_missing_invalid_duplicate_keys_and_duplicate_json_fields_make_no_run(self):
        for headers in ({"Authorization": "Bearer " + TOKEN},
            {"Authorization": "Bearer " + TOKEN, "Idempotency-Key": "bad-key"},
            {"Authorization": "Bearer " + TOKEN, "Idempotency-Key": str(uuid1())},
            [("Authorization", "Bearer " + TOKEN), ("Idempotency-Key", str(uuid4())),
             ("Idempotency-Key", str(uuid4()))]):
            response = await self.request("POST", "/api/v1/runs", headers=headers, json=self.body)
            self.assertEqual(response.status_code, 422, response.text)
        raw = json.dumps(self.body)[:-1] + ', "requestText": "second-value"}'
        response = await self.request("POST", "/api/v1/runs", headers={"Authorization": "Bearer " + TOKEN,
            "Idempotency-Key": str(uuid4()), "Content-Type": "application/json"}, content=raw)
        self.assertEqual(response.status_code, 422)
        self.assertEqual(self.count("run_submissions"), 0)
        self.assertEqual(self.count("workflow_runs"), 0)

    async def test_configuration_validation_and_forbidden_product_fields_still_apply(self):
        changed = {**self.body, "configuration": {**self.body["configuration"], "startingCommitHash": "f" * 40}}
        for body in (changed, {**self.body, "projectId": str(uuid4())},
                     {**self.body, "configurationId": "e" * 64}):
            self.assertEqual((await self.submit(uuid4(), body=body)).status_code, 422)
        self.assertEqual(self.count("workflow_runs"), 0)
        self.assertEqual(self.dispatcher.run_ids, [])

    async def test_persistent_replay_after_restart_ignores_current_template_and_never_reschedules(self):
        key = uuid4()
        original = await self.submit(key)
        restarted = self.make_app(SQLiteWorkflowRepository(self.path))
        restarted.state.submission_validator = Mock(side_effect=ValueError("Configuration changed"))
        replayed = await self.submit(key, app=restarted)
        self.assertEqual(replayed.status_code, 200, replayed.text)
        self.assertEqual(replayed.json(), original.json())
        restarted.state.submission_validator.assert_not_called()
        self.assertEqual(len(self.dispatcher.run_ids), 1)

    async def test_concurrent_requests_across_two_app_instances_create_one_complete_bundle(self):
        key = uuid4()
        other_app = self.make_app(SQLiteWorkflowRepository(self.path))
        results = await asyncio.gather(*(self.submit(key, app=self.app if i % 2 else other_app)
                                         for i in range(12)))
        self.assertEqual(sorted(r.status_code for r in results), [200] * 11 + [201])
        self.assertEqual(len({r.json()["run"]["runId"] for r in results}), 1)
        self.assertEqual(len(self.dispatcher.run_ids), 1)
        self.assertEqual(self.count("workflow_runs"), 1)
        self.assertEqual(self.count("workflow_steps"), 1)
        self.assertEqual(self.count("trace_events"), 3)
        self.assertEqual(self.count("run_submissions"), 1)

    async def test_transaction_failure_rolls_back_receipt_and_run_then_allows_safe_retry(self):
        key = uuid4()
        with patch("orchestrator.infrastructure.sqlite_workflows._insert_event", side_effect=RuntimeError("injected")):
            failed = await self.submit(key)
        self.assertEqual(failed.status_code, 500)
        for table in ("workflow_runs", "workflow_steps", "workspaces", "run_configurations", "trace_events", "run_submissions"):
            self.assertEqual(self.count(table), 0, table)
        self.assertEqual(self.dispatcher.run_ids, [])
        self.assertEqual((await self.submit(key)).status_code, 201)

    async def test_concurrent_different_payloads_with_one_key_cannot_replace_the_winner(self):
        key = uuid4()
        other_app = self.make_app(SQLiteWorkflowRepository(self.path))
        other_body = {**self.body, "requestText": "승인한 회원가입 요구사항대로 개발해 주세요."}
        results = await asyncio.gather(self.submit(key), self.submit(key, body=other_body, app=other_app))
        self.assertEqual(sorted(response.status_code for response in results), [201, 409])
        self.assertEqual(self.count("workflow_runs"), 1)
        self.assertEqual(self.count("run_submissions"), 1)
        self.assertEqual(self.count("trace_events"), 3)
        self.assertEqual(len(self.dispatcher.run_ids), 1)

    async def test_distinct_keys_are_distinct_starts_and_receipts_are_immutable(self):
        first, second = await self.submit(uuid4()), await self.submit(uuid4())
        self.assertNotEqual(first.json()["run"]["runId"], second.json()["run"]["runId"])
        for statement in ("UPDATE run_submissions SET payload_json=payload_json", "DELETE FROM run_submissions"):
            with self.assertRaises(sqlite3.IntegrityError), self.repository._transaction() as connection:
                connection.execute(statement)
        self.assertEqual(self.count("run_submissions"), 2)


class OwnedHostAPIWiringTests(_HostFixture, unittest.IsolatedAsyncioTestCase):
    async def test_actual_host_factory_enables_auth_public_template_and_no_execution(self):
        platform = await self.host()
        self.addAsyncCleanup(platform.aclose)
        app = platform.orchestrator_app
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://host") as client:
            self.assertEqual((await client.get("/api/v1/scenarios")).status_code, 401)
            path = "/api/v1/host/approved-configuration?scenarioId=" + str(SCN_001_ID)
            response = await client.get(path, headers={"Authorization": "Bearer " + self.environment["A2A_HOST_API_TOKEN"]})
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.json()["configuration"], self.inspect().approved_configuration.model_dump(mode="json", by_alias=True))
            for secret in self.environment.values():
                self.assertNotIn(secret, response.text)
            self.assertNotIn(str(self.directory), response.text)
            self.assertTrue(app.state.require_idempotency_key)
            await platform.aclose()
            closed = await client.get(path, headers={"Authorization": "Bearer " + self.environment["A2A_HOST_API_TOKEN"]})
            self.assertEqual(closed.status_code, 503)
        self.assertTrue(all(provider.completed == 0 for provider in self.providers))

    async def test_missing_malformed_and_reused_service_credentials_fail_before_allocation(self):
        for value in (None, "short", "x" * 32 + "\n", self.environment["A2A_PLATFORM_LLM_API_KEY"]):
            environment = dict(self.environment)
            environment.pop("A2A_HOST_API_TOKEN")
            if value is not None:
                environment["A2A_HOST_API_TOKEN"] = value
            with self.subTest(kind=type(value).__name__), self.assertRaises(HostFactoryError):
                await self.host(environ=environment)
            self.assertEqual(self.providers, [])
            self.assertFalse((self.directory / ".data").exists())


if __name__ == "__main__":
    unittest.main()
