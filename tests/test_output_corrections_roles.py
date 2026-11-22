"""Role wiring of bounded corrections: the real Planner agent, and Single/Multi parity.

Offline fake provider only. With corrections off the Planner fails exactly as
before; with the operator limit on, one value-free correction lets the model
fix a missing requirement link and the plan completes. Single's planning phase
and Multi's Planner produce identical feedback for the same draft, because both
use ``validate_planner_decision`` through the same ``contract_check``.
"""
import json
import unittest

from fixture_cleanup import set_up_borrowed

from agents.llm.budget import ExecutionBudget, LLMLimits
from agents.llm.corrections import contract_check
from agents.roles.planner_contract import validate_planner_decision
from agents.single.contracts import build_single_draft_contract
from agents.core.identity import SinglePhase
from test_llm_runtime import FakeProvider
import test_planner_agent as planner_fixture


class PlannerCorrectionTests(unittest.IsolatedAsyncioTestCase):
    # Borrow the Planner agent fixture without re-running its own test methods.
    def setUp(self):
        self.f = planner_fixture.PlannerAgentTests('test_constructor_is_inert_and_repr_safe')
        set_up_borrowed(self, self.f)
        self.scenario, self.draft, self.response = self.f.scenario, self.f.draft, self.f.response

    async def run_to(self, provider, state):
        return await self.f.run_to(provider, state)

    def broken_draft(self):
        draft = self.draft()
        draft['implementationPlan'][0]['requirementIds'].pop()  # drops REQ-008
        draft['implementationPlan'][0]['description'] = 'private-plan-prose-must-not-be-sent-back'
        return draft

    def limits(self, corrections, reminders=0):
        self.f.budget = ExecutionBudget(runtime_budget_ms=10000, limits=LLMLimits(
            max_model_calls=4, max_tool_calls=0, model_timeout_seconds=1, max_output_tokens=4096,
            max_output_corrections=corrections, max_question_reminders=reminders))

    def question(self):
        return self.draft(kind='INPUT_REQUIRED', questions=['가입 API 경로가 고정인가요?'])

    async def test_a_planner_question_pauses_unless_the_reminder_is_enabled(self):
        self.limits(2, reminders=0)
        provider = FakeProvider(self.response(self.question()), self.response())
        await self.run_to(provider, 'TASK_STATE_INPUT_REQUIRED')
        self.assertEqual(len(provider.requests), 1)
        self.limits(2, reminders=1)
        provider = FakeProvider(self.response(self.question()), self.response())
        task, _ = await self.run_to(provider, 'TASK_STATE_COMPLETED')
        self.assertEqual(len(provider.requests), 2)
        self.assertIn('QUESTIONS_NOT_ANSWERED', provider.requests[1].input_items_json)
        self.assertTrue(task.get('artifacts'))

    async def test_asking_again_after_the_reminder_still_pauses(self):
        self.limits(2, reminders=1)
        provider = FakeProvider(self.response(self.question()), self.response(self.question()))
        await self.run_to(provider, 'TASK_STATE_INPUT_REQUIRED')
        self.assertEqual(len(provider.requests), 2)

    async def test_without_the_limit_the_planner_fails_closed_as_before(self):
        self.limits(0)
        provider = FakeProvider(self.response(self.broken_draft()), self.response())
        task, _ = await self.run_to(provider, 'TASK_STATE_FAILED')
        self.assertEqual(task['status']['message']['parts'][0]['data']['code'], 'PLANNER_OUTPUT_INVALID')
        self.assertEqual(len(provider.requests), 1)

    async def test_one_correction_lets_the_planner_repair_a_missing_requirement(self):
        self.limits(2)
        provider = FakeProvider(self.response(self.broken_draft()), self.response())
        task, revisions = await self.run_to(provider, 'TASK_STATE_COMPLETED')
        self.assertEqual(len(provider.requests), 2)
        sent = json.loads(provider.requests[1].input_items_json)[-1]['content']
        correction = json.loads(sent)['hostCorrection']
        self.assertEqual(correction['code'], 'OUTPUT_CONTRACT_INVALID')
        self.assertIn({'fieldPath': correction['issues'][0]['fieldPath'], 'reasonCode': 'REQUIREMENT_NOT_COVERED',
                       'requirementKey': 'REQ-008'}, correction['issues'])
        self.assertNotIn('private-plan-prose', provider.requests[1].input_items_json)
        self.assertNotIn('private-plan-prose', json.dumps(revisions))
        self.assertTrue(task.get('artifacts'))
        self.assertEqual(len(self.f.usages), 2)  # both model calls are accounted

    async def test_the_same_planner_mistake_is_not_corrected_twice(self):
        self.limits(3)
        provider = FakeProvider(self.response(self.broken_draft()), self.response(self.broken_draft()))
        task, _ = await self.run_to(provider, 'TASK_STATE_FAILED')
        self.assertEqual(task['status']['message']['parts'][0]['data']['code'], 'PLANNER_OUTPUT_INVALID')
        self.assertEqual(len(provider.requests), 2)

    def test_single_planning_and_multi_planner_get_identical_feedback(self):
        draft = self.broken_draft()
        multi = contract_check(lambda data: validate_planner_decision(data, self.scenario))(draft)
        single_contract = build_single_draft_contract(SinglePhase.PLANNING, scenario=self.scenario)
        single = contract_check(single_contract.validate)(draft)
        self.assertEqual(multi, single)
        self.assertEqual(multi['code'], 'OUTPUT_CONTRACT_INVALID')
        self.assertTrue(multi['issues'])
        self.assertIsNone(contract_check(single_contract.validate)(self.draft()))
