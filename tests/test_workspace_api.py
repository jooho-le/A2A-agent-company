"""Explicit workspace preparation; no real Agent, model, or product execution."""

import asyncio
import tempfile
from pathlib import Path
from uuid import UUID, uuid4
import unittest

import httpx

from orchestrator.core.config import Settings
from orchestrator.domain import AgentRole, SCN_001_ID
from orchestrator.infrastructure import SQLiteWorkflowRepository
from orchestrator.main import create_app


class WorkspaceAPITests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name).resolve()
        self.base = self.root / "workspaces"
        self.repository = SQLiteWorkflowRepository(self.root / "api.sqlite3")
        self.app = create_app(repository=self.repository, settings=Settings(
            _env_file=None, database_path=str(self.root / "api.sqlite3"), workspace_root=str(self.base),
        ))

    def request(self, method, path, **kwargs):
        async def send():
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url="http://testserver") as client:
                return await client.request(method, path, **kwargs)
        return asyncio.run(send())

    def create_run(self):
        response = self.request("POST", "/api/v1/runs", json={"scenarioId": str(SCN_001_ID), "requestText": "회원가입 구현"})
        self.assertEqual(response.status_code, 201)
        run_id = response.json()["run"]["runId"]
        return self.repository.get_run(UUID(run_id))

    def test_app_run_and_get_metadata_do_not_provision_implicitly(self):
        self.assertFalse(self.base.exists())
        run = self.create_run()
        response = self.request("GET", f"/api/v1/runs/{run.run_id}/workspace")
        self.assertEqual(response.status_code, 200)
        self.assertFalse(self.base.exists())
        self.assertNotIn("rootPath", response.json())
        self.assertEqual(response.json()["permissions"]["DEVELOPER"]["write"], ["source/"])
        self.assertEqual(response.json()["permissions"]["QA"]["snapshot"], "READ_ONLY")

    def test_explicit_preparation_uses_server_ids_and_returns_no_host_path(self):
        run = self.create_run()
        response = self.request("POST", f"/api/v1/runs/{run.run_id}/workspace/provision")
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(body["workspaceId"], str(run.workspace_id))
        self.assertEqual(body["runId"], str(run.run_id))
        self.assertTrue(body["provisioned"])
        self.assertEqual(body["layoutVersion"], 1)
        self.assertNotIn("rootPath", body)
        self.assertNotIn(str(self.base), response.text)
        for name in ("planning", "source", "snapshots", "outputs/qa", "outputs/security"):
            self.assertTrue((self.base / str(run.workspace_id) / name).is_dir())
        bound = self.app.state.workspace_registry.bind(run.workspace_id, run_id=run.run_id, role=AgentRole.DEVELOPER)
        self.assertEqual(bound.workspace_id, run.workspace_id)

    def test_repeat_preparation_preserves_existing_source_and_run_state(self):
        run = self.create_run()
        endpoint = f"/api/v1/runs/{run.run_id}/workspace/provision"
        self.assertEqual(self.request("POST", endpoint).status_code, 200)
        source = self.base / str(run.workspace_id) / "source" / "existing.py"
        source.write_text("# Existing user content\n")
        response = self.request("POST", endpoint)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(source.read_text(), "# Existing user content\n")
        self.assertEqual(self.repository.get_run(run.run_id), run)
        self.assertEqual(len(self.repository.list_steps(run.run_id)), 1)

    def test_unknown_run_never_creates_a_workspace(self):
        response = self.request("POST", f"/api/v1/runs/{uuid4()}/workspace/provision")
        self.assertEqual(response.status_code, 404)
        self.assertFalse(self.base.exists())

    def test_unmarked_populated_directory_fails_without_wiping_or_exposing_data(self):
        run = self.create_run()
        directory = self.base / str(run.workspace_id)
        directory.mkdir(parents=True)
        existing = directory / "private-test-only-content"
        existing.write_text("retained private test data")
        response = self.request("POST", f"/api/v1/runs/{run.run_id}/workspace/provision")
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["detail"], "WORKSPACE_PROVISION_CONFLICT")
        self.assertNotIn(str(directory), response.text)
        self.assertNotIn("private-test-only-content", response.text)
        self.assertEqual(existing.read_text(), "retained private test data")
        self.assertEqual(self.repository.get_run(run.run_id), run)

    def test_openapi_has_separate_explicit_preparation_endpoint(self):
        schema = self.request("GET", "/openapi.json").json()
        endpoint = schema["paths"]["/api/v1/runs/{run_id}/workspace/provision"]
        self.assertIn("post", endpoint)
        self.assertNotIn("rootPath", str(endpoint))

    def test_broad_server_base_is_unavailable_without_host_path_disclosure(self):
        self.app = create_app(repository=self.repository, settings=Settings(
            _env_file=None, database_path=str(self.root / "api.sqlite3"), workspace_root="/",
        ))
        run = self.create_run()
        response = self.request("POST", f"/api/v1/runs/{run.run_id}/workspace/provision")
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["detail"], "WORKSPACE_ROOT_MISMATCH")
        self.assertFalse((Path("/") / str(run.workspace_id)).exists())
