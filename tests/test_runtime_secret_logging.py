import logging
import sys
import unittest
from uuid import uuid4

from google.protobuf.json_format import ParseDict
from a2a.types import Task

from agents.core.logging import AgentSDKRedactionFilter
from orchestrator.core.security import REDACTED, redact_data, redact_text, register_secret_values


class RuntimeSecretLoggingTests(unittest.TestCase):
    def test_host_secret_is_masked_without_label_and_opaque_ids_are_preserved(self):
        secret = "fixture-host-credential-" + uuid4().hex
        register_secret_values(secret)
        self.assertEqual(redact_text(f"provider prose {secret}"), "provider prose " + REDACTED)
        reference = "task:" + secret
        self.assertEqual(redact_data({"a2aTaskId": reference}), {"a2aTaskId": reference})
        self.assertEqual(redact_data({"safe": secret}), {"safe": REDACTED})
        self.assertEqual(redact_text(redact_text(secret)), REDACTED)

    def test_short_secret_does_not_corrupt_ordinary_text(self):
        register_secret_values("x", None, "")
        self.assertEqual(redact_text("example"), "example")
        self.assertEqual(redact_data({"apiKey": "x"}), {"apiKey": REDACTED})
        with self.assertRaisesRegex(ValueError, "INVALID_SECRET_REGISTRATION"):
            register_secret_values(123)

    def test_sdk_payload_source_prompt_env_and_raw_extra_are_not_logged(self):
        task = ParseDict({
            "id": "opaque-task", "contextId": "opaque-context",
            "status": {"state": "TASK_STATE_COMPLETED"},
            "artifacts": [{"artifactId": "opaque-artifact", "parts": [{"data": {
                "code": "RAW_SOURCE_FIXTURE", "prompt": "RAW_PROMPT_FIXTURE",
                "stdout": "RAW_STDOUT_FIXTURE", "env": "RAW_DOTENV_FIXTURE",
            }, "mediaType": "application/json"}]}],
        }, Task())
        for level in (logging.DEBUG, logging.INFO, logging.WARNING, logging.ERROR):
            record = logging.LogRecord("a2a.server.fixture", level, __file__, 1,
                                       "SDK response %s RAW_PROVIDER_PROSE", (task,), None)
            record.payload = "RAW_EXTRA_FIXTURE"
            record.stack_info = "RAW_STACK_FIXTURE"
            AgentSDKRedactionFilter().filter(record)
            rendered = logging.Formatter("%(message)s %(payload)s").format(record)
            for forbidden in ("RAW_SOURCE", "RAW_PROMPT", "RAW_STDOUT", "RAW_DOTENV",
                              "RAW_PROVIDER", "RAW_EXTRA", "RAW_STACK"):
                self.assertNotIn(forbidden, rendered)
            self.assertIn(REDACTED, rendered)

    def test_sdk_exception_retains_type_without_free_prose(self):
        try:
            raise RuntimeError("fixture-unlabelled-provider-error")
        except RuntimeError as error:
            record = logging.LogRecord("a2a.client.fixture", logging.ERROR, __file__, 1,
                                       "failed: %s", (error,), sys.exc_info())
        AgentSDKRedactionFilter().filter(record)
        rendered = logging.Formatter().format(record)
        self.assertIn("RuntimeError", rendered)
        self.assertNotIn("fixture-unlabelled-provider-error", rendered)

    def test_non_sdk_structured_log_is_unchanged_by_sdk_filter(self):
        record = logging.LogRecord("orchestrator.fixture", logging.INFO, __file__, 1,
                                   "Run %s", ("safe-run",), None)
        AgentSDKRedactionFilter().filter(record)
        self.assertEqual(record.getMessage(), "Run safe-run")

    def test_mcp_sdk_parser_exception_does_not_publish_invalid_peer_bytes(self):
        from pydantic import BaseModel, ValidationError

        class PeerMessage(BaseModel):
            number: int

        try:
            PeerMessage.model_validate({"number": "RAW_MCP_SOURCE_FIXTURE"})
        except ValidationError as error:
            record = logging.LogRecord("mcp.client.stdio", logging.ERROR, __file__, 1,
                                       "Failed to parse JSONRPC: %s", (error,), sys.exc_info())
        AgentSDKRedactionFilter().filter(record)
        rendered = logging.Formatter().format(record)
        self.assertNotIn("RAW_MCP_SOURCE_FIXTURE", rendered)
        self.assertIn("ValidationError", rendered)
