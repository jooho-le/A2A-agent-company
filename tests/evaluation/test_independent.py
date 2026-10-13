"""independent.py의 build_evaluation_report()(독립 평가 보고서 만들기)를 시험하는 파일이다.

확인하는 것:
  - 작업 번호(A2A ID) 없이도 보고서가 만들어지는가?
  - 한 요구사항에 검사가 여러 개여도 각 검사의 증거가 보존되는가?
  - "검사 통과율"과 "요구사항 통과율"이 따로 계산되는가?
  - 신분증(Manifest)이 다르거나 시험 범위가 맞지 않을 때 PASS가 나오지 않는가?
  - 개발 실행(Run) 기록(sourceRun)이 올바르게 붙는가?
"""
from dataclasses import replace  # 데이터 상자의 일부 값만 바꾼 복사본을 만드는 도구
import json
from uuid import UUID

import pytest

from evaluation.aggregation import CheckPlan, CheckResult, Status
from evaluation.independent import EvaluationBinding, SourceRunSummary, build_evaluation_report
from evaluation.manifest import ExecutionManifest


def uid(number):
    """시험용 가짜 UUIDv4. 같은 숫자를 주면 같은 값이 나온다."""
    return str(UUID(int=number, version=4))


def inputs():
    """모든 시험이 출발점으로 쓰는 "정상 입력"을 만든다. 시험마다 여기서 필요한 부분만 고쳐서 쓴다.

    구성: 요구사항 2개
      - QA 요구사항 uid(10)       ← 검사 uid(20) 하나
      - SECURITY 요구사항 uid(11)  ← 검사 uid(21), uid(22) 둘
    검사 3개의 결과는 모두 PASS이고, 증거 번호는 "검사 번호 + 100"이다.
    """
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
    """A2A 작업 번호 없이 보고서가 만들어지고, 같은 요구사항의 보안 검사 2개가 각자의 증거와 함께 남는다."""
    args = inputs()
    report = build_evaluation_report(**args)  # ** 는 사전의 내용을 이름 붙은 인자들로 풀어서 넘긴다.
    assert report['evaluationId'] != report['sourceRunId']
    assert 'a2aTaskId' not in report and 'finalVerdict' not in report
    assert report['evaluationVerdict'] == 'PASS'
    assert len(report['requirements']) == 2 and len(report['checks']) == 3
    assert report['requirements'][1]['checkIds'] == [uid(21), uid(22)]
    assert report['checks'][1]['evidenceIds'] == [uid(121)]
    assert report['checks'][2]['evidenceIds'] == [uid(122)]
    assert report['perspectives']['SECURITY']['validationVerdict'] == 'PASS'
    json.dumps(report)  # JSON 글자로 바꿀 수 없는 값이 있으면 여기서 오류가 난다.


def test_failure_details_and_requirement_denominator():
    """로그 검사 1개가 FAIL이면: 검사 통과율은 2/3, 요구사항 통과율은 1/2이고, 실패 설명이 보고서에 남는다.
    (요구사항 하나에 검사가 2개이므로, 검사 기준과 요구사항 기준의 분모가 서로 다르다.)"""
    args = inputs()
    args['results'][2] = CheckResult(uid(22), Status.FAIL, (uid(122),),
        expected_result='로그에 비밀번호 없음', actual_result='비밀번호 발견',
        reason='요청 본문이 로그에 기록됨', normalized_location='server.log:request')
    report = build_evaluation_report(**args)
    assert report['evaluationVerdict'] == 'FAIL'
    assert report['requiredCheckPassRate'] == pytest.approx(2/3)  # 소수 계산의 작은 오차를 허용해 비교한다.
    assert report['requiredRequirementPassRate'] == 0.5
    assert report['checks'][2]['reason'] == '요청 본문이 로그에 기록됨'
    assert report['checks'][2]['actualResult'] == '비밀번호 발견'
    assert report['requirements'][1]['confirmedFailureIds'] == [uid(22)]


def test_missing_check_does_not_hide_confirmed_failure():
    """로그 검사의 결과가 빠져서 전체가 UNVERIFIED여도, API 검사에서 이미 확인된 FAIL은 따로 남는다."""
    args = inputs()
    args['results'] = [args['results'][0], CheckResult(uid(21), Status.FAIL, (uid(121),))]
    report = build_evaluation_report(**args)
    assert report['evaluationVerdict'] == 'UNVERIFIED'
    assert report['confirmedFailureIds'] == [uid(21)]
    assert report['unverifiedTestIds'] == [uid(22)]
    assert report['checks'][2]['errorCode'] == 'RESULT_MISSING'


@pytest.mark.parametrize('observed', ['missing', 'different'])  # 신분증이 "없는 경우"와 "다른 경우"를 각각 시험한다.
def test_wrong_snapshot_cannot_supply_pass_or_failure(observed):
    """결과에 붙은 신분증(Manifest)이 없거나 다르면 PASS도 FAIL도 인정하지 않고, 통과율은 None(측정 못 함)으로 남긴다."""
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
    """알려 준 비밀번호·해시는 보고서 어디에도 남지 않는다. 그리고 호출한 쪽이 넘긴 원본 결과는 바뀌지 않는다."""
    args = inputs()
    raw = 'password=demo-secret; hash=demo-hash'
    args['results'][2] = CheckResult(uid(22), Status.FAIL, (uid(122),),
                                    actual_result=raw, reason=raw, expected_result=raw)
    args['sensitive_values'] = ['demo-secret', 'demo-hash']
    report = build_evaluation_report(**args)
    assert 'demo-secret' not in json.dumps(report)
    assert 'demo-hash' not in json.dumps(report)
    assert '[REDACTED]' in report['checks'][2]['actualResult']
    assert args['results'][2].actual_result == raw  # 원본은 그대로


@pytest.mark.parametrize('change', ['identity', 'architecture', 'suite', 'timezone',
                                    'coverage', 'binding', 'optional', 'duplicate', 'perspective'])
def test_invalid_identity_or_incomplete_plan_rejected(change):
    """아래 아홉 가지 잘못된 입력은 모두 ValueError로 거부된다.
      identity     : 채점 작업 번호와 개발 실행 번호가 같음
      architecture : 모르는 구조 이름
      suite        : 시험 묶음 해시가 올바른 모양이 아님
      timezone     : 시각에 시간대가 없음
      coverage     : 요구사항에 연결된 검사가 하나도 없음
      binding      : 어떤 검사의 꼬리표(요구사항·관점 연결)가 빠짐
      optional     : 필수 요구사항에 선택 검사만 연결됨
      duplicate    : 같은 검사의 결과가 두 번
      perspective  : QA/SECURITY가 아닌 관점"""
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
    """선택(참고용) 검사의 FAIL은 optional 목록에만 남고, 필수 판정과 필수 실패 목록에는 섞이지 않는다."""
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
    """만들어진 보고서는 스키마(JSON 모양 약속)를 통과한다. 반대로 필드를 빼거나 값을 망가뜨리면 스키마가 거부한다."""
    from jsonschema import ValidationError
    from evaluation.reporting import validate_contract

    report = build_evaluation_report(**inputs())
    validate_contract(report, 'independent_evaluation')
    # 일부러 보고서를 망가뜨리는 다섯 가지 방법. 각각 스키마 위반이어야 한다. (lambda = 이름 없는 한 줄 함수)
    for mutate in (
        lambda r: r.pop('suite'),                              # 필수 항목 삭제
        lambda r: r.update(finalVerdict='PASS'),               # 허용하지 않은 항목 추가
        lambda r: r.update(architecture='HYBRID'),             # 허용 값이 아닌 구조 이름
        lambda r: r['checks'][0].update(evidenceIds=[]),       # PASS인데 증거가 없음
        lambda r: r['checks'][0].update(perspective='OPS'),    # 허용 값이 아닌 관점
    ):
        broken = json.loads(json.dumps(report))  # JSON 변환을 거쳐 보고서를 통째로 복사한다.
        mutate(broken)
        with pytest.raises(ValidationError):
            validate_contract(broken, 'independent_evaluation')


def run_status(**change):
    """시스템 API(GET /api/v1/runs/{id})가 돌려주는 응답 모양의 가짜 실행 정보. 기본값은 "SUCCESS로 끝난 실행"이다.
    시험마다 change로 일부 값만 바꿔서 다른 상황을 만든다."""
    payload = dict(runId=uid(3), scenarioId=uid(4), workspaceId=uid(5), status='FINISHED',
                   resumeState=None, verdict='SUCCESS', codeVersion=1, fixAttempt=0,
                   terminationReason=None, createdAt='2026-10-06T12:00:00+09:00',
                   updatedAt='2026-10-06T12:05:30+09:00')
    payload.update(change)
    return payload


def test_source_run_is_recorded_beside_evaluation():
    """개발 실행 정보를 주면 최종 판정·수정 횟수·걸린 시간이 보고서에 기록되고, 주지 않으면 sourceRun은 None이다."""
    args = inputs()
    assert build_evaluation_report(**args)['sourceRun'] is None
    args['source_run'] = SourceRunSummary.from_run_status(run_status())
    run = build_evaluation_report(**args)['sourceRun']
    assert run['finalVerdict'] == 'SUCCESS' and run['fixAttempt'] == 0
    assert run['durationMs'] == 330000  # 12:00:00 ~ 12:05:30 = 5분 30초 = 330,000밀리초
    assert run['missedDefect'] is False


def test_missed_defect_when_orchestrator_success_but_evaluation_fail():
    """시스템(Orchestrator)은 SUCCESS라고 했는데 우리 시험은 FAIL이면, "놓친 결함(missedDefect)"이 True가 된다."""
    args = inputs()
    args['results'][2] = CheckResult(uid(22), Status.FAIL, (uid(122),))
    args['source_run'] = SourceRunSummary.from_run_status(run_status())
    assert build_evaluation_report(**args)['sourceRun']['missedDefect'] is True


def test_missed_defect_even_when_other_check_is_unverified():
    """다른 검사가 ERROR여서 전체 판정이 UNVERIFIED여도, 이미 확인된 FAIL이 있으면 missedDefect는 True다."""
    args = inputs()
    args['results'][1] = CheckResult(uid(21), Status.ERROR)
    args['results'][2] = CheckResult(uid(22), Status.FAIL, (uid(122),))
    args['source_run'] = SourceRunSummary.from_run_status(run_status())
    report = build_evaluation_report(**args)
    assert report['evaluationVerdict'] == 'UNVERIFIED'
    assert report['sourceRun']['missedDefect'] is True


def test_human_review_run_is_not_treated_as_finished():
    """HUMAN_REVIEW는 사람이 확인한 뒤 다시 이어갈 수 있는 "잠시 멈춤"이다. 그래서 끝난 시각과 걸린 시간을 기록하지 않는다."""
    args = inputs()
    args['source_run'] = SourceRunSummary.from_run_status(run_status(status='HUMAN_REVIEW', verdict='HUMAN_REVIEW'))
    run = build_evaluation_report(**args)['sourceRun']
    assert run['finalVerdict'] == 'HUMAN_REVIEW' and run['finishedAt'] is None and run['durationMs'] is None


def test_unfinished_run_has_no_finish_time():
    """아직 진행 중인 실행은 끝난 시각과 걸린 시간을 None으로 둔다."""
    args = inputs()
    args['source_run'] = SourceRunSummary.from_run_status(
        run_status(status='VALIDATING', verdict=None))
    run = build_evaluation_report(**args)['sourceRun']
    assert run['finishedAt'] is None and run['durationMs'] is None


@pytest.mark.parametrize('change', [
    dict(runId=uid(99)),                    # 다른 실행의 정보
    dict(codeVersion=2),                    # 채점한 코드가 최종 코드가 아님
])
def test_source_run_must_match_evaluated_run(change):
    """다른 실행의 기록이나, 채점한 코드와 다른 버전의 기록은 붙일 수 없다."""
    args = inputs()
    args['source_run'] = SourceRunSummary.from_run_status(run_status(**change))
    with pytest.raises(ValueError):
        build_evaluation_report(**args)


@pytest.mark.parametrize('change', [
    dict(status='DONE'),                                # 모르는 상태
    dict(verdict='PASS'),                               # 모르는 판정
    dict(verdict=None),                                 # FINISHED(끝남)인데 판정이 없음
    dict(status='ABORTED', verdict='FAIL'),             # 중단됐는데 판정이 있음
    dict(fixAttempt=4),                                 # 수정 횟수 한도(3) 초과
    dict(createdAt='2026-10-06T12:00:00'),              # 시간대가 없음
    dict(updatedAt='2026-10-06T11:00:00+09:00'),        # 끝난 시각이 시작보다 빠름
])
def test_invalid_source_run_rejected(change):
    """시스템의 상태 규칙에 맞지 않는 실행 정보는 만드는 순간 거부한다."""
    with pytest.raises(ValueError):
        SourceRunSummary.from_run_status(run_status(**change))
