# 독립 평가 결과를 회의·발표에서 바로 읽을 수 있는 Markdown 요약으로 만든다.
#   render_summary(report, scenario_contract=..., metrics=..., run_bundle=..., trace=..., runner=...)
# 넣는 내용
#   - 독립 평가 판정과 Orchestrator 판정, 놓친 결함, 채점 대상 신분 확인(Manifest) 결과
#   - 요구사항별·검사별 결과(근거 코드 포함)
#   - (run_bundle이 있으면) 코드 버전별 빌드·검증·발견한 문제·수정 요청 흐름: "실패 → 수정 → 재검증" 장면
#   - (metrics가 있으면) 시간·수정·재시도·모델 호출·토큰·비용, 협업 시간(전달·병렬·에이전트 밖·사람 대기)
#   - (trace가 있으면) Run 기록의 노출 검사·추적 검사 결과
#   - (regression이 있으면) 버전별 보호 테스트 결과에서 계산한 Regression Count(run_metrics.regression_from_reports)
#   - 최종 시험 점수(scoring.exam_score: 요구사항별 같은 비중, 검사 완료율, 확인된 보안 위반)
#   - (reference_report가 있으면) 순환용 시험지 참고 채점(차이의 차이 계산용 보조 지표, 판정·점수와 별개)
#   - (supplementary가 있으면) 보조 점검: 테스트가 지나가지 않은 줄, 정적 판정(판정·점수에 넣지 않음)
# 에이전트가 쓴 자유 글(Issue 제목·설명, 실패 상세)은 비밀값이 섞일 수 있어서 넣지 않는다. 검사 제목·상태·근거 코드,
# Issue의 종류·보고 역할·요구사항 이름만 쓴다. 모르는 값은 0이 아니라 "미확인"으로 적는다.
# 함께 넣는 자료(run_bundle·metrics·trace·runner)는 평가 보고서와 같은 Run·같은 최종 코드 버전·같은 평가의 것이어야 한다.
# 다르거나 출처를 적지 않은 자료가 있으면 요약을 만들지 않고 ValueError로 멈춘다(다른 실행 자료가 섞이지 않게).
#
# [처음 읽는 분을 위한 용어]
# - Markdown 표: "| 칸 | 칸 |" 줄과 "|---|---|" 구분 줄로 만드는 표. 칸 안의 "|"는 "\|"로 바꿔야 표가 깨지지 않는다.
# - 놓친 결함(missedDefect): Orchestrator는 SUCCESS라고 했는데 독립 평가에서 필수 검사 FAIL이 확인된 경우.
from typing import Mapping, Optional  # 자료형 이름표(실행에는 영향 없음)

from .scoring import exam_score  # 요구사항별 같은 비중 점수

UNKNOWN = '미확인'  # 모르는 값을 적는 글자(0으로 바꾸지 않는다)
OUTPUT_NAMES = {'A2A_MESSAGE_BODY': 'A2A Message 본문', 'ARTIFACT_FILE_CONTENT': 'Artifact 파일 내용',
                'AGENT_PROCESS_OUTPUT': '에이전트 프로세스 출력',
                'A2A_MESSAGE_BODY_BEFORE_REDACTION': '가리기 전 A2A 송수신 본문(저장된 기록은 가린 값)'}  # 노출 검사가 보지 못하는 출력의 표시 이름


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


REPORT_ROLES = {'QA_REPORT': 'QA', 'SECURITY_REPORT': 'SECURITY'}


def version_reports(run_trace) -> dict:
    # {코드 버전: {'QA': 칸 글자, 'SECURITY': 칸 글자}}. Run trace(trace.json)의 QA·Security 보고서 중 같은 코드 버전의
    # Snapshot(SOURCE Artifact)에 연결된 것만 읽는다: 보고서 executionManifest의 projectArtifactId·snapshotSha256이 그 버전
    # SOURCE의 artifactId·snapshotSha256과 같아야 한다(Codex 점검 2026-11-19 #5: 4e2b1fe8 요약이 실제 QA 11/11 PASS 보고서가
    # 있는데도 "판정 미확인"으로 적었다). 칸은 "판정 통과 검사 수/전체 검사 수"(예: PASS 11/11, FAIL 1/3)이고 판정은
    # 하나라도 FAIL이면 FAIL, 모두 PASS면 PASS, 그 밖은 UNVERIFIED다. 보고서가 있지만 Snapshot이 맞지 않으면 숫자를 쓰지
    # 않는다(다른 코드의 결과를 이 버전 결과로 읽지 않게). 같은 버전 보고서가 여럿이면 연결된 것 중 마지막 것이다.
    run_id = (run_trace or {}).get('runId')
    artifacts = [item for item in (run_trace or {}).get('artifacts', []) if isinstance(item, dict)
                 and item.get('runId') in (None, run_id)]
    sources = {item.get('codeVersion'): item for item in artifacts if item.get('artifactType') == 'SOURCE'}
    result, linked_seen = {}, set()
    for item in artifacts:
        role, version = REPORT_ROLES.get(item.get('artifactType')), item.get('codeVersion')
        if role is None or type(version) is not int:
            continue
        source = sources.get(version) or {}
        manifest = item.get('executionManifest') if isinstance(item.get('executionManifest'), dict) else {}
        linked = (source.get('artifactId') is not None and manifest.get('projectArtifactId') == source['artifactId']
                  and source.get('snapshotSha256') is not None
                  and manifest.get('snapshotSha256') == source['snapshotSha256']
                  and manifest.get('codeVersion') in (None, version))
        if not linked:
            if (version, role) not in linked_seen:
                result.setdefault(version, {})[role] = '보고서 Snapshot 불일치·미확인'
            continue
        rows = item.get('tests') if role == 'QA' else item.get('requirementResults')
        outcomes = [row.get('outcome') for row in rows or () if isinstance(row, dict)]
        if not outcomes:
            cell = '보고서에 검사 없음·미확인'
        else:
            verdict = ('FAIL' if 'FAIL' in outcomes else 'PASS' if all(value == 'PASS' for value in outcomes)
                       else 'UNVERIFIED')
            cell = f"{verdict} {outcomes.count('PASS')}/{len(outcomes)}"
        result.setdefault(version, {})[role] = cell
        linked_seen.add((version, role))
    return result


def _flow(run_bundle, keys, reports=None) -> list:
    # 코드 버전별 흐름 표의 줄들. 사건 종류와 보고서의 검사 결과(outcome)만 읽고 자유 글은 읽지 않는다.
    # 줄마다: 코드 버전 | 빌드 결과 | QA | Security | 그 버전에서 찾은 문제(보고 역할·종류·요구사항) | 다음 단계
    # reports: version_reports() 결과. 주면 QA·Security 칸을 같은 Snapshot에 연결된 실제 보고서에서 채우고,
    #          보고서가 정말 없을 때만 "미확인"으로 적는다. 주지 않으면(예전 호출) 사건·Issue만으로 적는다.
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
            # 역할(QA·SECURITY)의 이 버전 검증 칸. 연결된 보고서가 있으면 그 판정·통과 수를 쓴다.
            # 없으면: 검증 사건이 없으면 "-", 그 역할이 Issue를 냈으면 FAIL,
            # 아니면 "검증 종료·판정 미확인"(검증이 끝났다는 사건만으로 PASS라고 추정하지 않는다).
            if reports is not None and (reports.get(version) or {}).get(role):
                return reports[version][role]
            if not any(event.get('eventType') == 'VALIDATION_FINISHED' and event.get('actor') == role for event in at):
                return '-'
            if reports is not None:
                # 보고서를 받아 보았는데 이 버전·역할의 보고서가 실제로 없다.
                return 'FAIL(보고서 없음)' if any(issue.get('reporter') == role for issue in found) else '검증 종료·보고서 없음(미확인)'
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
    if reports is not None:
        rows.append('')
        rows.append('- QA·Security 칸: 같은 코드 버전 Snapshot에 연결된 에이전트 보고서의 판정과 통과 검사 수/전체 검사 수'
                    '(보고서에 든 검사 전부). Host 판정·독립 평가와는 다른 값이다.')
    return rows


def _result_layers(report, run_bundle, pipeline) -> str:
    # 서로 다른 세 결과를 한 줄에 나눠 적는다(Codex 점검 2026-11-19 #5: "완주·패키지 저장 / Host FAIL / 독립 시험 6·7"이
    # 한 판정으로 섞여 읽히지 않게). ① 파이프라인 완주: Run 끝 상태와 결과 패키지 저장 여부(pipeline이 없으면 미확인),
    # ② Host(Orchestrator) 판정, ③ 독립 제품 시험: 충족한 요구사항 수/전체, PASS 검사 수/전체.
    source = report.get('sourceRun') or {}
    status = ((run_bundle or {}).get('run') or {}).get('status') or source.get('workflowStatus')
    saved = None if pipeline is None else pipeline.get('packageSaved')
    package = {True: '결과 패키지 저장됨', False: '결과 패키지 기록 없음'}.get(saved, '결과 패키지 미확인')
    requirements = report.get('requirements', [])
    checks = report.get('checks', [])
    met = sum(1 for item in requirements if item.get('validationVerdict') == 'PASS')
    passed = sum(1 for item in checks if item.get('status') == 'PASS')
    verdict = ((run_bundle or {}).get('run') or {}).get('verdict') or source.get('finalVerdict')
    return (f"- 결과 구분: ① 파이프라인 {_cell(status)}·{package} · ② Host 판정 {_cell(verdict)} · "
            f"③ 독립 제품 시험 요구사항 {met}/{len(requirements)} 충족, 검사 {passed}/{len(checks)} PASS")


def _check_same_sources(report, metrics, run_bundle, trace, runner, regression=None, run_trace=None, pipeline=None):
    # 요약에 함께 넣는 자료의 출처를 평가 보고서와 대조한다. 다르거나 출처가 없으면 ValueError.
    #   run_bundle: Run 번호·최종 코드 버전, metrics: Run 번호(+ 값이 있으면 구조·코드 버전),
    #   trace: Run 번호(final_evaluation 결과의 trace에 들어 있다), runner: 평가 번호(final_evaluation 결과의 runner)
    run_id = report.get('sourceRunId')
    version = (report.get('expectedManifest') or {}).get('codeVersion')  # 채점한 최종 코드 버전
    if run_bundle is not None:
        run = run_bundle.get('run') or {}
        if run.get('runId') != run_id or (version is not None and run.get('codeVersion') != version):
            raise ValueError('Run bundle differs from the evaluated Run or final code version')
    if metrics is not None:
        if (metrics.get('runId') != run_id or metrics.get('architecture') not in (None, report.get('architecture'))
                or metrics.get('codeVersion') not in (None, version)):
            raise ValueError('Metrics differ from the evaluated Run')
    if trace is not None and trace.get('runId') != run_id:
        raise ValueError('Trace results differ from the evaluated Run')
    if runner is not None and runner.get('evaluationId') != report.get('evaluationId'):
        raise ValueError('Runner details belong to another evaluation')
    if run_trace is not None and run_trace.get('runId') != run_id:
        raise ValueError('Run trace differs from the evaluated Run')
    if pipeline is not None and pipeline.get('runId') != run_id:
        raise ValueError('Pipeline completion differs from the evaluated Run')
    if regression is not None and (regression.get('runId') != run_id
                                   or regression.get('suiteSha256') != (report.get('suite') or {}).get('sha256')
                                   or type(regression.get('finalCodeVersion')) is not int
                                   or regression['finalCodeVersion'] != version):
        raise ValueError('Regression results differ from the evaluated Run, protected suite or final code version')


def _exposure_line(exposure: Optional[Mapping]) -> str:
    # REQ-006(노출 금지) 전체 출력의 검증이 끝났는지 한 줄로 적는다. 보호 테스트·정적 판정이 모두 PASS여도
    # Run 기록에서 노출을 찾았거나, 기록 검사가 없거나, 보지 못한 출력이 있으면 "완료"라고 쓰지 않는다.
    if not exposure:
        return '- REQ-006 전체 출력의 노출 검증: 미완료(Run 기록 노출 검사 결과 없음)'
    if exposure.get('status') == 'FAIL':
        return '- **REQ-006 충족하지 않음**: Run 기록에서 비밀번호·해시 노출 발견(위 판정과 별개)'
    coverage = exposure.get('coverage') or {}
    gaps = [] if exposure.get('status') == 'PASS' else [f"Run 기록 노출 검사 {_cell(exposure.get('status'))}"]
    if not coverage:
        # 검사 범위를 모르면 무엇을 보지 못했는지도 모르므로 완료라고 쓰지 않는다.
        gaps.append('검사 범위 정보 없음')
    if coverage.get('notCollectedOutputs'):
        gaps.append('보지 못한 출력 ' + '·'.join(OUTPUT_NAMES.get(name, name) for name in coverage['notCollectedOutputs']))
    if coverage.get('missingDocuments') or coverage.get('incompleteDocuments'):
        gaps.append('받지 못한 기록 ' + '·'.join(coverage.get('missingDocuments', []) + coverage.get('incompleteDocuments', [])))
    if gaps:
        return f"- REQ-006 전체 출력의 노출 검증: 미완료({', '.join(gaps)})"
    return '- REQ-006 Run 기록 노출 검사: PASS(보지 못한 출력·받지 못한 기록 없음)'


def _percent(value) -> str:
    # 0~1 비율을 "97.6%"처럼 적는다. None이면 "미확인".
    return UNKNOWN if value is None else f'{value * 100:.1f}%'


def _score_line(score: Mapping) -> str:
    # 점수 한 줄: 점수(요구사항별 같은 비중)·검사 완료율. 미검증이 있으면 잠정 하한이라고 적는다.
    if score['score'] is None:
        return {'MANIFEST_NOT_CONFIRMED': '점수 없음(코드·환경 신분 미확인)',
                'EXAM_EXPOSED': '점수 없음(시험지 노출)'}.get(score.get('reason'), '점수 없음')
    return (f"{score['score']:.3f}(요구사항별 같은 비중){' 잠정 하한' if score['scoreIsLowerBound'] else ''}"
            f" · 검사 완료율 {_percent(score['completionRate'])}")


def render_summary(report: Mapping, *, scenario_contract: Optional[Mapping] = None, metrics: Optional[Mapping] = None,
                   run_bundle: Optional[Mapping] = None, trace: Optional[Mapping] = None,
                   runner: Optional[Mapping] = None, regression: Optional[Mapping] = None,
                   reference_report: Optional[Mapping] = None, supplementary: Optional[Mapping] = None,
                   run_trace: Optional[Mapping] = None, pipeline: Optional[Mapping] = None) -> str:
    # 독립 평가 보고서(report)와 선택 자료로 Markdown 요약 글을 만들어 돌려준다.
    # scenario_contract: 요구사항 표시 이름(REQ-001 등)을 쓰기 위한 시나리오 정책
    # metrics          : run_metrics() 결과(시간·수정·비용·협업 시간)
    # run_bundle       : Run 기록(코드 버전별 흐름 표), trace: Trace 검사 결과, runner: D5 실행 정보
    # regression       : run_metrics.regression_from_reports 결과(버전별 보호 테스트 결과의 PASS → FAIL 수)
    # reference_report : D5 결과의 referenceReport(같은 Run·같은 코드를 순환용 시험지로 참고 채점한 보고서)
    # supplementary    : D5 결과의 supplementary(지나간 줄 기록·정적 판정)
    # run_trace        : Run trace(trace.json, runId·artifacts). 주면 흐름 표의 QA·Security 칸을 같은 Snapshot의 실제 보고서로 채운다
    # pipeline         : {'runId', 'packageSaved'}. 결과 패키지 저장 여부(handoff에서 해시까지 확인한 package-completion)
    _check_same_sources(report, metrics, run_bundle, trace, runner, regression, run_trace, pipeline)  # 0) 다른 Run·평가의 자료가 섞이지 않았는지
    if reference_report is not None and (
            reference_report.get('sourceRunId') != report.get('sourceRunId')
            or reference_report.get('architecture') != report.get('architecture')
            or reference_report.get('expectedManifest') != report.get('expectedManifest')):
        raise ValueError('Reference report differs from the evaluated Run or final code')
    score = exam_score(report)
    keys = _requirement_keys(scenario_contract)
    source = report.get('sourceRun') or {}
    manifest = report.get('manifestCheck') or {}
    exposure = (trace or {}).get('exposure')
    traceability = (trace or {}).get('traceability')
    # 1) 머리 부분: 평가·Run 번호, 판정, 놓친 결함, 신분 확인 결과
    #    판정은 보호 테스트·정적 판정만의 결과이므로 검사 수를 함께 적고, 판정에 들어가지 않는 Run 기록 검사와
    #    REQ-006 전체 출력의 노출 검증 상태를 바로 아래에 따로 적는다(PASS 판정이 기록 속 노출을 가리지 않게).
    lines = ['# 독립 평가 요약', '',
             f"- 평가 번호 `{report.get('evaluationId')}` · 대상 Run `{report.get('sourceRunId')}` · 구조 {report.get('architecture')}"
             f" · 평가 시각 {report.get('createdAt')}",
             f"- **독립 평가 판정(실행 검사 {len(report.get('checks', []))}개): {report.get('evaluationVerdict')}**"
             + (f" · Orchestrator 판정 {_cell(source.get('finalVerdict'))}, 수정 {_cell(source.get('fixAttempt'))}회, "
                f"최종 코드 버전 {_cell(source.get('codeVersion'))}" if source else ''),
             f"- 위 판정에 들어가지 않는 Run 기록 검사: 노출(REQ-006 기록) {_cell((exposure or {}).get('status') or '결과 없음')}, "
             f"추적(REQ-008) {_cell((traceability or {}).get('status') or '결과 없음')}",
             _result_layers(report, run_bundle, pipeline),
             f"- 최종 시험 점수: {_score_line(score)}",
             _exposure_line(exposure),
             ]
    exposure_found = (supplementary or {}).get('examExposure')
    if exposure_found:
        # 채점할 코드에 시험지가 섞였다. 에이전트가 시험지를 볼 수 있었으므로 이 Run은 점수 비교에서 뺀다.
        lines.append(f"- **시험지 노출**: 채점할 Snapshot에 시험지·평가 코드가 들어 있어 실행·점수를 만들지 않음 "
                     f"({', '.join(_cell(item['path']) for item in exposure_found[:10])})")
    if score['securityViolations']:
        lines.append(f"- **확인된 보안 위반**(점수와 별개): {', '.join(score['securityViolations'])}")
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
        verdict = _cell(requirement.get('validationVerdict'))
        # REQ-006 줄은 보호 테스트·정적 판정 결과다. Run 기록 노출 검사가 PASS가 아니면 같은 칸에 덧붙여 가려지지 않게 한다.
        if key == 'REQ-006' and exposure and exposure.get('status') != 'PASS':
            verdict += f" · Run 기록 노출 {_cell(exposure.get('status'))}"
        lines.append(f"| {_cell(key)} | {verdict} | {_cell(checks)} |")
    # 3) 검사별 결과
    lines += ['', '## 검사별 결과', '', '| 검사 | 관점 | 결과 | 근거 코드 |', '|---|---|---|---|']
    for check in report.get('checks', []):
        lines.append(f"| {_cell(check.get('title'))} | {_cell(check.get('perspective'))} | {_cell(check.get('status'))} |"
                     f" {_cell(check.get('errorCode') or '-')} |")
    # 4) (선택) 코드 버전별 흐름, 실행 지표, Run 기록 검사
    if run_bundle:
        lines += ['', '## 실패 → 수정 → 재검증 흐름', ''] + _flow(
            run_bundle, keys, None if run_trace is None else version_reports(run_trace))
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
    if regression:
        # 비교할 수 없는 검사(미검증)가 있으면 숨은 Regression이 있을 수 있어 "최소 n건"으로 적는다.
        count = regression.get('regressionCount')
        lines += ['', '## Regression(버전 간 PASS → FAIL)', '',
                  f"- {'' if regression.get('complete') else '최소 '}{count}건 · 비교한 버전 쌍 "
                  f"{regression.get('comparedVersionPairs')}개 · 비교 못 한 검사 {regression.get('uncomparedChecks')}개",
                  '- 같은 검사 결과의 변화이며, 수정이 원인인지 확정하려면 동일 입력 재실행으로 확인해야 합니다.']
        lines += [f"  - v{item['fromVersion']} → v{item['toVersion']}: {_cell(titles.get(item['checkId'], item['checkId']).split(' ', 1)[0])}"
                  for item in regression.get('regressions', [])]
    if reference_report is not None:
        # 순환용 시험지는 Ver1이 개발 중에 결과를 본 시험지다. 최종 점수와 섞지 않고 차이의 차이(보조 지표) 계산에만 쓴다.
        reference = exam_score(reference_report)
        lines += ['', '## 순환용 시험지 참고 채점(판정·점수와 별개, 차이의 차이 계산용 보조 지표)', '',
                  f"- 판정 {_cell(reference_report.get('evaluationVerdict'))} · 점수 {_score_line(reference)}"]
    if supplementary:
        lines += ['', '## 보조 점검(판정·점수에 넣지 않음)', '']
        coverage = supplementary.get('coverage') or {}
        if coverage.get('loaded'):
            loaded = coverage['loaded']
            gaps = [f"{path} {'·'.join(str(line) for line in item['missing'][:20])}"
                    + (' 외' if len(item['missing']) > 20 else '')
                    for path, item in sorted((coverage.get('files') or {}).items()) if item['executed'] and item['missing']]
            lines.append(f"- 테스트가 지나간 줄(테스트가 불러온 파일 {loaded['files']}개 기준): {loaded['executed']}/{loaded['lines']}줄"
                         f"({_percent(loaded['rate'])}). 지나가지 않은 줄: {'; '.join(gaps) if gaps else '없음'}")
        elif coverage:
            lines.append(f"- 테스트가 지나간 줄: 기록 없음({_cell(coverage.get('error'))})")
        static = supplementary.get('static') or {}
        if static.get('requirements'):
            lines.append('- 정적 판정(실행 검사가 지나가지 않은 코드 경로까지 소스로 봄): ' + ', '.join(
                f"{key} {_cell(item.get('outcome'))}" for key, item in sorted(static['requirements'].items())))
        elif static:
            lines.append(f"- 정적 판정: 결과 없음({_cell(static.get('error'))})")
    if trace:
        lines += ['', '## Run 기록 검사', '']
        for name, label in (('exposure', '비밀번호·해시 노출(REQ-006 Orchestrator 기록)'), ('traceability', '추적 가능성(REQ-008)')):
            item = trace.get(name)
            if item:
                lines.append(f"- {label}: {item.get('status')}" + (f" ({', '.join(item.get('reasons', []))})" if item.get('reasons') else ''))
                coverage = item.get('coverage')
                if coverage:
                    # 노출 검사는 받은 기록만 본다. 못 본 출력이 있으므로 PASS여도 REQ-006 전체 충족이 아니라 부분 검사다.
                    lines.append(f"  - 검사 범위(부분 검사, REQ-006 전체 충족 아님): 읽은 기록 "
                                 f"{'·'.join(coverage.get('checkedDocuments') or []) or '없음'}, 보지 못한 출력 "
                                 f"{'·'.join(OUTPUT_NAMES.get(name, name) for name in coverage.get('notCollectedOutputs') or []) or '없음'}"
                                 + (f", 받지 못한 기록 {'·'.join(coverage['missingDocuments'] + coverage['incompleteDocuments'])}"
                                    if coverage.get('missingDocuments') or coverage.get('incompleteDocuments') else '')
                                 + f", 알려 준 비밀값 {_count(coverage.get('knownValues'), '개')}")
    return '\n'.join(lines) + '\n'
