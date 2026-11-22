"""Exercise the Host dispatcher with saved observations and fixture A2A/tool envelopes."""
from copy import deepcopy
import json
from pathlib import Path
import unittest

from a2a.types import Task
from google.protobuf.json_format import MessageToDict, ParseDict

from orchestrator.domain import AgentRole
import test_dispatch as dispatch


def archived_measurements():
    archive = json.loads((Path(__file__).parent / 'fixtures/signup_repair_observations.json').read_text())
    rows = deepcopy(archive['v2Observations'])
    measurements = {}
    for version in (2, 3, 4):
        for change in archive.get(f'v{version}Changes', []):
            row = next(r for r in rows if (r['caseId'], r['itemId']) == (change['caseId'], change['itemId']))
            row.update(change)
        measurements[version] = {'testId': archive['testId'], 'testVersion': archive['testVersion'],
                                 'outcome': 'FAIL', 'reason': None, 'observationStatus': 'COMPLETE',
                                 'observations': deepcopy(rows)}
    return measurements


class MeasuredClient(dispatch.FakePlannerClient):
    def __init__(self, measurements, *, role=AgentRole.SECURITY, second_issue=False, detached_receipt=False):
        super().__init__("TASK_STATE_COMPLETED")
        self.measurements = measurements
        self.role = role
        self.second_issue = second_issue
        self.detached_receipt = detached_receipt

    async def send_snapshot_handoff(self, handoff, recipient, request_text, **kwargs):
        task = await super().send_snapshot_handoff(handoff, recipient, request_text, **kwargs)
        if recipient != self.role:
            return task
        wire = MessageToDict(task)
        data = wire['artifacts'][0]['parts'][0]['data']
        # v1 is a synthetic unchanged baseline, used solely to put v3 at the old two-repeat threshold.
        version = int(data['codeVersion'])
        measured = deepcopy(self.measurements[max(2, version)])
        unit = dispatch.tool_evidence('run_unit_tests', data['executionManifest'])
        measured['evidenceRefs'] = [unit['evidenceRef']]
        entries = data['tests'] if recipient == AgentRole.QA else data['requirementResults']
        target = entries[-1]
        if self.detached_receipt:
            # Scanner evidence can support a Security report, but cannot attest unit subchecks.
            unit = target['toolEvidence']
            measured['evidenceRefs'] = [unit['evidenceRef']]
        target.update(outcome=measured.get('outcome', 'FAIL'), toolEvidence=unit, executionEvidence=[unit])
        if recipient == AgentRole.QA:
            target.update(testId=measured['testId'], details=json.dumps({'code':'MEASURED_UNIT_TEST', **measured}))
        else:
            target['details'] = json.dumps({'code':'MEASURED_SECURITY_TESTS', 'tests':[measured]})
        if self.second_issue:
            entries[0]['outcome'] = 'FAIL'
        return ParseDict(wire, Task())


class MeasuredStopTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.host = dispatch.PlannerDispatchTests('runTest')
        self.host.setUp()
        self.addCleanup(self.host.tearDown)

    async def dispatch(self, measurements, **kwargs):
        client = MeasuredClient(measurements, **kwargs)
        await self.host.dispatcher_for(client).dispatch_planner(self.host.run.run_id)
        repo, run_id = self.host.repository, self.host.run.run_id
        run = repo.get_run(run_id)
        issues = sorted(repo.list_issue_records(run_id), key=lambda i: i.code_version)
        events, _ = repo.list_events(run_id, limit=1000, offset=0)
        return run, issues, [e.event_type for e in events]

    async def check_generated_case(self, role, *, change_bytes=False, rename_case=False):
        rows = archived_measurements()
        identity = 'run_unit_tests:generated:test_signup.Signup.test_original'
        for version, measured in rows.items():
            measured['testId'] = identity + ('_replacement' if rename_case and version >= 3 else '')
            measured['testVersion'] = 'inputs:' + ('b' if change_bytes and version >= 3 else 'a') * 64
            measured.update(observationStatus='UNVERIFIED', observations=[], reason='DETAIL_RECORD_MISSING')
            if version >= 3:
                measured['outcome'] = 'PASS'
        return await self.dispatch(rows, role=role)

    async def test_passing_qa_replacement_cannot_resolve_a_prior_generated_failure(self):
        run, issues, events = await self.check_generated_case(AgentRole.QA, change_bytes=True)
        self.assertEqual((run.status.value, run.verdict.value), ('FINISHED', 'UNVERIFIED'))
        self.assertEqual({i.revalidation_result for i in issues}, {'UNVERIFIED'})
        self.assertIn('GENERATED_RECHECK_UNRESOLVED', events)

    async def test_passing_different_security_case_cannot_resolve_a_prior_generated_failure(self):
        run, issues, events = await self.check_generated_case(AgentRole.SECURITY, rename_case=True)
        self.assertEqual((run.status.value, run.verdict.value), ('FINISHED', 'UNVERIFIED'))
        self.assertEqual({i.revalidation_result for i in issues}, {'UNVERIFIED'})
        self.assertIn('GENERATED_RECHECK_UNRESOLVED', events)

    async def test_rerun_with_the_same_generated_input_can_resolve_the_original_failure(self):
        run, issues, events = await self.check_generated_case(AgentRole.SECURITY)
        self.assertEqual((run.status.value, run.verdict.value), ('FINISHED', 'SUCCESS'))
        self.assertEqual({i.revalidation_result for i in issues}, {'PASS'})
        self.assertNotIn('GENERATED_RECHECK_UNRESOLVED', events)

    async def test_partial_then_stalled_reaches_third_repair_and_persists_final_failure(self):
        run, issues, events = await self.dispatch(archived_measurements())
        self.assertEqual((run.status.value, run.verdict.value, run.code_version, run.fix_attempt), ('FINISHED','FAIL',4,3))
        self.assertEqual([i.consecutive_repeat_count for i in issues], [0,1,2,3])
        self.assertEqual([i.repeats_for_stop for i in issues], [0,1,0,1])
        self.assertEqual([i.measured_progress for i in issues], [None,'NO_PROGRESS','PARTIAL_IMPROVEMENT','NO_PROGRESS'])
        self.assertNotIn('SAME_ISSUE_REPEAT_LIMIT_REACHED', events)
        self.assertEqual(issues[2].new_code_version, 4)
        self.assertEqual([r.code_version for r in self.host.repository.list_project_artifacts(run.run_id)
                          if r.artifact_type == 'SECURITY_REPORT'], [1,2,3,4])

    async def test_two_unchanged_measured_qa_repairs_still_stop_early(self):
        rows = archived_measurements()
        rows[3] = rows[2]
        run, issues, events = await self.dispatch(rows, role=AgentRole.QA)
        self.assertEqual((run.verdict.value, run.code_version, issues[-1].repeats_for_stop), ('FAIL',3,2))
        self.assertEqual(issues[-1].measured_progress, 'NO_PROGRESS')
        self.assertIn('SAME_ISSUE_REPEAT_LIMIT_REACHED', events)

    async def test_changed_test_versions_do_not_consume_a_stall(self):
        rows = archived_measurements()
        rows[3]['testVersion'] = 'inputs:' + '0' * 64
        run, issues, events = await self.dispatch(rows)
        self.assertEqual((run.code_version, issues[-1].measured_progress, run.fix_attempt), (4,'NOT_COMPARABLE',3))
        self.assertNotIn('SAME_ISSUE_REPEAT_LIMIT_REACHED', events)

    async def test_scanner_receipt_cannot_confirm_improved_unit_observations(self):
        run, issues, events = await self.dispatch(archived_measurements(), detached_receipt=True)
        self.assertEqual((run.code_version, issues[-1].measured_progress, run.fix_attempt), (4,'NOT_COMPARABLE',3))
        self.assertNotIn('SAME_ISSUE_REPEAT_LIMIT_REACHED', events)

    async def test_unmeasured_issue_cannot_terminate_an_improving_issue(self):
        run, issues, events = await self.dispatch(archived_measurements(), second_issue=True)
        self.assertEqual(run.code_version, 4)
        self.assertEqual(sorted(i.repeats_for_stop for i in issues if i.code_version == 4), [0,1])
        self.assertNotIn('SAME_ISSUE_REPEAT_LIMIT_REACHED', events)


class MeasuredPackageTests(unittest.IsolatedAsyncioTestCase):
    async def test_ver1_third_fix_admitted_and_saved_with_ready_and_measurements(self):
        from contextlib import asynccontextmanager
        from dataclasses import replace
        from uuid import UUID
        import test_completion_paths as completion
        from test_llm_runtime import tool_response
        from orchestrator.sandbox.contracts import CLIResult

        helper = completion.CompletionPathTests('runTest')
        try:
            environment = helper.fixture(ver1=True)
        finally:
            completion.adopt_cleanups(self, helper)
        # The borrowed one-round fixture has a 30s deadline. This test runs four complete rounds.
        limits = environment.configuration.limits.model_copy(update={'runtime_budget_ms': 180_000})
        environment.configuration = environment.configuration.model_copy(update={'limits': limits})
        packages = completion.enroll_packages(environment)
        measurements = archived_measurements()
        peer = environment.peer_client

        @asynccontextmanager
        async def with_archived_results(configuration):
            async with peer(configuration) as client:
                if configuration.binding.role == AgentRole.SECURITY:
                    docker = environment.dockers[AgentRole.SECURITY][-1]
                    normal = docker.exit_code, docker.start_result
                    def serve(container):
                        args = container['Config']['Cmd']
                        if '--kind' not in args or args[args.index('--kind') + 1] != 'PROTECTED':
                            docker.exit_code, docker.start_result = normal
                            return
                        version = environment.repository.get_run(configuration.binding.run_id).code_version
                        result = json.loads(normal[1].stdout)
                        result['tests'][-1].update(outcome='FAIL', observations=measurements[max(2,version)]['observations'])
                        result.update(failed=1, passed=result['total'] - 1)
                        docker.exit_code = 1
                        docker.start_result = CLIResult(returncode=1, stdout=json.dumps(result).encode(), stderr=b'')
                    docker.start_hook = serve
                yield client
        environment.peer_client = with_archived_results
        providers = environment.providers()
        focus_by_round = {}
        for role in (AgentRole.DEVELOPER, AgentRole.QA, AgentRole.SECURITY):
            script = providers[role].script
            write, ready = script
            def next_write(request, *, action, version, role):
                call = action(request).tool_calls[0]
                args = json.loads(call.arguments_json)
                if role is AgentRole.DEVELOPER:
                    from test_owned_agent_pipeline import _task_input
                    focus = _task_input(request)['activeRepairFocus']['brief']
                    focus_by_round[focus['repairRound']['currentAttempt']] = focus
                    args['content'] = f"def signup():\n    return {version}\n"
                return tool_response(replace(call, call_id=f'{role.value}-write-{version}', arguments_json=json.dumps(args)))
            from functools import partial
            if role is AgentRole.DEVELOPER:
                def submit_paths(request, base=ready):
                    from test_owned_agent_pipeline import _task_input
                    from test_llm_runtime import text_response
                    paths = _task_input(request)['activeRepairFocus']['brief']['readyReportPaths']
                    draft = json.loads(base.output_text)
                    draft['repairPaths'] = {p: {'edit': None, 'sourceRef': None, 'recheck': 'Recheck on the new fixture.'} for p in paths}
                    return text_response(json.dumps(draft))
                ready = submit_paths
            for version in (2,3,4):
                script.extend([partial(next_write, action=write, version=version, role=role), ready])
                if role is AgentRole.DEVELOPER and version == 3:
                    # The archived v1→v2 comparison showed no progress. This partial
                    # candidate declines its extra priority-edit opportunity before build.
                    def keep_partial(request, submit=ready):
                        message = json.loads(json.loads(request.input_items_json)[-1]['content'])
                        self.assertEqual(message['hostCorrection']['code'], 'READY_PRIORITY_REPAIR_NEEDED')
                        return submit(request)
                    script.append(keep_partial)

        async with environment.running_platform(providers, model_cap=80, tool_cap=90, corrections=2) as (client, _):
            response = await client.post('/api/v1/runs', json=environment.submission())
            self.assertEqual(response.status_code, 201, response.text)
            run = environment.repository.get_run(UUID(response.json()['run']['runId']))
        self.assertEqual((run.status.value, run.verdict.value if run.verdict else None, run.code_version, run.fix_attempt),
                         ('FINISHED','FAIL',4,3),
                         [e.event_type for e in helper.events(environment, run)][-18:])
        self.assertEqual(packages.complete(run.run_id)['status'], 'COMPLETED')
        with environment.repository._connection() as c:
            final = json.loads(c.execute('SELECT report FROM host_final_reports WHERE run_id=?',(str(run.run_id),)).fetchone()[0])
        self.assertEqual((final['verdict'],final['codeVersion']), ('FAIL',4))
        outcome = final['repairOutcome']
        self.assertEqual((outcome['decisionEvent'], outcome['codeFixesUsed'], outcome['maxCodeFixAttempts']),
                         ('VALIDATION_DECISION_FINISHED', 3, 3))
        self.assertIsNone(outcome['triggerIssueId'])
        self.assertEqual(outcome['currentIssueIds'], [i['issueId'] for i in final['issues'] if i['codeVersion'] == 4])
        measured, = outcome['rechecks']
        self.assertEqual((measured['currentOutcome'], measured['editEffect']),
                         ('FAIL', {'status': 'NO_PROGRESS', 'counts': {'UNCHANGED': 31}}))
        review = final['artifacts']['CHANGE_REPORT']['readyReview']
        self.assertEqual((review['buildOutcome'],review['validationStatus']), ('PASS','PENDING_QA_SECURITY'))
        self.assertEqual([r['path'] for r in review['reportedPathEdits']], focus_by_round[3]['readyReportPaths'])
        self.assertTrue(all(r['edit'] is None for r in review['reportedPathEdits']))
        check, = review['rechecks']
        self.assertEqual(check['role'],'SECURITY')
        self.assertEqual(check['priorReportId'], measured['priorReportId'])
        self.assertEqual(final['artifacts']['SECURITY_REPORT']['artifactId'], measured['currentReportId'])
        self.assertEqual(set(check['unresolvedCases']), {'application_logs','db_error','malformed_list','malformed_misspelled'})
        self.assertEqual([i['stopRepeatCount'] for i in sorted(final['issues'], key=lambda i:i['codeVersion'])
                          if 'stopRepeatCount' in i], [1,0,1])
        for attempt, expected in ((1, (0, 0, None)), (2, (1, 1, 'NO_PROGRESS')), (3, (2, 0, 'PARTIAL_IMPROVEMENT'))):
            focus = focus_by_round[attempt]
            target, = focus['targets']
            progress = target['hostIssueProgress']
            self.assertEqual((progress['consecutiveRepeatCount'], progress['stopRepeatCount'], progress['measuredProgress']),
                             expected)
            self.assertEqual(focus['repairRound']['maxAdditionalAttemptsAfterThis'], 3 - attempt)
            if attempt > 1:
                check, = target['checks']
                self.assertEqual(check['previousEditEffect']['status'], expected[2])
