import json
import tempfile
import unittest
from pathlib import Path
from uuid import uuid4

import httpx
from fastapi.exceptions import RequestValidationError

from orchestrator.api.errors import request_validation_error_handler
from orchestrator.core.config import Settings
from orchestrator.domain import SCN_001_ID
from orchestrator.infrastructure import SQLiteWorkflowRepository
from orchestrator.main import create_app


class APIValidationErrorTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        path = Path(self.directory.name) / "api.sqlite3"
        self.repository = SQLiteWorkflowRepository(path)
        self.app = create_app(self.repository, settings=Settings(
            _env_file=None, database_path=str(path), planner_agent_url=None,
            developer_agent_url=None, qa_agent_url=None, security_agent_url=None,
        ))
        self.marker = "synthetic_validation_secret_75f3"

    def tearDown(self):
        self.directory.cleanup()

    async def request(self, method, path, **kwargs):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.app), base_url="http://testserver",
        ) as client:
            return await client.request(method, path, **kwargs)

    def assert_safe_error(self, response):
        self.assertEqual(response.status_code, 422)
        self.assertNotIn(self.marker, response.text)
        for error in response.json()["detail"]:
            self.assertEqual(set(error), {"type", "loc", "msg"})

    async def test_extra_api_key_does_not_echo_the_submitted_value(self):
        response = await self.request("POST", "/api/v1/runs", json={
            "scenarioId": str(SCN_001_ID), "requestText": "회원가입", "apiKey": self.marker,
        })
        self.assert_safe_error(response)
        self.assertEqual(response.json()["detail"][0]["loc"], ["body", "apiKey"])

    async def test_invalid_request_text_type_never_echoes_nested_secrets(self):
        response = await self.request("POST", "/api/v1/runs", json={
            "scenarioId": str(SCN_001_ID), "requestText": {"password": self.marker},
        })
        self.assert_safe_error(response)

    async def test_invalid_resume_input_type_does_not_echo_credentials(self):
        response = await self.request("POST", f"/api/v1/runs/{uuid4()}/resume", json={
            "inputData": [{"password": self.marker, "Authorization": f"Bearer {self.marker}"}],
        })
        self.assert_safe_error(response)

    async def test_invalid_path_and_query_keep_locations_without_raw_values(self):
        for path in (f"/api/v1/runs/{self.marker}", f"/api/v1/runs/{uuid4()}/events?limit={self.marker}"):
            with self.subTest(path=path):
                self.assert_safe_error(await self.request("GET", path))

    async def test_malformed_json_does_not_echo_the_request_body(self):
        response = await self.request("POST", "/api/v1/runs", content=(
            '{"requestText":{"password":"' + self.marker + '"}, broken'
        ), headers={"Content-Type": "application/json"})
        self.assert_safe_error(response)

    async def test_error_context_message_and_body_are_never_serialized(self):
        error = RequestValidationError([{
            "type": "value_error", "loc": ("body", "requestText"),
            "msg": self.marker, "input": self.marker,
            "ctx": {"error": ValueError(self.marker)},
        }], body={"password": self.marker})
        response = await request_validation_error_handler(None, error)
        body = response.body.decode()
        self.assertNotIn(self.marker, body)
        self.assertEqual(set(json.loads(body)["detail"][0]), {"type", "loc", "msg"})

    async def test_valid_submission_and_missing_resource_behavior_are_unchanged(self):
        response = await self.request("POST", "/api/v1/runs", json={
            "scenarioId": str(SCN_001_ID), "requestText": "회원가입",
        })
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.json()["dispatchStatus"], "NOT_CONFIGURED")
        missing = await self.request("GET", f"/api/v1/runs/{uuid4()}")
        self.assertEqual(missing.status_code, 404)
