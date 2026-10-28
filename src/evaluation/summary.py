# 독립 평가 결과를 회의·발표에서 바로 읽을 수 있는 Markdown 요약으로 만든다.
#   render_summary(report, scenario_contract=..., metrics=..., run_bundle=..., trace=..., runner=...)
# 넣는 내용
#   - 독립 평가 판정과 Orchestrator 판정, 놓친 결함, 채점 대상 신분 확인(Manifest) 결과
#   - 요구사항별·검사별 결과(근거 코드 포함)
#   - (run_bundle이 있으면) 코드 버전별 빌드·검증·발견한 문제·수정 요청 흐름: "실패 → 수정 → 재검증" 장면
#   - (metrics가 있으면) 시간·수정·재시도·모델 호출·토큰·비용, 협업 시간(전달·병렬·에이전트 밖·사람 대기)
#   - (trace가 있으면) Run 기록의 노출 검사·추적 검사 결과
# 에이전트가 쓴 자유 글(Issue 제목·설명, 실패 상세)은 비밀값이 섞일 수 있어서 넣지 않는다. 검사 제목·상태·근거 코드,
# Issue의 종류·보고 역할·요구사항 이름만 쓴다. 모르는 값은 0이 아니라 "미확인"으로 적는다.
#
# [처음 읽는 분을 위한 용어]
# - Markdown 표: "| 칸 | 칸 |" 줄과 "|---|---|" 구분 줄로 만드는 표. 칸 안의 "|"는 "\|"로 바꿔야 표가 깨지지 않는다.
# - 놓친 결함(missedDefect): Orchestrator는 SUCCESS라고 했는데 독립 평가에서 필수 검사 FAIL이 확인된 경우.
from typing import Mapping, Optional  # 자료형 이름표(실행에는 영향 없음)

UNKNOWN = '미확인'  # 모르는 값을 적는 글자(0으로 바꾸지 않는다)


def _cell(value) -> str:
    # 표 칸에 넣을 글자. None은 "미확인", 표를 깨뜨리는 "|"와 줄바꿈은 바꿔 쓴다.
    if value is None:
        return UNKNOWN
    return str(value).replace('|', '\\|').replace('\n', ' ')


def _seconds(ms) -> str:
    # 밀리초를 "11.2초"처럼 소수 한 자리 초로 적는다. None이면 "미확인".
    return UNKNOWN if ms is None else f'{ms / 1000:.1f}초'


def _count(value, unit) -> str:
    # 개수에 단위를 붙인다(예: 3회). None이면 단위 없이 "미확인"(예: "미확인회"처럼 어색해지지 않게).
    return UNKNOWN if value is None else f'{value}{unit}'


def _requirement_keys(scenario_contract) -> dict:
    # {요구사항 UUID: 표시 이름(REQ-001 등)} 표. 시나리오 정책이 없으면 빈 표다(UUID를 그대로 보여 준다).
    return {str(item['requirementId']): item['key'] for item in (scenario_contract or {}).get('requirements', [])}


def _flow(run_bundle, keys) -> list:
    # 코드 버전별 흐름 표의 줄들. 사건 종류만 읽고 자유 글은 읽지 않는다.
    # 줄마다: 코드 버전 | 빌드 결과 | QA | Security | 그 버전에서 찾은 문제(보고 역할·종류·요구사항) | 다음 단계
    events, issues = run_bundle.get('events', []), run_bundle.get('issues', [])
    # 사건에 나온 코드 버전들(중복 없이, 작은 번호부터)
    versions = sorted({event['codeVersion'] for event in events if isinstance(event.get('codeVersion'), int)})
    rows = ['| 코드 버전 | 빌드 | QA | Security | 발견한 문제 | 다음 |', '|---|---|---|---|---|---|']
    for version in versions:
        at = [event for event in events if event.get('codeVersion') == version]  # 이 버전의 사건들
        kinds = {event.get('eventType') for event in at}                            # 이 버전에서 일어난 사건 종류
        build = 'PASS' if 'BUILD_PASSED' in kinds else 'FAIL' if 'BUILD_FAILED' in kinds else '-'
        found = [issue for issue in issues if issue.get('codeVersion') == version]  # 이 버전에서 찾은 Issue

        def validation(role):
            # 역할(QA·SECURITY)의 이 버전 검증 칸. 검증 사건이 없으면 "-", 그 역할이 Issue를 냈으면 FAIL,
            # 아니면 "검증 종료·판정 미확인"(검증이 끝났다는 사건만으로 PASS라고 추정하지 않는다).
            if not any(event.get('eventType') == 'VALIDATION_FINISHED' and event.get('actor') == role for event in at):
                return '-'
            # 종료 사건·Issue 부재만으로는 검증 결과(PASS/UNVERIFIED)를 알 수 없다.
            return 'FAIL' if any(issue.get('reporter') == role for issue in found) else '검증 종료·판정 미확인'

        # 찾은 문제: "QA QA_ASSERTION_FAILURE(REQ-001)"처럼 보고 역할·종류·요구사항 이름만 적는다(자유 글은 넣지 않음).
        problems = ', '.join(f"{issue.get('reporter')} {issue.get('category')}("
                             + '·'.join(keys.get(str(item), '?') for item in issue.get('requirementIds') or ()) + ')'
                             for issue in found) or '-'
        # 다음 단계: 다음 버전으로 가는 수정 요청이 있으면 "수정 요청 → v다음", 판정 사건이 있으면 "판정 결과", 아니면 "-"
        following = ('수정 요청 → v' + str(version + 1)) if any(
            event.get('eventType') == 'FIX_REQUESTED' and event.get('codeVersion') == version + 1 for event in events) else \
            ('판정 ' + _cell(run_bundle.get('run', {}).get('verdict'))) if 'VERDICT_CREATED' in kinds else '-'
        rows.append(f'| v{version} | {build} | {validation("QA")} | {validation("SECURITY")} | {_cell(problems)} | {following} |')
    return rows


def render_summary(report: Mapping, *, scenario_contract: Optional[Mapping] = None, metrics: Optional[Mapping] = None,
                   run_bundle: Optional[Mapping] = None, trace: Optional[Mapping] = None,
                   runner: Optional[Mapping] = None) -> str:
    # 독립 평가 보고서(report)와 선택 자료로 Markdown 요약 글을 만들어 돌려준다.
    # scenario_contract: 요구사항 표시 이름(REQ-001 등)을 쓰기 위한 시나리오 정책
    # metrics          : run_metrics() 결과(시간·수정·비용·협업 시간)
    # run_bundle       : Run 기록(코드 버전별 흐름 표), trace: Trace 검사 결과, runner: D5 실행 정보
    keys = _requirement_keys(scenario_contract)
    source = report.get('sourceRun') or {}
    manifest = report.get('manifestCheck') or {}
    # 1) 머리 부분: 평가·Run 번호, 판정, 놓친 결함, 신분 확인 결과
    lines = ['# 독립 평가 요약', '',
             f"- 평가 번호 `{report.get('evaluationId')}` · 대상 Run `{report.get('sourceRunId')}` · 구조 {report.get('architecture')}"
             f" · 평가 시각 {report.get('createdAt')}",
             f"- **독립 평가 판정: {report.get('evaluationVerdict')}**"
             + (f" · Orchestrator 판정 {_cell(source.get('finalVerdict'))}, 수정 {_cell(source.get('fixAttempt'))}회, "
                f"최종 코드 버전 {_cell(source.get('codeVersion'))}" if source else ''),
             ]
    if source.get('missedDefect'):
        lines.append('- **놓친 결함**: Orchestrator는 SUCCESS였지만 독립 평가에서 필수 검사 FAIL이 확인됨')
    identity = '일치' if manifest.get('metadataMatches') else f"불일치·미확인({_cell(manifest.get('errorCode'))}) → 결과를 판정에 쓰지 않음"
    lines.append(f'- 채점 대상 신분(코드·환경 Manifest): {identity}')
    if runner:
        lines.append(f"- 보호 테스트 실행: {runner.get('kind')} · 오류 {_cell(runner.get('error'))}")
    # 2) 요구사항별 결과: 요구사항마다 판정과, 연결된 검사들의 "검사 번호 상태"를 한 칸에 적는다.
    lines += ['', '## 요구사항별 결과', '', '| 요구사항 | 결과 | 검사 |', '|---|---|---|']
    titles = {check.get('testId'): check.get('title', '') for check in report.get('checks', [])}
    statuses = {check.get('testId'): check.get('status') for check in report.get('checks', [])}
    for requirement in report.get('requirements', []):
        key = keys.get(str(requirement.get('requirementId')), requirement.get('requirementId'))
        # 검사 제목 "QA-SIGNUP-001 유효한 ..."에서 첫 단어(검사 번호)만 꺼내 쓴다.
        checks = ', '.join(f"{titles.get(check_id, check_id).split(' ', 1)[0]} {statuses.get(check_id)}"
                           for check_id in requirement.get('checkIds', []))
        lines.append(f"| {_cell(key)} | {_cell(requirement.get('validationVerdict'))} | {_cell(checks)} |")
    # 3) 검사별 결과
    lines += ['', '## 검사별 결과', '', '| 검사 | 관점 | 결과 | 근거 코드 |', '|---|---|---|---|']
    for check in report.get('checks', []):
        lines.append(f"| {_cell(check.get('title'))} | {_cell(check.get('perspective'))} | {_cell(check.get('status'))} |"
                     f" {_cell(check.get('errorCode') or '-')} |")
    # 4) (선택) 코드 버전별 흐름, 실행 지표, Run 기록 검사
    if run_bundle:
        lines += ['', '## 실패 → 수정 → 재검증 흐름', ''] + _flow(run_bundle, keys)
    if metrics:
        collaboration = metrics.get('collaboration') or {}
        cost = metrics.get('costUsd')  # 전체 비용(모든 호출의 비용을 확인했을 때만 값이 있다)
        # 비용을 모르면 "미확인"이라고 쓰고, 확인된 부분 합이 있으면 괄호로 덧붙인다.
        lines += ['', '## 실행 지표', '',
                  f"- 시작 ~ 판정 {_seconds(metrics.get('verdictMs'))}, 시작 ~ 첫 빌드 성공 {_seconds(metrics.get('firstBuildPassMs'))}",
                  f"- 수정 {_count(metrics.get('fixAttempts'), '회')}, 도구 재시도 {_count(metrics.get('toolRetries'), '회')}, "
                  f"Issue {_count(metrics.get('issues'), '건')}(수정 요청 {_cell(metrics.get('issuesFixRequested'))}, "
                  f"재검증 통과 {_cell(metrics.get('issuesFixed'))})",
                  f"- 모델 호출 {_count(metrics.get('modelCalls'), '회')}, 토큰 {_cell(metrics.get('totalTokens'))}, "
                  f"비용 {('$' + cost) if cost is not None else UNKNOWN + ('(확인된 부분 $' + metrics['knownCostUsd'] + ')' if metrics.get('knownCostUsd') else '')}"]
        if collaboration:
            lines += [f"- 협업: 에이전트가 일한 시간 {_seconds(collaboration.get('agentActiveMs'))}"
                      f"(병렬로 겹친 시간 {_seconds(collaboration.get('parallelOverlapMs'))}), "
                      f"에이전트 밖 시간 {_seconds(collaboration.get('outsideAgentMs'))}, "
                      f"일을 넘기는 전달 지연 {_seconds(collaboration.get('handoffMs'))}, "
                      f"사람 대기 {_seconds(collaboration.get('humanWaitMs'))}"
                      + (' (아직 대기 중)' if collaboration.get('humanWaitOpen') else '')]
            roles = collaboration.get('stepsByRole') or {}
            if roles:
                lines.append('- 역할별 Step 시간: ' + ', '.join(f"{role} {item['steps']}회 {_seconds(item['ms'])}"
                                                           for role, item in roles.items()))
    if trace:
        lines += ['', '## Run 기록 검사', '']
        for name, label in (('exposure', '비밀번호·해시 노출(REQ-006 Orchestrator 기록)'), ('traceability', '추적 가능성(REQ-008)')):
            item = trace.get(name)
            if item:
                lines.append(f"- {label}: {item.get('status')}" + (f" ({', '.join(item.get('reasons', []))})" if item.get('reasons') else ''))
    return '\n'.join(lines) + '\n'
