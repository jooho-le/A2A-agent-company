"""Synthetic operator rate fixtures only; no claim about real model prices."""

from copy import deepcopy
from dataclasses import FrozenInstanceError, replace
from decimal import Decimal, localcontext
from hashlib import sha256
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from agents.llm.contracts import TokenUsage, UsageRecord
from agents.platform.pricing import (
    ApprovedPriceBook, CostQuote, PricingError, load_price_book,
    quote_frozen_usage, validate_frozen_price_book,
)
from orchestrator.domain.run_configuration import ModelConfiguration
from orchestrator.domain.states import AgentRole


class PricingTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory(prefix="a2a-pricing-fixture-", dir="/tmp")
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name).resolve()
        self.approval_file = self.directory / "fixture-price-book.json"
        self.entry = {
            "entryId": "fixture-default", "provider": "openai",
            "requestedModelId": "fixture-model", "reportedModelId": "fixture-model-2099-01-01",
            "serviceTier": "default", "minInputTokens": 0, "maxInputTokens": None,
            "cacheWriteMode": "NOT_APPLICABLE", "inputUsdPerMillion": "2",
            "cachedInputUsdPerMillion": "0.5", "cacheWriteUsdPerMillion": None,
            "outputUsdPerMillion": "4",
        }
        self.data = {
            "schemaVersion": 1, "priceBookId": "synthetic-fixture-only", "priceBookVersion": "1.0.0",
            "sourceRef": "https://developers.openai.com/api/docs/pricing",
            "currency": "USD", "billingScope": "OPENAI_RESPONSES_GLOBAL_TEXT", "entries": [self.entry],
        }
        self.model = ModelConfiguration(provider="openai", modelId="fixture-model", temperature=0)
        self.record = UsageRecord(1, AgentRole.DEVELOPER, self.model, "fixture-model-2099-01-01",
            "completed", 1, TokenUsage(1000, 200, 1200, 400, 100),
            reported_service_tier="default", billing_context="OPENAI_RESPONSES_GLOBAL_TEXT")

    def write(self, data=None):
        self.approval_file.write_text(json.dumps(self.data if data is None else data), encoding="utf-8")
        return self.approval_file

    def load(self, data=None):
        return load_price_book(self.write(data))

    def rejected(self, operation, *arguments):
        with self.assertRaises(PricingError) as caught:
            operation(*arguments)
        self.assertEqual(str(caught.exception), "LLM_PRICING_INVALID")
        self.assertEqual(caught.exception.code, "LLM_PRICING_INVALID")
        self.assertIsNone(caught.exception.__cause__)
        self.assertNotIn(str(self.directory), str(caught.exception))
        self.assertNotIn("private-fixture-secret", str(caught.exception))

    def unknown(self, approved, record, reason, entry_id=None):
        self.assertEqual(approved.quote(record), CostQuote(None, entry_id, reason))

    def test_price_book_is_sealed_frozen_detached_and_path_free(self):
        approved = self.load()
        self.assertIs(type(approved), ApprovedPriceBook)
        self.assertEqual(repr(approved), "ApprovedPriceBook()")
        self.assertEqual(approved.approval_path, self.approval_file)
        self.assertEqual(approved.descriptor, {"calculationVersion": "USD_PER_MILLION_V1", "priceBook": self.data})
        expected = sha256(json.dumps(approved.descriptor, ensure_ascii=False, sort_keys=True,
            separators=(",", ":")).encode()).hexdigest()
        self.assertEqual(approved.sha256, expected)
        self.assertNotIn(str(self.directory), json.dumps(approved.descriptor))
        self.assertNotIn(str(self.directory), repr(approved))
        descriptor = approved.descriptor
        descriptor["priceBook"]["entries"][0]["inputUsdPerMillion"] = "999"
        self.assertEqual(approved.descriptor["priceBook"], self.data)
        self.assertEqual(approved.sha256, expected)
        with self.assertRaises(FrozenInstanceError):
            approved._definition_json = "{}"

    def test_private_constructor_performs_no_io(self):
        with patch.object(os, "open", side_effect=AssertionError("no reads")):
            self.rejected(ApprovedPriceBook)

    def test_load_and_verify_readonly_do_not_import_call_network_or_write(self):
        self.write()
        before = self.approval_file.read_bytes()
        with patch("socket.create_connection", side_effect=AssertionError("no network")), \
                patch.object(os, "system", side_effect=AssertionError("no process")), \
                patch.object(Path, "write_text", side_effect=AssertionError("no writes")), \
                patch.object(Path, "write_bytes", side_effect=AssertionError("no writes")), \
                patch("importlib.import_module", side_effect=AssertionError("no imports")):
            approved = load_price_book(self.approval_file)
            self.assertIsNone(approved.verify_integrity())
        self.assertEqual(self.approval_file.read_bytes(), before)
        self.assertEqual(set(self.directory.iterdir()), {self.approval_file})

    def test_quote_and_frozen_validation_perform_no_file_io(self):
        approved = self.load()
        frozen = approved.descriptor
        self.approval_file.unlink()
        with patch.object(os, "open", side_effect=AssertionError("pure")), \
                patch.object(Path, "lstat", side_effect=AssertionError("pure")):
            self.assertEqual(approved.quote(self.record), CostQuote("0.0022", "fixture-default", "KNOWN"))
            self.assertEqual(quote_frozen_usage(frozen, self.record), approved.quote(self.record))
            self.assertIsNone(validate_frozen_price_book(frozen))
        self.rejected(approved.verify_integrity)

    def test_all_root_and_entry_keys_are_required_and_closed(self):
        for key in self.data:
            with self.subTest(missing_root=key):
                changed = deepcopy(self.data)
                del changed[key]
                self.rejected(self.load, changed)
        for key in self.entry:
            with self.subTest(missing_entry=key):
                changed = deepcopy(self.data)
                del changed["entries"][0][key]
                self.rejected(self.load, changed)
        self.rejected(self.load, {**self.data, "apiKey": "private-fixture-secret"})
        changed = deepcopy(self.data)
        changed["entries"][0]["notes"] = "private-fixture-secret"
        self.rejected(self.load, changed)

    def test_root_types_and_identifiers_are_exact(self):
        for key, values in (
            ("schemaVersion", (True, False, 1.0, "1", None, 0, 2)),
            ("priceBookId", ("", "a/b", "한글", "a b", "a\n", None, True, "a" * 129)),
            ("priceBookVersion", ("", "a b", "1/2", "한글", None, True, "a" * 129)),
            ("currency", ("usd", "KRW", None, True)),
            ("billingScope", ("BATCH", "REGIONAL", "RESPONSES_AUDIO", None, True)),
            ("entries", ([], {}, None, True)),
        ):
            for value in values:
                with self.subTest(key=key, value=value):
                    self.rejected(self.load, {**self.data, key: value})

    def test_source_ref_requires_official_https_without_credentials_query_or_fragment(self):
        for value in ("https://platform.openai.com/docs/pricing", "https://developers.openai.com"):
            self.assertEqual(self.load({**self.data, "sourceRef": value}).descriptor["priceBook"]["sourceRef"], value)
        for value in ("http://developers.openai.com/pricing", "https://developers.openai.com.evil/pricing",
            "https://evil/pricing", "https://private-fixture-secret@developers.openai.com/pricing",
            "https://u:p@platform.openai.com/pricing", "https://developers.openai.com/pricing?x=1",
            "https://developers.openai.com/pricing?", "https://developers.openai.com/pricing#",
            "https://developers.openai.com/pricing#part", "https://developers.openai.com:443/pricing",
            "https://developers.openai.com\\evil/pricing", "https://developers.openai.com/pricing\n",
            "file:///private/path", "", None, True):
            with self.subTest(value=value):
                self.rejected(self.load, {**self.data, "sourceRef": value})

    def test_json_duplicates_nonfinite_bad_encoding_markdown_and_oversize_rejected(self):
        raw = json.dumps(self.data)
        for content in ("{\"schemaVersion\":1," + raw[1:],
            raw.replace('"minInputTokens": 0', '"minInputTokens": NaN'),
            raw.replace('"minInputTokens": 0', '"minInputTokens": Infinity'),
            "```json\n" + raw + "\n```", "", " " * 262_145):
            self.approval_file.write_text(content, encoding="utf-8")
            self.rejected(load_price_book, self.approval_file)
        self.approval_file.write_bytes(b"\xff")
        self.rejected(load_price_book, self.approval_file)

    def test_rates_are_plain_bounded_nonnegative_decimal_strings(self):
        for key in ("inputUsdPerMillion", "cachedInputUsdPerMillion", "outputUsdPerMillion"):
            for value in (None, True, 0, 1.2, "", "-1", "-0", "+1", "01", ".1", "1.",
                "1e2", "NaN", "Infinity", " 1", "1 ", "1000000", "0.0000000000001"):
                with self.subTest(key=key, value=value):
                    changed = deepcopy(self.data)
                    changed["entries"][0][key] = value
                    self.rejected(self.load, changed)
            for value in ("0", "1", "999999", "999999.999999999999", "0.000000000001"):
                changed = deepcopy(self.data)
                changed["entries"][0][key] = value
                self.load(changed)

    def test_entry_types_scope_tier_ranges_and_modes_are_strict(self):
        for key, values in (
            ("entryId", ("", "../entry", "a b", None, True)),
            ("provider", ("OPENAI", "other", None, True)),
            ("requestedModelId", ("", "model\n", "model space", "한글", None, True)),
            ("reportedModelId", ("", "model\n", "model space", None, True)),
            ("serviceTier", ("auto", "DEFAULT", "batch", "", None, True)),
            ("minInputTokens", (-1, True, 0.0, "0", None)),
            ("maxInputTokens", (-1, True, 0.0, "0")),
            ("cacheWriteMode", ("auto", "", None, True)),
            ("cacheWriteUsdPerMillion", ("0", "1", 0, True)),
        ):
            for value in values:
                with self.subTest(key=key, value=value):
                    changed = deepcopy(self.data)
                    changed["entries"][0][key] = value
                    self.rejected(self.load, changed)
        changed = deepcopy(self.data)
        changed["entries"][0].update(minInputTokens=10, maxInputTokens=9)
        self.rejected(self.load, changed)
        for tier in ("default", "flex", "priority", "scale", "fast", "ultrafast"):
            changed = deepcopy(self.data)
            changed["entries"][0]["serviceTier"] = tier
            self.load(changed)

    def test_metered_write_requires_a_rate_and_not_applicable_forbids_it(self):
        for rate in (None, 1, "1e3", "-1"):
            changed = deepcopy(self.data)
            changed["entries"][0].update(cacheWriteMode="METERED", cacheWriteUsdPerMillion=rate)
            self.rejected(self.load, changed)
        changed = deepcopy(self.data)
        changed["entries"][0].update(cacheWriteMode="METERED", cacheWriteUsdPerMillion="3")
        self.load(changed)

    def test_ranges_are_inclusive_and_cannot_overlap_for_the_same_identity(self):
        first = {**self.entry, "maxInputTokens": 100}
        for minimum, maximum in ((100, 200), (0, 1), (50, None), (0, None)):
            changed = deepcopy(self.data)
            changed["entries"] = [first, {**self.entry, "entryId": "fixture-second", "minInputTokens": minimum,
                "maxInputTokens": maximum}]
            self.rejected(self.load, changed)
        changed = deepcopy(self.data)
        changed["entries"] = [first, {**self.entry, "entryId": "fixture-second", "minInputTokens": 101}]
        approved = self.load(changed)
        for count, identity in ((0, "fixture-default"), (100, "fixture-default"), (101, "fixture-second")):
            record = replace(self.record, usage=TokenUsage(count, 0, count, 0))
            self.assertEqual(approved.quote(record).entry_id, identity)
        for field, value in (("requestedModelId", "different-model"), ("reportedModelId", "different-response"),
            ("serviceTier", "flex")):
            changed["entries"] = [self.entry, {**self.entry, "entryId": "fixture-second", field: value}]
            self.load(changed)

    def test_duplicate_entry_ids_and_more_than_128_entries_rejected(self):
        second = {**self.entry, "serviceTier": "flex"}
        self.rejected(self.load, {**self.data, "entries": [self.entry, second]})
        entries = [{**self.entry, "entryId": "fixture-" + str(index),
            "minInputTokens": index, "maxInputTokens": index} for index in range(129)]
        self.rejected(self.load, {**self.data, "entries": entries})
        approved = self.load({**self.data, "entries": entries[:128]})
        self.assertEqual(len(approved.descriptor["priceBook"]["entries"]), 128)

    def test_requested_revision_and_reported_model_are_exact_not_prefix_matches(self):
        approved = self.load()
        for model, reported in ((ModelConfiguration(provider="other", modelId="fixture-model", temperature=0),
            self.record.reported_model_id), (ModelConfiguration(provider="openai", modelId="fixture-model",
            modelRevision="fixture-model-2098-01-01", temperature=0), self.record.reported_model_id),
            (self.model, "fixture-model"), (self.model, "fixture-model-2099-01-02"), (self.model, None)):
            self.unknown(approved, replace(self.record, requested_model=model, reported_model_id=reported), "MODEL_UNMATCHED")
        changed = deepcopy(self.data)
        changed["entries"][0]["requestedModelId"] = "fixture-model-2098-01-01"
        model = ModelConfiguration(provider="openai", modelId="fixture-model", modelRevision="fixture-model-2098-01-01", temperature=0)
        self.assertEqual(self.load(changed).quote(replace(self.record, requested_model=model)).reason, "KNOWN")

    def test_unknown_usage_tier_context_and_unmatched_rates_never_become_zero(self):
        approved = self.load()
        self.unknown(approved, replace(self.record, usage=None), "USAGE_UNKNOWN")
        self.unknown(approved, replace(self.record, reported_service_tier=None), "TIER_UNKNOWN")
        self.unknown(approved, replace(self.record, billing_context=None), "CONTEXT_UNKNOWN")
        self.unknown(approved, replace(self.record, reported_service_tier="flex"), "RATE_UNMATCHED")
        for field, value, reason in (("reported_service_tier", "auto", "TIER_UNKNOWN"),
            ("billing_context", "OPENAI_BATCH", "CONTEXT_UNKNOWN")):
            record = replace(self.record)
            object.__setattr__(record, field, value)
            self.unknown(approved, record, reason)
        changed = deepcopy(self.data)
        changed["entries"][0].update(minInputTokens=0, maxInputTokens=999)
        self.unknown(self.load(changed), self.record, "RATE_UNMATCHED")

    def test_cached_detail_is_required_even_for_zero_input(self):
        approved = self.load()
        for usage in (TokenUsage(1000, 200, 1200), TokenUsage(0, 0, 0)):
            self.unknown(approved, replace(self.record, usage=usage), "TOKEN_DETAILS_UNKNOWN", "fixture-default")
        self.assertEqual(approved.quote(replace(self.record, usage=TokenUsage(0, 0, 0, 0))),
            CostQuote("0", "fixture-default", "KNOWN"))

    def test_not_applicable_write_accepts_none_or_zero_but_not_observed_positive(self):
        approved = self.load()
        for write in (None, 0):
            usage = replace(self.record.usage, cache_write_input_tokens=write)
            self.assertEqual(approved.quote(replace(self.record, usage=usage)).cost_usd, "0.0022")
        usage = replace(self.record.usage, cache_write_input_tokens=10)
        self.unknown(approved, replace(self.record, usage=usage), "TOKEN_DETAILS_UNKNOWN", "fixture-default")

    def test_metered_write_partitions_input_and_requires_actual_detail(self):
        changed = deepcopy(self.data)
        changed["entries"][0].update(cacheWriteMode="METERED", cacheWriteUsdPerMillion="3")
        approved = self.load(changed)
        self.unknown(approved, self.record, "TOKEN_DETAILS_UNKNOWN", "fixture-default")
        record = replace(self.record, usage=replace(self.record.usage, cache_write_input_tokens=100))
        # (500*2 + 400*0.5 + 100*3 + 200*4) / 1,000,000.
        self.assertEqual(approved.quote(record), CostQuote("0.0023", "fixture-default", "KNOWN"))

    def test_reasoning_is_already_output_and_not_double_charged(self):
        approved = self.load()
        quotes = [approved.quote(replace(self.record, usage=replace(self.record.usage, reasoning_output_tokens=value)))
            for value in (None, 0, 100, 200)]
        self.assertEqual(quotes, [CostQuote("0.0022", "fixture-default", "KNOWN")] * 4)

    def test_decimal_precision_is_local_and_fixed_string_never_binary_float(self):
        changed = deepcopy(self.data)
        changed["entries"][0].update(inputUsdPerMillion="0.000000000001",
            cachedInputUsdPerMillion="0", outputUsdPerMillion="0")
        approved = self.load(changed)
        record = replace(self.record, usage=TokenUsage(1, 0, 1, 0))
        with localcontext() as context:
            context.prec = 2
            self.assertEqual(approved.quote(record).cost_usd, "0.000000000000000001")
            self.assertEqual(context.prec, 2)
        self.assertIs(type(approved.quote(record).cost_usd), str)
        self.assertEqual(Decimal(approved.quote(record).cost_usd), Decimal("1e-18"))

    def test_large_observed_integer_is_calculated_exactly_not_approximated(self):
        approved = self.load()
        count = 10 ** 100 + 1
        record = replace(self.record, usage=TokenUsage(count, 0, count, 0))
        whole, fraction = divmod(count * 2, 1_000_000)
        expected = (str(whole) + "." + str(fraction).zfill(6)).rstrip("0").rstrip(".")
        self.assertEqual(approved.quote(record), CostQuote(expected, "fixture-default", "KNOWN"))
        changed = deepcopy(self.data)
        changed["entries"][0].update(inputUsdPerMillion="999999.999999999999",
            cachedInputUsdPerMillion="999999.999999999999", outputUsdPerMillion="999999.999999999999")
        record = replace(self.record, usage=TokenUsage(count, count, count * 2, count))
        numerator = count * 2 * 999999999999999999
        whole, fraction = divmod(numerator, 10 ** 18)
        expected = (str(whole) + "." + str(fraction).zfill(18)).rstrip("0").rstrip(".")
        self.assertEqual(self.load(changed).quote(record), CostQuote(expected, "fixture-default", "KNOWN"))

    def test_invalid_or_prepriced_usage_and_subclasses_fail_closed(self):
        approved = self.load()
        for value in (None, {}, object()):
            self.rejected(approved.quote, value)
        for field, value in (("cost_usd", "0"), ("sequence", True), ("sequence", 0),
            ("role", "Developer"), ("duration_ms", -1), ("duration_ms", True),
            ("outcome", ""), ("reported_model_id", True), ("reported_service_tier", True),
            ("billing_context", True), ("requested_model", None)):
            record = replace(self.record)
            object.__setattr__(record, field, value)
            self.rejected(approved.quote, record)
        class UsageSubclass(TokenUsage):
            pass
        self.rejected(approved.quote, replace(self.record, usage=UsageSubclass(1000, 200, 1200, 400)))
        class RecordSubclass(UsageRecord):
            pass
        record = RecordSubclass(1, AgentRole.DEVELOPER, self.model, self.record.reported_model_id,
            "completed", 0, self.record.usage)
        self.rejected(approved.quote, record)

    def test_forged_token_values_are_revalidated_before_any_quote(self):
        approved = self.load()
        for field, value in (("input_tokens", True), ("output_tokens", -1), ("total_tokens", 1),
            ("cached_input_tokens", 1001), ("cached_input_tokens", True),
            ("reasoning_output_tokens", 201), ("cache_write_input_tokens", 1001),
            ("cache_write_input_tokens", True), ("cache_write_input_tokens", 601)):
            usage = replace(self.record.usage)
            object.__setattr__(usage, field, value)
            self.rejected(approved.quote, replace(self.record, usage=usage))

    def test_forged_model_configuration_is_revalidated(self):
        approved = self.load()
        model = self.model.model_copy()
        object.__setattr__(model, "temperature", float("nan"))
        with patch("warnings.warn"):
            self.rejected(approved.quote, replace(self.record, requested_model=model))

    def test_frozen_descriptor_is_closed_and_calculation_version_is_pinned(self):
        descriptor = self.load().descriptor
        for changed in ({}, {"priceBook": self.data}, {**descriptor, "apiKey": "private-fixture-secret"},
            {**descriptor, "calculationVersion": "V2"}, {**descriptor, "calculationVersion": True},
            {**descriptor, "priceBook": []}):
            self.rejected(validate_frozen_price_book, changed)
            self.rejected(quote_frozen_usage, changed, self.record)
        invalid = deepcopy(descriptor)
        invalid["priceBook"]["entries"][0]["inputUsdPerMillion"] = 1.5
        self.rejected(validate_frozen_price_book, invalid)

    def test_integrity_detects_all_policy_changes_but_not_json_formatting(self):
        approved = self.load()
        before = approved.sha256
        self.approval_file.write_text(json.dumps(self.data, sort_keys=True, indent=2), encoding="utf-8")
        self.assertIsNone(approved.verify_integrity())
        for field, value in (("priceBookVersion", "2"), ("sourceRef", "https://platform.openai.com/docs/pricing")):
            changed = deepcopy(self.data)
            changed[field] = value
            self.write(changed)
            self.rejected(approved.verify_integrity)
        changed = deepcopy(self.data)
        changed["entries"][0]["inputUsdPerMillion"] = "3"
        self.write(changed)
        self.rejected(approved.verify_integrity)
        self.assertEqual(approved.sha256, before)
        self.assertEqual(approved.quote(self.record).cost_usd, "0.0022")

    def test_symlink_leaf_parent_directory_fifo_missing_and_empty_rejected(self):
        approved = self.load()
        symlink = self.directory / "link.json"
        symlink.symlink_to(self.approval_file)
        self.rejected(load_price_book, symlink)
        parent = self.directory / "linked-parent"
        parent.symlink_to(self.directory, target_is_directory=True)
        self.rejected(load_price_book, parent / self.approval_file.name)
        self.rejected(load_price_book, self.directory)
        self.rejected(load_price_book, self.directory / "missing.json")
        fifo = self.directory / "fifo.json"
        os.mkfifo(fifo)
        self.rejected(load_price_book, fifo)
        self.approval_file.unlink()
        self.approval_file.symlink_to(symlink)
        self.rejected(approved.verify_integrity)

    def test_privileged_mutation_cannot_bypass_shape_revalidation(self):
        approved = self.load()
        object.__setattr__(approved, "_definition_json", "{}")
        self.rejected(approved.quote, self.record)
        self.rejected(approved.verify_integrity)
        self.rejected(lambda: approved.descriptor)

    def test_cost_quote_is_frozen_and_validated(self):
        quote = CostQuote("0.0022", "fixture-default", "KNOWN")
        with self.assertRaises(FrozenInstanceError):
            quote.cost_usd = "0"
        for arguments in ((None, None, "KNOWN"), (0, "fixture-default", "KNOWN"),
            ("1e2", "fixture-default", "KNOWN"), ("-1", "fixture-default", "KNOWN"),
            ("0", None, "USAGE_UNKNOWN"), (None, None, "unknown"),
            (None, "../bad", "TOKEN_DETAILS_UNKNOWN")):
            self.rejected(CostQuote, *arguments)


if __name__ == "__main__":
    unittest.main()
