"""Actual local A2A/SQLite/Git export; deterministic LLM/Docker adapters."""

import asyncio
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from contextlib import redirect_stdout, redirect_stderr
import io
import json
from pathlib import Path
import sqlite3
import unittest
from unittest.mock import patch
from uuid import uuid4

from agents.core.identity import SingleAgentIdentity
from agents.platform.a2a_export import export_a2a_messages, expected_a2a_roles, A2AExportError
from agents.platform.evaluation_export import write_evaluation_export, EvaluationExportError, main
from agents.platform.export_readonly import ReadOnlyWorkflowRepository
from agents.platform.run_records import collect_run_metrics, metrics_from_records, _paged, RunRecordsError
from agents.platform.pricing import load_price_book
from evaluation.manifest import ExecutionManifest
import test_single_agent_pipeline as pipeline
import test_single_agent_fix_pipeline as fixing
import test_priced_telemetry as accounting
import test_single_agent_controls as controls


def database_hash(path):
    with sqlite3.connect(path) as connection:
        return sha256(connection.serialize()).hexdigest()


class SingleEvaluationExportTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.base = pipeline.SingleAgentPipelineTests("runTest")
        self.base.setUp()
        self.addCleanup(self.base.doCleanups)
        self.base.measured_security = True
        self.f = self.base.f

    def export(self, run_id, **changes):
        return write_evaluation_export(self.f.repository, run_id, self.f.directory / "handoff",
            stores={"SINGLE_AGENT": self.f.directory / "single.sqlite3"}, **changes)

    async def test_success_exports_exact_snapshots_one_identity_all_records_readonly_and_metrics_api(self):
        async with self.base.running() as (client, _):
            run_id = await self.base.submit(client)
            response = await client.get(f"/api/v1/runs/{run_id}/metrics")
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.json()["architecture"], "SINGLE_AGENT")
            self.assertTrue(response.json()["timingComplete"])
            self.assertEqual((await client.get(f"/api/v1/runs/{uuid4()}/metrics")).status_code, 404)
        paths = [self.f.repository.database_path, self.f.directory / "single.sqlite3"]
        before = [database_hash(path) for path in paths]
        with patch("orchestrator.infrastructure.sqlite_workflows.SQLiteWorkflowRepository.__init__", side_effect=AssertionError("write constructor")):
            files = self.export(run_id)
        self.assertEqual(before, [database_hash(path) for path in paths])
        handoff, a2a, bundle, trace, metrics = (json.loads(files[name].read_text()) for name in
            ("handoff.json", "a2a-messages.json", "bundle.json", "trace.json", "metrics.json"))
        self.assertEqual(set(a2a["roles"]), {"SINGLE_AGENT"})
        self.assertEqual(a2a["schemaVersion"], 2)
        self.assertEqual(len(a2a["roles"]["SINGLE_AGENT"]["contexts"]), 1)
        self.assertEqual(len(a2a["roles"]["SINGLE_AGENT"]["tasks"]), 4)
        self.assertTrue(handoff["coverage"]["a2aComplete"], handoff["coverage"])
        self.assertEqual(handoff["orchestratorVerdict"], "SUCCESS")
        self.assertTrue(handoff["readyForFinalEvaluation"])
        self.assertEqual([step["phase"] for step in handoff["steps"]], ["PLANNING", "IMPLEMENTING", "QA", "SECURITY"])
        self.assertEqual({step["agentIdentity"] for step in handoff["steps"]}, {"SINGLE_AGENT"})
        self.assertEqual(bundle["eventsTotal"], len(bundle["events"]))
        self.assertEqual(trace["telemetryTotal"], len(trace["telemetry"]))
        self.assertEqual(bundle["usage"]["total"], 7)
        self.assertEqual(metrics["modelCalls"], 7)
        self.assertEqual(metrics["totalTokens"], bundle["usage"]["summary"]["totalTokens"])
        self.assertIsNone(metrics["costUsd"])  # no approved operational price
        self.assertFalse(metrics["costComplete"])
        for name, identity in handoff["files"].items():
            self.assertEqual(sha256(files[name].read_bytes()).hexdigest(), identity["sha256"])
            self.assertEqual(files[name].stat().st_mode & 0o777, 0o600)
        manifest = ExecutionManifest.from_payload(json.loads(files["v1/manifest.json"].read_text()))
        self.assertEqual(manifest.snapshot_sha256, sha256(files["v1/source.tar"].read_bytes()).hexdigest())
        self.assertEqual(manifest.code_version, 1)

    async def test_missing_store_no_snapshot_and_existing_output_are_explicit(self):
        provider = self.base.provider()
        provider.script = [RuntimeError("inert refusal")]
        async with self.base.running(provider=provider) as (client, _):
            run_id = await self.base.submit(client)
        files = write_evaluation_export(self.f.repository, run_id, self.f.directory / "no-source", stores={})
        handoff = json.loads(files["handoff.json"].read_text())
        self.assertEqual(handoff["sourceStatus"], "SNAPSHOT_NOT_FOUND")
        self.assertFalse(handoff["readyForFinalEvaluation"])
        self.assertFalse(handoff["coverage"]["a2aComplete"])
        self.assertEqual(handoff["coverage"]["missingStores"], ["SINGLE_AGENT"])
        self.assertEqual(json.loads(files["run.json"].read_text())["exportedCodeVersions"], [])
        before = files["handoff.json"].read_bytes()
        with self.assertRaises(EvaluationExportError) as failure:
            write_evaluation_export(self.f.repository, run_id, self.f.directory / "no-source", stores={})
        self.assertEqual(failure.exception.code, "EXPORT_EXISTS")
        self.assertEqual(files["handoff.json"].read_bytes(), before)

    async def test_identity_mixed_stores_wrong_run_and_private_workspace_output(self):
        async with self.base.running() as (client, _):
            run_id = await self.base.submit(client)
        path = self.f.directory / "single.sqlite3"
        with self.assertRaises(A2AExportError) as failure:
            export_a2a_messages({"QA": path}, run_id)
        self.assertEqual(failure.exception.code, "STORE_ROLE_MISMATCH")
        with self.assertRaises(A2AExportError) as failure:
            export_a2a_messages({"QA": path, "SINGLE_AGENT": path}, run_id)
        self.assertEqual(failure.exception.code, "STORE_ARCHITECTURE_MISMATCH")
        other = export_a2a_messages({"SINGLE_AGENT": path}, uuid4())
        self.assertEqual(other["roles"]["SINGLE_AGENT"]["tasks"], [])
        multi = export_a2a_messages({}, run_id)
        self.assertEqual(set(multi), {"runId", "roles", "missingRoles"})
        self.assertEqual(set(multi["missingRoles"]), {"PLANNER", "DEVELOPER", "QA", "SECURITY"})
        workspace = self.f.repository.get_workspace(self.f.repository.get_run(run_id).workspace_id)
        target = Path(workspace.root_path) / "heldout-data"
        with self.assertRaises(EvaluationExportError) as failure:
            write_evaluation_export(self.f.repository, run_id, target, stores={"SINGLE_AGENT": path})
        self.assertEqual(failure.exception.code, "EXPORT_INSIDE_AGENT_WORKSPACE")
        self.assertFalse(target.exists())

    async def test_corrupt_registered_snapshot_fails_without_partial_export(self):
        async with self.base.running() as (client, _):
            run_id = await self.base.submit(client)
        # Read corruption fixture: do not change append-only application controls.
        with patch("agents.platform.snapshot_export.SQLiteArtifactContentStore._decode", side_effect=ValueError()):
            with self.assertRaises(EvaluationExportError) as failure:
                self.export(run_id)
        self.assertEqual(failure.exception.code, "SNAPSHOT_INTEGRITY_ERROR")
        self.assertFalse((self.f.directory / "handoff").exists())

    async def test_approved_synthetic_prices_are_pinned_before_single_calls(self):
        model = self.f.model.model_copy(update={"provider": "openai", "model_id": "fixture-model"})
        self.f.model = model
        self.f.configuration = self.f.configuration.model_copy(update={"model": model})
        self.f.settings = {role: value.model_copy(update={"llm_provider": "openai", "llm_model_id": "fixture-model"})
                           for role, value in self.f.settings.items()}
        provider = self.base.provider()
        provider.name = "openai"
        original = provider.complete
        async def complete(request):
            response = await original(request)
            return replace(response, usage=replace(response.usage, cached_input_tokens=0),
                model_id="fixture-model", reported_service_tier="default",
                billing_context="OPENAI_RESPONSES_GLOBAL_TEXT")
        provider.complete = complete
        path = self.f.directory / "synthetic-prices.json"
        path.write_text(json.dumps(accounting.synthetic_prices()))
        async with self.base.running(provider=provider, price_book=load_price_book(path)) as (client, _):
            run_id = await self.base.submit(client)
            response = await client.get(f"/api/v1/runs/{run_id}/usage")
            self.assertEqual(response.status_code, 200, response.text)
            self.assertTrue(response.json()["summary"]["costComplete"], response.json()["summary"])
            self.assertEqual({row["binding"]["role"] for row in response.json()["records"]}, {"SINGLE_AGENT"})
        result = collect_run_metrics(self.f.repository, run_id)
        self.assertTrue(result["costComplete"])
        self.assertIsNotNone(result["costUsd"])
        self.assertEqual(result["modelCalls"], 7)


class SingleFailureExportTests(unittest.IsolatedAsyncioTestCase):
    async def test_wait_resume_preserves_a2a_coverage_and_actual_outcome(self):
        harness = controls._Controls(self)
        harness.interrupt(controls.AgentRole.QA)
        async with harness.running() as client:
            run_id = await controls.SingleAgentControlsTests.start(self, harness, client)
            before = harness.step(run_id, controls.AgentRole.QA)
            files = write_evaluation_export(harness.f.repository, run_id, harness.f.directory / "waiting",
                stores={"SINGLE_AGENT": harness.f.directory / "single.sqlite3"})
            self.assertFalse(json.loads(files["handoff.json"].read_text())["readyForFinalEvaluation"])
            await controls.SingleAgentControlsTests.answer(self, harness, client, run_id, before)
            files = write_evaluation_export(harness.f.repository, run_id, harness.f.directory / "resumed",
                stores={"SINGLE_AGENT": harness.f.directory / "single.sqlite3"})
            handoff = json.loads(files["handoff.json"].read_text())
            self.assertTrue(handoff["coverage"]["a2aComplete"], handoff["coverage"])
            self.assertEqual(handoff["orchestratorVerdict"], "SUCCESS")
            self.assertGreater(json.loads(files["metrics.json"].read_text())["humanWaitMs"], 0)

    async def test_cancelled_run_without_snapshot_is_retained(self):
        harness = controls._Controls(self)
        harness.interrupt(controls.AgentRole.PLANNER)
        async with harness.running() as client:
            run_id = await controls.SingleAgentControlsTests.start(self, harness, client)
            response = await client.post(f"/api/v1/runs/{run_id}/cancel", json={"reason": "inert export fixture cancellation"})
            self.assertEqual(response.status_code, 200, response.text)
        files = write_evaluation_export(harness.f.repository, run_id, harness.f.directory / "cancelled",
            stores={"SINGLE_AGENT": harness.f.directory / "single.sqlite3"})
        handoff = json.loads(files["handoff.json"].read_text())
        self.assertEqual(handoff["runStatus"], "ABORTED")
        self.assertTrue(handoff["isTerminal"])
        self.assertFalse(handoff["finalSourceAvailable"])
        self.assertFalse(handoff["readyForFinalEvaluation"])
        self.assertTrue(json.loads(files["metrics.json"].read_text())["timingComplete"])

    async def test_all_four_candidates_and_failure_are_preserved(self):
        harness = fixing._SingleFixHarness(self, qa_mode="distinct-failures")
        async with harness.running() as (client, _):
            response = await asyncio.wait_for(client.post("/api/v1/runs", json=harness.f.submission()), timeout=50)
            self.assertEqual(response.status_code, 201, response.text)
            run_id = response.json()["run"]["runId"]
        files = write_evaluation_export(harness.f.repository, run_id, harness.f.directory / "failed-handoff",
            stores={"SINGLE_AGENT": harness.f.directory / "single.sqlite3"})
        summary = json.loads(files["run.json"].read_text())
        self.assertEqual(summary["exportedCodeVersions"], [1, 2, 3, 4])
        self.assertEqual(summary["verdict"], "FAIL")
        metrics = json.loads(files["metrics.json"].read_text())
        self.assertEqual(metrics["fixAttempts"], 3)
        self.assertEqual(metrics["codeVersion"], 4)
        a2a = json.loads(files["a2a-messages.json"].read_text())
        self.assertEqual(len(a2a["roles"]["SINGLE_AGENT"]["contexts"]), 1)
        for version in range(1, 5):
            manifest = ExecutionManifest.from_payload(json.loads(files[f"v{version}/manifest.json"].read_text()))
            self.assertEqual(manifest.snapshot_sha256, sha256(files[f"v{version}/source.tar"].read_bytes()).hexdigest())


class MetricsContractTests(unittest.TestCase):
    def test_consumer_expected_roles_follow_actual_architecture(self):
        self.assertEqual(expected_a2a_roles("SINGLE_AGENT"), ("SINGLE_AGENT",))
        self.assertEqual(set(expected_a2a_roles("MULTI_AGENT")), {"PLANNER", "DEVELOPER", "QA", "SECURITY"})
        with self.assertRaises(A2AExportError):
            expected_a2a_roles("unknown")

    def test_operator_cli_preserves_source_absence_and_rejects_overwrite(self):
        base = accounting.PricedTelemetryTests("runTest")
        base.setUp()
        self.addCleanup(base.doCleanups)
        arguments = ["--database", str(base.repository.database_path), "--run-id", str(base.run.run_id),
                     "--out", str(base.directory / "cli-handoff")]
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()) as errors:
            self.assertEqual(main(arguments), 0)
            self.assertEqual(main(arguments), 1)
        self.assertEqual(errors.getvalue(), "EXPORT_EXISTS\n")
        handoff = json.loads((base.directory / "cli-handoff/handoff.json").read_text())
        self.assertEqual(handoff["architecture"], "MULTI_AGENT")
        self.assertEqual(handoff["sourceStatus"], "SNAPSHOT_NOT_FOUND")
        self.assertFalse(handoff["consistency"]["crossDatabaseAtomic"])

    def test_wall_clock_waiting_retry_dedup_and_unknown_measurements(self):
        start = datetime(2026, 11, 1, tzinfo=timezone.utc)
        def event(kind, seconds, state):
            return {"eventType": kind, "occurredAt": (start + timedelta(seconds=seconds)).isoformat(), "workflowState": state}
        bundle = {"run": {"runId": str(uuid4()), "status": "FINISHED", "verdict": "FAIL", "fixAttempt": 3, "codeVersion": 4},
            "configuration": {"configuration": {"architecture": "SINGLE_AGENT"}}, "usage": None, "issues": [{}, {}],
            "events": [event("RUN_STARTED", 0, "PLANNING"), event("BUILD_PASSED", 2, "IMPLEMENTING"),
                event("WORKFLOW_STATE_CHANGED", 3, "WAITING_INPUT"), event("WORKFLOW_STATE_CHANGED", 8, "QA_RUNNING"),
                event("VERDICT_CREATED", 10, "FINISHED")]}
        retry = {"binding": {"eventType": "MCP_TOOL_CALLED"}, "detail": {"logicalCallId": "one", "toolAttempt": 1}}
        result = metrics_from_records(bundle, {"telemetry": [retry, retry]}, now=start + timedelta(seconds=100))
        self.assertEqual(result["wallClockMs"], 10000)
        self.assertEqual(result["firstBuildPassMs"], 2000)
        self.assertEqual(result["humanWaitMs"], 5000)
        self.assertEqual(result["recordedToolRetries"], 1)
        self.assertIsNone(result["totalTokens"])
        self.assertIsNone(result["costUsd"])
        bundle["run"].update(status="WAITING_INPUT", verdict=None)
        result = metrics_from_records(bundle, {"telemetry": None}, now=start + timedelta(seconds=100))
        self.assertIsNone(result["wallClockMs"])
        self.assertFalse(result["timingComplete"])
        self.assertTrue(result["humanWaitOpen"])
        bundle["events"] = []
        self.assertIsNone(metrics_from_records(bundle, {"telemetry": None})["humanWaitMs"])

    def test_all_pages_are_read_and_truncation_is_an_error(self):
        values = list(range(1101))
        def loader(limit, offset):
            return values[offset:offset + limit], len(values)
        self.assertEqual(_paged(loader), (values, len(values)))
        with self.assertRaises(RunRecordsError):
            _paged(lambda **kwargs: ([], 1))
        with self.assertRaises(RunRecordsError):
            _paged(loader, key_limit=1000)

    def test_readonly_view_forbids_schema_and_mutation(self):
        base = accounting.PricedTelemetryTests("runTest")
        base.setUp()
        self.addCleanup(base.doCleanups)
        view = ReadOnlyWorkflowRepository(base.repository.database_path)
        before = database_hash(view.database_path)
        with view.snapshot():
            with view._connection() as connection:
                with self.assertRaises(sqlite3.OperationalError):
                    connection.execute("CREATE TABLE unexpected(value TEXT)")
            with self.assertRaises(ValueError):
                with view._transaction():
                    pass
        self.assertEqual(before, database_hash(view.database_path))


if __name__ == "__main__":
    unittest.main()
