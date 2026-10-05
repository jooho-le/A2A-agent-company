"""build_report() 테스트: dev 스키마 호환, 근거 없는 판정·다른 코드 결과·모순된 실행 근거 거부(가짜 데이터)."""
from copy import deepcopy
from dataclasses import replace
import json
from uuid import UUID

import pytest
from jsonschema import ValidationError

from evaluation.aggregation import CheckPlan, CheckResult, Status
from evaluation.manifest import ExecutionManifest
# pytest가 TestBinding을 테스트 클래스로 오해하지 않도록 이름을 바꿔 import한다.
from evaluation.reporting import TestBinding as Binding, build_report, validate_contract


def uid(n):
    """테스트용 가짜 UUIDv4."""
    return str(UUID(int=n, version=4))


def inputs(role='QA', status=Status.PASS):
    """최소 정상 입력(요구사항 uid(5), 검사 uid(6), 증거 uid(7), 도구 실행 uid(8)). 테스트마다 일부만 바꾼다."""
    manifest = ExecutionManifest('repo', 1, uid(1), 'sha1', 'a'*40, 'b'*40,
                                 'c'*64, 'sha256:'+'d'*64, 'sha256:'+'e'*64)
    return dict(
        role=role, context=dict(artifactId=uid(2), artifactVersion=1,
            previousArtifactId=None, runId=uid(3), workflowStepId=uid(4),
            a2aTaskId='task/opaque', a2aArtifactId='artifact/opaque',
            createdAt='2026-10-04T12:00:00Z', requirementIds=[uid(5)]),
        expected=manifest, observed=manifest, plan=[CheckPlan(uid(6))],
        results=[CheckResult(uid(6), status, (uid(7),))],
        bindings={uid(6): Binding('QA-SIGNUP-001', uid(5), 'Signup')},
        tool_evidence={uid(6): dict(toolName='run_unit_tests' if role == 'QA' else 'run_security_scan',
            executionId=uid(8), executionManifest=manifest.to_payload(),
            evidenceRef='artifact://evidence/report.json', attempts=[dict(attempt=0,
                outcome='UNVERIFIED' if status in (Status.ERROR, Status.NOT_RUN) else 'PASS',
                evidenceRef='artifact://evidence/attempt.json')])},
        findings=[] if role == 'SECURITY' else None,
    )


@pytest.mark.parametrize('role', ['QA', 'SECURITY'])
@pytest.mark.parametrize('status', [Status.PASS, Status.FAIL, Status.ERROR, Status.NOT_RUN])
def test_reports_preserve_product_status_and_tool_provenance(role, status):
    """QA·SECURITY × 4개 상태: 판정 변환, 원래 상태 보존, 실행 근거 유지, 스키마 통과."""
    args = inputs(role, status)
    report = build_report(**args)
    row = report['tests' if role == 'QA' else 'requirementResults'][0]
    assert row['outcome'] == (status.value if status in (Status.PASS, Status.FAIL) else 'UNVERIFIED')
    assert json.loads(row['details'])['localStatus'] == status.value
    assert row['toolEvidence'] == args['tool_evidence'][uid(6)]
    assert 'finalVerdict' not in report
    if role == 'QA':
        assert row['testId'] == 'QA-SIGNUP-001'
    validate_contract(report, 'qa_report' if role == 'QA' else 'security_report')


def test_missing_result_is_unverified_without_invented_execution():
    """결과가 없으면 UNVERIFIED(RESULT_MISSING)로 쓰고, 있지도 않은 도구 실행 기록을 만들지 않는다."""
    args = inputs(); args['results'] = []; args['tool_evidence'] = {}
    row = build_report(**args)['tests'][0]
    assert row['outcome'] == 'UNVERIFIED'
    assert row['toolEvidence'] is None
    assert json.loads(row['details'])['errorCode'] == 'RESULT_MISSING'


@pytest.mark.parametrize('status', [Status.PASS, Status.FAIL])
def test_product_verdict_without_tool_evidence_rejected(status):
    """PASS·FAIL 판정에 도구 실행 근거가 없으면 "말만 있는 판정"이므로 거부한다."""
    args = inputs(status=status); args['tool_evidence'] = {}
    with pytest.raises(ValueError, match='actual Tool'):
        build_report(**args)


@pytest.mark.parametrize('alteration', ['manifest', 'tool', 'gap', 'retry', 'outcome', 'uri'])
def test_bad_provenance_rejected(alteration):
    """다른 스냅샷·역할에 안 맞는 도구·시도 번호 누락·성공 후 재시도·도구 FAIL·로컬 경로 근거를 거부한다."""
    args = inputs(); evidence = args['tool_evidence'][uid(6)]
    if alteration == 'manifest': evidence['executionManifest']['snapshotSha256'] = 'f'*64
    if alteration == 'tool': evidence['toolName'] = 'run_security_scan'
    if alteration == 'gap': evidence['attempts'][0]['attempt'] = 1
    if alteration == 'retry':
        evidence['attempts'].append(dict(evidence['attempts'][0], attempt=1))
    if alteration == 'outcome': evidence['attempts'][0]['outcome'] = 'FAIL'
    if alteration == 'uri': evidence['evidenceRef'] = 'file:///tmp/report'
    with pytest.raises((ValueError, ValidationError)):
        build_report(**args)


def test_safe_retry_exhaustion_is_preserved():
    """안전한 오류(RESOURCE_BUSY)로 최초 1회 + 재시도 2회를 모두 실패한 기록이 그대로 보존된다."""
    args = inputs(status=Status.ERROR); evidence = args['tool_evidence'][uid(6)]
    evidence['attempts'] = [dict(attempt=i, outcome='UNVERIFIED',
        evidenceRef=f'artifact://evidence/{i}', errorKind='RESOURCE_BUSY') for i in range(3)]
    report = build_report(**args)
    assert len(report['tests'][0]['toolEvidence']['attempts']) == 3


@pytest.mark.parametrize('alteration', ['binding_missing', 'requirement', 'optional', 'na',
                                      'context', 'lineage', 'snapshot', 'task'])
def test_ambiguous_input_rejected(alteration):
    """대응표 누락·요구사항 불일치·선택 검사·N/A·허용 안 된 키·버전 연결 오류·다른 코드·빈 Task ID를 거부한다."""
    args = inputs()
    if alteration == 'binding_missing': args['bindings'] = {}
    if alteration == 'requirement': args['context']['requirementIds'] = [uid(99)]
    if alteration == 'optional': args['plan'] = [CheckPlan(uid(6), required=False)]
    if alteration == 'na': args['results'] = [CheckResult(uid(6), Status.NOT_APPLICABLE)]
    if alteration == 'context': args['context']['finalVerdict'] = 'SUCCESS'
    if alteration == 'lineage': args['context']['artifactVersion'] = 2
    if alteration == 'snapshot': args['observed'] = replace(args['expected'], code_version=2)
    if alteration == 'task': args['context']['a2aTaskId'] = ' '
    with pytest.raises((ValueError, ValidationError)):
        build_report(**args)


def test_security_findings_preserved_without_mutating_input():
    """보안 발견사항은 보고서에 그대로 담기고, 보고서를 고쳐도 호출자의 원본은 바뀌지 않는다."""
    args = inputs('SECURITY')
    finding = dict(findingId='finding-1', severity='HIGH', disposition='CONFIRMED',
                   title='Storage', description='Synthetic defect', requirementId=uid(5))
    args['findings'] = [finding]
    before = deepcopy(args)
    report = build_report(**args)
    assert report['findings'] == [finding]
    assert args == before
    report['findings'][0]['title'] = 'changed'
    assert finding['title'] == 'Storage'


@pytest.mark.parametrize('alteration', ['missing', 'duplicate', 'foreign_requirement', 'multiple_checks'])
def test_security_ambiguous_evidence_or_findings_rejected(alteration):
    """SECURITY 규칙: findings 미지정·중복 ID·범위 밖 요구사항·요구사항당 검사 2개를 거부한다."""
    args = inputs('SECURITY')
    if alteration == 'missing': args['findings'] = None
    if alteration in ('duplicate', 'foreign_requirement'):
        f = dict(findingId='f', severity='LOW', disposition='SUSPECTED', title='t',
                 description='d', requirementId=uid(999) if alteration == 'foreign_requirement' else uid(5))
        args['findings'] = [f, f] if alteration == 'duplicate' else [f]
    if alteration == 'multiple_checks':
        args['plan'].append(CheckPlan(uid(9)))
        args['bindings'][uid(9)] = Binding('SEC-2', uid(5), 'Second check')
    with pytest.raises(ValueError):
        build_report(**args)


def test_manifest_roundtrip_and_unknown_fields():
    """Manifest를 JSON 형식으로 바꿨다가 되돌리면 같고, 모르는 필드(snapshotId)가 섞이면 거부한다."""
    manifest = inputs()['expected']
    assert ExecutionManifest.from_payload(manifest.to_payload()) == manifest
    with pytest.raises(ValueError):
        ExecutionManifest.from_payload(dict(manifest.to_payload(), snapshotId=uid(90)))


@pytest.mark.parametrize('uri', ['artifact:', 'https:/missing-host', 'FILE:///tmp/result'])
def test_schema_uri_is_also_compatible_with_dev_semantics(uri):
    """스키마는 통과해도 dev가 거부하는 주소(빈 경로·호스트 없음·대문자 FILE)는 거부한다."""
    args = inputs(); args['tool_evidence'][uid(6)]['evidenceRef'] = uri
    with pytest.raises((ValueError, ValidationError)):
        build_report(**args)


def test_same_execution_id_cannot_have_conflicting_evidence():
    """두 검사가 같은 executionId를 쓰면서 근거 내용(evidenceRef)이 다르면 모순이므로 거부한다."""
    args = inputs()
    args['plan'].append(CheckPlan(uid(9)))
    args['bindings'][uid(9)] = Binding('QA-2', uid(5), 'Second')
    args['results'].append(CheckResult(uid(9), Status.PASS, (uid(7),)))
    args['tool_evidence'][uid(9)] = deepcopy(args['tool_evidence'][uid(6)])
    args['tool_evidence'][uid(9)]['evidenceRef'] = 'artifact://conflicting/report'
    with pytest.raises(ValueError, match='Contradictory'):
        build_report(**args)


def test_diagnostics_survive_agent_conversion_and_mask_known_secrets():
    """기대값·실제값·위치·사유가 보고서로 전달되고, 알려 준 비밀값(demo-secret)은 [REDACTED]로 가려진다."""
    args = inputs(status=Status.FAIL)
    args['results'] = [CheckResult(uid(6), Status.FAIL, (uid(7),),
        expected_result='password absent', actual_result='demo-secret found',
        reason='response leaked demo-secret', normalized_location='response.body',
        error_code='SECRET_EXPOSURE')]
    args['sensitive_values'] = ['demo-secret']
    row = build_report(**args)['tests'][0]
    assert row['actualResult'] == '[REDACTED] found'
    assert row['expectedResult'] == 'password absent'
    assert row['normalizedLocation'] == 'response.body'
    assert json.loads(row['details'])['reason'] == 'response leaked [REDACTED]'
    assert json.loads(row['details'])['errorCode'] == 'SECRET_EXPOSURE'
