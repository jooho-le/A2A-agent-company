"""Single accounting uses actual identity and the same immutable ledger."""

import unittest
from unittest.mock import patch

from agents.core.identity import SingleAgentIdentity
from agents.llm.contracts import TokenUsage
from agents.platform.telemetry_store import TelemetryStoreError
import test_priced_telemetry as fixtures


class SinglePricedTelemetryTests(unittest.TestCase):
    def setUp(self):
        self.f = fixtures.PricedTelemetryTests("runTest")
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        configuration = fixtures.RunConfiguration
        with patch.object(fixtures, "RunConfiguration", side_effect=lambda **values: configuration(architecture="SINGLE_AGENT", **values)):
            self.f.initialize_run()
        self.f.binding["role"] = "SINGLE_AGENT"

    def account(self, usage=None, **values):
        return self.f.account(usage, role=SingleAgentIdentity.SINGLE_AGENT, **values)

    def test_exact_price_cache_reasoning_and_real_single_identity(self):
        _, saved = self.account(TokenUsage(100, 30, 130, cached_input_tokens=40, reasoning_output_tokens=20))
        self.assertEqual(saved["costUsd"], "0.00044")
        records, _ = self.f.store.list_usage(self.f.run.run_id)
        self.assertEqual(records[0]["binding"]["role"], "SINGLE_AGENT")
        self.assertEqual(records[0]["detail"]["role"], "SINGLE_AGENT")
        self.assertTrue(self.f.store.usage_summary(self.f.run.run_id)["costComplete"])

    def test_unknown_usage_and_actual_model_are_null_not_free(self):
        self.account(None)
        self.account(TokenUsage(10, 2, 12), reported_model_id=None)
        summary = self.f.store.usage_summary(self.f.run.run_id)
        self.assertIsNone(summary["totalTokens"])
        self.assertIsNone(summary["costUsd"])
        self.assertFalse(summary["costComplete"])
        self.assertEqual(summary["knownTotalTokens"], 12)

    def test_pending_reservation_and_changed_prices_do_not_reset_cost(self):
        self.account(TokenUsage(100, 30, 130, cached_input_tokens=40))
        self.f.definition["entries"][0]["inputUsdPerMillion"] = "99"
        self.f.write_book()
        self.assertEqual(self.f.store.usage_summary(self.f.run.run_id)["costUsd"], "0.00044")
        self.f.budget.reserve_model_call()
        self.assertIsNone(self.f.store.usage_summary(self.f.run.run_id)["costUsd"])

    def test_logical_role_cannot_impersonate_single_cost_identity(self):
        with self.assertRaises(TelemetryStoreError):
            self.f.account(TokenUsage(10, 2, 12))


if __name__ == "__main__":
    unittest.main()
