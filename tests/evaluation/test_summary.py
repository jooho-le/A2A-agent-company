"""summary.py(회의·발표용 결과 요약)를 시험한다.

확인하는 것:
  - 판정·요구사항별·검사별 결과, 실패 → 수정 → 재검증 흐름, 실행·협업 지표, Run 기록 검사가 요약에 들어가는가?
  - 모르는 값은 0이 아니라 "미확인"으로 적는가?
  - Issue 제목·설명 같은 자유 글(비밀값이 섞일 수 있음)은 요약에 넣지 않는가?
"""
import copy
import json
from pathlib import Path

from evaluation import protected_tests as pt
from evaluation.aggregation import CheckResult, Status
from evaluation.independent import SourceRunSummary, build_evaluation_report
from evaluation.manifest import ExecutionManifest
from evaluation.summary import render_summary
from evaluation.trace_checks import check_traceability
from orchestrator.domain.scenario_registry import SCN_001_ID, get_scenario

CONTRACT = get_scenario(SCN_001_ID).planner_contract()
FIXTURES = Path(__file__).parent / 'fixtures'
RUN = json.loads((FIXTURES / 'dev_pipeline_runs.json').read_text(encoding='utf-8'))['runs'][
    'qa_failure_actual_fix_and_revalidation_preserve_all_lineages_and_shared_budget']
SECRET = 'Zq7' + 'uX2pLm9'


def report(fail_key=None):
    """SCN-001 검사 11개로 독립 평가 보고서를 만든다. 모두 PASS이고, fail_key로 고른 검사 하나만 FAIL로 바꿀 수 있다.
    Orchestrator는 SUCCESS라고 한 Run(dev 파이프라인 표본)을 대상으로 둔다."""
    inputs = pt.evaluation_inputs(CONTRACT)
    names = {check['checkId']: check['key'] for check in pt.load_suite()['checks']}
    results = [CheckResult(check.test_id, Status.FAIL if names[check.test_id] == fail_key else Status.PASS,
                           ('6fa459ea-ee8a-4ca4-894e-db77e160355e',)) for check in inputs['plan']]
    manifest = ExecutionManifest('repo', 2, '0c2c1b06-3d23-4c51-9a0c-6e1b4e7b7a11', 'sha1', 'a' * 40, 'b' * 40, 'c' * 64,
                                 'sha256:' + 'd' * 64, 'sha256:' + 'e' * 64)
    source = SourceRunSummary(RUN['run']['runId'], 'FINISHED', 'SUCCESS', 2, 1,
                              '2026-10-28T10:00:00+00:00', '2026-10-28T10:01:00+00:00')
    return build_evaluation_report(
        evaluation_id='5b6c1c56-37a7-4c33-9a7a-3a1d1a7e0b11', source_run_id=RUN['run']['runId'], architecture='MULTI_AGENT',
        suite_id=inputs['suite_id'], suite_version=inputs['suite_version'], suite_sha256=inputs['suite_sha256'],
        created_at='2026-10-28T12:00:00+09:00', required_requirement_ids=inputs['required_requirement_ids'],
        expected=manifest, observed=manifest, plan=inputs['plan'], results=results, bindings=inputs['bindings'],
        source_run=source)


def test_summary_shows_results_flow_metrics_and_trace():
    """판정·요구사항별 결과·코드 버전별 흐름(v1 QA FAIL → 수정 요청 → v2)·실행 지표·협업 시간·추적 검사가
    요약에 들어가고, 모르는 토큰·비용은 "미확인"으로 적힌다."""
    metrics = {'verdictMs': 11239, 'firstBuildPassMs': 2100, 'fixAttempts': 1, 'toolRetries': 0, 'issues': 1,
               'issuesFixRequested': 1, 'issuesFixed': 1, 'modelCalls': 13, 'totalTokens': None, 'costUsd': None,
               'knownCostUsd': '0.01',
               'collaboration': {'agentActiveMs': 11120, 'parallelOverlapMs': 4411, 'outsideAgentMs': 119,
                                 'handoffMs': 744, 'humanWaitMs': 0, 'humanWaitOpen': False,
                                 'stepsByRole': {'QA': {'steps': 2, 'ms': 4447}}}}
    text = render_summary(report(), scenario_contract=CONTRACT, metrics=metrics, run_bundle=RUN,
                          trace={'traceability': check_traceability(RUN, CONTRACT).to_dict()})
    assert '**독립 평가 판정: PASS**' in text and 'Orchestrator 판정 SUCCESS' in text
    assert '| REQ-005 | PASS | SEC-SIGNUP-001 PASS, SEC-SIGNUP-002 PASS |' in text
    assert '| v1 | PASS | FAIL | 검증 종료·판정 미확인 | QA QA_ASSERTION_FAILURE(' in text and '수정 요청 → v2' in text
    assert '| v2 | PASS | 검증 종료·판정 미확인 | 검증 종료·판정 미확인 | - | 판정 SUCCESS |' in text
    assert '토큰 미확인' in text and '비용 미확인(확인된 부분 $0.01)' in text
    assert '병렬로 겹친 시간 4.4초' in text and '추적 가능성(REQ-008): PASS' in text


def test_missed_defect_is_called_out():
    """Orchestrator는 SUCCESS인데 독립 평가에서 필수 검사 FAIL이 나오면 "놓친 결함"으로 드러내 적는다."""
    text = render_summary(report(fail_key='SEC-SIGNUP-002'), scenario_contract=CONTRACT)
    assert '**놓친 결함**' in text and '**독립 평가 판정: FAIL**' in text


def test_free_text_from_agents_is_not_copied():
    """Issue 제목·설명에 비밀값이 있어도 요약에는 옮겨지지 않는다(종류·보고 역할·요구사항 이름만 쓴다)."""
    bundle = copy.deepcopy(RUN)
    for issue in bundle['issues']:
        issue['title'] = 'password ' + SECRET
        issue['description'] = SECRET
    text = render_summary(report(), scenario_contract=CONTRACT, run_bundle=bundle)
    assert SECRET not in text
