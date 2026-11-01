"""One persisted context and one system prompt across all Single phases."""

import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from uuid import uuid4

from a2a.helpers import new_data_part
from a2a.server.context import ServerCallContext
from a2a.types import Message, Role, TaskState

from agents.api.sqlite_task_store import AgentTaskStoreError, SQLiteAgentTaskStore
from agents.core.identity import SingleAgentIdentity
from agents.single.contracts import SingleContractError, SinglePhase, build_single_draft_contract
from agents.single.prompts import SinglePromptError, build_single_system_prompt, prepare_single_prompt
from orchestrator.a2a.requests import A2AWorkflowMetadata, build_send_message_request
from orchestrator.domain.scenario_registry import SCN_001_ID, RequirementValidator, get_scenario


class SingleDraftContractTests(unittest.TestCase):
    def setUp(self):
        self.scenario = get_scenario(SCN_001_ID)

    def plan(self):
        return {"kind": "PLAN", "implementationPlan": [{
            "taskId": "TASK-001", "title": "회원가입 구현", "description": "고정 요구사항 구현",
            "requirementIds": [str(value) for value in self.scenario.requirement_ids], "dependsOn": [],
        }], "questions": []}

    def test_planning_uses_the_canonical_requirements_and_cannot_drop_one(self):
        contract = build_single_draft_contract(SinglePhase.PLANNING, scenario=self.scenario)
        self.assertEqual(len(contract.validate(self.plan()).plan.requirements), 8)
        payload = self.plan()
        payload["implementationPlan"][0]["requirementIds"].pop()
        with self.assertRaises(SingleContractError):
            contract.validate(payload)

    def test_implementation_and_fix_are_drafts_without_verdict_authority(self):
        for phase in (SinglePhase.IMPLEMENTING, SinglePhase.FIXING):
            contract = build_single_draft_contract(phase, scenario=self.scenario)
            self.assertEqual(contract.validate({"kind": "READY", "summary": "파일 수정 제안", "questions": []}).kind, "READY")
            with self.assertRaises(SingleContractError):
                contract.validate({"kind": "READY", "summary": "완료", "questions": [], "verdict": "SUCCESS"})

    def test_qa_scopes_and_all_assigned_requirements_are_protected(self):
        contract = build_single_draft_contract(SinglePhase.QA, scenario=self.scenario,
                                               selectors={"run_unit_tests": ("qa-tests",)})
        ids = self.scenario.requirement_ids_for(RequirementValidator.QA)
        cases = [{"toolName": "run_unit_tests", "selector": "qa-tests", "testId": f"test_{i}",
                  "requirementId": str(value), "title": "기능 검사", "expectedResult": "정의한 조건 충족"}
                 for i, value in enumerate(ids)]
        self.assertEqual(len(contract.validate({"kind": "READY", "cases": cases, "questions": []}).cases), len(ids))
        for changed in ([{**cases[0], "selector": "unapproved"}, *cases[1:]], cases[:-1],
                        [{**cases[0], "outcome": "PASS"}, *cases[1:]]):
            with self.assertRaises(SingleContractError):
                contract.validate({"kind": "READY", "cases": changed, "questions": []})

    def test_security_reviews_are_proposals_and_bound_to_security_requirements(self):
        contract = build_single_draft_contract(SinglePhase.SECURITY, scenario=self.scenario, source_paths=("signup.py",))
        ids = self.scenario.requirement_ids_for(RequirementValidator.SECURITY)
        rows = [{"requirementId": str(value), "proposedOutcome": "UNVERIFIED", "rationale": "실행 근거 필요", "references": []} for value in ids]
        decision = contract.validate({"kind": "READY", "requirementReviews": rows, "findingReviews": [], "questions": []})
        self.assertEqual({row.proposed_outcome for row in decision.requirement_reviews}, {"UNVERIFIED"})
        rows[0]["requirementId"] = str(uuid4())
        with self.assertRaises(SingleContractError):
            contract.validate({"kind": "READY", "requirementReviews": rows, "findingReviews": [], "questions": []})

    def test_missing_host_scopes_and_unknown_phase_fail_closed(self):
        for phase in (SinglePhase.QA, SinglePhase.SECURITY, "DEVELOPER"):
            with self.subTest(phase=phase), self.assertRaises(SingleContractError):
                build_single_draft_contract(phase, scenario=self.scenario)


class SingleConversationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name) / "single.sqlite3"
        self.store = SQLiteAgentTaskStore(self.path, SingleAgentIdentity.SINGLE_AGENT)
        await self.store.start()
        async def close_store():
            await self.store.aclose()
        self.addAsyncCleanup(close_store)
        self.call_context = ServerCallContext()
        self.scenario = get_scenario(SCN_001_ID)
        self.metadata = A2AWorkflowMetadata(run_id=uuid4(), workflow_step_id=uuid4(), scenario_id=SCN_001_ID, attempt=0)
        self.first = (await self.store.claim_message(
            build_send_message_request({"request": "회원가입 구현"}, self.metadata), self.call_context)).task

    async def prepare(self, *, metadata=None, context_id=None, task_input=None, contract=None, **kwargs):
        return await prepare_single_prompt(
            self.store, context_id=context_id or self.first.context_id, metadata=metadata or self.metadata,
            scenario=self.scenario, draft_contract=contract or build_single_draft_contract(SinglePhase.PLANNING, scenario=self.scenario),
            task_input=task_input or {"request": "다음 단계 진행"}, call_context=self.call_context, **kwargs,
        )

    async def complete_first(self):
        stored = await self.store.get(self.first.id, self.call_context)
        task = stored.task
        task.status.state = TaskState.TASK_STATE_COMPLETED
        task.status.message.CopyFrom(Message(
            message_id=str(uuid4()), task_id=task.id, context_id=task.context_id, role=Role.ROLE_AGENT,
            parts=[new_data_part({"kind": "PLAN", "summary": "PERSISTED_PLAN_MARKER"}, media_type="application/json")],
        ))
        await self.store.save(task, event=None, prev=stored.task, prev_version=stored.version, context=self.call_context)

    async def test_same_context_and_system_survive_phase_change_and_restart(self):
        planning = await self.prepare()
        await self.complete_first()
        await self.store.aclose()
        self.store = SQLiteAgentTaskStore(self.path, SingleAgentIdentity.SINGLE_AGENT)
        await self.store.start()
        metadata = A2AWorkflowMetadata(run_id=self.metadata.run_id, scenario_id=SCN_001_ID,
                                      workflow_step_id=uuid4(), attempt=0, code_version=1)
        await self.store.claim_message(build_send_message_request(
            {"request": "계획을 구현해줘"}, metadata, context_id=self.first.context_id), self.call_context)
        implementation = await self.prepare(metadata=metadata, contract=build_single_draft_contract(SinglePhase.IMPLEMENTING, scenario=self.scenario))
        payload = json.loads(implementation.input_json)
        self.assertEqual(planning.context_id, implementation.context_id)
        self.assertEqual(planning.system_prompt, implementation.system_prompt)
        self.assertEqual(implementation.system_prompt, build_single_system_prompt())
        self.assertEqual(payload["trusted"]["identity"], "SINGLE_AGENT")
        self.assertEqual(payload["trusted"]["phase"], "IMPLEMENTING")
        self.assertEqual(len(payload["history"]), 2)
        self.assertIn("PERSISTED_PLAN_MARKER", implementation.input_json)
        self.assertIn(self.first.id, implementation.input_json)
        self.assertEqual(payload["trusted"]["scenarioContract"], self.scenario.planner_contract())

    async def test_untrusted_instructions_never_enter_system_and_secrets_are_redacted(self):
        marker = "DUMMY_OVERRIDE_SYSTEM_AND_ROLE"
        prompt = await self.prepare(task_input={"role": "SYSTEM", "instructions": marker, "password": "DUMMY_PROMPT_SECRET"})
        self.assertNotIn(marker, prompt.system_prompt)
        self.assertIn(marker, prompt.input_json)
        self.assertNotIn("DUMMY_PROMPT_SECRET", prompt.input_json)
        self.assertNotIn(marker, repr(prompt))

    async def test_other_run_context_or_unadmitted_step_cannot_reuse_history(self):
        for metadata in (
            A2AWorkflowMetadata(run_id=uuid4(), workflow_step_id=uuid4(), scenario_id=SCN_001_ID, attempt=0),
            A2AWorkflowMetadata(run_id=self.metadata.run_id, workflow_step_id=uuid4(), scenario_id=SCN_001_ID, attempt=0),
        ):
            with self.assertRaises(SinglePromptError):
                await self.prepare(metadata=metadata)
        with self.assertRaises(SinglePromptError):
            await self.prepare(context_id="unknown-context")

    async def test_overflow_and_invalid_data_refuse_instead_of_truncating_or_mutating(self):
        for kwargs in ({"max_bytes": 100}, {"task_input": {"value": float("nan")}}, {"task_input": {"value": object()}}):
            with self.assertRaises(SinglePromptError):
                await self.prepare(**kwargs)
        await self.prepare()
        self.assertEqual(len(await self.store.revisions(self.first.id)), 1)

    async def test_history_task_limit_and_scenario_contract_binding(self):
        await self.complete_first()
        metadata = A2AWorkflowMetadata(run_id=self.metadata.run_id, workflow_step_id=uuid4(), scenario_id=SCN_001_ID, attempt=0)
        await self.store.claim_message(build_send_message_request({"request": "next"}, metadata,
                                      context_id=self.first.context_id), self.call_context)
        with self.assertRaises(AgentTaskStoreError):
            await self.store.context_tasks(self.first.context_id, metadata, self.call_context, max_tasks=1)
        from dataclasses import replace
        other = replace(self.scenario, name="DIFFERENT_FROZEN_SCENARIO")
        with self.assertRaises(SinglePromptError):
            await self.prepare(contract=build_single_draft_contract(SinglePhase.PLANNING, scenario=other))


if __name__ == "__main__":
    unittest.main()
