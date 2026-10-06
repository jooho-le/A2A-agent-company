"""Regression checks for SDK alias parsing, protobuf logs, and response boundaries."""

import logging
import os
import sys
import unittest
from unittest.mock import patch
from uuid import uuid4

import httpx
from a2a.types import Task
from a2a.utils.errors import InvalidParamsError
from google.protobuf.json_format import MessageToDict, ParseDict

from agents.api.routes import MAX_REQUEST_BYTES
from agents.api.task_store import sanitize_task
from agents.api.validation import parse_project_request
from agents.core.config import AgentSettings
from agents.core.logging import AgentSDKRedactionFilter
from agents.main import create_app


def wire_request() -> dict:
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


class AgentParsingAndLogSafetyTests(unittest.TestCase):
    def test_proto_aliases_cannot_override_validated_message_configuration_or_parts(self):
        for path, alias, value in (
            (("configuration",), "return_immediately", False),
            (("configuration",), "accepted_output_modes", ["text/plain"]),
            (("configuration",), "task_push_notification_config", {"url": "https://example.test"}),
            (("message",), "message_id", "untrusted-id"),
            (("message",), "context_id", "untrusted-context"),
            (("message", "parts", 0), "media_type", "text/plain"),
        ):
            with self.subTest(alias=alias):
                body = wire_request()
                target = body
                for key in path:
                    target = target[key]
                target[alias] = value
                with self.assertRaises(InvalidParamsError):
                    parse_project_request(body)

    def test_arbitrary_json_data_keys_are_not_treated_as_protocol_aliases(self):
        body = wire_request()
        body["message"]["parts"][0]["data"].update(
            message_id="domain-value", return_immediately="domain-value",
        )
        parsed = parse_project_request(body)
        data = MessageToDict(parsed.message.parts[0].data)
        self.assertEqual(data["message_id"], "domain-value")
        self.assertTrue(parsed.configuration.return_immediately)

    def test_tenant_routing_is_not_silently_accepted(self):
        body = wire_request()
        body["tenant"] = "unimplemented-tenant"
        with self.assertRaises(InvalidParamsError):
            parse_project_request(body)

    def test_sdk_protobuf_log_arguments_are_redacted_before_string_rendering(self):
        task = ParseDict({
            "id": str(uuid4()), "contextId": str(uuid4()),
            "status": {"state": "TASK_STATE_COMPLETED"},
            "artifacts": [{
                "artifactId": "agent-artifact", "parts": [{"data": {
                    "password": "DUMMY_PROTOBUF_LOG_PASSWORD",
                    "apiKey": "DUMMY_PROTOBUF_LOG_KEY",
                }, "mediaType": "application/json"}],
            }],
        }, Task())
        record = logging.LogRecord(
            "a2a.server.request_handlers.default_request_handler_v2", logging.DEBUG,
            __file__, 1, "Processing Task: %s", (task,), None,
        )
        self.assertTrue(AgentSDKRedactionFilter().filter(record))
        self.assertNotIn("DUMMY_PROTOBUF_LOG_PASSWORD", record.getMessage())
        self.assertNotIn("DUMMY_PROTOBUF_LOG_KEY", record.getMessage())
        self.assertIn("[REDACTED]", record.getMessage())

    def test_sdk_exception_text_is_not_rendered_in_arguments_or_tracebacks(self):
        try:
            raise RuntimeError("DUMMY_UNLABELED_PROVIDER_SECRET")
        except RuntimeError as error:
            record = logging.LogRecord(
                "a2a.server.agent_execution.active_task", logging.ERROR,
                __file__, 1, "Executor failed: %s", (error,), sys.exc_info(),
            )
        AgentSDKRedactionFilter().filter(record)
        output = logging.Formatter().format(record)
        self.assertNotIn("DUMMY_UNLABELED_PROVIDER_SECRET", output)
        self.assertIn("RuntimeError", output)

    def test_response_redaction_keeps_opaque_task_context_and_artifact_ids(self):
        task = ParseDict({
            "id": "task:password=opaque", "contextId": "context:Bearer opaque",
            "status": {"state": "TASK_STATE_COMPLETED"},
            "artifacts": [{
                "artifactId": "artifact:password=opaque",
                "parts": [{"data": {"password": "DUMMY_RESPONSE_SECRET"}}],
            }],
        }, Task())
        sanitized = sanitize_task(task)
        self.assertEqual(sanitized.id, task.id)
        self.assertEqual(sanitized.context_id, task.context_id)
        self.assertEqual(sanitized.artifacts[0].artifact_id, task.artifacts[0].artifact_id)
        self.assertNotIn("DUMMY_RESPONSE_SECRET", str(MessageToDict(sanitized)))
        self.assertIn("DUMMY_RESPONSE_SECRET", str(MessageToDict(task)))


class AgentHTTPSafetyTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        environment = patch.dict(os.environ, {}, clear=True)
        environment.start()
        self.addCleanup(environment.stop)
        self.app = create_app(AgentSettings(role="PLANNER", _env_file=None))
        self.headers = {"A2A-Version": "1.0", "Content-Type": "application/a2a+json"}

    async def request(self, method, path, **kwargs):
        async with self.app.router.lifespan_context(self.app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=self.app), base_url="http://127.0.0.1:8101",
            ) as client:
                return await client.request(method, path, headers=self.headers, **kwargs)

    async def test_oversized_body_is_rejected_before_agent_execution(self):
        response = await self.request("POST", "/message:send", content=b"x" * (MAX_REQUEST_BYTES + 1))
        self.assertEqual(response.status_code, 400)
        self.assertNotIn("x" * 100, response.text)

    async def test_exponent_overflow_is_rejected_as_nonfinite_json(self):
        body = '{"message":{"parts":[{"data":{"value":1e999,"password":"DUMMY_OVERFLOW_SECRET"}}]}}'
        response = await self.request("POST", "/message:send", content=body)
        self.assertEqual(response.status_code, 400)
        self.assertNotIn("DUMMY_OVERFLOW_SECRET", response.text)

    async def test_duplicate_get_query_fields_are_rejected_before_task_lookup(self):
        response = await self.request("GET", "/tasks/missing?historyLength=1&historyLength=2")
        self.assertEqual(response.status_code, 400)

    async def test_openapi_exposes_protocol_header_and_send_body_example(self):
        response = await self.request("GET", "/openapi.json")
        operation = response.json()["paths"]["/message:send"]["post"]
        self.assertTrue(any(item["name"] == "A2A-Version" for item in operation["parameters"]))
        example = operation["requestBody"]["content"]["application/a2a+json"]["example"]
        self.assertTrue(parse_project_request(example).configuration.return_immediately)


if __name__ == "__main__":
    unittest.main()
