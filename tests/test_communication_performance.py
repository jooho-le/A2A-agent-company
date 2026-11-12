"""70: completed-only statistics, parallel interval union and honest gaps."""

from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from diagnostics.communication.performance import performance_summary, timeline_summary
from diagnostics.communication.timing import CLOCK
from diagnostics.communication.live import write_html


def row(role="QA", *, start=0, end=10, duration=10, phase="COMPLETED", clock_id="shared", output_tokens=5):
    return {"role": role, "phase": phase, "model": {"status": "COMPLETED", "usage": {"output_tokens": output_tokens}},
        "delivery": {"check": {"status": "PASS"}}, "checks": {"sourceGrounding": {"status": "PASS"}},
        "timing": {"schemaVersion": 1, "clock": CLOCK, "clockId": clock_id, "ordering": "PASS",
            "interval": {"status": "MEASURED", "startMs": start, "endMs": end},
            "phases": {name: {"status": "MEASURED", "durationMs": duration} for name in
                       ("agentProcessingMs", "providerExchangeMs", "totalMs")}}}


class PerformanceSummaryTests(unittest.TestCase):
    def test_parallel_work_is_union_not_sum_and_uses_caller_elapsed(self):
        rows = [row("QA", start=0, end=100), row("SECURITY", start=25, end=125)]
        result = performance_summary(rows, elapsed_ms=150)
        self.assertEqual(result["timeline"], {"status": "MEASURED", "observedSamples": 2,
            "unverifiedSamples": 0, "invalidSamples": 0, "probeWorkSumMs": 200,
            "observedUnionMs": 125, "observedEnvelopeMs": 125, "concurrentMs": 75, "maxConcurrency": 2})
        self.assertEqual(result["diagnosticElapsedMs"], 150)

    def test_gaps_touching_intervals_and_three_way_overlap_are_distinct(self):
        rows = [row(start=0, end=10), row(start=0, end=10), row(start=0, end=10), row(start=20, end=30)]
        result = timeline_summary(rows)
        self.assertEqual((result["probeWorkSumMs"], result["observedUnionMs"], result["concurrentMs"]), (40, 20, 10))
        self.assertEqual((result["observedEnvelopeMs"], result["maxConcurrency"]), (30, 3))
        adjacent = timeline_summary([row(start=0, end=10), row(start=10, end=20)])
        self.assertEqual((adjacent["observedUnionMs"], adjacent["concurrentMs"], adjacent["maxConcurrency"]), (20, 0, 1))

    def test_mixed_clocks_or_reverse_intervals_are_not_combined(self):
        for rows in ([row(), row(clock_id="other-run")], [row(), row(start=20, end=10)]):
            result = timeline_summary(rows)
            self.assertEqual(result["status"], "INVALID")
            self.assertIsNone(result["observedUnionMs"])
            self.assertIsNone(result["probeWorkSumMs"])

    def test_missing_intervals_remain_partial_and_skipped_work_is_not_timed(self):
        unknown = row()
        unknown.pop("timing")
        result = timeline_summary([row(), unknown, row(phase="SKIPPED")])
        self.assertEqual(result["status"], "PARTIAL")
        self.assertEqual((result["observedSamples"], result["unverifiedSamples"]), (1, 1))
        self.assertEqual(result["observedUnionMs"], 10)

    def test_failed_and_canceled_fast_calls_do_not_improve_completed_percentiles(self):
        rows = [row(duration=value) for value in (100, 200, 300)]
        rows += [row(duration=1, phase="ERROR"), row(duration=2, phase="CANCELED"), row(phase="SKIPPED")]
        result = performance_summary(rows)["roles"]["QA"]
        self.assertEqual((result["completedSamples"], result["incompleteSamples"], result["notExecutedSamples"]), (3, 2, 1))
        self.assertEqual((result["phases"]["agentProcessingMs"]["p50Ms"], result["phases"]["agentProcessingMs"]["p95Ms"]), (200, 300))

    def test_missing_invalid_or_nonfinite_durations_are_counted_without_fabrication(self):
        rows = [row(duration=10), row(duration=float("inf")), row(duration=-1), row(duration=True)]
        missing = row()
        missing["timing"]["phases"].pop("agentProcessingMs")
        result = performance_summary([*rows, missing])["roles"]["QA"]["phases"]["agentProcessingMs"]
        self.assertEqual((result["samples"], result["invalid"], result["unverified"]), (1, 3, 1))
        self.assertEqual(result["p50Ms"], 10)

    def test_reverse_phase_order_invalidates_even_positive_phase_samples(self):
        value = row()
        value["timing"]["ordering"] = "FAIL"
        result = performance_summary([value])["roles"]["QA"]["phases"]["agentProcessingMs"]
        self.assertEqual((result["samples"], result["invalid"], result["p50Ms"]), (0, 1, None))

    def test_token_context_missing_and_zero_usage_do_not_create_infinite_rates(self):
        result = performance_summary([row(duration=100, output_tokens=10), row(output_tokens=0), row(output_tokens=None)])
        rate = result["roles"]["QA"]["providerMsPerOutputToken"]
        self.assertEqual((rate["samples"], rate["unverified"], rate["p50MsPerToken"]), (1, 2, 10))

    def test_role_spread_is_descriptive_and_zero_baseline_has_no_ratio(self):
        result = performance_summary([row("QA", duration=0), row("SECURITY", duration=100)])["roleSpread"]
        self.assertEqual((result["fastestRole"], result["slowestRole"], result["deltaMs"], result["ratio"]), ("QA", "SECURITY", 100, None))
        ordinary = performance_summary([row("QA", duration=10), row("SECURITY", duration=100)])["roleSpread"]
        self.assertEqual((ordinary["deltaMs"], ordinary["ratio"]), (90, 10))

    def test_content_failure_remains_separate_from_measured_completion(self):
        value = row()
        value["checks"]["sourceGrounding"]["status"] = "FAIL"
        original = deepcopy(value)
        result = performance_summary([value])["roles"]["QA"]
        self.assertEqual((result["completedSamples"], result["contentContractPassedSamples"]), (1, 0))
        self.assertEqual(value, original)

    def test_empty_or_legacy_data_has_no_invented_duration_or_dominant_phase(self):
        self.assertIsNone(performance_summary([], elapsed_ms=float("nan"))["diagnosticElapsedMs"])
        self.assertEqual(performance_summary([])["timeline"]["status"], "UNVERIFIED")
        value = row()
        value.pop("timing")
        result = performance_summary([value])["roles"]["QA"]
        self.assertIsNone(result["dominantPhase"])
        self.assertIsNone(result["dominantModelPhase"])
        self.assertEqual(result["phases"]["totalMs"]["unverified"], 1)


class PerformanceReportTests(unittest.TestCase):
    def report(self):
        value = row()
        value["checks"].update(request={"endToEnd": {"status": "PASS"}}, response={"status": "PASS"},
                               metadata={"status": "PASS"}, responseMetadata={"status": "PASS"}, identity="PASS")
        value["checks"]["sourceGrounding"]["differences"] = []
        value["model"]["apiInputMatches"] = True
        value["timings"] = {"totalMs": 10}
        run = {"architecture": "MULTI_AGENT", "rows": [value], "performance": performance_summary([value])}
        return {"runs": [run], "modelId": "synthetic-model", "calls": [], "status": "SYNTHETIC_TEST"}

    def test_generated_summary_shows_phase_table_and_marks_unobserved_values(self):
        report = self.report()
        with TemporaryDirectory() as directory:
            write_html(Path(directory), report)
            html = (Path(directory) / "report.html").read_text()
        self.assertIn('단계별 지연 관측', html)
        self.assertIn('API 교환 ms', html)
        self.assertIn('미관측', html)
        self.assertIn('송수신 일치: 1/1', html)

    def test_missing_delivery_evidence_cannot_appear_as_transport_pass_in_html(self):
        report = self.report()
        report['runs'][0]['rows'][0]['delivery']['check']['status'] = 'UNVERIFIED'
        with TemporaryDirectory() as directory:
            write_html(Path(directory), report)
            html = (Path(directory) / "report.html").read_text()
        self.assertIn('송수신 일치: 0/1', html)
