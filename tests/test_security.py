import io
import logging
import unittest

from orchestrator.core.logging import SecretRedactionFilter, configure_logging
from orchestrator.core.security import redact_data, redact_text


class SecretRedactionTests(unittest.TestCase):
    def test_nested_sensitive_keys_are_masked_without_mutating_input(self) -> None:
        original = {
            "Password": "DUMMY_PASSWORD",
            "nested": [{"password_hash": "DUMMY_HASH", "accessToken": "DUMMY_ACCESS"}],
            "refresh_token": "DUMMY_REFRESH",
            "API-Key": "DUMMY_KEY",
            "clientSecret": "DUMMY_SECRET",
            "DB_PASSWORD": "DUMMY_DB_PASSWORD",
            "Authorization": "Bearer DUMMY_AUTH",
            "safe": 7,
        }
        result = redact_data(original)
        for secret in ("DUMMY_PASSWORD", "DUMMY_HASH", "DUMMY_ACCESS", "DUMMY_REFRESH", "DUMMY_KEY", "DUMMY_SECRET", "DUMMY_AUTH", "DUMMY_DB_PASSWORD"):
            self.assertNotIn(secret, str(result))
        self.assertEqual(result["Authorization"], "Bearer [REDACTED]")
        self.assertEqual(result["safe"], 7)
        self.assertEqual(original["Password"], "DUMMY_PASSWORD")
        self.assertIsNot(result["nested"], original["nested"])

    def test_quoted_and_korean_free_text_secret_values_are_masked(self) -> None:
        text = 'password="two words"; 비밀번호: DUMMY_KOREAN; "apiKey": "DUMMY_KEY"; token=DUMMY_TOKEN'
        redacted = redact_text(text)
        for secret in ("two words", "DUMMY_KOREAN", "DUMMY_KEY", "DUMMY_TOKEN"):
            self.assertNotIn(secret, redacted)
        self.assertIn('password="[REDACTED]"', redacted)
        self.assertEqual(redact_text(redacted), redacted)

    def test_authorization_preserves_scheme_but_never_credential(self) -> None:
        for text in (
            "Authorization: Bearer DUMMY_AUTH",
            "authorization=Basic DUMMY_AUTH",
            '{"Authorization": "Bearer DUMMY_AUTH"}',
        ):
            with self.subTest(text=text):
                result = redact_text(text)
                self.assertNotIn("DUMMY_AUTH", result)
                self.assertIn("[REDACTED]", result)
                self.assertTrue("Bearer" in result or "Basic" in result)
                self.assertEqual(redact_text(result), result)

    def test_standard_password_hash_and_jwt_patterns_are_masked(self) -> None:
        argon = "$argon2id$v=19$m=19456,t=2,p=1$c2FsdA$aGFzaA"
        bcrypt = "$2b$12$" + "A" * 53
        jwt = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.ZHVtbXk"
        text = f"result: {argon}; report: {bcrypt}; input: {jwt}; Bearer DUMMY_AUTH"
        result = redact_text(text)
        for secret in (argon, bcrypt, jwt, "DUMMY_AUTH"):
            self.assertNotIn(secret, result)
        self.assertEqual(result.count("[REDACTED]"), 4)

    def test_snapshot_ids_digests_and_requirement_text_remain_usable(self) -> None:
        safe = {
            "snapshotSha256": "a" * 64,
            "commitHash": "b" * 40,
            "dependencyLockHash": "sha256:" + "c" * 64,
            "containerImageDigest": "sha256:" + "d" * 64,
            "projectArtifactId": "f7f9e5c3-ffc3-4b3f-918b-21e1b956ce76",
            "a2aTaskId": "password=opaque-agent-owned-task-id",
            "a2aArtifactId": "token=opaque-agent-owned-artifact-id",
            "agentContextId": "authorization=opaque-agent-owned-context-id",
            "passwordPolicy": {"minLength": 8},
            "description": "비밀번호는 최소 8자이며 Password Hash를 응답에 노출하지 않는다.",
            "count": 3,
            "enabled": True,
            "missing": None,
        }
        self.assertEqual(redact_data(safe), safe)
        self.assertEqual(redact_data((safe, "safe text")), (safe, "safe text"))


class LoggingRedactionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = logging.getLogger()
        self.original_handlers = list(self.root.handlers)
        self.original_level = self.root.level
        self.child = logging.getLogger("orchestrator.redaction.test")
        self.child_handlers = list(self.child.handlers)
        self.child_level = self.child.level
        self.child_propagate = self.child.propagate
        self.root.handlers = []
        self.child.handlers = []
        self.child.propagate = False
        self.child.setLevel(logging.DEBUG)
        self.stream = io.StringIO()
        self.handler = logging.StreamHandler(self.stream)
        self.child.addHandler(self.handler)

    def tearDown(self) -> None:
        self.root.handlers = self.original_handlers
        self.root.setLevel(self.original_level)
        self.child.handlers = self.child_handlers
        self.child.setLevel(self.child_level)
        self.child.propagate = self.child_propagate

    def test_existing_root_and_child_handlers_receive_one_filter(self) -> None:
        root_stream = io.StringIO()
        root_handler = logging.StreamHandler(root_stream)
        self.root.addHandler(root_handler)
        self.root.setLevel(logging.INFO)
        configure_logging("INFO")
        configure_logging("INFO")
        self.root.warning("password=%s", "DUMMY_ROOT")
        self.child.warning("token=%s", "DUMMY_CHILD")
        self.assertNotIn("DUMMY_ROOT", root_stream.getvalue())
        self.assertNotIn("DUMMY_CHILD", self.stream.getvalue())
        for handler in (root_handler, self.handler):
            self.assertEqual(sum(isinstance(item, SecretRedactionFilter) for item in handler.filters), 1)

    def test_structured_messages_numeric_arguments_and_exception_are_sanitized(self) -> None:
        configure_logging("INFO")
        self.child.warning({"password": "DUMMY_MESSAGE"})
        self.child.warning("password=%(password)d", {"password": 1234567})
        try:
            raise ValueError("api_key=DUMMY_EXCEPTION")
        except ValueError:
            self.child.exception("authorization=Bearer DUMMY_AUTH")
        output = self.stream.getvalue()
        for secret in ("DUMMY_MESSAGE", "1234567", "DUMMY_EXCEPTION", "DUMMY_AUTH"):
            self.assertNotIn(secret, output)
        self.assertIn("ValueError", output)

    def test_secret_extra_fields_used_by_custom_formatter_are_sanitized(self) -> None:
        self.handler.setFormatter(logging.Formatter("%(message)s %(password)s"))
        configure_logging("INFO")
        self.child.warning("safe", extra={"password": "DUMMY_EXTRA"})
        self.assertEqual(self.stream.getvalue(), "safe [REDACTED]\n")


if __name__ == "__main__":
    unittest.main()
