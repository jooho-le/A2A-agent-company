"""build_evaluation_report() 테스트: A2A ID 없이 생성, 다중 검사 증거 보존, 통과율 구분, 범위·코드 불일치 시 PASS 금지."""
from dataclasses import replace
import json
from uuid import UUID

import pytest

from evaluation.aggregation import CheckPlan, CheckResult, Status
from evaluation.independent import EvaluationBinding, build_evaluation_report
from evaluation.manifest import ExecutionManifest


def uid(number):
    """테스트용 가짜 UUIDv4."""
    return str(UUID(int=number, version=4))


def inputs():
    """정상 입력: QA 요구 uid(10)←검사 20, SECURITY 요구 uid(11)←검사 21·22. 모두 PASS, 증거는 번호+100."""
    manifest = ExecutionManifest('test-repo', 1, uid(1), 'sha1', 'a'*40, 'b'*40,
                                 'c'*64, 'sha256:'+'d'*64, 'sha256:'+'e'*64)
    return dict(
        evaluation_id=uid(2), source_run_id=uid(3), architecture='MULTI_AGENT',
        suite_id='membership-protected', suite_version='1', suite_sha256='f'*64,
        created_at='2026-10-06T12:00:00+09:00',
        required_requirement_ids=[uid(10), uid(11)],
        expected=manifest, observed=manifest,
        plan=[CheckPlan(uid(n)) for n in (20, 21, 22)],
        results=[CheckResult(uid(n), Status.PASS, (uid(n+100),)) for n in (20, 21, 22)],
        bindings={
            uid(20): EvaluationBinding(uid(10), 'QA', '정상 가입'),
            uid(21): EvaluationBinding(uid(11), 'SECURITY', 'API 비밀번호 노출'),
            uid(22): EvaluationBinding(uid(11), 'SECURITY', '로그 비밀번호 노출'),
        },
    )


def test_independent_identity_and_multiple_security_checks():
    """A2A ID 없이 보고서가 만들어지고, 같은 요구사항의 보안 검사 2개가 각자의 증거와 함께 남는다."""
    args = inputs()
    report = build_evaluation_report(**args)
    assert report['evaluationId'] != report['sourceRunId']
    assert 'a2aTaskId' not in report and 'finalVerdict' not in report
    assert report['evaluationVerdict'] == 'PASS'
    assert len(report['requirements']) == 2 and len(report['checks']) == 3
    assert report['requirements'][1]['checkIds'] == [uid(21), uid(22)]
    assert report['checks'][1]['evidenceIds'] == [uid(121)]
    assert report['checks'][2]['evidenceIds'] == [uid(122)]
    assert report['perspectives']['SECURITY']['validationVerdict'] == 'PASS'
    json.dumps(report)


def test_failure_details_and_requirement_denominator():
    """로그 검사 1개가 FAIL이면: 검사 통과율 2/3, 요구사항 통과율 1/2이고 실패 설명이 보존된다."""
    args = inputs()
    args['results'][2] = CheckResult(uid(22), Status.FAIL, (uid(122),),
        expected_result='로그에 비밀번호 없음', actual_result='비밀번호 발견',
        reason='요청 본문이 로그에 기록됨', normalized_location='server.log:request')
    report = build_evaluation_report(**args)
    assert report['evaluationVerdict'] == 'FAIL'
    assert report['requiredCheckPassRate'] == pytest.approx(2/3)
    assert report['requiredRequirementPassRate'] == 0.5
    assert report['checks'][2]['reason'] == '요청 본문이 로그에 기록됨'
    assert report['checks'][2]['actualResult'] == '비밀번호 발견'
    assert report['requirements'][1]['confirmedFailureIds'] == [uid(22)]


def test_missing_check_does_not_hide_confirmed_failure():
    """로그 검사 결과가 빠져 전체가 UNVERIFIED여도, API 검사에서 확인된 FAIL은 따로 남는다."""
    args = inputs()
    args['results'] = [args['results'][0], CheckResult(uid(21), Status.FAIL, (uid(121),))]
    report = build_evaluation_report(**args)
    assert report['evaluationVerdict'] == 'UNVERIFIED'
    assert report['confirmedFailureIds'] == [uid(21)]
    assert report['unverifiedTestIds'] == [uid(22)]
    assert report['checks'][2]['errorCode'] == 'RESULT_MISSING'


@pytest.mark.parametrize('observed', ['missing', 'different'])
def test_wrong_snapshot_cannot_supply_pass_or_failure(observed):
    """결과의 Manifest가 없거나 다르면 PASS도 FAIL도 인정하지 않고 Manifest 오류로 남긴다."""
    args = inputs()
    args['observed'] = None if observed == 'missing' else replace(args['expected'], code_version=2)
    report = build_evaluation_report(**args)
    assert report['evaluationVerdict'] == 'UNVERIFIED'
    assert report['requiredRequirementPassRate'] is None
    assert report['requiredCheckPassRate'] is None
    assert all(p['requiredPassRate'] is None for p in report['perspectives'].values())
    assert report['confirmedFailureIds'] == []
    assert not report['manifestCheck']['metadataMatches']


def test_diagnostics_redact_known_password_and_hash_without_mutating_input():
    """알려 준 비밀번호·해시는 보고서 어디에도 남지 않고, 호출자가 넘긴 원본 결과는 바뀌지 않는다."""
    args = inputs()
    raw = 'password=demo-secret; hash=demo-hash'
    args['results'][2] = CheckResult(uid(22), Status.FAIL, (uid(122),),
                                    actual_result=raw, reason=raw, expected_result=raw)
    args['sensitive_values'] = ['demo-secret', 'demo-hash']
    report = build_evaluation_report(**args)
    assert 'demo-secret' not in json.dumps(report)
    assert 'demo-hash' not in json.dumps(report)
    assert '[REDACTED]' in report['checks'][2]['actualResult']
    assert args['results'][2].actual_result == raw


@pytest.mark.parametrize('change', ['identity', 'architecture', 'suite', 'timezone',
                                    'coverage', 'binding', 'optional', 'duplicate', 'perspective'])
def test_invalid_identity_or_incomplete_plan_rejected(change):
    """같은 실행 ID·잘못된 구조·해시 아닌 suite·시간대 없음·범위 누락·선택 검사만·중복 결과·잘못된 관점을 거부한다."""
    args = inputs()
    if change == 'identity': args['evaluation_id'] = args['source_run_id']
    if change == 'architecture': args['architecture'] = 'UNKNOWN'
    if change == 'suite': args['suite_sha256'] = 'latest'
    if change == 'timezone': args['created_at'] = '2026-10-06T12:00:00'
    if change == 'coverage': args['required_requirement_ids'].append(uid(12))
    if change == 'binding': del args['bindings'][uid(20)]
    if change == 'optional': args['plan'][0] = CheckPlan(uid(20), required=False)
    if change == 'duplicate': args['results'].append(args['results'][0])
    if change == 'perspective': args['bindings'][uid(20)] = EvaluationBinding(uid(10), 'DEV', '잘못된 관점')
    with pytest.raises(ValueError):
        build_evaluation_report(**args)


def test_absent_perspective_is_not_reported_as_pass():
    """QA 검사만 한 평가에서 SECURITY 관점은 PASS가 아니라 None(검사 없음)으로 표시된다."""
    args = inputs()
    args['required_requirement_ids'] = [uid(10)]
    args['plan'] = args['plan'][:1]; args['results'] = args['results'][:1]
    args['bindings'] = {uid(20): args['bindings'][uid(20)]}
    assert build_evaluation_report(**args)['perspectives']['SECURITY'] is None


def test_optional_check_failure_kept_separate():
    """선택 검사의 FAIL은 optional 목록에만 남고 필수 판정·필수 실패 목록에는 섞이지 않는다."""
    args = inputs()
    args['plan'].append(CheckPlan(uid(23), required=False))
    args['bindings'][uid(23)] = EvaluationBinding(uid(10), 'QA', '선택 화면 검사')
    args['results'].append(CheckResult(uid(23), Status.FAIL, (uid(123),)))
    report = build_evaluation_report(**args)
    assert report['evaluationVerdict'] == 'PASS'
    assert report['confirmedFailureIds'] == []
    assert report['optionalConfirmedFailureIds'] == [uid(23)]
    assert report['requirements'][0]['optionalConfirmedFailureIds'] == [uid(23)]


def test_report_matches_schema_and_schema_rejects_tampering():
    """만들어진 보고서는 스키마를 통과하고, 필드를 빼거나 값을 망가뜨리면 스키마가 거부한다."""
    from jsonschema import ValidationError
    from evaluation.reporting import validate_contract

    report = build_evaluation_report(**inputs())
    validate_contract(report, 'independent_evaluation')
    for mutate in (
        lambda r: r.pop('suite'),
        lambda r: r.update(finalVerdict='PASS'),
        lambda r: r.update(architecture='HYBRID'),
        lambda r: r['checks'][0].update(evidenceIds=[]),
        lambda r: r['checks'][0].update(perspective='OPS'),
    ):
        broken = json.loads(json.dumps(report))
        mutate(broken)
        with pytest.raises(ValidationError):
            validate_contract(broken, 'independent_evaluation')
