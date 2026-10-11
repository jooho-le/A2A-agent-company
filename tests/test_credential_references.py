import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

import httpx
from pydantic import ValidationError

from orchestrator.core import security
from orchestrator.core.config import Settings
from orchestrator.core.security import (
    contains_credential_reference, contains_registered_secret,
    contains_uri_credentials, redact_data, redact_text,
)
from orchestrator.domain import RunConfiguration, RunConfigurationArtifact, SCN_001_ID
from orchestrator.infrastructure import SQLiteWorkflowRepository
from orchestrator.main import create_app


class CredentialReferenceTests(unittest.TestCase):
    def test_uri_userinfo_is_detected_and_redacted_in_free_text(self):
        for ref in (
            "https://user:synthetic_url_secret@host.example/tests",
            "https://synthetic_url_secret@host.example/tests",
            "https://user%3Asynthetic_url_secret@host.example/tests",
            "postgresql://user:synthetic_url_secret@[::1]:5432/db",
            "//user:synthetic_url_secret@host.example/tests",
            "https://first@user:synthetic_url_secret@host.example/tests",
        ):
            with self.subTest(ref=ref):
                self.assertTrue(contains_uri_credentials(ref))
                result = redact_text("failed reference: " + ref)
                self.assertNotIn("synthetic_url_secret", result)
                self.assertIn("[REDACTED]@", result)
                self.assertEqual(redact_text(result), result)

    def test_secret_query_and_fragment_parameters_support_encoded_keys(self):
        for suffix in (
            "?token=synthetic_url_secret", "?API-Key=synthetic_url_secret&version=1",
            "#access_token=synthetic_url_secret", "?%61pi%5fkey=synthetic_url_secret",
            "?%2561pi%255fkey=synthetic_url_secret", "#view?client_secret=synthetic_url_secret",
            "?password=synthetic%5furl%5fsecret", "?safe=1;api_key=synthetic_url_secret",
        ):
            with self.subTest(suffix=suffix):
                ref = "https://host.example/tests" + suffix
                self.assertTrue(contains_credential_reference(ref))
                result = redact_text(ref)
                self.assertNotIn("synthetic_url_secret", result)
                self.assertNotIn("synthetic%5furl%5fsecret", result)
                self.assertIn("[REDACTED]", result)
                self.assertEqual(redact_text(result), result)

    def test_masked_uri_still_cannot_be_an_immutable_reference(self):
        for ref in (
            "https://[REDACTED]@host.example/tests",
            "https://host.example/tests?token=[REDACTED]",
            "https%3A%2F%2Fuser%3Asecret%40host.example%2Ftests",
        ):
            with self.subTest(ref=ref):
                self.assertTrue(contains_credential_reference(ref))
                with self.assertRaises(ValidationError):
                    RunConfiguration(protected_test_suite_ref=ref)

    def test_encoded_uri_and_nested_redirects_cannot_leak_or_be_persisted(self):
        for ref in (
            "https%3A%2F%2Fuser%3Asynthetic_encoded_secret%40host.example%2Ftests",
            "%68ttps%3A%2F%2Fuser%3Asynthetic_encoded_secret%40host.example%2Ftests",
            "h%74tps%3A%2F%2Fuser%3Asynthetic_encoded_secret%40host.example%2Ftests",
            "https%253A%252F%252Fuser%253Asynthetic_encoded_secret%2540host.example%252Ftests",
            "https%25253A%25252F%25252Fuser%25253Asynthetic_encoded_secret%252540host.example%25252Ftests",
            "%2F%2Fuser%3Asynthetic_encoded_secret%40host.example%2Ftests",
            "https://host.example/tests?%61pi_key%3Dsynthetic_encoded_secret",
            "https://host.example/tests#token%3Dsynthetic_encoded_secret",
            "https://host.example/tests?redirect=https%3A%2F%2Fuser%3Asynthetic_encoded_secret%40nested.example%2Fx",
            "https://host.example/tests?redirect=https://user:synthetic_encoded_secret@nested.example/x",
            "https://host.example/tests#https%3A%2F%2Fuser%3Asynthetic_encoded_secret%40nested.example%2Fx",
        ):
            with self.subTest(ref=ref):
                self.assertTrue(contains_uri_credentials(ref))
                result = redact_text("failed reference: " + ref)
                self.assertNotIn("synthetic_encoded_secret", result)
                self.assertEqual(redact_text(result), result)
                with self.assertRaises(ValidationError):
                    RunConfiguration(scanner_profile_ref=ref)

    def test_safe_refs_hashes_and_agent_owned_opaque_ids_are_unchanged(self):
        for ref in (
            "artifact://protected/tests", "artifact://protected/scanner",
            "https://host.example/a%20b?revision=3#cases", "tests/signup-v1.json",
            "https://[::1]:8080/tests?passwordPolicy=minLength8",
            "sha256:" + "a" * 64,
        ):
            with self.subTest(ref=ref):
                self.assertFalse(contains_credential_reference(ref))
                self.assertEqual(redact_text(ref), ref)
                config = RunConfiguration(protected_test_suite_ref=ref, scanner_profile_ref=ref)
                self.assertEqual(config.protected_test_suite_ref, ref)
                self.assertEqual(config.scanner_profile_ref, ref)
        opaque = {"a2aTaskId": "https://user:secret@host.example/task"}
        self.assertEqual(redact_data(opaque), opaque)
        self.assertFalse(contains_uri_credentials("/" * 10000))

    def test_plain_and_registered_credentials_are_rejected_as_refs(self):
        with patch.object(security, "_KNOWN_SECRETS", ("synthetic_registry_secret",)):
            self.assertTrue(contains_registered_secret("synthetic_registry_secret"))
            self.assertTrue(contains_registered_secret(b"prefix synthetic_registry_secret suffix"))
            self.assertFalse(contains_registered_secret(b"ordinary bytes"))
            for ref in ("api_key=synthetic_label_secret", "artifact://host/synthetic_registry_secret"):
                with self.subTest(ref=ref):
                    with self.assertRaises(ValidationError):
                        RunConfiguration(scanner_profile_ref=ref)
        with patch.object(security, "_KNOWN_SECRETS", ("灡" * 8,)):
            for encoding in ("utf-8", "utf-16-le", "utf-16-be", "utf-32-le", "utf-32-be"):
                with self.subTest(encoding=encoding):
                    self.assertTrue(contains_registered_secret(("灡" * 8).encode(encoding)))

    def test_artifact_reload_revalidates_legacy_credentials_without_rewriting(self):
        artifact = RunConfigurationArtifact(run_id=uuid4(), scenario_id=SCN_001_ID, workspace_id=uuid4())
        payload = artifact.model_dump(mode="json", by_alias=True)
        payload["configuration"]["scannerProfileRef"] = "https://user:synthetic_legacy_secret@host.example/profile"
        original = json.dumps(payload)
        with self.assertRaises(ValidationError) as caught:
            RunConfigurationArtifact.model_validate_json(original)
        self.assertNotIn("synthetic_legacy_secret", str(caught.exception))
        self.assertEqual(json.dumps(payload), original)

    def test_nested_metadata_uses_url_redaction_without_mutating_input(self):
        data = {"detail": [{"ref": "https://user:synthetic_url_secret@host.example/tests?%61pi_key=synthetic_query_secret"}]}
        result = redact_data(data)
        self.assertNotIn("synthetic_url_secret", str(result))
        self.assertNotIn("synthetic_query_secret", str(result))
        self.assertIn("synthetic_url_secret", str(data))
        self.assertEqual(redact_data(result), result)


class CredentialReferenceAPITests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.path = Path(self.directory.name) / "workflow.sqlite3"
        self.repository = SQLiteWorkflowRepository(self.path)
        self.app = create_app(self.repository, settings=Settings(
            _env_file=None, database_path=str(self.path), planner_agent_url=None,
            developer_agent_url=None, qa_agent_url=None, security_agent_url=None,
        ))

    def tearDown(self):
        self.directory.cleanup()

    async def request(self, method, path, **kwargs):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.app), base_url="http://testserver",
        ) as client:
            return await client.request(method, path, **kwargs)

    async def test_unsafe_refs_return_422_without_echo_or_run_persistence(self):
        for field in ("protectedTestSuiteRef", "scannerProfileRef"):
            for ref in (
                "https://user:synthetic_submission_secret@host.example/tests",
                "https://host.example/tests?%61pi%5fkey=synthetic_submission_secret",
                "https://host.example/tests#token=synthetic_submission_secret",
                "https://host.example/tests?redirect=https%3A%2F%2Fuser%3Asynthetic_submission_secret%40nested.example%2Fx",
            ):
                with self.subTest(field=field, ref=ref):
                    response = await self.request("POST", "/api/v1/runs", json={
                        "scenarioId": str(SCN_001_ID), "requestText": "회원가입",
                        "configuration": {field: ref},
                    })
                    self.assertEqual(response.status_code, 422)
                    self.assertNotIn("synthetic_submission_secret", response.text)
                    for error in response.json()["detail"]:
                        self.assertEqual(set(error), {"type", "loc", "msg"})
                    with sqlite3.connect(self.path) as connection:
                        self.assertEqual(connection.execute("SELECT COUNT(*) FROM workflow_runs").fetchone()[0], 0)
                        self.assertEqual(connection.execute("SELECT COUNT(*) FROM run_configurations").fetchone()[0], 0)

    async def test_safe_config_is_frozen_and_request_urls_are_redacted(self):
        refs = {"protectedTestSuiteRef": "artifact://protected/tests", "scannerProfileRef": "https://host.example/scanner?version=1"}
        response = await self.request("POST", "/api/v1/runs", json={
            "scenarioId": str(SCN_001_ID),
            "requestText": "회원가입 https://user:synthetic_request_secret@host.example/spec",
            "configuration": refs,
        })
        self.assertEqual(response.status_code, 201)
        run_id = response.json()["run"]["runId"]
        artifacts = await self.request("GET", f"/api/v1/runs/{run_id}/artifacts")
        self.assertEqual(artifacts.status_code, 200)
        artifact = artifacts.json()["artifacts"][0]
        for field, ref in refs.items():
            self.assertEqual(artifact["configuration"][field], ref)
        from uuid import UUID
        stored = self.repository.get_run(UUID(run_id))
        self.assertNotIn("synthetic_request_secret", stored.request_text)
        stored_config = self.repository.get_run_configuration(UUID(run_id))
        self.assertEqual(stored_config.to_artifact_json(), artifact)
