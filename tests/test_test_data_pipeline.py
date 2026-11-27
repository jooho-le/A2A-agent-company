"""Real Host/A2A/guards/receipts, fake provider and Docker. No live calls."""
from dataclasses import replace
import json
import unittest
from unittest import mock
from uuid import UUID

from agents.platform.package_service import HostPackageService
from agents.runtime.test_guidance import unit_test_data_contract
from orchestrator.domain import AgentRole, WorkflowStatus
from test_llm_runtime import tool_response
from test_runtime_test_data import BAD_SOURCE, SAFE_SOURCE
import test_development_stop_paths as completion
import test_single_agent_pipeline as single_fixture


def with_source(original, content, call_id):
    def write(request):
        response = original(request)
        call = response.tool_calls[0]
        args = json.loads(call.arguments_json)
        args['content'] = content
        return tool_response(replace(call, call_id=call_id, arguments_json=json.dumps(args)))
    return write


def fixture_source(role, requirements):
    name = 'Signup' if role is AgentRole.QA else 'Security'
    # Inert to FakeDocker, but valid unittest bodies; standalone runner behavior
    # is exercised independently with hand-written trusted fixtures.
    result = 'import unittest\n' + SAFE_SOURCE + f'class {name}(unittest.TestCase):\n'
    for index in range(1, len(requirements) + 1):
        result += f'    def test_req_{index}(self):\n        self.assertEqual(len(short_password), 7)\n'
    return result


class TestDataPipelineTests(unittest.IsolatedAsyncioTestCase):
    def check_feedback(self, provider, expected_count):
        feedback = []
        contracts = []
        for request in provider.requests:
            self.assertNotIn('SYNTHETIC_FIRST_BLOCKED', request.input_items_json)
            self.assertNotIn('SYNTHETIC_SECOND_BLOCKED', request.input_items_json)
            for item in json.loads(request.input_items_json):
                if item.get('role') != 'user': continue
                data = json.loads(item['content'])
                if 'hostToolFeedback' in data: feedback.append(data['hostToolFeedback'])
                for target in data.get('taskInput', {}).get('testTargets', []):
                    if target.get('format') == 'python-unittest': contracts.append(target['testData'])
        self.assertTrue(contracts)
        self.assertTrue(all(value == unit_test_data_contract() for value in contracts))
        # Each accepted rewrite and final READY retains its prior feedback.
        self.assertGreaterEqual(len(feedback), expected_count)
        self.assertTrue(all([item['sourceLine'] for item in f['sourceIssues']] == [1, 3] for f in feedback))

    async def exercise_multi(self, ver1):
        owner = completion.DevelopmentStopPaths('runTest')
        try:
            helper, f, packages = owner.fixture(ver1)
        finally:
            completion.completion_fixture.adopt_cleanups(self, owner)
        providers = f.providers()
        for role in (AgentRole.QA, AgentRole.SECURITY):
            provider = providers[role]
            original, ready = provider.script
            from orchestrator.domain.scenario_registry import RequirementValidator
            requirements = f.scenario.requirement_ids_for(RequirementValidator(role.value))
            provider.script = [with_source(original, BAD_SOURCE, role.value + '-rejected'),
                with_source(original, fixture_source(role, requirements), role.value + '-corrected'), ready]
        run = await helper.submit(f, providers)
        self.assertEqual((run.status, run.verdict.value if run.verdict else None), (WorkflowStatus.FINISHED, 'SUCCESS'), run)
        self.assertEqual(packages.complete(run.run_id)['status'], 'COMPLETED')
        for role in (AgentRole.QA, AgentRole.SECURITY): self.check_feedback(providers[role], 1)

    async def test_multi_ver1_both_roles_recover_and_save_final_result(self):
        await self.exercise_multi(True)

    async def test_multi_ver2_uses_identical_test_data_and_recovery(self):
        await self.exercise_multi(False)

    async def test_single_qa_and_security_use_the_same_contract_and_bounded_correction(self):
        for name in ('agents.runtime.developer.start_precheck_for', 'agents.single.runtime.start_precheck_for'):
            patcher = mock.patch(name, lambda *_: None); patcher.start(); self.addCleanup(patcher.stop)
        base = single_fixture.SingleAgentPipelineTests('runTest')
        try:
            base.setUp()
        finally:
            completion.completion_fixture.adopt_cleanups(self, base)
        base.measured_security = True
        f = base.f; packages = HostPackageService(f.repository); prepare = f.prepare_workspace
        def enrolled(run_id): packages.enroll(run_id); prepare(run_id)
        f.prepare_workspace = enrolled
        provider = base.provider()
        from orchestrator.domain.scenario_registry import RequirementValidator
        # Replace from the end so inserting QA's correction cannot move Security's index.
        for index, role in ((len(provider.script) - 2, AgentRole.SECURITY), (3, AgentRole.QA)):
            original = provider.script[index]
            requirements = f.scenario.requirement_ids_for(RequirementValidator(role.value))
            provider.script[index:index + 1] = [with_source(original, BAD_SOURCE, role.value + '-rejected'),
                with_source(original, fixture_source(role, requirements), role.value + '-corrected')]
        async with base.running(provider=provider, cap=32, tool_cap=40, corrections=2) as (client, _):
            response = await client.post('/api/v1/runs', json=f.submission())
            self.assertEqual(response.status_code, 201, response.text)
            run = f.repository.get_run(UUID(response.json()['run']['runId']))
        self.assertEqual((run.status, run.verdict.value if run.verdict else None), (WorkflowStatus.FINISHED, 'SUCCESS'), run)
        self.assertEqual(packages.complete(run.run_id)['status'], 'COMPLETED')
        self.check_feedback(provider, 2)


class FixtureIsolationTests(unittest.TestCase):
    def test_nested_async_fixture_restores_protected_policy_and_start_checks(self):
        import agents.runtime.developer as developer
        import agents.single.runtime as single
        import test_owned_agent_pipeline as multi
        baseline = multi.create_platform, developer.start_precheck_for, single.start_precheck_for
        # Reproduce reuse by a synchronous owner: borrowed async fixtures have
        # no runner, so invoking their doCleanups would leave mocks installed.
        for _ in range(2):
            owner = unittest.TestCase('runTest')
            borrowed = completion.DevelopmentStopPaths('runTest')
            try:
                _helper, f, _packages = borrowed.fixture(True)
            finally:
                completion.completion_fixture.adopt_cleanups(owner, borrowed)
            self.assertIsNot(multi.create_platform, baseline[0])
            self.assertTrue(owner.doCleanups())
            self.assertEqual((multi.create_platform, developer.start_precheck_for, single.start_precheck_for), baseline)
            self.assertFalse(f.directory.exists())
