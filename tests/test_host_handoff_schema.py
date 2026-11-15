"""Actual Host OpenAPI describes auth/required input without credential values."""

import json
import unittest

from orchestrator.core.config import Settings
from orchestrator.main import create_app
from test_owned_host import _HostFixture


class HostHandoffSchemaTests(_HostFixture, unittest.IsolatedAsyncioTestCase):
    async def test_owned_host_schema_matches_auth_submission_lookup_and_zip_contracts(self):
        platform = await self.host()
        self.addAsyncCleanup(platform.aclose)
        schema = platform.orchestrator_app.openapi()
        self.assertEqual(schema["components"]["securitySchemes"]["HostServiceBearer"]["scheme"], "bearer")
        for path, operations in schema["paths"].items():
            if path.startswith("/api/v1/"):
                for operation in operations.values():
                    self.assertEqual(operation["security"], [{"HostServiceBearer": []}], path)
        post = schema["paths"]["/api/v1/runs"]["post"]
        key = next(value for value in post["parameters"] if value["name"] == "Idempotency-Key")
        self.assertTrue(key["required"])
        self.assertEqual(key["schema"]["type"], "string")
        self.assertEqual(key["schema"]["format"], "uuid")
        self.assertIn("configuration", schema["components"]["schemas"]["CreateRunRequest"]["required"])
        self.assertIn("200", post["responses"])
        self.assertIn("201", post["responses"])
        lookup = schema["paths"]["/api/v1/submissions/{internal_key}"]["get"]
        self.assertIn("404", lookup["responses"])
        properties = schema["components"]["schemas"]["SubmissionLookupResponse"]["properties"]
        self.assertEqual(set(properties), {"internalKey", "status", "requestSha256", "runId", "createdAt",
                                          "originalResponse", "run", "recovery"})
        download = schema["paths"]["/api/v1/runs/{run_id}/artifacts/{artifact_id}/download"]["get"]
        self.assertEqual(download["responses"]["200"]["content"]["application/zip"]["schema"]["format"], "binary")
        self.assertIn("X-Snapshot-Sha256", download["responses"]["200"]["headers"])
        self.assertIn("X-Archive-Sha256", download["responses"]["200"]["headers"])
        text = json.dumps(schema)
        for secret in self.environment.values():
            self.assertNotIn(secret, text)
        self.assertNotIn(str(self.directory), text)
        self.assertTrue(all(provider.completed == 0 for provider in self.providers))
        self.assertEqual(platform.orchestrator_app.openapi(), schema)

    async def test_owned_schema_does_not_change_the_separate_bootstrap_contract(self):
        platform = await self.host()
        self.addAsyncCleanup(platform.aclose)
        platform.orchestrator_app.openapi()
        bootstrap = create_app(settings=Settings(_env_file=None, log_level="CRITICAL"))
        schema = bootstrap.openapi()
        self.assertNotIn("HostServiceBearer", schema["components"].get("securitySchemes", {}))
        self.assertNotIn("configuration", schema["components"]["schemas"]["CreateRunRequest"].get("required", []))


if __name__ == "__main__":
    unittest.main()
