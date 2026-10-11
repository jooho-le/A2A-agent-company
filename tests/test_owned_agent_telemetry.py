"""Owned four-Agent accounting over official HTTP, with inert model/CLI seams.

The real Dispatcher, receipts, Git, SQLite and MCP boundaries execute. Neither
an external LLM nor Docker nor generated product code executes on the Host.
Fixtures are composed, not inherited, so their test methods are not collected.
"""

import asyncio
from dataclasses import replace
import json
from pathlib import Path
import unittest
from unittest.mock import patch
from uuid import UUID, uuid4

from jsonschema import Draft202012Validator, FormatChecker

import test_owned_agent_pipeline as pipeline_fixture
from agents.llm.contracts import LLMErrorCode
from agents.platform.budgets import RunBudgetError, RunBudgetRegistry
from agents.platform.telemetry_store import AgentTelemetryStore
from orchestrator.domain import A2ATaskState, AgentRole, WorkflowStatus
from test_llm_runtime import FakeProvider, text_response


def _question(*, usage="measured"):
    return text_response(json.dumps({"kind": "INPUT_REQUIRED",
        "questions": ["동결 요구사항을 유지한 채 계속할까요?"], "implementationPlan": []},
        ensure_ascii=False), usage=usage)


class OwnedAgentTelemetryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.base = pipeline_fixture.OwnedAgentPipelineTests()
        self.base.setUp()
        for function, arguments, keywords in self.base._cleanups:
            self.addCleanup(function, *arguments, **keywords)
        self.base._cleanups.clear()

    async def submit(self, client, *, body=None):
        response = await asyncio.wait_for(client.post("/api/v1/runs",
            json=self.base.submission() if body is None else body), 25)
        self.assertEqual(response.status_code, 201, response.text)
        return UUID(response.json()["run"]["runId"])

    async def get_accounting(self, client, run_id):
        usage = await client.get(f"/api/v1/runs/{run_id}/usage")
        telemetry = await client.get(f"/api/v1/runs/{run_id}/telemetry")
        self.assertEqual(usage.status_code, 200, usage.text)
        self.assertEqual(telemetry.status_code, 200, telemetry.text)
        return usage.json(), telemetry.json()

    def stored_runtime_json(self):
        with self.base.repository._connection() as connection:
            rows = connection.execute("SELECT payload_json FROM agent_runtime_events").fetchall()
        return "\n".join(row[0] for row in rows)

    def assert_binding(self, binding, step):
        self.assertEqual(binding["runId"], str(step.run_id))
        self.assertEqual(binding["workflowStepId"], str(step.workflow_step_id))
        self.assertEqual(binding["a2aTaskId"], step.a2a_task_id)
        self.assertEqual(binding["agentContextId"], step.agent_context_id)
        self.assertEqual(binding["attempt"], step.attempt)
        self.assertEqual(binding.get("codeVersion"), step.code_version)
        self.assertEqual(binding.get("requirementIds", []), [str(value) for value in step.requirement_ids])
        self.assertEqual(binding.get("inputArtifactIds", []), [str(value) for value in step.input_artifact_ids])

    async def test_actual_four_roles_account_six_models_five_tools_and_physical_attempts_once(self):
        async with self.base.running_platform() as (client, providers):
            run_id = await self.submit(client)
            usage, telemetry = await self.get_accounting(client, run_id)
            budget = self.base.platform.budgets.resolve(self.base.repository.get_run_configuration(run_id))
            self.assertEqual((budget.model_calls, budget.tool_calls, budget.total_tokens), (6, 5, 90))
            self.assertEqual(sum(len(provider.requests) for provider in providers.values()), 6)
            self.assertEqual(usage["total"], 6)
            self.assertEqual({record["sequence"] for record in usage["records"]}, set(range(1, 7)))
            self.assertEqual({record["role"] for record in usage["records"]}, {role.value for role in AgentRole})
            self.assertEqual({key: usage["summary"][key] for key in
                ("modelCalls", "toolCalls", "recordedModelCalls", "knownTotalTokens", "totalTokens")},
                {"modelCalls": 6, "toolCalls": 5, "recordedModelCalls": 6,
                 "knownTotalTokens": 90, "totalTokens": 90})
            self.assertTrue(usage["summary"]["usageComplete"])
            self.assertIsNone(usage["summary"]["costUsd"])
            self.assertFalse(usage["summary"]["executionBlocked"])
            self.assertEqual(usage["summary"]["pendingModelSequences"], [])
            self.assertEqual(usage["summary"]["pendingToolSequences"], [])
            steps = {step.agent_role.value: step for step in self.base.repository.list_steps(run_id)}
            for record in usage["records"]:
                self.assert_binding(record["binding"], steps[record["role"]])
                self.assertEqual(record["outcome"], "completed")
                self.assertTrue(record["usageKnown"])
                self.assertEqual(record["totalTokens"], 15)
                self.assertIsNone(record["costUsd"])
            events, _total = self.base.repository.list_events(run_id, limit=500, offset=0)
            self.assertEqual(sum(event.event_type == "LLM_MODEL_CALLED" for event in events), 6)
            self.assertEqual(sum(event.event_type == "LLM_CALL_FINISHED" for event in events), 6)
            self.assertEqual(sum(event.event_type == "MCP_TOOL_CALLED" for event in events), 5)
            self.assertEqual(sum(event.event_type == "MCP_TOOL_FINISHED" for event in events), 5)
            physical = [row for row in telemetry["events"] if row["kind"] == "MCP"]
            self.assertEqual(len(physical), 10)
            self.assertEqual({row["toolName"] for row in physical},
                {"write_source_file", "run_build", "write_test_file", "run_unit_tests", "run_security_scan"})
            pairs = {(row["logicalCallId"], row["toolAttempt"], row["binding"]["eventType"])
                     for row in physical}
            self.assertEqual(len(pairs), 10)
            # All canonical Trace payloads remain closed; accounting is adjacent.
            schema = json.loads((Path(__file__).resolve().parents[1] /
                "schemas/project/trace_event.schema.json").read_text(encoding="utf-8"))
            validator = Draft202012Validator(schema, format_checker=FormatChecker())
            for event in events:
                validator.validate(event.to_trace_json())
            for row in telemetry["events"]:
                if row["kind"] == "MCP":
                    self.assert_binding({**row["binding"], "attempt": row["workflowAttempt"]},
                                        steps[row["binding"]["actor"]])
            # Late report ingestion must not duplicate physically recorded events.
            before = (len(events), telemetry["total"])
            for artifact in self.base.repository.list_project_artifacts(run_id):
                if artifact.artifact_type == "QA_REPORT":
                    evidences = [test.tool_evidence for test in artifact.tests if test.tool_evidence is not None]
                    self.base.repository.ingest_tool_evidence(run_id, artifact.workflow_step_id, evidences)
                elif artifact.artifact_type == "SECURITY_REPORT":
                    evidences = [result.tool_evidence for result in artifact.requirement_results
                                 if result.tool_evidence is not None]
                    self.base.repository.ingest_tool_evidence(run_id, artifact.workflow_step_id, evidences)
            after_events, _ = self.base.repository.list_events(run_id, limit=500, offset=0)
            _, after_telemetry = await self.get_accounting(client, run_id)
            self.assertEqual((len(after_events), after_telemetry["total"]), before)

    async def test_pagination_preserves_full_summary_and_omits_prompt_source_and_provider_prose(self):
        providers = self.base.providers()
        original = providers[AgentRole.PLANNER].script[0]
        providers[AgentRole.PLANNER] = FakeProvider(replace(original,
            model_id="private provider prose password=untrusted-provider-secret"))
        body = self.base.submission()
        body["requestText"] = "작업 데이터 전용 비밀 아닌 원문 sentinel-not-for-telemetry를 구현해줘."
        async with self.base.running_platform(providers) as (client, _):
            run_id = await self.submit(client, body=body)
            complete, telemetry = await self.get_accounting(client, run_id)
            response = await client.get(f"/api/v1/runs/{run_id}/usage?limit=2&offset=1")
            self.assertEqual(response.status_code, 200, response.text)
            page = response.json()
            self.assertEqual((page["total"], page["limit"], page["offset"], len(page["records"])), (6, 2, 1, 2))
            self.assertEqual(page["summary"], complete["summary"])
            self.assertEqual(page["records"], complete["records"][1:3])
            tail = await client.get(f"/api/v1/runs/{run_id}/telemetry?limit=1&offset=9999")
            self.assertEqual((tail.status_code, tail.json()["events"], tail.json()["total"]),
                             (200, [], telemetry["total"]))
            raw = json.dumps((complete, telemetry), ensure_ascii=False) + self.stored_runtime_json()
            for forbidden in ("sentinel-not-for-telemetry", "untrusted-provider-secret", "private provider prose",
                              "def signup()", "generated-inert-candidate", "inert fixture; not executed on Host",
                              "input_items_json", "system_prompt", "output_items_json", ".env"):
                self.assertNotIn(forbidden, raw)
            planner = next(record for record in complete["records"] if record["role"] == "PLANNER")
            self.assertIsNone(planner["reportedModelId"])

    async def test_unknown_usage_stays_null_and_blocks_durable_recovery_without_new_calls(self):
        providers = self.base.providers()
        providers[AgentRole.PLANNER] = FakeProvider(_question(usage=None))
        async with self.base.running_platform(providers) as (client, _):
            run_id = await self.submit(client)
            self.assertIs(self.base.repository.get_run(run_id).status, WorkflowStatus.WAITING_INPUT)
            usage, _telemetry = await self.get_accounting(client, run_id)
            summary, record = usage["summary"], usage["records"][0]
            self.assertEqual((summary["modelCalls"], summary["recordedModelCalls"], summary["knownTotalTokens"]),
                             (1, 1, 0))
            self.assertFalse(summary["usageComplete"])
            self.assertIsNone(summary["totalTokens"])
            self.assertTrue(summary["executionBlocked"])
            self.assertIsNone(summary["costUsd"])
            self.assertFalse(record["usageKnown"])
            self.assertTrue(all(record[key] is None for key in ("inputTokens", "outputTokens", "totalTokens", "costUsd")))
            restarted = RunBudgetRegistry(self.base.repository, limits=self.base.platform.budgets._limits,
                telemetry_store=AgentTelemetryStore(self.base.repository.database_path))
            with self.assertRaises(RunBudgetError):
                restarted.resolve(self.base.repository.get_run_configuration(run_id))
            self.assertEqual(len(providers[AgentRole.PLANNER].requests), 1)

    async def test_provider_failure_receipt_keeps_only_stable_code_not_private_error(self):
        providers = self.base.providers()
        providers[AgentRole.PLANNER] = FakeProvider(RuntimeError(
            "private-provider-error-prose password=private-password-value api_key=private-api-key-value"))
        async with self.base.running_platform(providers) as (client, _):
            run_id = await self.submit(client)
            usage, telemetry = await self.get_accounting(client, run_id)
            self.assertIs(self.base.repository.get_run(run_id).status, WorkflowStatus.HUMAN_REVIEW)
            self.assertEqual(usage["total"], 1)
            self.assertEqual(usage["records"][0]["outcome"], LLMErrorCode.PROVIDER.value)
            self.assertIsNone(usage["summary"]["totalTokens"])
            raw = json.dumps((usage, telemetry)) + self.stored_runtime_json()
            for secret in ("private-provider-error-prose", "private-password-value", "private-api-key-value"):
                self.assertNotIn(secret, raw)

    async def test_working_cancel_persists_unknown_usage_before_run_aborted(self):
        entered, drained = asyncio.Event(), asyncio.Event()

        async def slow(_request):
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                drained.set()

        providers = self.base.providers()
        providers[AgentRole.PLANNER] = FakeProvider(slow)
        async with self.base.running_platform(providers) as (client, _):
            submission = asyncio.create_task(client.post("/api/v1/runs", json=self.base.submission()))
            try:
                await asyncio.wait_for(entered.wait(), 6)

                async def task_ready():
                    while True:
                        with self.base.repository._connection() as connection:
                            row = connection.execute("SELECT run_id FROM workflow_runs LIMIT 1").fetchone()
                        if row is not None:
                            run_id = UUID(row[0])
                            steps = self.base.repository.list_steps(run_id)
                            if any(step.a2a_task_id and step.a2a_task_state is A2ATaskState.WORKING for step in steps):
                                return run_id
                        await asyncio.sleep(.02)

                run_id = await asyncio.wait_for(task_ready(), 6)
                response = await asyncio.wait_for(client.post(f"/api/v1/runs/{run_id}/cancel",
                    json={"reason": "USER_CANCELLED"}), 6)
                self.assertEqual(response.status_code, 200, response.text)
                self.assertTrue(drained.is_set())
                self.assertEqual((response.json()["status"], response.json()["verdict"]), ("ABORTED", None))
                usage, _telemetry = await self.get_accounting(client, run_id)
                self.assertEqual((usage["total"], usage["records"][0]["outcome"]), (1, "canceled"))
                self.assertIsNone(usage["records"][0]["totalTokens"])
                self.assertTrue(usage["summary"]["executionBlocked"])
                self.assertEqual(usage["summary"]["pendingModelSequences"], [])
                events, _ = self.base.repository.list_events(run_id, limit=500, offset=0)
                kinds = [event.event_type for event in events]
                self.assertLess(kinds.index("LLM_CALL_FINISHED"), kinds.index("RUN_ABORTED"))
                self.assertEqual((await asyncio.wait_for(submission, 6)).status_code, 201)
            finally:
                if not submission.done():
                    submission.cancel()
                    with self.assertRaises(asyncio.CancelledError):
                        await submission

    async def test_measured_waiting_run_restores_same_utc_deadline_counters_and_ids_after_restart(self):
        providers = self.base.providers()
        providers[AgentRole.PLANNER] = FakeProvider(_question())
        async with self.base.running_platform(providers) as (client, _):
            run_id = await self.submit(client)
            configuration = self.base.repository.get_run_configuration(run_id)
            original = self.base.platform.budgets.resolve(configuration)
            before = original.snapshot
            steps = self.base.repository.list_steps(run_id)
            restarted = RunBudgetRegistry(self.base.repository, limits=original.limits,
                telemetry_store=AgentTelemetryStore(self.base.repository.database_path))
            restored = restarted.resolve(configuration)
            self.assertIsNot(restored, original)
            self.assertEqual((restored.model_calls, restored.tool_calls, restored.total_tokens), (1, 0, 15))
            self.assertEqual(restored.snapshot.deadline_utc, before.deadline_utc)
            self.assertEqual(restored.snapshot.created_at_utc, before.created_at_utc)
            self.assertEqual(restored.snapshot.accounted_usage, before.accounted_usage)
            self.assertLessEqual(restored.deadline_monotonic, original.deadline_monotonic + .005)
            self.assertGreater(restored.snapshot.revision, before.revision)
            self.assertEqual(self.base.repository.list_steps(run_id), steps)
            self.assertEqual(len(providers[AgentRole.PLANNER].requests), 1)
            usage, _ = await self.get_accounting(client, run_id)
            self.assertEqual((usage["summary"]["modelCalls"], usage["summary"]["totalTokens"]), (1, 15))

    async def test_input_continuation_usage_changes_attempt_not_task_context_or_fix_cycle(self):
        providers = self.base.providers()
        providers[AgentRole.PLANNER].script.insert(0, _question())
        async with self.base.running_platform(providers) as (client, _):
            run_id = await self.submit(client)
            before = self.base.repository.list_steps(run_id)[0]
            deadline = self.base.platform.budgets.resolve(self.base.repository.get_run_configuration(run_id)).snapshot.deadline_utc
            response = await asyncio.wait_for(client.post(f"/api/v1/runs/{run_id}/resume", json={
                "workflowStepId": str(before.workflow_step_id), "inputData": {"answer": "동결 기준을 유지하고 계속하세요."}}), 25)
            self.assertEqual(response.status_code, 200, response.text)
            usage, _ = await self.get_accounting(client, run_id)
            planner = [row for row in usage["records"] if row["role"] == "PLANNER"]
            self.assertEqual(len(planner), 2)
            self.assertEqual({row["binding"]["attempt"] for row in planner}, {0, 1})
            for row in planner:
                self.assertEqual((row["binding"]["workflowStepId"], row["binding"]["a2aTaskId"], row["binding"]["agentContextId"]),
                                 (str(before.workflow_step_id), before.a2a_task_id, before.agent_context_id))
            self.assertEqual((usage["summary"]["modelCalls"], usage["summary"]["toolCalls"], usage["summary"]["totalTokens"]),
                             (7, 5, 105))
            run = self.base.repository.get_run(run_id)
            self.assertEqual((run.fix_attempt, run.code_version), (0, 1))
            self.assertEqual(self.base.platform.budgets.resolve(self.base.repository.get_run_configuration(run_id)).snapshot.deadline_utc,
                             deadline)

    async def test_accounting_api_errors_are_safe_and_unknown_run_is_checked_first(self):
        providers = self.base.providers()
        providers[AgentRole.PLANNER] = FakeProvider(_question())
        async with self.base.running_platform(providers) as (client, _):
            run_id = await self.submit(client)
            app = self.base.platform.orchestrator_app
            with patch.object(app.state, "telemetry_store", None):
                for route in ("usage", "telemetry"):
                    absent = await client.get(f"/api/v1/runs/{run_id}/{route}")
                    unknown = await client.get(f"/api/v1/runs/{uuid4()}/{route}")
                    self.assertEqual((absent.status_code, absent.json()["detail"]),
                                     (503, "RUNTIME_TELEMETRY_NOT_CONFIGURED"))
                    self.assertEqual(unknown.status_code, 404)
            store = self.base.platform.telemetry_store
            for route, operation in (("usage", "list_usage"), ("telemetry", "list_events")):
                with patch.object(store, operation, side_effect=RuntimeError("secret SQL path password=api-error-secret")):
                    response = await client.get(f"/api/v1/runs/{run_id}/{route}")
                    self.assertEqual((response.status_code, response.json()["detail"]),
                                     (503, "RUNTIME_TELEMETRY_UNAVAILABLE"))
                    self.assertNotIn("api-error-secret", response.text)
            for query in ("limit=0", "limit=501", "offset=-1"):
                self.assertEqual((await client.get(f"/api/v1/runs/{run_id}/usage?{query}")).status_code, 422)


if __name__ == "__main__":
    unittest.main()
