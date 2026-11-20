"""Five structural changes. All models/Docker are synthetic; no provider calls."""
from contextlib import asynccontextmanager
from hashlib import sha256
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest import mock
from uuid import UUID, uuid4

from mcp_tools.tools import unit_runner
from orchestrator.application.validation_output import decide_verdict
from orchestrator.domain import AgentRole, ValidationOutcome, WorkflowStatus
import test_mcp_files as file_fixture
import test_developer_workspace as workspace_fixture
import test_validation_artifacts as verdict_fixture
import test_completion_paths as completion
import test_development_stop_paths as stop_fixture
import test_owned_agent_fix_pipeline as fix_fixture


class PartialEditTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_developer_engine_accepts_partial_edit_and_records_build(self):
        import test_developer_agent as developer_fixture
        from agents.llm.contracts import ToolCall
        from test_llm_runtime import FakeProvider, tool_response
        f = developer_fixture.DeveloperAgentTests('runTest'); f.setUp(); completion.adopt_cleanups(self, f)
        path = f.root / 'source' / 'signup.py'; original = path.read_text()
        old = original.splitlines()[-1]
        provider = FakeProvider(tool_response(ToolCall(call_id='read', name='read_project_file',
            arguments_json=json.dumps({'workspaceId': str(f.run.workspace_id), 'path': 'source/signup.py'}))),
            tool_response(ToolCall(call_id='partial-edit', name='edit_source_file',
            arguments_json=json.dumps({'workspaceId': str(f.run.workspace_id), 'path': 'source/signup.py',
                'expectedSha256': sha256(original.encode()).hexdigest(), 'oldText': old,
                'newText': "    return 'partial-candidate'"}))), f.draft_response())
        task, _ = await f.run_to(provider, 'TASK_STATE_COMPLETED')
        self.assertTrue(task['artifacts'])
        self.assertEqual(path.read_text(), original.replace(old, "    return 'partial-candidate'", 1))
        self.assertEqual([name for name, _ in f.tool_calls()], ['read_project_file', 'edit_source_file', 'run_build'])

    async def test_cas_preserves_siblings_and_rejects_ambiguous_or_stale_edit(self):
        f = file_fixture.FileToolTests('runTest'); f.setUp(); completion.adopt_cleanups(self, f)
        dispatcher = f.dispatcher()
        args = f.arguments(oldText="'initial'", newText="'fixed'", expectedSha256=sha256(f.original).hexdigest())
        self.assertEqual((await dispatcher.call_tool('edit_source_file', args)).error_code, 'EDIT_READ_REQUIRED')
        await dispatcher.call_tool('read_project_file', f.arguments())
        result = await dispatcher.call_tool('edit_source_file', args)
        self.assertIsNone(result.error_code)
        self.assertEqual(f.file.read_bytes(), f.original.replace(b"'initial'", b"'fixed'"))
        self.assertEqual((await dispatcher.call_tool('edit_source_file', args)).error_code, 'EDIT_STALE_READ')
        await dispatcher.call_tool('read_project_file', f.arguments())
        for old in ('', 'absent'):
            result = await dispatcher.call_tool('edit_source_file', {**args, 'oldText': old,
                'expectedSha256': sha256(f.file.read_bytes()).hexdigest()})
            self.assertIsNotNone(result.error_code)
        from mcp_tools.runtime import MCPProtocolError
        with self.assertRaises(MCPProtocolError):
            await f.dispatcher(AgentRole.QA).call_tool('edit_source_file', args)
        original = b'value = 1\nother = 1\n'; f.file.write_bytes(original)
        await dispatcher.call_tool('read_project_file', f.arguments())
        result = await dispatcher.call_tool('edit_source_file', {**args, 'oldText': '1', 'newText': '2',
            'expectedSha256': sha256(original).hexdigest()})
        self.assertEqual(result.error_code, 'EDIT_TARGET_AMBIGUOUS')
        self.assertEqual(f.file.read_bytes(), original)


class BaselineTests(unittest.TestCase):
    def test_restore_keeps_parent_and_is_not_ready_progress(self):
        f = workspace_fixture.DeveloperWorkspaceTests('runTest'); f.setUp(); completion.adopt_cleanups(self, f)
        good = f.make_checkpoint(); good.prepare(); snapshot = good._parent
        original = (f.source / 'app.py').read_bytes()
        (f.source / 'app.py').write_bytes(b'# broken candidate\n')
        (f.source / 'regression.txt').write_bytes(b'candidate-only\n')
        bad = good.checkpoint()
        repair = f.make_checkpoint(parent_commit_hash=bad.commit_hash); repair.prepare()
        repair.restore_repair_baseline(snapshot)
        self.assertEqual((f.source / 'app.py').read_bytes(), original)
        self.assertFalse((f.source / 'regression.txt').exists())
        self.assertFalse(repair.has_source_changes())
        (f.source / 'app.py').write_bytes(original + b'# repaired defect\n')
        self.assertTrue(repair.has_source_changes())
        final = repair.checkpoint()
        self.assertEqual(final.parent_commit_hash, bad.commit_hash)
        self.assertEqual(final.baseline_commit_hash, f.baseline)


DATABASE = '''import os
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, declarative_base
engine = create_engine(os.environ['DATABASE_URL'])
Base = declarative_base()
Session = sessionmaker(bind=engine)
def get_db():
    with Session() as db:
        yield db
'''
MODELS = '''from sqlalchemy import Column, Integer
from database import Base
class User(Base):
    __tablename__ = 'users'
    id = Column(Integer, primary_key=True)
'''
MAIN = '''from contextlib import asynccontextmanager
from fastapi import FastAPI
from database import Base, engine
from models import User
@asynccontextmanager
async def lifespan(app):
    Base.metadata.create_all(engine)
    yield
app = FastAPI(lifespan=lifespan)
@app.get('/')
def ready():
    return {'ready': True}
'''
CASES = '''from _host_app_test import AppTestCase
class Cases(AppTestCase):
    def test_a(self):
        self.assertEqual(self.client.get('/').status_code, 200)
        self.assertEqual(self.db.query(self.User).count(), 0)
        self.db.add(self.User(id=1)); self.db.commit()
    def test_b(self):
        self.assertEqual(self.db.query(self.User).count(), 0)
'''


class HostFixtureTests(unittest.TestCase):
    def test_precheck_knows_host_ids_and_reports_independent_lifecycle_and_import_errors(self):
        from agents.runtime.generated_tests_precheck import inspect_generated_tests, test_ids
        with tempfile.TemporaryDirectory() as root:
            root = Path(root); source = root / 'source'; source.mkdir(); tests = root / 'tests'; tests.mkdir()
            for name, content in {'main.py': MAIN, 'database.py': DATABASE, 'models.py': MODELS}.items():
                (source / name).write_text(content)
            file = tests / 'test_host.py'; file.write_text(CASES)
            self.assertEqual(test_ids(file), {'test_host.Cases.test_a', 'test_host.Cases.test_b'})
            rows = [{'selector': 'qa', 'testId': name} for name in test_ids(file)]
            self.assertEqual(inspect_generated_tests(tests, [('qa', 'test_*.py')], rows, 'cases', source, host_app_fixture=True), [])
            file.write_text('from main import absent_name\nimport unittest\nclass Cases(unittest.TestCase):\n    def test_a(self): pass\n')
            reasons = {r['reasonCode'] for r in inspect_generated_tests(tests, [('qa', 'test_*.py')], rows, 'cases', source, host_app_fixture=True)}
            self.assertTrue({'TEST_IMPORT_NAME_MISSING', 'TEST_HOST_FIXTURE_REQUIRED'} <= reasons)

    def run_fixture(self, main=MAIN, cases=CASES):
        # Handwritten trusted regression fixture, NOT agent-generated Source.
        with tempfile.TemporaryDirectory() as root:
            root = Path(root); source = root / 'source'; source.mkdir(); tests = root / 'tests'; tests.mkdir()
            for name, content in {'main.py': main, 'database.py': DATABASE, 'models.py': MODELS}.items():
                (source / name).write_text(content)
            name = 'test_host_' + uuid4().hex
            (tests / (name + '.py')).write_text(cases)
            with mock.patch.dict(os.environ, {'DATABASE_URL': 'original-must-not-be-used'}):
                report, code = unit_runner.run_tests(tests, source)
                self.assertEqual(os.environ['DATABASE_URL'], 'original-must-not-be-used')
            sys.modules.pop(name, None)
            self.assertNotIn('main', sys.modules)
            self.assertNotIn('database', sys.modules)
            self.assertNotIn('models', sys.modules)
            self.assertNotIn('_host_app_test', sys.modules)
            return report, code

    def test_lifespan_database_import_order_and_per_case_isolation(self):
        report, code = self.run_fixture()
        self.assertEqual((code, report['passed']), (0, 2), report)

    def test_missing_product_initialization_is_not_hidden(self):
        report, code = self.run_fixture(MAIN.replace('Base.metadata.create_all(engine)', 'pass'))
        self.assertEqual((code, report['failed']), (1, 2), report)
        self.assertTrue(all('OperationalError' in row.get('details', '') for row in report['tests']))

    def test_failure_still_cleans_up_and_runs_independent_cases(self):
        report, code = self.run_fixture(cases=CASES.replace('self.db.add(self.User(id=1)); self.db.commit()',
            'self.db.add(self.User(id=1)); self.db.commit(); self.fail("synthetic failure")'))
        self.assertEqual((code, report['passed'], report['failed']), (1, 1, 1), report)

    def test_startup_error_is_not_pass_and_cleanup_still_runs(self):
        report, code = self.run_fixture(MAIN.replace('Base.metadata.create_all(engine)', 'raise RuntimeError("start failed")'))
        self.assertEqual(code, 1, report)
        self.assertEqual(report['passed'], 0)
        self.assertEqual(report['skipped'], 2)

    def test_lifecycle_override_is_rejected(self):
        report, code = self.run_fixture(cases=CASES.replace('    def test_a(self):', '    def setUp(self): pass\n    def test_a(self):'))
        self.assertEqual((report, code), (None, 2))


def blocked_detail(evidence, *, selector='protected-security', status=400):
    return {'code': 'SECURITY_TESTS_UNVERIFIED', 'tests': [{
        'testId': f'run_unit_tests:{selector}:test_security.Cases.test_hash', 'outcome': 'UNVERIFIED',
        'evidenceRefs': [evidence.evidence_ref], 'observationStatus': 'COMPLETE',
        'observations': [
            {'caseId': 'SEC001', 'itemId': 'http_status', 'kind': 'precondition', 'actual': status,
             'status': 'FAIL', 'reason': 'EXPECTATION_MISMATCH'},
            {'caseId': 'SEC001', 'itemId': 'argon2id', 'kind': 'assertion', 'actual': None,
             'status': 'NOT_RUN', 'reason': 'PRECONDITION_OR_OBSERVATION_UNAVAILABLE'}]}]}


class RecoveryCauseTests(unittest.TestCase):
    def reports(self):
        f = verdict_fixture.ValidationArtifactTests('runTest'); f.setUp()
        qa = f.make_qa(ValidationOutcome.FAIL)
        security = f.make_security()
        old = security.requirement_results[0]
        evidence = old.tool_evidence.model_copy(update={'tool_name': 'run_unit_tests'})
        case = old.model_copy(update={'outcome': ValidationOutcome.UNVERIFIED, 'tool_evidence': None,
            'execution_evidence': (evidence,), 'details': json.dumps(blocked_detail(evidence))})
        security = type(security).model_validate({**security.model_dump(), 'requirement_results': [case]})
        return qa, security

    def decision(self, qa, security, fix=0):
        return decide_verdict(qa, security, fix_attempt=fix, requirements_authoritative=True,
                              protected_selectors=frozenset({'run_unit_tests:protected-security'}))

    def test_receipt_backed_blocked_checks_route_to_known_product_failure(self):
        qa, security = self.reports()
        self.assertEqual(self.decision(qa, security).target_status, WorkflowStatus.FIX_REQUIRED)
        end = self.decision(qa, security, 3)
        self.assertEqual((end.target_status, end.verdict.value), (WorkflowStatus.FINISHED, 'FAIL'))
        self.assertEqual(security.requirement_results[0].outcome, ValidationOutcome.UNVERIFIED)

    def test_unknown_skip_or_unrelated_observation_cannot_bypass_protected_hold(self):
        qa, security = self.reports(); case = security.requirement_results[0]
        for change in ('no_ref', 'unknown', 'not_error', 'unrelated_case', 'unknown_sibling'):
            data = json.loads(case.details); row = data['tests'][0]
            if change == 'no_ref': row['evidenceRefs'] = []
            if change == 'unknown': row['observationStatus'] = 'UNVERIFIED'
            if change == 'not_error': row['observations'][0]['actual'] = 200
            if change == 'unrelated_case': row['observations'][1]['caseId'] = 'OTHER'
            if change == 'unknown_sibling': row['observations'][1]['status'] = 'UNVERIFIED'
            altered = security.model_copy(update={'requirement_results': (case.model_copy(update={'details': json.dumps(data)}),)})
            self.assertEqual(self.decision(qa, altered).target_status, WorkflowStatus.HUMAN_REVIEW)

    def test_protected_technical_not_run_still_holds(self):
        qa, security = self.reports(); case = security.requirement_results[0]
        data = json.loads(case.details); data['code'] = 'SECURITY_TESTS_NOT_RUN'
        row = data['tests'][0]; row.clear(); row.update(testId='run_unit_tests:protected-security:test_x',
            outcome='UNVERIFIED', notRunReason='NOT_RUN_SCOPE_FAILED')
        altered = security.model_copy(update={'requirement_results': (case.model_copy(update={
            'tool_evidence': case.execution_evidence[0], 'details': json.dumps(data)}),)})
        self.assertEqual(self.decision(qa, altered).target_status, WorkflowStatus.HUMAN_REVIEW)

    def test_foreign_snapshot_or_test_reference_rejected_by_report_model(self):
        _qa, security = self.reports(); case = security.requirement_results[0]
        for foreign in ('snapshot', 'reference'):
            data = security.model_dump(mode='json', by_alias=True)
            row = data['requirementResults'][0]
            if foreign == 'snapshot':
                row['executionEvidence'][0]['executionManifest']['snapshotSha256'] = 'f' * 64
            else:
                details = json.loads(row['details']); details['tests'][0]['evidenceRefs'] = ['artifact://foreign/report']
                row['details'] = json.dumps(details)
            with self.assertRaises(ValueError): type(security).model_validate(data)

    def test_security_failure_does_not_hide_unexplained_protected_sibling(self):
        qa, security = self.reports(); case = security.requirement_results[0]
        data = json.loads(case.details)
        data['tests'][0]['observationStatus'] = 'UNVERIFIED'
        data['tests'].append({'testId': 'run_unit_tests:generated:test_fail', 'outcome': 'FAIL',
                             'evidenceRefs': [case.execution_evidence[0].evidence_ref]})
        changed = case.model_copy(update={'outcome': ValidationOutcome.FAIL,
            'tool_evidence': case.execution_evidence[0], 'details': json.dumps(data)})
        report = security.model_copy(update={'requirement_results': (changed,)})
        self.assertEqual(self.decision(qa, report).target_status, WorkflowStatus.HUMAN_REVIEW)


class BoundaryTests(unittest.IsolatedAsyncioTestCase):
    async def test_review_retains_last_registered_candidate_when_fix_has_no_snapshot(self):
        owner = stop_fixture.DevelopmentStopPaths('runTest')
        try: helper, f, packages = owner.fixture(False)
        finally: completion.adopt_cleanups(self, owner)
        from agents.llm.contracts import LLMErrorCode, LLMRuntimeError
        f.qa_failure = True
        providers = f.providers()
        # An unproven agent error cannot be converted to a completed verdict.
        providers[AgentRole.DEVELOPER].script.append(LLMRuntimeError(LLMErrorCode.NO_PROGRESS))
        run = await helper.submit(f, providers)
        self.assertEqual(run.status, WorkflowStatus.HUMAN_REVIEW)
        self.assertEqual(run.code_version, 1)
        self.assertEqual(f.repository.list_steps(run.run_id)[-1].code_version, 2)
        self.assertEqual(packages.complete(run.run_id)['status'], 'NOT_COMPLETED')
        report, = packages.boundary_reports(run.run_id)
        self.assertFalse(report['completed'])
        self.assertEqual((report['codeVersion'], report['retainedCodeVersion']), (1, 1))
        self.assertEqual(report['steps'][-1]['code_version'], 2)
        source = next(a for a in report['artifacts'] if a['artifactType'] == 'SOURCE')
        self.assertEqual(report['retainedSnapshotSha256'], source['snapshotSha256'])
        self.assertTrue({'QA_REPORT', 'SECURITY_REPORT'} <= {a['artifactType'] for a in report['artifacts']})
        with f.repository._connection() as connection:
            self.assertFalse(connection.execute('SELECT 1 FROM host_result_packages').fetchall())

    async def test_hold_is_saved_immutable_and_does_not_claim_completion(self):
        owner = stop_fixture.DevelopmentStopPaths('runTest')
        try: helper, f, packages = owner.fixture(True)
        finally: completion.adopt_cleanups(self, owner)
        # Valid Planner question is an automatic boundary, not a completed package.
        from test_llm_runtime import FakeProvider, text_response
        providers = f.providers()
        question = text_response(json.dumps({
            'kind': 'INPUT_REQUIRED', 'questions': ['승인되지 않은 정책 선택을 확인해 주세요.'],
            'implementationPlan': [], 'unapprovedAssumptions': []}))
        providers[AgentRole.PLANNER] = FakeProvider(question, question)
        run = await helper.submit(f, providers)
        self.assertEqual(run.status, WorkflowStatus.WAITING_INPUT)
        await packages.after_dispatch(run.run_id)
        saved = packages.boundary_reports(run.run_id)
        self.assertEqual(len(saved), 1)
        self.assertFalse(saved[0]['completed'])
        self.assertIn('SOURCE', saved[0]['missingArtifacts'])
        packages.complete(run.run_id)
        self.assertEqual(packages.boundary_reports(run.run_id), saved)
        from agents.platform.evaluation_export import write_evaluation_export
        exported = write_evaluation_export(f.repository, run.run_id, f.directory / 'boundary-export',
            stores={role: settings.database_path for role, settings in f.settings.items()})
        self.assertEqual(json.loads(exported['boundary-reports.json'].read_text()), saved)
        self.assertNotIn('package-completion.json', exported)
        # A later resumed state cannot rewrite the immutable automatic boundary.
        f.repository.resume_run_with_step(run.run_id)
        packages.complete(run.run_id)
        self.assertEqual(packages.boundary_reports(run.run_id), saved)
        with f.repository._connection() as connection:
            self.assertFalse(connection.execute('SELECT 1 FROM host_result_packages').fetchall())
            original = connection.execute('SELECT * FROM host_boundary_reports').fetchone()
        for sql, params in [
            ('UPDATE host_boundary_reports SET report=?', (b'{}',)),
            ('DELETE FROM host_boundary_reports', ()),
            ('INSERT OR REPLACE INTO host_boundary_reports VALUES(?,?,?)', tuple(original)),
        ]:
            with self.assertRaises(sqlite3.IntegrityError):
                with f.repository._transaction() as connection: connection.execute(sql, params)
        self.assertEqual(packages.boundary_reports(run.run_id), saved)


class BaselinePipelineTests(unittest.IsolatedAsyncioTestCase):
    async def adversarial_chain(self, ending, *, security=False):
        from agents.llm.budget import LLMLimits
        from agents.llm.contracts import ToolCall
        from test_llm_runtime import tool_response
        from test_development_rejection import rejected
        from test_owned_agent_pipeline import _task_input
        helper = fix_fixture.OwnedAgentFixPipelineTests('runTest')
        harness = fix_fixture._FixHarness(self, build_failure_versions={2}, model_cap=60, output_corrections=2,
            qa_mode='pass' if security else 'initial-failure', security_failure_versions={1} if security else ())
        packages = completion.enroll_packages(harness.base)
        providers = harness.providers()
        dev = providers[AgentRole.DEVELOPER]
        old = "    return 'inert-candidate-1'"
        source = f"def signup():\n{old}\n"
        digest = sha256(source.encode()).hexdigest()
        def tool(name, call_id, **args):
            def respond(request):
                task = _task_input(request)
                self.assert_rebased_request(task, category='SECURITY_REQUIREMENT_FAILURE' if security else 'QA_ASSERTION_FAILURE')
                return tool_response(ToolCall(call_id=call_id, name=name,
                    arguments_json=json.dumps({'workspaceId': task['workspaceId'], 'path': 'source/signup.py', **args})))
            return respond
        ready = dev.script[1]
        assigned = harness.base.scenario.requirement_ids
        sequence = [tool('read_project_file', 'read-restored'),
            tool('edit_source_file', 'stale-edit', oldText=old, newText="    return 'inert-candidate-3'", expectedSha256='0'*64),
            tool('read_project_file', 'reread-restored'), rejected('NO_CHANGE_REQUIRED', assigned)]
        if ending == 'fix':
            sequence += [tool('edit_source_file', 'valid-edit', oldText=old,
                newText="    return 'inert-candidate-3'", expectedSha256=digest), ready]
        else:
            sequence += [rejected('NO_CHANGE_REQUIRED' if ending == 'stop' else 'POLICY_BLOCKED', assigned)]
        dev.script = dev.script[:4] + sequence
        harness.providers = lambda: providers
        # This fixture is deliberately inert signup.py, not the SCN001 FastAPI scaffold.
        with mock.patch('agents.runtime.developer.start_precheck_for', return_value=None):
            run, artifacts, steps, calls = await helper.submit(harness)
        self.assertEqual(len([a for a in artifacts if a.artifact_type == 'SOURCE']), 3 if ending == 'fix' else 2)
        if ending == 'hold':
            self.assertEqual(run.status.value, 'HUMAN_REVIEW')
            await packages.after_dispatch(run.run_id)
            self.assertTrue(packages.boundary_reports(run.run_id))
            return
        self.assertEqual((run.status.value, run.verdict.value if run.verdict else None),
                         ('FINISHED', 'SUCCESS' if ending == 'fix' else 'FAIL'), run)
        self.assertEqual(run.code_version, 3 if ending == 'fix' else 2)
        self.assertEqual(packages.complete(run.run_id)['status'], 'COMPLETED')
        with harness.base.repository._connection() as c:
            records = [json.loads(r[0]) for r in c.execute('SELECT payload_json FROM agent_runtime_events')]
        routes = [r['detail']['recovery'] for r in records
                  if r.get('detail', {}).get('recovery', {}).get('developmentRejection')]
        self.assertEqual(routes[0]['developmentRejection']['basis'], 'UNCHANGED_MEASURED_FAILURE')
        base = routes[0]['developmentRejection']['failureEvidence']
        self.assertEqual(base['sourceArtifactId'], str(next(a.artifact_id for a in artifacts
            if a.artifact_type == 'SOURCE' and a.code_version == 1)))

    async def test_conflict_reread_rejection_then_minimal_fix_revalidates_and_saves(self):
        await self.adversarial_chain('fix')

    async def test_conflict_reread_repeated_no_change_saves_last_failure(self):
        await self.adversarial_chain('stop')

    async def test_conflict_reread_policy_refusal_keeps_boundary(self):
        await self.adversarial_chain('hold')

    async def test_security_failure_build_regression_conflict_then_repair_saves_success(self):
        await self.adversarial_chain('fix', security=True)

    async def test_security_failure_build_regression_repeated_refusal_saves_failure(self):
        await self.adversarial_chain('stop', security=True)

    def assert_rebased_request(self, data, category='QA_ASSERTION_FAILURE'):
        active, baseline = data['fixRequest'], data['repairBaseline']
        historical = baseline['rejectedCandidateHistory']
        self.assertEqual(historical['sourceArtifactId'], baseline['rejectedSourceArtifactId'])
        self.assertEqual(len(historical['requestSha256']), 64)
        self.assertNotIn('BUILD_CODE_FAILURE', json.dumps(data))
        self.assertTrue(active['issues'])
        self.assertTrue(all(i['category'] == category and i['codeVersion'] == 1
                            and i['sourceArtifactId'] == baseline['sourceArtifactId'] for i in active['issues']))
        self.assertNotIn('issues', historical)
        self.assertEqual(active['workingSourceArtifactId'], baseline['sourceArtifactId'])
        self.assertEqual(active['gitParentSourceArtifactId'], baseline['rejectedSourceArtifactId'])
        self.assertTrue(active['validationFeedback'])
        self.assertTrue(all(r['codeVersion'] == 1 for r in active['validationFeedback']))
        self.assertNotIn('expectedResult', json.dumps(active))
        self.assertNotIn('expected_result', json.dumps(active))

    async def test_failed_candidate_restores_measured_good_base_then_packages_new_candidate(self):
        helper = fix_fixture.OwnedAgentFixPipelineTests('runTest')
        harness = fix_fixture._FixHarness(self, build_failure_versions={2})
        packages = completion.enroll_packages(harness.base)
        run, artifacts, _steps, _calls = await helper.submit(harness)
        self.assertEqual(run.status, WorkflowStatus.FINISHED, run)
        self.assertEqual(run.verdict.value, 'SUCCESS')
        self.assertEqual((run.code_version, run.fix_attempt), (3, 2))
        self.assertTrue(any('repairBaseline' in row for row in harness.fix_inputs))
        self.assert_rebased_request(next(row for row in harness.fix_inputs if 'repairBaseline' in row))
        events, _ = harness.base.repository.list_events(run.run_id, limit=1000, offset=0)
        restored = [e for e in events if e.event_type == 'DEVELOPMENT_BASELINE_RESTORED']
        self.assertEqual(len(restored), 1)
        self.assertEqual(len([a for a in artifacts if a.artifact_type == 'SOURCE']), 3)
        self.assertEqual(packages.complete(run.run_id)['status'], 'COMPLETED')

    async def test_single_restores_same_active_issues_and_packages_revalidated_candidate(self):
        import test_single_agent_fix_pipeline as single_fix
        helper = single_fix.SingleAgentFixPipelineTests('runTest')
        harness = single_fix._SingleFixHarness(self, build_failures=(2,))
        packages = completion.enroll_packages(harness.f)
        run, _artifacts, _steps, _provider = await helper.submit(harness)
        self.assertEqual((run.status.value, run.verdict.value), ('FINISHED', 'SUCCESS'))
        self.assert_rebased_request(next(row for row in harness.fix_inputs if 'repairBaseline' in row))
        self.assertEqual(packages.complete(run.run_id)['status'], 'COMPLETED')


class RepairProjectionTests(unittest.TestCase):
    def test_projection_preserves_history_and_does_not_mutate_original_request(self):
        from types import SimpleNamespace
        from copy import deepcopy
        from agents.runtime.repair_baseline import with_repair_baseline
        request = {'fixRequest': {'attempt': 3, 'issues': [{'category': 'BUILD_CODE_FAILURE'}]}}
        checkpoint = SimpleNamespace(repair_baseline={'sourceArtifactId': 'good', 'rejectedSourceArtifactId': 'bad',
            'fixRequest': {'attempt': 3, 'issues': [{'category': 'SECURITY_REQUIREMENT_FAILURE'}]}})
        before, baseline = deepcopy(request), deepcopy(checkpoint.repair_baseline)
        projected = with_repair_baseline(request, checkpoint)
        self.assertEqual(projected['fixRequest']['issues'][0]['category'], 'SECURITY_REQUIREMENT_FAILURE')
        self.assertEqual(projected['repairBaseline']['rejectedCandidateHistory']['sourceArtifactId'], 'bad')
        self.assertNotIn('BUILD_CODE_FAILURE', json.dumps(projected))
        projected['fixRequest']['issues'].clear()
        projected['repairBaseline']['rejectedCandidateHistory']['issueIds'].clear()
        self.assertEqual((request, checkpoint.repair_baseline), (before, baseline))

    def test_partial_edit_conflict_requires_current_hash_and_unique_fragment(self):
        from agents.llm.corrections import tool_error_output
        error = tool_error_output('edit_source_file', 'WRITE_CONFLICT', 1)['toolError']
        self.assertFalse(error['changedState'])
        self.assertIn('oldText must occur exactly once', error['instruction'])
        self.assertIn('always requires expectedSha256', error['instruction'])
        self.assertNotIn('omit expectedSha256', error['instruction'])


class BlockedProductPipelineTests(unittest.IsolatedAsyncioTestCase):
    async def test_qa_failure_and_blocked_security_recover_then_store_success(self):
        owner = stop_fixture.DevelopmentStopPaths('runTest')
        try: helper, f, packages = owner.fixture(True)
        finally: completion.adopt_cleanups(self, owner)
        original = f.peer_client
        from dataclasses import replace
        from test_llm_runtime import tool_response
        from orchestrator.sandbox.contracts import CLIResult

        @asynccontextmanager
        async def peer(configuration):
            async with original(configuration) as client:
                role = configuration.binding.role
                if role in (AgentRole.QA, AgentRole.SECURITY):
                    docker = f.dockers[role][-1]
                    passing = (docker.exit_code, docker.start_result)
                    def hook(container):
                        docker.exit_code, docker.start_result = passing
                        if f.repository.get_run(configuration.binding.run_id).code_version != 1:
                            return
                        argv = container['Config']['Cmd']
                        if '--kind' not in argv: return
                        kind = argv[argv.index('--kind') + 1]
                        report = json.loads(passing[1].stdout)
                        if role is AgentRole.QA:
                            report['tests'][0]['outcome'] = 'FAIL'
                            report['passed'] -= 1; report['failed'] += 1
                            docker.exit_code = 1
                        elif kind == 'PROTECTED':
                            for index, row in enumerate(report['tests']):
                                row['outcome'] = 'SKIP'; row['details'] = 'precondition unavailable'
                                row['observations'] = [
                                    {'caseId': 'SEC' + str(index), 'itemId': 'http_status', 'kind': 'precondition',
                                     'actual': 400, 'status': 'FAIL', 'reason': 'EXPECTATION_MISMATCH'},
                                    {'caseId': 'SEC' + str(index), 'itemId': 'argon2id', 'kind': 'assertion',
                                     'actual': None, 'status': 'NOT_RUN', 'reason': 'PRECONDITION_OR_OBSERVATION_UNAVAILABLE'}]
                            report['skipped'] = report['total']; report['passed'] = report['failed'] = 0
                        docker.start_result = CLIResult(returncode=docker.exit_code,
                            stdout=json.dumps(report).encode(), stderr=b'')
                    docker.start_hook = hook
                yield client
        f.peer_client = peer
        providers = f.providers()
        write, ready = providers[AgentRole.DEVELOPER].script
        def fix(request):
            call = write(request).tool_calls[0]
            data = json.loads(call.arguments_json)
            data['content'] = "def signup():\n    return 'repaired-inert-candidate'\n"
            return tool_response(replace(call, call_id='developer-real-fix', arguments_json=json.dumps(data)))
        providers[AgentRole.DEVELOPER].script.extend([fix, ready])
        for role in (AgentRole.QA, AgentRole.SECURITY):
            original_script = list(providers[role].script)
            for index, action in enumerate(original_script):
                def replay(request, action=action, role=role, index=index):
                    response = action(request)
                    if response.tool_calls:
                        return tool_response(replace(response.tool_calls[0], call_id=f'{role.value}-fix-{index}'))
                    return response
                providers[role].script.extend([replay])
        run = await helper.submit(f, providers)
        self.assertEqual((run.status.value, run.verdict.value if run.verdict else None), ('FINISHED', 'SUCCESS'), run)
        self.assertEqual((run.code_version, run.fix_attempt), (2, 1))
        artifacts = f.repository.list_project_artifacts(run.run_id)
        first = next(a for a in artifacts if a.artifact_type == 'SECURITY_REPORT' and a.code_version == 1)
        self.assertTrue(all(r.outcome.value == 'UNVERIFIED' and r.tool_evidence is None
                            and len(r.execution_evidence) == 2 for r in first.requirement_results))
        recorded_refs = {row['toolEvidence']['evidenceRef']
                         for row in f.repository.list_tool_attempts(run.run_id)}
        first_refs = {receipt.evidence_ref for result in first.requirement_results
                      for receipt in result.execution_evidence}
        self.assertEqual(len(first_refs), 2)
        self.assertTrue(first_refs <= recorded_refs)
        self.assertEqual(packages.complete(run.run_id)['status'], 'COMPLETED')
        with f.repository._connection() as connection:
            final = json.loads(connection.execute('SELECT report FROM host_final_reports WHERE run_id=?',
                (str(run.run_id),)).fetchone()[0])
        self.assertEqual((final['verdict'], final['codeVersion']), ('SUCCESS', 2))


if __name__ == '__main__': unittest.main()
