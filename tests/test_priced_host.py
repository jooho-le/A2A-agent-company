"""43 Host/CLI wiring with synthetic prices and inert existing Host fixtures."""

from contextlib import redirect_stderr, redirect_stdout
from copy import deepcopy
import io
import json
import unittest
from unittest.mock import patch

import test_owned_host as host_fixture
from test_owned_host import _HostFixture
from test_priced_telemetry import synthetic_prices
from agents.platform.composition import PlatformPreparationError
from agents.platform.host import HostFactoryError
from agents.platform.run_host import main


class _PricingHostFixture(_HostFixture):
    def setUp(self):
        super().setUp()
        self.price_file = self.base / "price-book.json"
        self.price_data = synthetic_prices()
        self.write_prices()

    def write_prices(self):
        self.price_file.write_text(json.dumps(self.price_data), encoding="utf-8")

    def inspect(self, **options):
        options.setdefault("price_book_file", self.price_file)
        return super().inspect(**options)


class PricedHostInspectionTests(_PricingHostFixture, unittest.TestCase):
    def test_pricebook_is_policy_bound_without_storage_secrets_or_execution(self):
        with patch("agents.platform.host.SQLiteWorkflowRepository", side_effect=AssertionError("inert")):
            priced = self.inspect()
        self.assertNotEqual(priced.policy_sha256, self.inspect(price_book_file=None).policy_sha256)
        self.assertEqual(priced.policy_sha256, self.inspect().policy_sha256)
        self.assertFalse((self.directory / ".data").exists())
        self.assertNotIn(str(self.price_file), repr(priced))

    def test_same_run_policy_includes_rates_identity_version_and_range(self):
        baseline = self.inspect().policy_sha256
        for key, value in (("priceBookVersion", "2.0.0"), ("priceBookId", "another-fixture")):
            old = deepcopy(self.price_data)
            self.price_data[key] = value
            self.write_prices()
            self.assertNotEqual(self.inspect().policy_sha256, baseline)
            self.price_data = old
        for key, value in (("inputUsdPerMillion", "3"), ("reportedModelId", "fixture-model-2026-01-01"),
                           ("minInputTokens", 12), ("serviceTier", "flex")):
            old = deepcopy(self.price_data)
            self.price_data["entries"][0][key] = value
            self.write_prices()
            self.assertNotEqual(self.inspect().policy_sha256, baseline)
            self.price_data = old

    def test_price_assets_in_workspace_or_reference_are_rejected(self):
        self.runtime["workspaceRoot"] = str(self.base)
        self.write()
        with self.assertRaises(HostFactoryError):
            self.inspect()

    def test_cli_pricebook_check_is_readonly_and_unapproved_example_fails_safely(self):
        argv = ["--runtime", str(self.runtime_file), "--tools", str(self.tools_file),
                "--source", str(self.source_file), "--price-book", str(self.price_file)]
        output, errors = io.StringIO(), io.StringIO()
        with redirect_stdout(output), redirect_stderr(errors):
            self.assertEqual(main(argv), 0)
        self.assertIs(json.loads(output.getvalue())["executionReady"], False)
        self.assertFalse((self.directory / ".data").exists())
        self.price_data["priceBookId"] = None
        self.write_prices()
        output, errors = io.StringIO(), io.StringIO()
        with redirect_stdout(output), redirect_stderr(errors):
            self.assertEqual(main(argv), 1)
        self.assertEqual(output.getvalue(), "")
        self.assertEqual(errors.getvalue().strip(), "OWNED_HOST_CONFIGURATION_INVALID")


class PricedHostCreationTests(_PricingHostFixture, unittest.IsolatedAsyncioTestCase):
    claimed_run = host_fixture.OwnedHostCreationTests.claimed_run

    async def test_preparation_pins_prices_before_model_without_charging_or_marking_success(self):
        platform = await self.host(price_book_file=self.price_file)
        self.addAsyncCleanup(platform.aclose)
        run, _frozen, _root = self.claimed_run(platform)
        await platform.dispatcher._before_dispatch(run.run_id)
        summary = platform.telemetry_store.usage_summary(run.run_id)
        self.assertEqual(summary["priceBookVersion"], "1.0.0")
        self.assertEqual(summary["modelCalls"], 0)
        self.assertEqual(summary["costUsd"], "0")  # No model call reserved or executed.
        self.assertTrue(all(provider.completed == 0 for provider in self.providers))

    async def test_modified_approval_rejects_new_submission_before_model(self):
        platform = await self.host(price_book_file=self.price_file)
        self.addAsyncCleanup(platform.aclose)
        approved = self.inspect().approved_configuration
        self.price_data["entries"][0]["inputUsdPerMillion"] = "3"
        self.write_prices()
        with self.assertRaises(Exception):
            platform.orchestrator_app.state.submission_validator(approved)
        self.assertTrue(all(provider.completed == 0 for provider in self.providers))

    async def test_restart_cannot_replace_frozen_price_version(self):
        first = await self.host(price_book_file=self.price_file)
        self.addAsyncCleanup(first.aclose)
        run, _frozen, root = self.claimed_run(first)
        await first.dispatcher._before_dispatch(run.run_id)
        before = {str(path.relative_to(root)): path.read_bytes() for path in root.rglob("*") if path.is_file()}
        await first.aclose()
        self.price_data["priceBookVersion"] = "2.0.0"
        self.write_prices()
        second = await self.host(price_book_file=self.price_file)
        self.addAsyncCleanup(second.aclose)
        with self.assertRaises(PlatformPreparationError):
            await second.dispatcher._before_dispatch(run.run_id)
        self.assertEqual(before, {str(path.relative_to(root)): path.read_bytes() for path in root.rglob("*") if path.is_file()})
        self.assertEqual(second.telemetry_store.usage_summary(run.run_id)["priceBookVersion"], "1.0.0")
        self.assertTrue(all(provider.completed == 0 for provider in self.providers))
