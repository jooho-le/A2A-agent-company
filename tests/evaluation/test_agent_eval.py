"""agent_eval.py(Agent별 평가표 세 개: 작업 정확성·정보 전달·처리시간과 오류)를 시험한다.

확인하는 것:
  - dev 시험 하네스가 만든 실제 Run 기록(Multi: QA 실패 → 수정 → 재검증, Single: 한 번에 성공)을 dev evaluation_export로
    꺼낸 것(fixtures/agent_eval_runs.json)에서 세 표가 채워지는가? 독립 평가가 필요한 칸은 보고서가 없으면 미검증인가?
  - 받은 입력이 규칙과 다르거나(요구사항 누락·다른 버전·A2A 기록 없음), 받은 입력과 다른 결과를 내면(요구사항 누락·
    다른 Snapshot 검사) 그 단계를 FAIL·미검증으로 잡고, 처음 문제 난 단계를 오류 발생 지점으로 알리는가?
  - QA·Security 판정 불일치를 세되 외부 시험 PASS를 근거로 오탐이라고 단정하지 않는가? 다른 Run은 거부하는가?
  - 모르는 시간·토큰·비용을 0으로 채우지 않는가? 에이전트 오류(단계 실패·A2A 실패·LLM 실패)를 처리시간 표에 남기는가?
  - QA가 찾은 제품 결함은 에이전트 오류가 아니라 productDefects로 따로 남는가?
  - 시나리오 정책을 주지 않으면 Run이 고정한 정책(없으면 등록 시나리오)을 쓰고, 다른 시나리오 정책은 거부하는가?
  - 평가 기준이 없는 새 phase(예: 팀장 보조 에이전트)를 다른 역할로 채점하지 않고 미검증으로 두되 시간·오류는 재는가?
  - 여러 Run을 (시나리오, 구조)별로 모으고, 모르는 값을 0으로 넣지 않으며, 같은 Run을 두 번 세지 않는가?
"""
import copy
import json
from pathlib import Path

import pytest

from evaluation.agent_eval import agent_tables, load_export, render_markdown, render_summary_markdown, summarize
from orchestrator.domain.scenario_registry import SCN_001_ID, get_scenario

FIXTURE = json.loads((Path(__file__).parent / 'fixtures/agent_eval_runs.json').read_text(encoding='utf-8'))
CONTRACT = get_scenario(SCN_001_ID).planner_contract()
KEYS = {str(item['requirementId']): item['key'] for item in CONTRACT['requirements']}
IDS = {key: requirement for requirement, key in KEYS.items()}


def run(name='multiFix'):
    export = copy.deepcopy(FIXTURE[name])
    # 옛 고정 파일은 records만 저장했다. 사용량을 확인하는 시험에서는 수집 완전성을 명시한 입력으로 만든다.
    usage = export['bundle']['usage']
    usage['total'] = len(usage['records'])
    usage['recordsConsistent'] = True
    usage['summary'] = {'modelCalls': usage['total']}
    for sequence, row in enumerate(usage['records'], 1):
        row['detail']['sequence'] = sequence
        row['binding']['runId'] = export['bundle']['run']['runId']
    return export


def accuracy(tables, item):
    return next(row for row in tables['accuracy'] if row['item'] == item)


def step(export, phase, version):
    return next(item for item in export['handoff']['steps'] if item['phase'] == phase and item['codeVersion'] == version)


def task(export, phase, version):
    target = step(export, phase, version)
    return next(row for row in export['a2a']['roles'][target['agentIdentity']]['tasks'] if row['taskId'] == target['a2aTaskId'])


def output(export, phase, version, kind):
    target = step(export, phase, version)
    return next(item for item in export['trace']['artifacts']
                if item['workflowStepId'] == target['workflowStepId'] and item['artifactType'] == kind)


def report(statuses, run_id):
    """시험용 최소 독립 평가 보고서. statuses: {(관점, REQ 키): 상태}."""
    checks = [{'testId': f'{perspective}-{key}', 'requirementId': IDS[key], 'perspective': perspective,
               'status': status, 'required': True, 'title': f'{perspective}-{key} 검사'}
              for (perspective, key), status in statuses.items()]
    requirements = [{'requirementId': IDS[key], 'checkIds': [row['testId'] for row in checks if row['requirementId'] == IDS[key]]}
                    for key in sorted({key for _perspective, key in statuses})]
    verdict = 'FAIL' if 'FAIL' in statuses.values() else 'PASS' if set(statuses.values()) == {'PASS'} else 'UNVERIFIED'
    version = next((item['bundle']['run']['codeVersion'] for item in FIXTURE.values()
                    if isinstance(item, dict) and 'bundle' in item
                    and item['bundle']['run']['runId'] == run_id), 2)
    export = next(item for item in FIXTURE.values() if isinstance(item, dict) and 'bundle' in item
                  and item['bundle']['run']['runId'] == run_id)
    project = output(export, 'QA', version, 'QA_REPORT')['executionManifest']['projectArtifactId']
    return {'expectedManifest': {'codeVersion': version, 'projectArtifactId': project}, 'sourceRunId': run_id,
            'evaluationVerdict': verdict, 'manifestCheck': {'errorCode': None, 'metadataMatches': True},
            'checks': checks, 'requirements': requirements, 'sourceRun': {'runId': run_id}}


ALL_PASS = {**{('QA', key): 'PASS' for key in ('REQ-001', 'REQ-002', 'REQ-003', 'REQ-004', 'REQ-007')},
            **{('SECURITY', key): 'PASS' for key in ('REQ-003', 'REQ-005', 'REQ-006')}}


# ---------------------------------------------------------------- 실제 Run 기록

def test_multi_run_fills_all_three_tables():
    """Multi(QA 실패 → 수정 → 재검증): 전달·해석은 모두 PASS, 독립 평가가 필요한 칸만 미검증, 결함은 따로 남는다."""
    tables = agent_tables(run(), CONTRACT)
    assert (tables['architecture'], tables['runStatus'], tables['orchestratorVerdict']) == ('MULTI_AGENT', 'FINISHED', 'SUCCESS')
    assert [(row['role'], row['phase'], row['codeVersion']) for row in tables['handoffs']] == [
        ('PLANNER', 'PLANNING', None), ('DEVELOPER', 'IMPLEMENTING', 1), ('QA', 'QA', 1), ('SECURITY', 'SECURITY', 1),
        ('DEVELOPER', 'FIXING', 2), ('QA', 'QA', 2), ('SECURITY', 'SECURITY', 2)]
    assert all(row[part]['status'] == 'PASS' for row in tables['handoffs'] for part in ('delivery', 'interpretation'))
    statuses = {row['item']: row['status'] for row in tables['accuracy']}
    assert statuses == {
        '계획이 요구사항을 모두 작업에 연결(의존관계 모순 없음)': 'PASS',
        '계획 내용 타당성(조건 보존·작업 구체성)': 'UNVERIFIED', '버전마다 빌드 성공': 'PASS',
        '최종 결과물이 독립 평가 통과': 'UNVERIFIED', '수정 요청을 고침(고친 버전에서 PASS)': 'PASS',
        'QA 판정이 독립 평가와 일치': 'UNVERIFIED', 'Issue가 실제 FAIL 검사를 가리킴': 'NOT_APPLICABLE',
        'Security 판정이 독립 평가와 일치': 'UNVERIFIED'}
    assert [row['status'] for row in tables['accuracy'] if row['item'] == 'Issue가 실제 FAIL 검사를 가리킴'] == ['PASS', 'NOT_APPLICABLE']
    assert tables['firstProblem'] is None  # QA가 결함을 찾은 것은 에이전트 오류가 아니다
    assert tables['productDefects'] == [{'reporter': 'QA', 'codeVersion': 1, 'category': 'QA_ASSERTION_FAILURE',
                                         'requirements': ['REQ-001'], 'fixedIn': 2}]
    timing = {row['role']: row for row in tables['timing']}
    assert {role: row['steps'] for role, row in timing.items()} == {'PLANNER': 1, 'DEVELOPER': 2, 'QA': 2, 'SECURITY': 2}
    assert {role: row['llmCalls'] for role, row in timing.items()} == {'PLANNER': 1, 'DEVELOPER': 4, 'QA': 4, 'SECURITY': 4}
    assert all(row['agentMs'] > 0 and row['status'] == 'PASS' and row['errors'] == [] for row in timing.values())
    assert timing['QA']['toolCalls'] == 4 and timing['QA']['toolMs'] > 0 and timing['QA']['toolOutcomes'] == ['PASS']
    assert all(row['costUsd'] is None for row in timing.values())  # 승인 단가가 없는 표본: 0원이 아니라 모름


def test_single_run_uses_phases_as_rows():
    """Single은 에이전트가 하나지만 단계 역할(기획·구현·QA·Security)로 같은 표가 된다."""
    export = run('single')
    assert {item['agentIdentity'] for item in export['handoff']['steps']} == {'SINGLE_AGENT'}
    tables = agent_tables(export, CONTRACT)
    assert tables['architecture'] == 'SINGLE_AGENT'
    assert [row['role'] for row in tables['handoffs']] == ['PLANNER', 'DEVELOPER', 'QA', 'SECURITY']
    assert all(row[part]['status'] == 'PASS' for row in tables['handoffs'] for part in ('delivery', 'interpretation'))
    assert [row['steps'] for row in tables['timing']] == [1, 1, 1, 1]
    assert accuracy(tables, '수정 요청을 고침(고친 버전에서 PASS)')['status'] == 'NOT_APPLICABLE'


def test_load_export_reads_the_four_files_and_rejects_mixed_runs(tmp_path):
    export = run()
    names = {'bundle': 'bundle.json', 'trace': 'trace.json', 'a2a': 'a2a-messages.json', 'handoff': 'handoff.json'}
    for key, name in names.items():
        (tmp_path / name).write_text(json.dumps(export[key]), encoding='utf-8')
    assert load_export(tmp_path) == export
    export['a2a']['runId'] = FIXTURE['single']['a2a']['runId']
    (tmp_path / 'a2a-messages.json').write_text(json.dumps(export['a2a']), encoding='utf-8')
    with pytest.raises(ValueError):
        load_export(tmp_path)


# ---------------------------------------------------------------- 정보 전달

def test_missing_requirement_in_received_input_is_a_delivery_failure():
    """QA가 받은 요구사항에서 REQ-004가 빠지면 그 단계의 전달이 FAIL이고 오류 발생 지점이 된다."""
    export = run()
    received = task(export, 'QA', 1)['metadata']
    received['requirementIds'].remove(IDS['REQ-004'])
    tables = agent_tables(export, CONTRACT)
    row = next(item for item in tables['handoffs'] if item['phase'] == 'QA' and item['codeVersion'] == 1)
    assert row['delivery']['status'] == 'FAIL' and row['delivery']['detail']['missing'] == [IDS['REQ-004']]
    assert tables['firstProblem'] == {'kind': 'delivery', 'role': 'QA', 'phase': 'QA', 'codeVersion': 1, 'status': 'FAIL',
                                      'detail': row['delivery']['detail']}


@pytest.mark.parametrize('change, field', [
    (lambda metadata: metadata.update(codeVersion=2), 'codeVersionMismatch'),
    (lambda metadata: metadata['projectArtifactIds'].append('00000000-0000-4000-8000-000000000001'), 'extra'),
])
def test_wrong_version_or_extra_input_is_a_delivery_failure(change, field):
    export = run()
    change(task(export, 'SECURITY', 1)['metadata'])
    row = next(item for item in agent_tables(export, CONTRACT)['handoffs'] if item['phase'] == 'SECURITY' and item['codeVersion'] == 1)
    assert row['delivery']['status'] == 'FAIL' and row['delivery']['detail'][field]


def test_fix_step_must_receive_the_failed_report():
    """수정 단계는 직전 Snapshot과 Issue의 보고서를 받아야 한다(그 밖의 Artifact는 더 있어도 된다)."""
    export = run()
    issue = export['bundle']['issues'][0]
    task(export, 'FIXING', 2)['metadata']['projectArtifactIds'].remove(issue['reportArtifactId'])
    row = next(item for item in agent_tables(export, CONTRACT)['handoffs'] if item['phase'] == 'FIXING')
    assert row['delivery']['status'] == 'FAIL' and row['delivery']['detail']['missing'] == [issue['reportArtifactId']]


def test_step_without_a2a_record_is_unverified_not_pass():
    export = run()
    target = step(export, 'IMPLEMENTING', 1)
    roles = export['a2a']['roles'][target['agentIdentity']]
    roles['tasks'] = [row for row in roles['tasks'] if row['taskId'] != target['a2aTaskId']]
    tables = agent_tables(export, CONTRACT)
    row = next(item for item in tables['handoffs'] if item['phase'] == 'IMPLEMENTING')
    assert (row['delivery']['status'], row['delivery']['detail']) == ('UNVERIFIED', 'A2A_TASK_NOT_IN_EXPORT')
    assert row['interpretation']['status'] == 'UNVERIFIED'
    assert tables['firstProblem']['status'] == 'UNVERIFIED' and tables['firstProblem']['phase'] == 'IMPLEMENTING'


def test_report_that_skips_a_received_requirement_is_an_interpretation_failure():
    """QA 보고서에 받은 요구사항(REQ-007)의 검사가 없으면 해석 FAIL이다."""
    export = run()
    qa = output(export, 'QA', 2, 'QA_REPORT')
    qa['tests'] = [test for test in qa['tests'] if test['requirementId'] != IDS['REQ-007']]
    row = next(item for item in agent_tables(export, CONTRACT)['handoffs'] if item['phase'] == 'QA' and item['codeVersion'] == 2)
    assert row['interpretation']['status'] == 'FAIL'
    assert row['interpretation']['detail']['missingRequirements'] == [IDS['REQ-007']]


def test_testing_another_snapshot_is_an_interpretation_failure():
    """Security가 받은 Snapshot이 아닌 다른 Snapshot을 검사했으면 해석 FAIL이다(v2 단계가 v1 Snapshot을 검사)."""
    export = run()
    v1 = output(export, 'SECURITY', 1, 'SECURITY_REPORT')['executionManifest']['projectArtifactId']
    output(export, 'SECURITY', 2, 'SECURITY_REPORT')['executionManifest']['projectArtifactId'] = v1
    row = next(item for item in agent_tables(export, CONTRACT)['handoffs'] if item['phase'] == 'SECURITY' and item['codeVersion'] == 2)
    assert row['interpretation']['status'] == 'FAIL' and row['interpretation']['detail']['testedOtherSnapshot']


def test_developer_source_missing_received_requirement_is_an_interpretation_failure():
    export = run()
    source = output(export, 'IMPLEMENTING', 1, 'SOURCE')
    source['requirementIds'].remove(IDS['REQ-005'])
    row = next(item for item in agent_tables(export, CONTRACT)['handoffs'] if item['phase'] == 'IMPLEMENTING')
    assert row['interpretation']['status'] == 'FAIL' and row['interpretation']['detail']['missingRequirements'] == [IDS['REQ-005']]


# ---------------------------------------------------------------- 작업 정확성

def test_planner_missing_requirement_fails_accuracy_and_interpretation():
    export = run()
    requirement = next(item for item in export['trace']['artifacts'] if item['artifactType'] == 'REQUIREMENT')
    requirement['payload']['requirements'] = [item for item in requirement['payload']['requirements']
                                              if item['requirementId'] != IDS['REQ-006']]
    tables = agent_tables(export, CONTRACT)
    row = accuracy(tables, '계획이 요구사항을 모두 작업에 연결(의존관계 모순 없음)')
    assert row['status'] == 'FAIL' and row['detail']['requirementListMismatch'] is True
    assert row['detail']['uncoveredRequirements'] == []  # 작업 연결과 요구사항 목록 손상을 구분
    assert tables['handoffs'][0]['interpretation']['status'] == 'FAIL'


def test_judgement_accuracy_counts_disagreement_without_asserting_false_failures():
    """독립 평가와 대조: QA는 마지막 버전에서 모두 PASS라고 했는데 평가에서 REQ-002가 FAIL이면 놓친 결함이다."""
    export = run()
    run_id = export['bundle']['run']['runId']
    tables = agent_tables(export, CONTRACT, report(ALL_PASS, run_id))
    assert accuracy(tables, 'QA 판정이 독립 평가와 일치')['status'] == 'PASS'
    assert accuracy(tables, '최종 결과물이 독립 평가 통과')['status'] == 'PASS'
    missed = agent_tables(export, CONTRACT, report({**ALL_PASS, ('QA', 'REQ-002'): 'FAIL'}, run_id))
    row = accuracy(missed, 'QA 판정이 독립 평가와 일치')
    assert (row['status'], row['detail']['missedDefects'], row['value']) == ('FAIL', ['REQ-002'], 'v2 일치 4/5')
    assert accuracy(missed, 'Security 판정이 독립 평가와 일치')['status'] == 'PASS'  # 관점별로 따로 본다
    assert accuracy(missed, '최종 결과물이 독립 평가 통과')['status'] == 'FAIL'
    # Security FAIL·외부 PASS는 불일치다. 외부 시험이 다루지 않은 결함일 수 있어 오탐으로 단정하지 않는다.
    sec = output(export, 'SECURITY', 2, 'SECURITY_REPORT')
    for item in sec['requirementResults']:
        if item['requirementId'] == IDS['REQ-005']:
            item['outcome'] = 'FAIL'
    wrong = accuracy(agent_tables(export, CONTRACT, report(ALL_PASS, run_id)), 'Security 판정이 독립 평가와 일치')
    assert (wrong['status'], wrong['detail']['agentFailExternalPass']) == ('FAIL', ['REQ-005'])
    assert wrong['detail']['interpretation'] == 'DISAGREEMENT_REQUIRES_REVIEW'
    assert 'falseFailures' not in wrong['detail']


def test_unverified_evaluation_check_is_not_counted_as_agreement():
    export = run()
    tables = agent_tables(export, CONTRACT, report({**ALL_PASS, ('QA', 'REQ-004'): 'UNVERIFIED'}, export['bundle']['run']['runId']))
    row = accuracy(tables, 'QA 판정이 독립 평가와 일치')
    assert (row['status'], row['detail']['notComparable'], row['value']) == ('UNVERIFIED', ['REQ-004'], 'v2 일치 4/5')


def test_report_from_another_run_is_refused():
    with pytest.raises(ValueError):
        agent_tables(run(), CONTRACT, report(ALL_PASS, FIXTURE['single']['bundle']['run']['runId']))


def test_primary_report_run_id_is_checked_even_when_optional_source_run_is_absent():
    export = run()
    independent = report(ALL_PASS, export['bundle']['run']['runId'])
    independent.pop('sourceRun')
    independent['sourceRunId'] = FIXTURE['single']['bundle']['run']['runId']
    with pytest.raises(ValueError, match='another Run'):
        agent_tables(export, CONTRACT, independent)


@pytest.mark.parametrize('damage,reason', [
    ('no_version', 'EVALUATED_VERSION_UNKNOWN'), ('no_identity', 'SNAPSHOT_IDENTITY_UNKNOWN'),
    ('different_artifact', 'PROJECT_ARTIFACT_MISMATCH'), ('unverified_manifest', 'EVALUATION_MANIFEST_NOT_VERIFIED'),
    ('no_checks', 'INDEPENDENT_CHECKS_MISSING')])
def test_judgement_requires_same_verified_artifact_and_actual_checks(damage, reason):
    export = run()
    independent = report(ALL_PASS, export['bundle']['run']['runId'])
    if damage == 'no_version':
        independent['expectedManifest'].pop('codeVersion')
    elif damage == 'no_identity':
        independent['expectedManifest'].pop('projectArtifactId')
    elif damage == 'different_artifact':
        independent['expectedManifest']['projectArtifactId'] = 'different'
    elif damage == 'unverified_manifest':
        independent['manifestCheck']['metadataMatches'] = False
    else:
        independent['checks'] = []
    row = accuracy(agent_tables(export, CONTRACT, independent), 'QA 판정이 독립 평가와 일치')
    assert row['status'] == 'UNVERIFIED' and row['detail'] == reason


def test_earlier_version_pass_does_not_mean_final_product_pass():
    export = run()
    independent = report(ALL_PASS, export['bundle']['run']['runId'])
    independent['expectedManifest']['codeVersion'] = 1
    row = accuracy(agent_tables(export, CONTRACT, independent), '최종 결과물이 독립 평가 통과')
    assert row['status'] == 'UNVERIFIED' and row['detail'] == 'FINAL_VERSION_NOT_EVALUATED'


@pytest.mark.parametrize('damage,reason', [
    ('other_source', 'PROJECT_ARTIFACT_MISMATCH'),
    ('missing_source', 'FINAL_SOURCE_MISSING'),
    ('missing_run', 'EVALUATION_RUN_IDENTITY_UNKNOWN'),
    ('other_architecture', 'EVALUATION_ARCHITECTURE_MISMATCH'),
    ('missing_checks', 'INDEPENDENT_CHECKS_INCOMPLETE'),
    ('missing_requirement', 'INDEPENDENT_CHECKS_INVALID'),
    ('empty_requirement_checks', 'INDEPENDENT_CHECKS_INCOMPLETE'),
])
def test_final_product_does_not_trust_pass_without_bound_complete_evidence(damage, reason):
    export = run()
    independent = report(ALL_PASS, export['bundle']['run']['runId'])
    if damage == 'other_source':
        independent['expectedManifest']['projectArtifactId'] = output(export, 'IMPLEMENTING', 1, 'SOURCE')['artifactId']
    elif damage == 'missing_source':
        export['trace']['artifacts'].remove(output(export, 'FIXING', 2, 'SOURCE'))
    elif damage == 'missing_run':
        independent.pop('sourceRunId')
        independent.pop('sourceRun')
    elif damage == 'other_architecture':
        independent['architecture'] = 'SINGLE_AGENT'
    elif damage == 'missing_checks':
        independent['checks'] = []
    elif damage == 'missing_requirement':
        independent['requirements'].pop()
    else:
        independent['requirements'][0]['checkIds'] = []
    row = accuracy(agent_tables(export, CONTRACT, independent), '최종 결과물이 독립 평가 통과')
    assert row['status'] == 'UNVERIFIED' and row['detail'] == reason


def test_judgement_keeps_missing_independent_requirement_in_the_denominator():
    export = run()
    incomplete = {key: status for key, status in ALL_PASS.items() if key != ('QA', 'REQ-004')}
    independent = report(incomplete, export['bundle']['run']['runId'])
    row = accuracy(agent_tables(export, CONTRACT, independent), 'QA 판정이 독립 평가와 일치')
    assert (row['status'], row['value'], row['detail']['notComparable']) == ('UNVERIFIED', 'v2 일치 4/5', ['REQ-004'])
    # 남은 검사에서 확인된 불일치는 미수집 검사 때문에 지워지지 않는다.
    independent = report({**incomplete, ('QA', 'REQ-002'): 'FAIL'}, export['bundle']['run']['runId'])
    row = accuracy(agent_tables(export, CONTRACT, independent), 'QA 판정이 독립 평가와 일치')
    assert row['status'] == 'FAIL' and row['detail']['missedDefects'] == ['REQ-002']
    assert row['detail']['notComparable'] == ['REQ-004']


@pytest.mark.parametrize('field', ['snapshotSha256', 'containerImageDigest', 'dependencyLockHash'])
def test_judgement_and_final_product_reject_same_id_with_conflicting_manifest(field):
    export = run()
    independent = report(ALL_PASS, export['bundle']['run']['runId'])
    independent['expectedManifest'][field] = 'a' * 64
    output(export, 'QA', 2, 'QA_REPORT')['executionManifest'][field] = 'b' * 64
    output(export, 'FIXING', 2, 'SOURCE')[field] = 'b' * 64
    tables = agent_tables(export, CONTRACT, independent)
    for item in ('QA 판정이 독립 평가와 일치', '최종 결과물이 독립 평가 통과'):
        assert accuracy(tables, item)['status'] == 'UNVERIFIED'


def test_judgement_without_run_identity_and_original_unverified_are_not_passed():
    export = run()
    independent = report(ALL_PASS, export['bundle']['run']['runId'])
    independent.pop('sourceRunId')
    independent.pop('sourceRun')
    assert accuracy(agent_tables(export, CONTRACT, independent), 'QA 판정이 독립 평가와 일치')['status'] == 'UNVERIFIED'
    independent = report(ALL_PASS, export['bundle']['run']['runId'])
    independent['evaluationVerdict'] = 'UNVERIFIED'
    assert accuracy(agent_tables(export, CONTRACT, independent), '최종 결과물이 독립 평가 통과')['status'] == 'UNVERIFIED'


@pytest.mark.parametrize('damage', ['duplicate_check', 'reused_check', 'wrong_perspective', 'duplicate_link', 'nontext_perspective'])
def test_independent_check_identity_and_requirement_assignment_cannot_inflate_accuracy(damage):
    export = run()
    independent = report(ALL_PASS, export['bundle']['run']['runId'])
    if damage == 'duplicate_check':
        independent['checks'].append(copy.deepcopy(independent['checks'][0]))
        independent['checks'][0]['status'] = 'FAIL'
    elif damage == 'reused_check':
        independent['checks'] = independent['checks'][:1]
        for requirement in independent['requirements']:
            requirement['checkIds'] = [independent['checks'][0]['testId']]
    elif damage == 'wrong_perspective':
        independent['checks'][0]['perspective'] = 'SECURITY'
    elif damage == 'nontext_perspective':
        independent['checks'][0]['perspective'] = []
    else:
        independent['requirements'][0]['checkIds'] *= 2
    tables = agent_tables(export, CONTRACT, independent)
    for item in ('최종 결과물이 독립 평가 통과', 'QA 판정이 독립 평가와 일치'):
        row = accuracy(tables, item)
        assert row['status'] == 'UNVERIFIED' and row['detail'] == 'INDEPENDENT_CHECKS_INVALID'


def test_missing_security_perspective_is_not_hidden_by_qa_for_same_requirement():
    export = run()
    incomplete = {key: status for key, status in ALL_PASS.items() if key != ('SECURITY', 'REQ-003')}
    independent = report(incomplete, export['bundle']['run']['runId'])
    tables = agent_tables(export, CONTRACT, independent)
    assert accuracy(tables, '최종 결과물이 독립 평가 통과')['status'] == 'UNVERIFIED'
    row = accuracy(tables, 'Security 판정이 독립 평가와 일치')
    assert (row['status'], row['value'], row['detail']['notComparable']) == ('UNVERIFIED', 'v2 일치 2/3', ['REQ-003'])


def test_unfixed_issue_and_ungrounded_issue_fail():
    """고친 버전에서도 Issue의 요구사항이 FAIL이면 수정 실패, Issue가 PASS 검사를 가리키면 근거 없음이다."""
    export = run()
    issue = export['bundle']['issues'][0]
    for test in output(export, 'QA', 2, 'QA_REPORT')['tests']:
        if test['requirementId'] in issue['requirementIds']:
            test['outcome'] = 'FAIL'
    assert accuracy(agent_tables(export, CONTRACT), '수정 요청을 고침(고친 버전에서 PASS)')['status'] == 'FAIL'
    export = run()
    for test in output(export, 'QA', 1, 'QA_REPORT')['tests']:
        test['outcome'] = 'PASS'
    grounding = [row for row in agent_tables(export, CONTRACT)['accuracy'] if row['item'] == 'Issue가 실제 FAIL 검사를 가리킴'][0]
    assert (grounding['role'], grounding['status'], grounding['value']) == ('QA', 'FAIL', '0/1')


def test_failed_build_is_counted_per_version():
    export = run()
    next(item for item in export['trace']['artifacts'] if item['artifactType'] == 'BUILD_REPORT')['executionOutcome'] = 'FAIL'
    row = accuracy(agent_tables(export, CONTRACT), '버전마다 빌드 성공')
    assert (row['status'], row['value']) == ('FAIL', '1/2 버전')


@pytest.mark.parametrize('damage', ['missing', 'unverified', 'duplicate'])
def test_missing_or_unverified_build_is_not_a_pass_or_confirmed_failure(damage):
    export = run()
    first = output(export, 'IMPLEMENTING', 1, 'BUILD_REPORT')
    if damage == 'missing':
        export['trace']['artifacts'].remove(first)
    elif damage == 'unverified':
        first['executionOutcome'] = 'UNVERIFIED'
    else:
        export['trace']['artifacts'].append(copy.deepcopy(first))
    row = accuracy(agent_tables(export, CONTRACT), '버전마다 빌드 성공')
    assert (row['status'], row['value']) == ('UNVERIFIED', '1/2 버전')
    output(export, 'FIXING', 2, 'BUILD_REPORT')['executionOutcome'] = 'FAIL'
    assert accuracy(agent_tables(export, CONTRACT), '버전마다 빌드 성공')['status'] == 'FAIL'


# ---------------------------------------------------------------- 처리시간·오류

def test_agent_errors_are_recorded_and_become_the_first_problem():
    """단계 실패·A2A 실패·LLM 실패는 처리시간 표의 오류이고, 전달·해석이 정상이면 오류 발생 지점이 된다."""
    export = run()
    step(export, 'SECURITY', 1)['status'] = 'FAILED'
    target = step(export, 'QA', 2)
    for row in export['a2a']['roles'][target['agentIdentity']]['events']:
        if row['taskId'] == target['a2aTaskId'] and row['event']['status']['state'] == 'TASK_STATE_COMPLETED':
            row['event']['status']['state'] = 'TASK_STATE_FAILED'
    planner = step(export, 'PLANNING', None)['workflowStepId']
    next(item for item in export['bundle']['usage']['records'] if item['binding']['workflowStepId'] == planner)['detail']['outcome'] = 'refused'
    tables = agent_tables(export, CONTRACT)
    timing = {row['role']: row for row in tables['timing']}
    assert timing['PLANNER']['errors'] == ['LLM 호출 refused'] and timing['PLANNER']['status'] == 'FAIL'
    assert timing['SECURITY']['errors'] == ['SECURITY v1: 단계 FAILED']
    assert timing['QA']['errors'] == ['QA v2: A2A TASK_STATE_FAILED']
    assert timing['QA']['agentMs'] is not None  # 실패도 끝난 상태라 시간은 잰다
    assert tables['firstProblem'] == {'kind': 'agentError', 'role': 'PLANNER', 'status': 'FAIL', 'detail': 'LLM 호출 refused'}


def test_unknown_measurements_stay_unknown():
    """사용량 기록이 없거나, 끝나지 않은 도구 호출·끝 상태가 없는 Task는 0이 아니라 None(모름)이다."""
    export = run()
    export['bundle']['usage'] = None
    export['trace']['telemetry'] = [item for item in export['trace']['telemetry']
                                    if not (item['binding']['eventType'] == 'MCP_TOOL_FINISHED'
                                            and item['binding']['workflowStepId'] == step(export, 'QA', 1)['workflowStepId'])]
    target = step(export, 'SECURITY', 2)
    roles = export['a2a']['roles'][target['agentIdentity']]
    roles['events'] = [row for row in roles['events']
                       if not (row['taskId'] == target['a2aTaskId'] and row['event']['status']['state'] == 'TASK_STATE_COMPLETED')]
    timing = {row['role']: row for row in agent_tables(export, CONTRACT)['timing']}
    assert all(row[key] is None for row in timing.values() for key in ('llmCalls', 'inputTokens', 'outputTokens', 'costUsd'))
    assert all(row['status'] == 'UNVERIFIED' for row in timing.values())
    assert timing['QA']['toolMs'] is None and 'NOT_FINISHED' in timing['QA']['toolOutcomes']
    assert timing['SECURITY']['agentMs'] is None
    assert timing['DEVELOPER']['toolMs'] is not None


def test_partial_cost_is_unknown_not_a_smaller_number():
    export = run()
    records = export['bundle']['usage']['records']
    for item in records:
        item['detail']['costUsd'] = 0.001
    developer = {item['workflowStepId'] for item in export['handoff']['steps'] if item['phase'] in ('IMPLEMENTING', 'FIXING')}
    assert {row['role']: row['costUsd'] for row in agent_tables(export, CONTRACT)['timing']}['DEVELOPER'] == 0.004
    next(item for item in records if item['binding']['workflowStepId'] in developer)['detail']['costUsd'] = None
    assert {row['role']: row['costUsd'] for row in agent_tables(export, CONTRACT)['timing']}['DEVELOPER'] is None


@pytest.mark.parametrize('damage,warning', [
    ('missing_rows', 'USAGE_RECORD_COUNT_UNVERIFIED'),
    ('inconsistent', 'USAGE_RECORDS_INCONSISTENT'),
    ('missing_total', 'USAGE_RECORD_COUNT_UNVERIFIED'),
    ('pending', 'USAGE_CALL_COUNT_UNVERIFIED'),
    ('call_count', 'USAGE_CALL_COUNT_UNVERIFIED'),
    ('duplicate', 'USAGE_SEQUENCE_DUPLICATED'),
    ('missing_sequence', 'USAGE_SEQUENCE_INVALID'),
    ('unassigned', 'USAGE_ROLE_UNASSIGNED'),
    ('foreign_run', 'USAGE_ROLE_UNASSIGNED'),
    ('conflicting_value', 'USAGE_VALUE_MISMATCH'),
])
def test_incomplete_usage_never_becomes_confirmed_zero_or_a_smaller_total(damage, warning):
    export = run()
    usage = export['bundle']['usage']
    for item in usage['records']:
        item['detail']['costUsd'] = '0.25'
    original_count = len(usage['records'])
    if damage == 'missing_rows': usage['records'].pop()
    elif damage == 'inconsistent': usage['recordsConsistent'] = False
    elif damage == 'missing_total': del usage['total']
    elif damage == 'pending': usage['summary']['pendingModelSequences'] = [original_count + 1]
    elif damage == 'call_count': usage['summary']['modelCalls'] += 1
    elif damage == 'duplicate': usage['records'][-1]['detail']['sequence'] = usage['records'][0]['detail']['sequence']
    elif damage == 'missing_sequence': del usage['records'][0]['detail']['sequence']
    elif damage == 'unassigned': usage['records'][0]['binding']['workflowStepId'] = 'unknown-step'
    elif damage == 'foreign_run': usage['records'][0]['binding']['runId'] = 'another-run'
    else: usage['records'][0]['costUsd'] = '999'
    tables = agent_tables(export, CONTRACT)
    assert all(row[key] is None for row in tables['timing']
               for key in ('llmCalls', 'llmMs', 'inputTokens', 'outputTokens', 'costUsd', 'reviewLlmCalls'))
    assert all(row['status'] == 'UNVERIFIED' and warning in row['usageWarnings'] for row in tables['timing'])
    # 안전하게 배정한 고유 영수증의 부분 지출은 보존하되 중복/충돌/다른 Run 기록은 세지 않는다.
    known_calls = sum(row['knownLlmCalls'] or 0 for row in tables['timing'])
    assert 0 < known_calls <= original_count
    assert sum(row['knownCostUsd'] or 0 for row in tables['timing']) == known_calls * 0.25
    assert all(row['costUsd']['known'] == 0 for row in summarize([tables])['groups'][0]['timing'])


def test_missing_usage_rows_are_unknown_but_confirmed_zero_calls_remain_zero():
    export = run()
    usage = export['bundle']['usage']
    usage['records'] = []
    usage['recordsConsistent'] = False
    rows = agent_tables(export, CONTRACT)['timing']
    assert all(row['costUsd'] is None and row['knownCostUsd'] is None and row['llmCalls'] is None for row in rows)
    assert all(row['status'] == 'UNVERIFIED' for row in rows)
    usage.update(total=0, recordsConsistent=True, summary={'modelCalls': 0})
    rows = agent_tables(export, CONTRACT)['timing']
    assert all(row['costUsd'] == 0 and row['knownCostUsd'] == 0 and row['llmCalls'] == 0 for row in rows)
    assert all(row['usageComplete'] is True for row in rows)


@pytest.mark.parametrize('damage,warning,tokens_known', [
    ('usage_unknown', 'USAGE_TOKEN_SUMMARY_UNVERIFIED', False),
    ('cost_unknown', 'USAGE_COST_SUMMARY_UNVERIFIED', True),
    ('total_tokens', 'USAGE_TOKEN_SUMMARY_MISMATCH', False),
    ('known_tokens', 'USAGE_TOKEN_SUMMARY_MISMATCH', False),
    ('cost', 'USAGE_COST_SUMMARY_MISMATCH', True),
    ('known_cost', 'USAGE_COST_SUMMARY_MISMATCH', True),
    ('receipt_total', 'USAGE_TOKEN_VALUE_MISMATCH', False),
])
def test_summary_uncertainty_or_conflict_never_confirms_role_measurements(damage, warning, tokens_known):
    export = run()
    usage = export['bundle']['usage']
    for item in usage['records']:
        item['detail']['costUsd'] = '0.25'
        item['detail']['totalTokens'] = item['detail']['inputTokens'] + item['detail']['outputTokens']
    token_sum = sum(item['detail']['inputTokens'] + item['detail']['outputTokens'] for item in usage['records'])
    cost_sum = str(len(usage['records']) * 0.25)
    usage['summary'].update(usageComplete=True, totalTokens=token_sum, knownTotalTokens=token_sum,
                            costComplete=True, costUsd=cost_sum, knownCostUsd=cost_sum)
    baseline = agent_tables(export, CONTRACT)['timing']
    assert all(row['status'] == 'PASS' and row['usageComplete'] for row in baseline)
    if damage == 'usage_unknown':
        usage['summary'].update(usageComplete=False, totalTokens=None)
    elif damage == 'cost_unknown':
        usage['summary'].update(costComplete=False, costUsd=None)
    elif damage == 'total_tokens': usage['summary']['totalTokens'] += 1
    elif damage == 'known_tokens': usage['summary']['knownTotalTokens'] += 1
    elif damage == 'cost': usage['summary']['costUsd'] = '999'
    elif damage == 'known_cost': usage['summary']['knownCostUsd'] = '999'
    else: usage['records'][0]['detail']['totalTokens'] += 1
    rows = agent_tables(export, CONTRACT)['timing']
    for row, previous in zip(rows, baseline):
        # A complete call ledger remains useful even when token/cost measurements conflict.
        assert row['llmCalls'] == previous['llmCalls'] and row['llmMs'] == previous['llmMs']
        assert row['callRecordsComplete'] is True and row['usageComplete'] is False
        assert row['status'] == 'UNVERIFIED' and warning in row['usageWarnings']
        assert row['costUsd'] is None and row['reviewCostUsd'] is None
        assert row['knownCostUsd'] == previous['knownCostUsd']
        assert (row['inputTokens'] is not None) == tokens_known
        assert (row['outputTokens'] is not None) == tokens_known


@pytest.mark.parametrize('key,value', [('inputTokens', None), ('outputTokens', -1), ('inputTokens', True),
                                      ('outputTokens', 'unknown'), ('durationMs', None), ('durationMs', float('nan'))])
def test_unknown_call_measurements_do_not_crash_or_become_zero(key, value):
    export = run()
    record = export['bundle']['usage']['records'][0]
    record['detail'][key] = value
    planner = next(row for row in agent_tables(export, CONTRACT)['timing'] if row['role'] == 'PLANNER')
    assert planner['llmCalls'] == 1  # 호출 자체는 확인됨. 그 호출의 측정값만 미확인이다.
    assert planner['llmMs' if key == 'durationMs' else key] is None


def test_unknown_provider_usage_cannot_be_reported_as_zero_tokens_or_cost():
    export = run()
    detail = export['bundle']['usage']['records'][0]['detail']
    detail.update(usageKnown=False, inputTokens=0, outputTokens=0, costUsd='0')
    planner = next(row for row in agent_tables(export, CONTRACT)['timing'] if row['role'] == 'PLANNER')
    assert planner['llmCalls'] == 1 and planner['llmMs'] == 1
    assert all(planner[key] is None for key in ('inputTokens', 'outputTokens', 'costUsd', 'knownCostUsd'))


@pytest.mark.parametrize('role,kind,field', [('QA', 'QA_REPORT', 'tests'), ('SECURITY', 'SECURITY_REPORT', 'requirementResults')])
@pytest.mark.parametrize('second', ['missing', 'UNVERIFIED', 'FAIL'])
def test_fix_requires_every_issue_requirement_and_preserves_confirmed_failure(role, kind, field, second):
    export = run()
    issue = export['bundle']['issues'][0]
    issue['reporter'] = role
    checked = output(export, role, 2, kind)
    targets = list(dict.fromkeys(row['requirementId'] for row in checked[field]))[:2]
    issue['requirementIds'] = targets
    checked[field] = [row for row in checked[field] if row['requirementId'] in targets]
    for row in checked[field]:
        row['outcome'] = 'FAIL' if row['requirementId'] == targets[0] else second
    if second == 'missing':
        checked[field] = [row for row in checked[field] if row['requirementId'] != targets[1]]
    result = accuracy(agent_tables(export, CONTRACT), '수정 요청을 고침(고친 버전에서 PASS)')
    assert result['status'] == ('FAIL' if second == 'FAIL' else 'UNVERIFIED')
    assert result['value'] == '0/1' and result['detail']['confirmedFailedIssues'] == 1
    assert targets[0] in result['detail']['issues'][0]['confirmedFailedRequirements']


def test_one_pass_cannot_cover_an_omitted_issue_requirement():
    export = run()
    checked = output(export, 'QA', 2, 'QA_REPORT')
    targets = list(dict.fromkeys(row['requirementId'] for row in checked['tests']))[:2]
    export['bundle']['issues'][0]['requirementIds'] = targets
    checked['tests'] = [row for row in checked['tests'] if row['requirementId'] == targets[0]]
    result = accuracy(agent_tables(export, CONTRACT), '수정 요청을 고침(고친 버전에서 PASS)')
    assert result['status'] == 'UNVERIFIED' and result['value'] == '0/1'
    assert result['detail']['issues'][0]['missingRequirements'] == [targets[1]]


def test_host_decimal_string_costs_are_summed_exactly():
    # dev Host는 비용을 "0.001" 같은 십진 문자열로 준다(pricing.UsageRecord.cost_usd). 숫자로 더하다 멈추면 안 된다(점검 2).
    export = run()
    records = export['bundle']['usage']['records']
    for item in records:
        item['detail']['costUsd'] = '0.001'
    tables = agent_tables(export, CONTRACT)
    assert {row['role']: row['costUsd'] for row in tables['timing']}['DEVELOPER'] == 0.004
    assert '$0.004' in render_markdown(tables)  # 표 만들기까지 끝난다
    for item in records:
        item['detail']['costUsd'] = '0.1'
    assert {row['role']: row['costUsd'] for row in agent_tables(export, CONTRACT)['timing']}['DEVELOPER'] == 0.4  # 0.1×4가 정확히 0.4


@pytest.mark.parametrize('bad', ['abc', '-0.001', 'NaN', 'Infinity', True, [], '1e400'])
def test_malformed_costs_are_unknown_not_crashes(bad):
    export = run()
    records = export['bundle']['usage']['records']
    for item in records:
        item['detail']['costUsd'] = '0.001'
    developer = {item['workflowStepId'] for item in export['handoff']['steps'] if item['phase'] in ('IMPLEMENTING', 'FIXING')}
    next(item for item in records if item['binding']['workflowStepId'] in developer)['detail']['costUsd'] = bad
    assert {row['role']: row['costUsd'] for row in agent_tables(export, CONTRACT)['timing']}['DEVELOPER'] is None


# ---------------------------------------------------------------- 보여 주기

def test_markdown_shows_three_tables_unknowns_and_defects():
    text = render_markdown(agent_tables(run(), CONTRACT))
    for heading in ('## ① 작업 정확성', '## ② 정보 전달', '## ③ 처리시간·오류', '## 오류 발생 지점', '## 발견된 제품 결함'):
        assert heading in text
    assert '독립 평가 보고서 없음(그 칸은 미검증)' in text and '| 미확인 |' in text  # 비용 모름은 0이 아니라 미확인
    assert 'QA가 v1에서 REQ-001 결함 발견(QA_ASSERTION_FAILURE) → v2에서 수정 요청' in text
    assert text.count('\n| 수정 | Developer | v2 |') == 1


def test_cli_writes_json_and_markdown(tmp_path):
    from evaluation.agent_eval import main
    export = run('single')
    folder = tmp_path / 'export'
    folder.mkdir()
    for key, name in {'bundle': 'bundle.json', 'trace': 'trace.json', 'a2a': 'a2a-messages.json', 'handoff': 'handoff.json'}.items():
        (folder / name).write_text(json.dumps(export[key]), encoding='utf-8')
    output_json = {'report': report(ALL_PASS, export['bundle']['run']['runId'])}  # D5 결과 전체 모양도 받는다
    (tmp_path / 'output.json').write_text(json.dumps(output_json), encoding='utf-8')
    assert main(['--export', str(folder), '--report', str(tmp_path / 'output.json'), '--out', str(tmp_path / 'out')]) == 0
    tables = json.loads((tmp_path / 'out/agent_tables.json').read_text(encoding='utf-8'))
    assert tables['evaluationReport'] is True and tables['architecture'] == 'SINGLE_AGENT'
    assert accuracy(tables, 'QA 판정이 독립 평가와 일치')['status'] == 'PASS'
    assert '독립 평가 보고서 있음' in (tmp_path / 'out/agent_tables.md').read_text(encoding='utf-8')


# ---------------------------------------------------------------- 시나리오 정책·새 역할

def test_run_frozen_contract_is_the_default():
    """정책을 주지 않으면 Run이 고정한 정책을 쓰고, 그것이 없으면 Run의 scenarioId로 등록 시나리오를 찾는다."""
    export = run()
    assert agent_tables(export) == agent_tables(export, CONTRACT)  # 고정 정책 없음 → 등록된 SCN-001
    assert agent_tables(export)['scenarioKey'] == 'SCN-001'
    frozen = copy.deepcopy(CONTRACT)
    frozen['scenarioKey'] = 'SCN-TEST'  # 등록 시나리오가 아니라 Run이 고정한 정책을 읽는지 본다
    export['bundle']['configuration'] = {'scenarioContract': frozen}
    tables = agent_tables(export)
    assert tables['scenarioKey'] == 'SCN-TEST' and accuracy(tables, '계획이 요구사항을 모두 작업에 연결(의존관계 모순 없음)')['status'] == 'PASS'


def test_contract_of_another_scenario_or_no_contract_is_refused():
    export = run()
    export['bundle']['configuration'] = {'scenarioContract': CONTRACT}
    other = {**CONTRACT, 'requirements': CONTRACT['requirements'][:-1]}  # 요구사항이 다른 정책 = 다른 시나리오
    with pytest.raises(ValueError, match='differs'):
        agent_tables(export, other)
    export = run()
    export['bundle']['run']['scenarioId'] = '00000000-0000-4000-8000-0000000000aa'  # 등록되지 않은 시나리오
    with pytest.raises(ValueError, match='not found'):
        agent_tables(export)
    del export['bundle']['run']['scenarioId']
    with pytest.raises(ValueError, match='not found'):
        agent_tables(export)


def test_unmapped_phase_is_unverified_but_still_timed():
    """평가 기준이 없는 phase는 Planner·Security 등으로 채점하지 않고 미검증, 시간·오류 표에는 따로 남는다."""
    export = run()
    planner = step(export, 'PLANNING', None)
    export['handoff']['steps'].insert(1, {**planner, 'workflowStepId': '00000000-0000-4000-8000-0000000000bb',
                                          'phase': 'LEAD_ASSIST', 'status': 'FAILED'})
    tables = agent_tables(export, CONTRACT)
    row = tables['handoffs'][1]
    assert (row['role'], row['delivery']['detail'], row['interpretation']['status']) == ('LEAD_ASSIST', 'PHASE_NOT_MAPPED', 'UNVERIFIED')
    assert accuracy(tables, '평가 기준이 정해지지 않은 에이전트 단계') == {
        'role': 'LEAD_ASSIST', 'item': '평가 기준이 정해지지 않은 에이전트 단계', 'status': 'UNVERIFIED', 'value': None,
        'detail': 'PHASE_NOT_MAPPED'}
    timing = {item['role']: item for item in tables['timing']}
    assert timing['LEAD_ASSIST']['steps'] == 1 and timing['LEAD_ASSIST']['errors'] == ['LEAD_ASSIST vNone: 단계 FAILED']
    assert timing['PLANNER']['steps'] == 1  # 다른 역할의 표는 그대로
    assert tables['firstProblem']['role'] == 'LEAD_ASSIST'
    assert '| LEAD_ASSIST | LEAD_ASSIST | - | 미검증 · PHASE_NOT_MAPPED |' in render_markdown(tables)


# ---------------------------------------------------------------- 여러 Run 모으기

def _other_run(export, run_id):
    """같은 기록을 다른 Run 번호로(모으기 시험용)"""
    for part in ('trace', 'a2a', 'handoff'):
        export[part]['runId'] = run_id
    export['bundle']['run']['runId'] = run_id
    return export


def test_summary_groups_runs_and_keeps_unknowns_out_of_medians():
    broken = _other_run(run(), '00000000-0000-4000-8000-0000000000cc')
    task(broken, 'QA', 1)['metadata']['requirementIds'].remove(IDS['REQ-004'])  # 전달 누락
    broken['bundle']['usage'] = None  # 사용량 모름
    summary = summarize([agent_tables(run(), CONTRACT), agent_tables(run('single'), CONTRACT), agent_tables(broken, CONTRACT)])
    assert summary['runs'] == 3
    assert [(group['scenario'], group['architecture'], group['runs']) for group in summary['groups']] == [
        ('SCN-001', 'SINGLE_AGENT', 1), ('SCN-001', 'MULTI_AGENT', 2)]
    multi = summary['groups'][1]
    qa = next(row for row in multi['handoffs'] if row['role'] == 'QA')
    assert qa['steps'] == 4 and qa['delivery'] == {'PASS': 3, 'FAIL': 1, 'UNVERIFIED': 0, 'NOT_APPLICABLE': 0}
    assert {row['where']: row['runs'] for row in multi['firstProblems']} == {'없음': 1, 'QA · QA 검증 · delivery': 1}
    planner = next(row for row in multi['timing'] if row['role'] == 'PLANNER')
    assert planner['llmCalls']['known'] == 1 and planner['llmCalls']['runs'] == 2  # 모르는 Run은 빼고 셈
    assert planner['costUsd'] == {'median': None, 'known': 0, 'runs': 2}  # 가짜 LLM 기록에는 비용이 없다(0이 아님)
    fixes = next(row for row in multi['accuracy'] if row['item'] == '수정 요청을 고침(고친 버전에서 PASS)')
    assert fixes['counts']['PASS'] == 2
    text = render_summary_markdown(summary)
    assert '## SCN-001 · MULTI_AGENT (Run 2개, 독립 평가 보고서 0개)' in text and '(알려진 1/2 Run)' in text
    assert '| QA | 4 | PASS 3 · FAIL 1 |' in text


def test_summary_refuses_the_same_run_twice_and_unknown_schema():
    tables = agent_tables(run(), CONTRACT)
    with pytest.raises(ValueError, match='twice'):
        summarize([tables, copy.deepcopy(tables)])
    with pytest.raises(ValueError, match='schema'):
        summarize([{**tables, 'schemaVersion': 2}])


def test_cli_summarizes_a_results_folder(tmp_path):
    from evaluation.agent_eval import main
    for name in ('multiFix', 'single'):
        folder = tmp_path / 'results' / name
        folder.mkdir(parents=True)
        (folder / 'agent_tables.json').write_text(json.dumps(agent_tables(run(name), CONTRACT)), encoding='utf-8')
    assert main(['--summarize', str(tmp_path / 'results'), '--out', str(tmp_path / 'out')]) == 0
    summary = json.loads((tmp_path / 'out/agent_summary.json').read_text(encoding='utf-8'))
    assert [group['architecture'] for group in summary['groups']] == ['SINGLE_AGENT', 'MULTI_AGENT']
    assert '### ③ 처리시간·오류' in (tmp_path / 'out/agent_summary.md').read_text(encoding='utf-8')
    with pytest.raises(SystemExit):
        main(['--summarize', str(tmp_path / 'results'), '--report', 'x.json', '--out', str(tmp_path / 'out2')])
