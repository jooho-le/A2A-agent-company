"""Agent별 평가표의 수신 기록 대조와 역할별 의미 검토 호출(최신 dev ea06d12 기록)을 시험한다.

확인하는 것:
  - 실제 dev 기록에서 단계마다 첫 접수 기록이 Task 입력 metadata·Context와 같으면 PASS인가?
  - 접수 기록의 값·Context가 다르면 FAIL이고 오류 발생 지점으로 잡히는가? 숫자 표기(0과 0.0) 차이는 같은 값인가?
  - 접수 기록 목록이 없는 예전 내보내기·Task에 접수 기록이 없는 경우는 FAIL이 아니라 미검증이고 오류 지점을 바꾸지 않는가?
  - 역할별 LLM 호출 중 의미 검토 호출을 따로 세고, 원격 측정이 없으면 모름으로 두는가?
  - 여러 Run 요약·Markdown에 새 칸이 들어가는가?
"""
import copy
import json
from pathlib import Path

import pytest

from evaluation.agent_eval import agent_tables, render_markdown, render_summary_markdown, summarize

FIXTURE = json.loads((Path(__file__).parent / 'fixtures/dev_outcome_runs.json').read_text(encoding='utf-8'))
OLD = json.loads((Path(__file__).parent / 'fixtures/agent_eval_runs.json').read_text(encoding='utf-8'))


def real(name):
    data = copy.deepcopy(FIXTURE[name])
    return {key: data[key] for key in ('bundle', 'trace', 'a2a', 'handoff')}


@pytest.mark.parametrize('name,reviews', [('multiFix', {'PLANNER': 1, 'DEVELOPER': 2, 'QA': 2, 'SECURITY': 2}),
                                          ('single', {'PLANNER': 1, 'DEVELOPER': 1, 'QA': 1, 'SECURITY': 1})])
def test_real_runs_have_matching_receipts_and_separated_review_calls(name, reviews):
    tables = agent_tables(real(name))
    assert {row['receipt']['status'] for row in tables['handoffs']} == {'PASS'}
    assert all(row['receipt']['value'] == '접수 기록 1건' for row in tables['handoffs'])
    assert tables['firstProblem'] is None
    timing = {row['role']: row for row in tables['timing'] if row['steps']}
    assert {role: row['reviewLlmCalls'] for role, row in timing.items()} == reviews
    assert all(row['reviewLlmCalls'] < row['llmCalls'] for row in timing.values())
    assert all((row['reviewInputTokens'], row['reviewOutputTokens']) == (10 * row['reviewLlmCalls'], 5 * row['reviewLlmCalls'])
               for row in timing.values())


def _receipt(export, role='DEVELOPER', index=0):
    rows = export['a2a']['roles'][role]['receipts']
    return sorted(rows, key=lambda row: row['taskId'])[index] if index else rows[0]


@pytest.mark.parametrize('change,field', [
    (lambda row: row['metadata'].update(codeVersion=9), 'codeVersion'),
    (lambda row: row['metadata'].update(requirementIds=row['metadata']['requirementIds'][1:]), 'requirementIds'),
    (lambda row: row['metadata'].update(workflowStepId='00000000-0000-4000-8000-000000000000'), 'workflowStepId'),
    (lambda row: row['metadata'].pop('projectArtifactIds'), 'projectArtifactIds'),
    (lambda row: row.update(contextId='other-context'), 'contextId'),
])
def test_receipt_that_differs_from_the_task_input_is_a_failure(change, field):
    export = real('multiFix')
    first_developer = _receipt(export)
    change(first_developer)
    tables = agent_tables(export)
    row = next(item for item in tables['handoffs'] if item['role'] == 'DEVELOPER' and item['receipt']['status'] == 'FAIL')
    assert field in row['receipt']['detail']['mismatchedFields']
    assert tables['firstProblem']['kind'] == 'receipt' and tables['firstProblem']['role'] == 'DEVELOPER'


def test_number_spelling_difference_is_not_a_mismatch():
    export = real('multiFix')
    receipt = _receipt(export)
    receipt['metadata']['attempt'] = float(receipt['metadata']['attempt'])
    receipt['metadata']['codeVersion'] = float(receipt['metadata']['codeVersion'] or 0) or receipt['metadata']['codeVersion']
    assert {row['receipt']['status'] for row in agent_tables(export)['handoffs']} == {'PASS'}


def test_missing_receipt_records_are_unverified_and_do_not_become_the_first_problem():
    export = real('multiFix')
    del export['a2a']['roles']['QA']['receipts']
    export['a2a']['roles']['SECURITY']['receipts'] = []
    tables = agent_tables(export)
    by_role = {}
    for row in tables['handoffs']:
        by_role.setdefault(row['role'], set()).add((row['receipt']['status'], row['receipt']['detail']))
    assert by_role['QA'] == {('UNVERIFIED', 'RECEIPTS_NOT_IN_EXPORT')}
    assert by_role['SECURITY'] == {('UNVERIFIED', 'NO_RECEIPT_FOR_TASK')}
    assert tables['firstProblem'] is None


def test_previous_export_format_stays_comparable():
    # 11.5 고정 기록은 수집 총수·호출 번호가 없어 사용량 전체를 확인할 수 없다. 예전 파일도 표 생성은 가능하다.
    export = copy.deepcopy(OLD['multiFix'])
    tables = agent_tables(export)
    assert {row['receipt']['detail'] for row in tables['handoffs']} == {'RECEIPTS_NOT_IN_EXPORT'}
    assert {row['reviewLlmCalls'] for row in tables['timing'] if row['steps']} == {None}
    assert {row['llmCalls'] for row in tables['timing'] if row['steps']} == {None}
    assert all('USAGE_RECORD_COUNT_UNVERIFIED' in row['usageWarnings'] for row in tables['timing'])
    del export['trace']['telemetry']
    assert {row['reviewLlmCalls'] for row in agent_tables(export)['timing'] if row['steps']} == {None}


def test_summary_and_markdown_show_receipts_and_review_calls():
    tables = [agent_tables(real('multiFix')), agent_tables(real('single'))]
    text = render_markdown(tables[0])
    assert '| 단계 | 에이전트 | 버전 | 전달 완전성 | 해석 정확성 | 수신 기록 대조 |' in text
    assert 'PASS (접수 기록 1건)' in text and '그중 의미 검토 호출' in text
    summary = summarize(tables)
    multi = next(group for group in summary['groups'] if group['architecture'] == 'MULTI_AGENT')
    developer = next(row for row in multi['handoffs'] if row['role'] == 'DEVELOPER')
    assert developer['receipt'] == {'PASS': 2, 'FAIL': 0, 'UNVERIFIED': 0, 'NOT_APPLICABLE': 0}
    timing = next(row for row in multi['timing'] if row['role'] == 'DEVELOPER')
    assert timing['reviewLlmCalls'] == {'median': 2, 'known': 1, 'runs': 1}
    rendered = render_summary_markdown(summary)
    assert '| 에이전트 | 단계 | 전달 완전성 | 해석 정확성 | 수신 기록 대조 |' in rendered and '| Developer | 2 | PASS 2 | PASS 2 | PASS 2 |' in rendered


def test_held_run_keeps_receipts_and_review_calls_of_the_work_done():
    tables = agent_tables(real('developerSemanticHold'))
    assert [row['receipt']['status'] for row in tables['handoffs']] == ['PASS', 'PASS']
    timing = {row['role']: row for row in tables['timing'] if row['steps']}
    assert (timing['PLANNER']['reviewLlmCalls'], timing['DEVELOPER']['reviewLlmCalls']) == (1, 1)
