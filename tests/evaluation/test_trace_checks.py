"""trace_checks.py(D4 Trace 검사)와 run_metrics.regression_count를 시험한다.

확인하는 것:
  - dev 파이프라인 테스트가 만든 실제 Run 기록(fixtures/dev_pipeline_runs.json)에서 추적 검사가 정상 Run을
    FAIL로 만들지 않고, 판정 없이 멈춘 Run은 UNVERIFIED로 두는가?
  - 사건·Issue를 하나씩 지운 변형에서 끊어진 연결을 FAIL로 찾는가?
  - 노출 검사가 비밀번호 필드 값·해시 형식·알려 준 값을 찾고, 설명용 이름·가린 값은 통과시키는가?
  - 결과에 비밀번호·해시 값이 남지 않는가?
  - Regression 계산이 PASS→FAIL만 세고 비교할 수 없는 검사를 따로 알리는가?
"""
import copy
import json
from pathlib import Path
from uuid import UUID

import pytest

from evaluation.aggregation import Status
from evaluation.run_metrics import regression_count
from evaluation.trace_checks import check_trace_exposure, check_traceability, fetch_trace_documents
from orchestrator.domain.scenario_registry import SCN_001_ID, get_scenario

ROOT = Path(__file__).resolve().parents[2]
RUNS = json.loads((Path(__file__).parent / 'fixtures/dev_pipeline_runs.json').read_text(encoding='utf-8'))['runs']
CONTRACT = get_scenario(SCN_001_ID).planner_contract()
CRITERIA = json.loads((ROOT / 'src/evaluation/security/scn001_security_criteria.json').read_text(encoding='utf-8'))
QA_FIX = 'qa_failure_actual_fix_and_revalidation_preserve_all_lineages_and_shared_budget'
BUILD_GAP = 'build_failure_gap_keeps_report_lineage_distinct_from_code_version'


def uid(number):
    """시험용 가짜 UUIDv4. 같은 숫자면 같은 값이다."""
    return str(UUID(int=number, version=4))


# ---------------------------------------------------------------- REQ-008 추적 가능성


@pytest.mark.parametrize('name, status', [
    (QA_FIX, 'PASS'), (BUILD_GAP, 'PASS'),
    ('three_distinct_failed_fix_cycles_stop_at_four_candidates', 'PASS'),
    ('repeated_issue_stops_after_two_unsolved_fix_cycles', 'PASS'),
    ('initial_build_failure_is_fixed_then_missing_security_proof_stays_unverified', 'PASS'),
    ('fix_cannot_reset_budget_after_initial_agents_spend_call_cap', 'UNVERIFIED'),
])
def test_real_dev_runs_are_traceable(name, status):
    """SUCCESS·FAIL·HUMAN_REVIEW·빌드 실패 경로 모두 연결이 복원된다. 판정 없이 멈춘 Run만 확인 불가다."""
    result = check_traceability(RUNS[name], CONTRACT)
    assert result.status == status, result.reasons


def without(bundle, predicate):
    """Run 기록을 복사하고 predicate에 맞는 사건을 지운다(사건 총수도 맞춘다). 연결을 일부러 끊는 데 쓴다."""
    bundle = copy.deepcopy(bundle)
    bundle['events'] = [event for event in bundle['events'] if not predicate(event)]
    bundle['eventsTotal'] = len(bundle['events'])
    return bundle


def first_issue(bundle):
    """Run 기록의 첫 Issue 번호"""
    return bundle['issues'][0]['issueId']


@pytest.mark.parametrize('name, mutate, reason', [
    (QA_FIX, lambda b: without(b, lambda e: e['eventType'] == 'ISSUE_CREATED'), 'ISSUE_EVENT_MISSING'),
    (QA_FIX, lambda b: without(b, lambda e: e['eventType'] == 'SNAPSHOT_FROZEN' and e['codeVersion'] == 2),
     'SNAPSHOT_EVENT_MISSING'),
    (QA_FIX, lambda b: without(b, lambda e: e['eventType'] == 'FIX_REQUESTED'), 'FIX_EVENT_MISSING'),
    (QA_FIX, lambda b: without(b, lambda e: e['eventType'] == 'FIX_REQUESTED'), 'FIX_REQUEST_UNLINKED'),
    (QA_FIX, lambda b: without(b, lambda e: e['eventType'] == 'VERDICT_CREATED'), 'VERDICT_EVENT_MISSING'),
    (QA_FIX, lambda b: without(b, lambda e: e['eventType'] == 'VALIDATION_FINISHED' and e['actor'] == 'SECURITY'),
     'REQUIREMENT_NOT_VALIDATED'),
    (QA_FIX, lambda b: without(b, lambda e: e['eventType'] == 'PLANNER_OUTPUT_VALIDATED'), 'REQUIREMENTS_NOT_PLANNED'),
    (QA_FIX, lambda b: dict(copy.deepcopy(b), issues=[]), 'FIX_WITHOUT_ISSUE'),
    (QA_FIX, lambda b: dict(copy.deepcopy(b), issues=[]), 'FIX_ISSUE_UNKNOWN'),
    (QA_FIX, lambda b: dict(copy.deepcopy(b), issues=[dict(b['issues'][0], requirementIds=[uid(99)])]),
     'ISSUE_REQUIREMENT_UNKNOWN'),
    # 수정됐지만 재검증 결과가 비어 있는 QA Issue: 수정 버전 이후의 QA 검증 사건까지 지우면 재검증을 찾을 수 없다.
    (BUILD_GAP, lambda b: without(b, lambda e: e['eventType'] == 'VALIDATION_FINISHED' and e['actor'] == 'QA'
                                  and e['codeVersion'] >= 2), 'REVALIDATION_UNTRACEABLE'),
])
def test_broken_links_fail(name, mutate, reason):
    """실제 Run 기록에서 사건·Issue를 하나씩 지우거나 바꾸면, 끊어진 연결을 그 사유 코드와 함께 FAIL로 찾는다."""
    result = check_traceability(mutate(RUNS[name]), CONTRACT)
    assert result.status == 'FAIL' and reason in result.reasons


def test_partial_events_are_unverified():
    """사건 총수보다 받은 사건이 적으면(일부만 받음) 판정하지 않고 EVENTS_INCOMPLETE 미검증이다."""
    bundle = copy.deepcopy(RUNS[QA_FIX])
    bundle['eventsTotal'] += 1
    assert check_traceability(bundle, CONTRACT).reasons == ('EVENTS_INCOMPLETE',)


def test_trace_result_maps_to_check_result():
    """추적 검사 PASS는 증거 번호가 붙은 PASS로, 판정 없이 멈춘 Run은 RUN_NOT_FINISHED 미검증(증거 번호 없음)으로 옮겨진다."""
    passed = check_traceability(RUNS[QA_FIX], CONTRACT).to_check_result(uid(1), uid(2))
    assert passed.status == Status.PASS and passed.evidence_ids == (uid(2),)
    stopped = check_traceability(RUNS['fix_cannot_reset_budget_after_initial_agents_spend_call_cap'], CONTRACT)
    result = stopped.to_check_result(uid(1), uid(2))
    assert result.status == Status.ERROR and result.error_code == 'RUN_NOT_FINISHED' and result.evidence_ids == ()


# ---------------------------------------------------------------- REQ-006 기록 속 노출

def documents(**changes):
    """노출 검사에 넣을 깨끗한 Run 기록 묶음. 설명용 정책 이름·가린 값([REDACTED], ****)만 있어 노출이 없다.
    changes로 일부 문서만 바꿔 노출 사례를 만든다."""
    clean = {'events': copy.deepcopy(RUNS[QA_FIX]['events']), 'eventsTotal': len(RUNS[QA_FIX]['events']),
             'issues': copy.deepcopy(RUNS[QA_FIX]['issues']),
             'artifacts': [{'artifactType': 'RUN_CONFIGURATION', 'content': {
                 'securityPolicy': {'passwordHashPolicy': {'algorithm': 'argon2id', 'memoryKiB': 19456},
                                    'secretExposure': 'Passwords and password hashes must not appear.'},
                 'passwordMinLength': '8', 'password_error_message': '비밀번호는 8자 이상'}}],
             'toolAttempts': [{'tool': 'run_unit_tests', 'arguments': {'password': '[REDACTED]', 'pwd': '****'}}],
             'telemetry': [], 'telemetryTotal': 0}
    clean.update(changes)
    return clean


def test_clean_run_records_pass():
    """정책 이름·설명 글·가린 값만 있는 기록은 노출이 아니다(PASS)."""
    assert check_trace_exposure(documents(), CRITERIA).status == 'PASS'


def test_hash_format_names_in_report_text_are_not_exposure():
    """QA·Security 보고서가 형식 이름("$argon2id$ 형식")을 설명하는 것은 해시 값 노출이 아니다."""
    text = {'artifacts': [{'tests': [{'expectedResult': '저장값이 $argon2id$ 형식이고 $2b$ bcrypt가 아님'}]}]}
    assert check_trace_exposure(documents(**text), CRITERIA).status == 'PASS'


def test_protected_test_uses_the_same_hash_shape():
    """보호 테스트 SEC-SIGNUP-005와 D4 노출 검사는 같은 해시 모양 규칙을 쓴다."""
    import ast
    from evaluation import protected_tests
    from evaluation.trace_checks import HASH_FORMAT
    source = protected_tests.protected_files('final')['tests/signup/signup_security_checks.py']
    assign = next(node for node in ast.parse(source).body if isinstance(node, ast.Assign)
                  and getattr(node.targets[0], 'id', None) == 'HASH_FORMAT')
    assert ast.literal_eval(assign.value.args[0]) == HASH_FORMAT.pattern


SECRET = 'Zq7' + 'uX2pLm9'
HASHED = '$argon2id$v=19$m=65536,t=3,p=4$c2FsdHNhbHQ$' + 'h' * 43


@pytest.mark.parametrize('change, kind, path', [
    ({'toolAttempts': [{'arguments': {'password': SECRET}}]}, 'PASSWORD_FIELD_VALUE', '/0/arguments/password'),
    ({'artifacts': [{'content': {'user': {'hashed_password': 'abc'}}}]}, 'PASSWORD_FIELD_VALUE',
     '/0/content/user/hashed_password'),
    ({'artifacts': [{'content': {'log': 'stored ' + HASHED}}]}, 'PASSWORD_HASH_FORMAT', '/0/content/log'),
    ({'telemetry': [{'prompt': 'user typed ' + SECRET}], 'telemetryTotal': 1}, 'KNOWN_SECRET_VALUE', '/0/prompt'),
])
def test_exposures_are_found_without_values(change, kind, path):
    """비밀번호 필드 값·해시 필드 값·해시 형식·알려 준 비밀값을 위치(JSON 경로)와 함께 FAIL로 찾고,
    결과에는 찾은 값 자체를 남기지 않는다."""
    result = check_trace_exposure(documents(**change), CRITERIA, known_values=[SECRET])
    assert result.status == 'FAIL' and kind in result.reasons
    assert any(item['path'] == path and item['kind'] == kind for item in result.findings)
    dumped = json.dumps(result.to_dict(), ensure_ascii=False)
    assert SECRET not in dumped and '$argon2' not in dumped


def test_missing_or_partial_records_are_unverified_but_findings_still_fail():
    """기록이 없거나 일부만 있으면 미검증이지만, 받은 부분에서 노출을 찾았으면 FAIL이다."""
    missing = check_trace_exposure(documents(toolAttempts=None), CRITERIA)
    assert missing.status == 'UNVERIFIED' and 'TRACE_DOCUMENT_MISSING' in missing.reasons
    partial = check_trace_exposure(documents(eventsTotal=999), CRITERIA)
    assert partial.status == 'UNVERIFIED' and 'TRACE_DOCUMENT_INCOMPLETE' in partial.reasons
    leaked = documents(eventsTotal=999, issues=[{'actualResult': HASHED}])
    assert check_trace_exposure(leaked, CRITERIA).status == 'FAIL'


def test_fetch_trace_documents_reads_every_page():
    """사건·Telemetry를 쪽(page) 단위로 끝까지 읽어 총수와 맞추고, Run 번호가 UUID 모양이 아니면 거부한다."""
    run_id = uid(5)
    pages = {'events': [{'n': i} for i in range(3)], 'telemetry': [{'n': i} for i in range(2)]}

    def get(path, query):
        """dev Run 조회 API를 흉내 낸다. 사건·Telemetry는 한 번에 2개씩만 돌려준다."""
        name = path.rsplit('/', 1)[1]
        if name in pages:
            items = pages[name][query['offset']:query['offset'] + 2]
            return {'events': items, 'total': len(pages[name])}
        return {'issues': {'issues': []}, 'artifacts': {'artifacts': []}, 'tool-attempts': {'attempts': []}}[name]

    fetched = fetch_trace_documents(get, run_id)
    assert len(fetched['events']) == fetched['eventsTotal'] == 3
    assert len(fetched['telemetry']) == fetched['telemetryTotal'] == 2
    with pytest.raises(ValueError):
        fetch_trace_documents(get, 'not-a-uuid')


# ---------------------------------------------------------------- Regression


def test_regression_counts_only_pass_to_fail():
    """코드 버전 사이에 PASS → FAIL로 바뀐 검사만 Regression으로 센다(FAIL → PASS는 세지 않는다)."""
    result = regression_count({1: {'a': 'PASS', 'b': 'FAIL', 'c': 'PASS'},
                               2: {'a': 'FAIL', 'b': 'PASS', 'c': 'PASS'},
                               3: {'a': 'PASS', 'b': 'FAIL', 'c': 'PASS'}})
    assert result['regressionCount'] == 2 and result['complete'] is True
    assert result['regressions'] == [{'fromVersion': 1, 'toVersion': 2, 'checkId': 'a'},
                                     {'fromVersion': 2, 'toVersion': 3, 'checkId': 'b'}]


def test_regression_reports_uncompared_checks_and_rejects_gaps():
    """한쪽이 미검증(ERROR)이라 비교할 수 없는 검사는 따로 세어 complete=False로 알린다.
    빈 입력, 버전 번호가 비거나 1부터 시작하지 않는 입력은 거부한다."""
    result = regression_count({1: {'a': 'PASS', 'b': 'ERROR'}, 2: {'a': 'PASS', 'b': 'FAIL'}})
    assert result['regressionCount'] == 0 and result['uncomparedChecks'] == 1 and result['complete'] is False
    for broken in ({}, {1: {}, 3: {}}, {2: {}}):
        with pytest.raises(ValueError):
            regression_count(broken)


def a2a_document(run_id, **roles):
    """dev a2a_export 모양의 A2A Message 본문 문서. roles에 없는 역할은 빠진 저장소(missingRoles)로 둔다."""
    every = ('PLANNER', 'DEVELOPER', 'QA', 'SECURITY')
    empty = {'contexts': [], 'tasks': [], 'history': [], 'receipts': [], 'events': []}
    return {'runId': run_id, 'roles': {role: roles.get(role, empty) for role in every if role in roles or not roles},
            'missingRoles': [] if not roles else sorted(set(every) - set(roles))}


def test_a2a_message_bodies_are_searched_when_given():
    """A2A Message 본문 문서를 주면 그 안의 비밀번호 칸 값도 찾아 FAIL이고, 본문을 본 것으로 검사 범위에 적는다.
    깨끗한 본문이면 PASS이고 "보지 못한 출력"에서 A2A Message 본문이 빠진다. 네 역할 중 빠진 저장소가 있으면
    일부만 본 것이라 미검증이다. 다른 Run의 문서는 받지 않는다."""
    run_id = RUNS[QA_FIX]['run']['runId']
    base = documents(runId=run_id)
    clean = check_trace_exposure({**base, 'a2aMessages': a2a_document(run_id)}, CRITERIA)
    assert clean.status == 'PASS' and 'a2aMessages' in clean.coverage['checkedDocuments']
    assert 'A2A_MESSAGE_BODY' not in clean.coverage['notCollectedOutputs']
    leaked = {'contexts': ['c'], 'tasks': [], 'events': [], 'receipts': [],
              'history': [{'taskId': 't', 'position': 0, 'message': {'parts': [{'data': {'password': SECRET}}]}}]}
    every = {role: leaked if role == 'QA' else a2a_document(run_id)['roles'][role]
             for role in ('PLANNER', 'DEVELOPER', 'QA', 'SECURITY')}
    found = check_trace_exposure({**base, 'a2aMessages': a2a_document(run_id, **every)}, CRITERIA)
    assert found.status == 'FAIL' and {item['document'] for item in found.findings} == {'a2aMessages'}
    assert SECRET not in json.dumps(found.to_dict(), ensure_ascii=False)
    partial = check_trace_exposure({**base, 'a2aMessages': a2a_document(run_id, QA=every['QA'] | {'history': []})}, CRITERIA)
    assert partial.status == 'UNVERIFIED' and 'a2aMessages' in partial.coverage['incompleteDocuments']
    with pytest.raises(ValueError):
        check_trace_exposure({**base, 'a2aMessages': a2a_document('00000000-0000-4000-8000-000000000099')}, CRITERIA)
