"""Opt-in real LLM loop/SQLite journal with authored model and Docker fakes.

No cloud LLM, real Docker daemon, or generated Source runs on the Host. The
existing execution fixture supplies the actual Dispatcher/receipt boundary.
"""

import asyncio
import unittest

import test_llm_runtime as llm_helpers
import test_mcp_execution_runtime as execution_fixtures
from agents.llm.budget import ExecutionBudget, LLMLimits
from agents.llm.contracts import (
    LLMErrorCode, LLMRuntimeError, StructuredOutput, ToolCall, json_text, parse_json,
)
from agents.llm.engine import LLMEngine
from agents.roles.prompts import prepare_role_prompt
from mcp_tools.execution_store import ToolEvidenceStoreError
from orchestrator.a2a.requests import A2AWorkflowMetadata
from orchestrator.domain.run_configuration import ModelConfiguration
from orchestrator.domain.states import AgentRole
from orchestrator.domain.tool_evidence import ToolExecutionOutcome
from orchestrator.domain.retry_policy import RetryDecision


class TrackedLLMIntegrationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.runtime = execution_fixtures.ExecutionRuntimeTests("runTest")
        self.runtime.setUp()
        # Execute transferred callbacks in this active IsolatedAsyncio runner;
        # the reused fixture instance itself is not running a unittest loop.
        for cleanup, arguments, keywords in self.runtime._cleanups:
            self.addCleanup(cleanup, *arguments, **keywords)
        self.runtime._cleanups.clear()
        self.executor = self.runtime.executor
        self.role = AgentRole.DEVELOPER
        self.workspace_id = str(self.runtime.run.workspace_id)
        self.model = ModelConfiguration(
            provider="fake", model_id="fake-model", temperature=0, seed=17,
        )
        self.output = StructuredOutput(
            "role_analysis", llm_helpers.object_schema({"answer": {"type": "string"}}),
        )
        self.prompt = prepare_role_prompt(
            self.role, task_input={"request": "회원가입 기능 구현"},
            metadata=A2AWorkflowMetadata(
                run_id=self.runtime.run.run_id,
                workflow_step_id=self.runtime.fixture.step.workflow_step_id,
                scenario_id=self.runtime.run.scenario_id,
                attempt=self.runtime.fixture.step.attempt,
            ),
        )
        # This limit counts model-requested logical calls. Physical MCP
        # attempts are separately limited and recorded by the opt-in executor.
        self.budget = ExecutionBudget(
            runtime_budget_ms=10000,
            limits=LLMLimits(max_model_calls=2, max_tool_calls=1),
        )

    def provider(self, name="run_build", arguments=None):
        call = ToolCall("model-call-1", name, json_text(
            self.runtime.arguments if arguments is None else arguments,
        ))
        return llm_helpers.FakeProvider(
            llm_helpers.tool_response(call), llm_helpers.text_response(),
        )

    async def run_engine(self, provider):
        engine = LLMEngine(
            role=self.role, provider=provider, tools=self.executor.list_tools(),
            tool_executor=self.executor,
        )
        return await engine.run(
            prompt=self.prompt, model=self.model, output=self.output,
            budget=self.budget, workspace_id=self.workspace_id,
        )

    def record(self):
        self.assertEqual(len(self.executor.logical_call_ids), 1)
        return self.runtime.store.get(
            self.runtime.binding, self.executor.logical_call_ids[0],
        )

    async def assert_generic_tool_error(self, provider):
        with self.assertRaises(LLMRuntimeError) as caught:
            await self.run_engine(provider)
        self.assertEqual(caught.exception.code, LLMErrorCode.TOOL_FAILED)
        self.assertEqual(str(caught.exception), "LLM_TOOL_EXECUTION_FAILED")
        self.assertEqual(len(provider.requests), 1)
        self.assertEqual(self.budget.tool_calls, 1)
        self.assertEqual(len(caught.exception.records), 1)
        return self.record()

    async def test_model_build_call_has_real_private_receipt_and_structured_final(self):
        before = self.runtime.fixture.repository.get_run(self.runtime.run.run_id)
        provider = self.provider()
        result = await self.run_engine(provider)
        record = self.record()
        evidence = record.to_tool_evidence()
        self.assertEqual(result.data, {"answer": "done"})
        self.assertEqual((result.tool_calls, self.budget.tool_calls), (1, 1))
        self.assertEqual((self.budget.model_calls, len(result.records)), (2, 2))
        self.assertEqual(evidence.outcome, ToolExecutionOutcome.PASS)
        self.assertEqual(evidence.execution_manifest, self.runtime.fixture.snapshot.execution_manifest())
        attempt = record.attempts[0]
        self.assertIsNotNone(attempt.execution_id)
        self.assertIsNotNone(attempt.execution_manifest_id)
        self.assertEqual(len(attempt.receipt_refs), 2)
        receipt = self.runtime.outputs.get(self.runtime.run.run_id, attempt.execution_manifest_id)
        self.assertEqual(receipt.execution_id, attempt.execution_id)
        self.assertEqual(receipt.metadata_sha256, attempt.receipt_metadata_sha256)
        continuation = parse_json(provider.requests[1].input_items_json)[-1]
        self.assertEqual(continuation["type"], "function_call_output")
        self.assertEqual(continuation["call_id"], "model-call-1")
        self.assertEqual(parse_json(continuation["output"]), receipt.tool_output())
        self.assertEqual(self.runtime.store.read_evidence(
            self.runtime.binding, evidence.evidence_ref,
        ), record.to_dict())
        self.assertEqual(self.runtime.fixture.repository.get_run(self.runtime.run.run_id), before)
        self.assertEqual(len(self.runtime.session.calls), 1)

    async def test_two_busy_retries_are_three_attempts_but_one_llm_budget_call(self):
        self.runtime.session.errors = ["RESOURCE_BUSY", "RESOURCE_BUSY"]
        provider = self.provider()
        result = await self.run_engine(provider)
        evidence = self.record().to_tool_evidence()
        self.assertEqual(result.data, {"answer": "done"})
        self.assertEqual((result.tool_calls, self.budget.tool_calls), (1, 1))
        self.assertEqual(len(self.runtime.session.calls), 3)
        self.assertEqual(evidence.retries_used, 2)
        self.assertEqual([attempt.attempt for attempt in evidence.attempts], [0, 1, 2])
        self.assertEqual([attempt.outcome.value for attempt in evidence.attempts],
                         ["UNVERIFIED", "UNVERIFIED", "PASS"])
        self.assertEqual([attempt.error_kind for attempt in evidence.attempts],
                         ["RESOURCE_BUSY", "RESOURCE_BUSY", None])
        self.assertEqual(len(provider.requests), 2)
        self.assertEqual(self.budget.total_tokens, 30)

    async def test_busy_exhaustion_preserves_three_attempts_behind_generic_llm_error(self):
        self.runtime.session.errors = ["RESOURCE_BUSY"] * 4
        record = await self.assert_generic_tool_error(self.provider())
        evidence = record.to_tool_evidence()
        self.assertEqual(len(self.runtime.session.calls), 3)
        self.assertEqual(len(evidence.attempts), 3)
        self.assertTrue(evidence.retries_exhausted)
        self.assertEqual(record.attempts[-1].retry_decision, RetryDecision.DO_NOT_RETRY)
        self.assertEqual(evidence.outcome, ToolExecutionOutcome.UNVERIFIED)
        self.assertIsNone(self.runtime.fixture.repository.get_run(self.runtime.run.run_id).verdict)

    async def test_permanent_failure_keeps_safe_host_evidence_without_model_retry(self):
        self.runtime.session.errors = ["PERMISSION_DENIED"]
        record = await self.assert_generic_tool_error(self.provider())
        self.assertEqual(len(self.runtime.session.calls), 1)
        evidence = record.to_tool_evidence()
        self.assertEqual(evidence.attempts[0].error_kind, "PERMISSION_DENIED")
        self.assertFalse(evidence.attempts[0].can_retry)
        self.assertEqual(record.attempts[0].delivery_state, "REPLIED")
        self.assertEqual(self.runtime.store.read_evidence(
            self.runtime.binding, record.evidence_ref,
        ), record.to_dict())

    async def test_unknown_write_result_is_not_replayed_and_remains_host_only(self):
        async def lose_reply(*_):
            raise asyncio.TimeoutError()

        self.runtime.session.before_reply = lose_reply
        arguments = {"workspaceId": self.workspace_id, "path": "source/added.py",
                     "content": "# desired file\n"}
        record = await self.assert_generic_tool_error(self.provider("write_source_file", arguments))
        self.assertEqual(len(self.runtime.session.calls), 1)
        self.assertEqual((self.runtime.fixture.source / "added.py").read_text(), arguments["content"])
        attempt = record.attempts[0]
        self.assertEqual(attempt.error_kind, "WRITE_RESULT_UNKNOWN")
        self.assertEqual(attempt.retry_decision, RetryDecision.INSPECT_STATE)
        self.assertTrue(attempt.result_unknown)
        self.assertFalse(attempt.retry_safe)
        self.assertIsNone(record.execution_manifest)
        with self.assertRaises(ToolEvidenceStoreError):
            record.to_tool_evidence()
        journal = self.runtime.store.read_evidence(self.runtime.binding, record.evidence_ref)
        self.assertNotIn(arguments["content"], json_text(journal))
        self.assertNotIn("added.py", json_text(journal))


if __name__ == "__main__":
    unittest.main()
