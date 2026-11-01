"""summary.py(회의·발표용 결과 요약)를 시험한다.

확인하는 것:
  - 판정·요구사항별·검사별 결과, 실패 → 수정 → 재검증 흐름, 실행·협업 지표, Run 기록 검사가 요약에 들어가는가?
  - 모르는 값은 0이 아니라 "미확인"으로 적는가?
  - Issue 제목·설명 같은 자유 글(비밀값이 섞일 수 있음)은 요약에 넣지 않는가?
  - 다른 Run·다른 코드 버전·다른 평가의 자료를 섞어 넣으면 요약을 만들지 않고 멈추는가? (외부 점검 2026-10-29 F4)
  - 버전별 독립 평가 보고서에서 Regression Count를 만들어 요약에 적는가? 노출 검사의 검사 범위(부분 검사)를 적는가?
  - 판정이 검사 몇 개의 결과인지 적고, 판정에 들어가지 않는 Run 기록 노출 FAIL과 REQ-006 전체 검증 미완료를 드러내는가?
    (외부 점검 2026-10-29 2차)
  - 최종 시험 점수(요구사항별 같은 비중·검사 완료율)와 확인된 보안 위반, 순환용 시험지 참고 채점, 보조 점검(지나가지 않은
    줄·정적 판정)을 판정과 따로 적는가? 다른 Run의 참고 보고서는 받지 않는가?
"""
import copy
import json
from pathlib import Path

import pytest

from evaluation import protected_tests as pt
from evaluation.aggregation import CheckResult, Status
from evaluation.independent import SourceRunSummary, build_evaluation_report
from evaluation.manifest import ExecutionManifest
from evaluation.run_metrics import regression_from_reports
from evaluation.summary import render_summary
from evaluation.trace_checks import check_traceability
from orchestrator.domain.scenario_registry import SCN_001_ID, get_scenario

CONTRACT = get_scenario(SCN_001_ID).planner_contract()
FIXTURES = Path(__file__).parent / 'fixtures'
RUN = json.loads((FIXTURES / 'dev_pipeline_runs.json').read_text(encoding='utf-8'))['runs'][
    'qa_failure_actual_fix_and_revalidation_preserve_all_lineages_and_shared_budget']
SECRET = 'Zq7' + 'uX2pLm9'


def report(fail_key=None, version=2, exam='final', status=None):
    """SCN-001 실행 검사 9개(시험지 exam)로 독립 평가 보고서를 만든다. 모두 PASS이고, fail_key로 고른 검사 하나만 FAIL로 바꿀 수 있다.
    Orchestrator는 SUCCESS라고 한 Run(dev 파이프라인 표본)을 대상으로 두고, version은 채점한 코드 버전이다."""
    inputs = pt.evaluation_inputs(CONTRACT, exam)
    names = {check['checkId']: check['key'] for check in pt.load_suite()['checks']}
    results = [CheckResult(check.test_id, (status or Status.FAIL) if names[check.test_id] == fail_key else Status.PASS,
                           ('6fa459ea-ee8a-4ca4-894e-db77e160355e',)) for check in inputs['plan']]
    manifest = ExecutionManifest('repo', version, '0c2c1b06-3d23-4c51-9a0c-6e1b4e7b7a11', 'sha1', 'a' * 40, 'b' * 40, 'c' * 64,
                                 'sha256:' + 'd' * 64, 'sha256:' + 'e' * 64)
    # Run 요약은 최종 코드 버전(2)을 채점할 때만 넣는다(중간 버전 보고서는 Run의 최종 상태와 비교할 수 없다).
    source = SourceRunSummary(RUN['run']['runId'], 'FINISHED', 'SUCCESS', 2, 1,
                              '2026-10-28T10:00:00+00:00', '2026-10-28T10:01:00+00:00') if version == 2 else None
    return build_evaluation_report(
        evaluation_id='5b6c1c56-37a7-4c33-9a7a-3a1d1a7e0b11', source_run_id=RUN['run']['runId'], architecture='MULTI_AGENT',
        suite_id=inputs['suite_id'], suite_version=inputs['suite_version'], suite_sha256=inputs['suite_sha256'],
        created_at='2026-10-28T12:00:00+09:00', required_requirement_ids=inputs['required_requirement_ids'],
        expected=manifest, observed=manifest, plan=inputs['plan'], results=results, bindings=inputs['bindings'],
        source_run=source)


def test_summary_shows_results_flow_metrics_and_trace():
    """판정·요구사항별 결과·코드 버전별 흐름(v1 QA FAIL → 수정 요청 → v2)·실행 지표·협업 시간·추적 검사가
    요약에 들어가고, 모르는 토큰·비용은 "미확인"으로 적힌다."""
    metrics = {'runId': RUN['run']['runId'], 'architecture': 'MULTI_AGENT', 'codeVersion': 2, 'verdictMs': 11239, 'firstBuildPassMs': 2100, 'fixAttempts': 1, 'toolRetries': 0, 'issues': 1,
               'issuesFixRequested': 1, 'issuesFixed': 1, 'modelCalls': 13, 'totalTokens': None, 'costUsd': None,
               'knownCostUsd': '0.01',
               'collaboration': {'agentActiveMs': 11120, 'parallelOverlapMs': 4411, 'outsideAgentMs': 119,
                                 'handoffMs': 744, 'humanWaitMs': 0, 'humanWaitOpen': False,
                                 'stepsByRole': {'QA': {'steps': 2, 'ms': 4447}}}}
    text = render_summary(report(), scenario_contract=CONTRACT, metrics=metrics, run_bundle=RUN,
                          trace={'runId': RUN['run']['runId'], 'traceability': check_traceability(RUN, CONTRACT).to_dict()})
    assert '**독립 평가 판정(실행 검사 9개): PASS**' in text and 'Orchestrator 판정 SUCCESS' in text
    assert '| REQ-005 | PASS | SEC-SIGNUP-002 PASS |' in text
    assert '| v1 | PASS | FAIL | 검증 종료·판정 미확인 | QA QA_ASSERTION_FAILURE(' in text and '수정 요청 → v2' in text
    assert '| v2 | PASS | 검증 종료·판정 미확인 | 검증 종료·판정 미확인 | - | 판정 SUCCESS |' in text
    assert '토큰 미확인' in text and '비용 미확인(확인된 부분 $0.01)' in text
    assert '병렬로 겹친 시간 4.4초' in text and '추적 가능성(REQ-008): PASS' in text


def test_missed_defect_is_called_out():
    """Orchestrator는 SUCCESS인데 독립 평가에서 필수 검사 FAIL이 나오면 "놓친 결함"으로 드러내 적는다."""
    text = render_summary(report(fail_key='SEC-SIGNUP-002'), scenario_contract=CONTRACT)
    assert '**놓친 결함**' in text and '**독립 평가 판정(실행 검사 9개): FAIL**' in text


def test_free_text_from_agents_is_not_copied():
    """Issue 제목·설명에 비밀값이 있어도 요약에는 옮겨지지 않는다(종류·보고 역할·요구사항 이름만 쓴다)."""
    bundle = copy.deepcopy(RUN)
    for issue in bundle['issues']:
        issue['title'] = 'password ' + SECRET
        issue['description'] = SECRET
    text = render_summary(report(), scenario_contract=CONTRACT, run_bundle=bundle)
    assert SECRET not in text


def foreign(document, **changes):
    """document를 복사하고 일부 식별 값만 바꾼다(다른 Run·버전·평가의 자료 흉내)."""
    document = copy.deepcopy(document)
    document.update(changes)
    return document


OTHER_RUN = '6fa459ea-ee8a-4ca4-894e-db77e1603599'


@pytest.mark.parametrize('extra', [
    {'run_bundle': dict(RUN, run=foreign(RUN['run'], runId=OTHER_RUN))},          # 다른 Run의 흐름 자료
    {'run_bundle': dict(RUN, run=foreign(RUN['run'], codeVersion=1))},            # 같은 Run의 다른 코드 버전
    {'metrics': {'runId': OTHER_RUN, 'costUsd': '123.45'}},                        # 다른 Run의 비용·시간
    {'metrics': {'costUsd': '123.45'}},                                            # 출처(runId)가 없는 지표
    {'metrics': {'runId': RUN['run']['runId'], 'architecture': 'SINGLE_AGENT'}},  # 다른 구조
    {'metrics': {'runId': RUN['run']['runId'], 'codeVersion': 1}},                # 다른 코드 버전
    {'trace': {'traceability': {'status': 'PASS'}}},                               # 출처가 없는 Trace 결과
    {'trace': {'runId': OTHER_RUN, 'traceability': {'status': 'PASS'}}},          # 다른 Run의 Trace 결과
    {'runner': {'kind': 'docker', 'error': None}},                                 # 출처가 없는 실행 정보
    {'runner': {'kind': 'docker', 'error': None, 'evaluationId': OTHER_RUN}},      # 다른 평가의 실행 정보
], ids=['bundle-run', 'bundle-version', 'metrics-run', 'metrics-no-id', 'metrics-architecture', 'metrics-version',
        'trace-no-id', 'trace-run', 'runner-no-id', 'runner-evaluation'])
def test_mixed_sources_are_refused(extra):
    """F4: 평가 보고서와 다른 Run·코드 버전·구조·평가의 자료, 또는 출처를 알 수 없는 자료를 넣으면 ValueError로 멈춘다.
    (머리에는 Run A, 비용·흐름은 Run B인 요약이 조용히 만들어지지 않게 한다.)"""
    with pytest.raises(ValueError):
        render_summary(report(), scenario_contract=CONTRACT, **extra)


def test_sources_from_the_same_evaluation_are_accepted():
    """같은 Run·버전·평가의 자료(구조를 모르는 지표 포함)는 그대로 받아 요약을 만든다."""
    evaluated = report()
    text = render_summary(evaluated, scenario_contract=CONTRACT, run_bundle=RUN,
                          metrics={'runId': RUN['run']['runId'], 'architecture': None, 'costUsd': '0.5'},
                          trace={'runId': RUN['run']['runId']},
                          runner={'kind': 'docker', 'error': None, 'evaluationId': evaluated['evaluationId']})
    assert '$0.5' in text and '보호 테스트 실행: docker' in text


def test_regression_is_computed_from_version_reports_and_shown():
    """v1은 모두 PASS, v2에서 SEC-SIGNUP-002가 FAIL이면 Regression 1건이다. 요약에 건수와 바뀐 검사가 적힌다."""
    first, final = report(version=1), report(fail_key='SEC-SIGNUP-002', version=2)
    regression = regression_from_reports([final, first])  # 순서와 상관없이 버전 번호로 정렬한다
    assert (regression['regressionCount'], regression['complete'], regression['runId']) == (1, True, RUN['run']['runId'])
    text = render_summary(final, scenario_contract=CONTRACT, regression=regression)
    assert '- 1건 · 비교한 버전 쌍 1개 · 비교 못 한 검사 0개' in text and 'v1 → v2: SEC-SIGNUP-002' in text


@pytest.mark.parametrize('change', ['same-version', 'other-run', 'other-suite', 'no-version'])
def test_regression_reports_must_share_run_and_suite(change):
    """Regression은 같은 Run·같은 보호 묶음으로 버전마다 한 번 채점한 보고서끼리만 비교한다. 어기면 ValueError."""
    first, final = report(version=1), report(version=2)
    if change == 'same-version':
        first = report(version=2)
    elif change == 'other-run':
        first = foreign(first, sourceRunId=OTHER_RUN)
    elif change == 'other-suite':
        first = foreign(first, suite=dict(first['suite'], sha256='f' * 64))
    else:
        first = foreign(first, expectedManifest=None)
    with pytest.raises(ValueError):
        regression_from_reports([first, final])


def test_regression_from_another_run_is_refused_in_summary():
    """다른 Run의 보고서로 계산한 Regression 결과는 요약에 넣지 않는다(F4와 같은 출처 대조)."""
    regression = dict(regression_from_reports([report(version=1), report(version=2)]), runId=OTHER_RUN)
    with pytest.raises(ValueError):
        render_summary(report(), scenario_contract=CONTRACT, regression=regression)


def test_exposure_coverage_is_shown_as_partial_check():
    """노출 검사가 PASS여도 보지 못한 출력(A2A Message 본문 등)을 적어 REQ-006 전체 충족이 아님을 드러낸다."""
    exposure = {'status': 'PASS', 'reasons': [], 'findings': [],
                'coverage': {'checkedDocuments': ['events', 'issues'], 'missingDocuments': ['telemetry'],
                             'incompleteDocuments': [], 'notCollectedOutputs': ['A2A_MESSAGE_BODY'], 'knownValues': 2}}
    text = render_summary(report(), scenario_contract=CONTRACT, trace={'runId': RUN['run']['runId'], 'exposure': exposure})
    assert ('검사 범위(부분 검사, REQ-006 전체 충족 아님): 읽은 기록 events·issues, 보지 못한 출력 A2A Message 본문, '
            '받지 못한 기록 telemetry, 알려 준 비밀값 2개') in text


def test_record_exposure_failure_is_not_hidden_behind_a_passing_verdict():
    """검사 11개가 모두 PASS여도 Run 기록에서 노출을 찾으면, 머리 부분과 REQ-006 줄에서 REQ-006을 충족하지 않는다고
    드러낸다. 판정 줄에는 그 판정이 보호 테스트·정적 판정 몇 개의 결과인지 적는다(외부 점검 2026-10-29 2차)."""
    exposure = {'status': 'FAIL', 'reasons': ['PASSWORD_FIELD_VALUE'], 'findings': [],
                'coverage': {'checkedDocuments': ['events'], 'missingDocuments': [], 'incompleteDocuments': [],
                             'notCollectedOutputs': ['A2A_MESSAGE_BODY'], 'knownValues': 0}}
    text = render_summary(report(), scenario_contract=CONTRACT, trace={'runId': RUN['run']['runId'], 'exposure': exposure})
    assert '**독립 평가 판정(실행 검사 9개): PASS**' in text
    assert '- 위 판정에 들어가지 않는 Run 기록 검사: 노출(REQ-006 기록) FAIL, 추적(REQ-008) 결과 없음' in text
    assert '- **REQ-006 충족하지 않음**: Run 기록에서 비밀번호·해시 노출 발견(위 판정과 별개)' in text
    assert '| REQ-006 | PASS · Run 기록 노출 FAIL | SEC-SIGNUP-005 PASS |' in text


@pytest.mark.parametrize('exposure, line, row', [
    (None, '- REQ-006 전체 출력의 노출 검증: 미완료(Run 기록 노출 검사 결과 없음)', '| REQ-006 | PASS |'),
    ({'status': 'PASS', 'reasons': [], 'findings': [],
      'coverage': {'checkedDocuments': ['events'], 'missingDocuments': [], 'incompleteDocuments': [],
                   'notCollectedOutputs': ['A2A_MESSAGE_BODY', 'AGENT_PROCESS_OUTPUT'], 'knownValues': 0}},
     '- REQ-006 전체 출력의 노출 검증: 미완료(보지 못한 출력 A2A Message 본문·에이전트 프로세스 출력)', '| REQ-006 | PASS |'),
    ({'status': 'UNVERIFIED', 'reasons': ['DOCUMENT_MISSING'], 'findings': [],
      'coverage': {'checkedDocuments': [], 'missingDocuments': ['events'], 'incompleteDocuments': [],
                   'notCollectedOutputs': [], 'knownValues': 0}},
     '- REQ-006 전체 출력의 노출 검증: 미완료(Run 기록 노출 검사 UNVERIFIED, 받지 못한 기록 events)',
     '| REQ-006 | PASS · Run 기록 노출 UNVERIFIED |'),
    ({'status': 'PASS', 'reasons': [], 'findings': [],
      'coverage': {'checkedDocuments': ['events'], 'missingDocuments': [], 'incompleteDocuments': [],
                   'notCollectedOutputs': [], 'knownValues': 0}},
     '- REQ-006 Run 기록 노출 검사: PASS(보지 못한 출력·받지 못한 기록 없음)', '| REQ-006 | PASS |'),
    # 검사 범위(coverage)가 빠진 결과는 무엇을 보지 못했는지 모르므로 완료라고 쓰지 않는다.
    ({'status': 'PASS', 'reasons': [], 'findings': []},
     '- REQ-006 전체 출력의 노출 검증: 미완료(검사 범위 정보 없음)', '| REQ-006 | PASS |'),
])
def test_req006_completeness_is_stated_separately(exposure, line, row):
    """11개 검사 결과와 별개로, REQ-006 전체 출력의 노출 검증이 끝났는지(기록 검사 결과·보지 못한 출력)를 한 줄로 적는다.
    기록 검사가 PASS가 아니면 REQ-006 줄에도 덧붙인다."""
    trace = {'runId': RUN['run']['runId']} if exposure is None else {'runId': RUN['run']['runId'], 'exposure': exposure}
    text = render_summary(report(), scenario_contract=CONTRACT, trace=trace)
    assert line in text and row in text


def test_score_and_security_violation_are_shown_apart_from_the_verdict():
    """점수는 요구사항별 같은 비중이다: REQ-005(검사 1개)의 FAIL과 REQ-003(검사 3개 중 1개)의 FAIL은 점수에서 다르게
    깎인다. Security FAIL은 "확인된 보안 위반"으로 따로 적고, 미검증이 있으면 점수를 잠정 하한으로 적는다."""
    text = render_summary(report(fail_key='SEC-SIGNUP-002'), scenario_contract=CONTRACT)
    assert f'- 최종 시험 점수: {6 / 7:.3f}(요구사항별 같은 비중) · 검사 완료율 100.0%' in text
    assert '- **확인된 보안 위반**(점수와 별개): SEC-SIGNUP-002' in text
    text = render_summary(report(fail_key='QA-SIGNUP-005'), scenario_contract=CONTRACT)
    assert f'- 최종 시험 점수: {(6 + 2 / 3) / 7:.3f}(요구사항별 같은 비중) · 검사 완료율 100.0%' in text
    assert '확인된 보안 위반' not in text
    text = render_summary(report(fail_key='QA-SIGNUP-001', status=Status.ERROR), scenario_contract=CONTRACT)
    assert f'- 최종 시험 점수: {6 / 7:.3f}(요구사항별 같은 비중) 잠정 하한 · 검사 완료율 {8 / 9 * 100:.1f}%' in text


def test_reference_and_supplementary_checks_are_listed_apart():
    """순환용 시험지 참고 채점과 보조 점검(지나가지 않은 줄·정적 판정)은 판정·점수와 다른 절에 적는다.
    다른 Run의 참고 보고서는 받지 않는다."""
    supplementary = {
        'coverage': {'error': None, 'loaded': {'files': 2, 'lines': 30, 'executed': 27, 'rate': 0.9},
                     'files': {'database.py': {'lines': 12, 'executed': 10, 'missing': [16, 17]},
                               'routers/auth.py': {'lines': 18, 'executed': 17, 'missing': [40]},
                               'unused.py': {'lines': 5, 'executed': 0, 'missing': [1, 2, 3, 4, 5]}}},
        'static': {'error': None, 'requirements': {'REQ-003': {'outcome': 'PASS'}, 'REQ-005': {'outcome': 'UNVERIFIED'},
                                                   'REQ-006': {'outcome': 'FAIL'}}}}
    text = render_summary(report(), scenario_contract=CONTRACT, reference_report=report(exam='practice'),
                          supplementary=supplementary)
    assert '## 순환용 시험지 참고 채점(판정·점수와 별개, 차이의 차이 계산용 보조 지표)' in text
    assert '- 판정 PASS · 점수 1.000(요구사항별 같은 비중) · 검사 완료율 100.0%' in text
    assert ('- 테스트가 지나간 줄(테스트가 불러온 파일 2개 기준): 27/30줄(90.0%). '
            '지나가지 않은 줄: database.py 16·17; routers/auth.py 40') in text
    assert '- 정적 판정(실행 검사가 지나가지 않은 코드 경로까지 소스로 봄): REQ-003 PASS, REQ-005 UNVERIFIED, REQ-006 FAIL' in text
    other = dict(report(exam='practice'), sourceRunId='5b6c1c56-37a7-4c33-9a7a-3a1d1a7e0b99')
    with pytest.raises(ValueError):
        render_summary(report(), scenario_contract=CONTRACT, reference_report=other)
