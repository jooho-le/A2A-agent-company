"""43 pricing through all owned roles and HTTP; synthetic model/CLI only.

Actual local SQLite, Git, A2A and receipt boundaries execute, but no external
provider, Docker or generated product code executes. These prices are fixtures.
"""

import asyncio
from dataclasses import replace
import json
import unittest
from unittest.mock import patch
from uuid import UUID

import test_owned_agent_pipeline as pipeline_fixture
from agents.llm.contracts import TokenUsage
from agents.platform.composition import create_platform
from agents.platform.pricing import load_price_book
from orchestrator.domain import AgentRole, WorkflowStatus
from orchestrator.domain.run_configuration import ModelConfiguration
from test_llm_runtime import FakeProvider
from test_priced_telemetry import synthetic_prices


class _PricedFixtureProvider(FakeProvider):
    name = "openai"

    def __init__(self, *script, unknown_tier=False):
        super().__init__(*script)
        self.unknown_tier = unknown_tier

    async def complete(self, request):
        response = await super().complete(request)
        return replace(response, model_id="fixture-model",
            usage=TokenUsage(10, 5, 15, cached_input_tokens=4),
            reported_service_tier=None if self.unknown_tier else "default",
            billing_context="OPENAI_RESPONSES_GLOBAL_TEXT")


class PricedPipelineTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.base = pipeline_fixture.OwnedAgentPipelineTests()
        self.base.setUp()
        for function, arguments, keywords in self.base._cleanups:
            self.addCleanup(function, *arguments, **keywords)
        self.base._cleanups.clear()
        self.base.model = ModelConfiguration(provider="openai", model_id="fixture-model", temperature=0, seed=17)
        self.base.configuration = self.base.configuration.model_copy(update={"model": self.base.model})
        self.base.settings = {role: settings.model_copy(update={"llm_provider": "openai",
            "llm_model_id": "fixture-model"}) for role, settings in self.base.settings.items()}
        self.path = self.base.directory / "price-book.json"
        self.path.write_text(json.dumps(synthetic_prices()), encoding="utf-8")
        self.book = load_price_book(self.path)

    def providers(self, *, unknown_security_tier=False):
        return {role: _PricedFixtureProvider(*provider.script,
                    unknown_tier=unknown_security_tier and role is AgentRole.SECURITY)
                for role, provider in self.base.providers().items()}

    def platform_factory(self, **options):
        return create_platform(**options, price_book=self.book)

    async def test_all_roles_costs_are_projected_by_existing_http_without_promoting_success(self):
        with patch.object(pipeline_fixture, "create_platform", side_effect=self.platform_factory):
            async with self.base.running_platform(self.providers()) as (client, providers):
                submitted = await asyncio.wait_for(client.post("/api/v1/runs", json=self.base.submission()), 25)
                self.assertEqual(submitted.status_code, 201, submitted.text)
                run_id = UUID(submitted.json()["run"]["runId"])
                response = await client.get(f"/api/v1/runs/{run_id}/usage")
                self.assertEqual(response.status_code, 200, response.text)
                body = response.json()
                self.assertEqual({row["role"] for row in body["records"]}, {role.value for role in AgentRole})
                self.assertEqual({row["sequence"] for row in body["records"]}, set(range(1, 7)))
                self.assertTrue(all(row["costUsd"] == "0.000064" for row in body["records"]))
                self.assertEqual(body["summary"]["costUsd"], "0.000384")
                self.assertTrue(body["summary"]["costComplete"])
                self.assertEqual(sum(len(provider.requests) for provider in providers.values()), 6)
                self.assertIs(self.base.repository.get_run(run_id).status, WorkflowStatus.HUMAN_REVIEW)
                page = await client.get(f"/api/v1/runs/{run_id}/usage?limit=1&offset=3")
                self.assertEqual(page.json()["summary"], body["summary"])
                telemetry = await client.get(f"/api/v1/runs/{run_id}/telemetry")
                costs = [row["costUsd"] for row in telemetry.json()["events"] if row["recordType"] == "USAGE"]
                self.assertEqual(costs, ["0.000064"] * 6)

    async def test_unknown_price_is_not_a_token_budget_failure_or_a_free_call(self):
        with patch.object(pipeline_fixture, "create_platform", side_effect=self.platform_factory):
            async with self.base.running_platform(self.providers(unknown_security_tier=True)) as (client, _):
                submitted = await asyncio.wait_for(client.post("/api/v1/runs", json=self.base.submission()), 25)
                self.assertEqual(submitted.status_code, 201, submitted.text)
                run_id = UUID(submitted.json()["run"]["runId"])
                body = (await client.get(f"/api/v1/runs/{run_id}/usage")).json()
                summary = body["summary"]
                self.assertTrue(summary["usageComplete"])
                self.assertEqual(summary["totalTokens"], 90)
                self.assertFalse(summary["executionBlocked"])
                self.assertFalse(summary["costComplete"])
                self.assertIsNone(summary["costUsd"])
                self.assertEqual(summary["knownCostUsd"], "0.00032")
                security = next(row for row in body["records"] if row["role"] == "SECURITY")
                self.assertEqual(summary["unknownCostSequences"], [security["sequence"]])
                self.assertEqual(security["costStatus"], "TIER_UNKNOWN")
                self.assertIsNone(security["costUsd"])
                self.assertIs(self.base.repository.get_run(run_id).status, WorkflowStatus.HUMAN_REVIEW)
