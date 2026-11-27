# Agent별 평가표 세 개(11.2 텔로스 회의·자문: "에이전트별 작업 정확성, 정보 전달 과정, 처리시간과 오류")를 한 Run의
# 기록으로 만든다. 최종 결과물 점수(D5)만으로는 어느 에이전트·어느 전달 단계에서 문제가 생겼는지 알 수 없기 때문이다.
#   ① 작업 정확성(accuracy)  : 에이전트마다 맡은 일을 요구사항대로 했는가?
#        Planner   = 계획(implementationPlan)이 요구사항을 모두 작업에 연결했고 의존관계에 모순이 없는가(자동),
#                    계획 내용이 조건을 보존하고 구체적인가(사람 검토). 요구사항 목록 자체는 모델이 아니라 Host가
#                    시나리오에서 채우므로(dev planner_contract) Planner 능력으로 세지 않는다.
#        Developer = 버전마다 빌드가 됐는가, 수정 요청을 실제로 고쳤는가
#        QA·Security = 자기 판정이 같은 코드 버전의 독립 평가와 맞는가(놓친 결함·잘못된 FAIL), Issue가 실제 실패를 가리키는가
#        전체 시스템 = 최종 결과물이 독립 평가를 통과했는가(기획·QA·Security 피드백이 모두 반영된 결과라 Developer 몫으로 세지 않음)
#   ② 정보 전달(handoffs)     : 단계(에이전트 작업)마다
#        전달 완전성 = 받은 입력(요구사항 ID·Artifact ID·코드 버전)이 규칙상 받아야 할 입력과 같은가
#        해석 정확성 = 받은 입력에 맞는 결과를 냈는가(받은 요구사항을 모두 다뤘나, 받은 코드 버전·Snapshot을 다뤘나)
#        전달 경로   = 받은 Artifact를 다른 에이전트가 만들었나(AGENT_TO_AGENT), 같은 에이전트가 만들었나(SAME_AGENT).
#                      Single은 모두 같은 에이전트라 "에이전트 간 전달"이 없다(그것을 Single의 만점으로 읽지 않게 적는다).
#   ③ 처리시간·오류(timing)    : 에이전트마다 작업 수·처리 시간·LLM 호출·토큰·도구 호출과 오류
#   그리고 오류 발생 지점(firstProblem): 위 셋에서 처음 문제가 난 단계. QA·Security가 찾은 제품 결함은 에이전트 오류가
#   아니라 일을 제대로 한 결과라서 따로(productDefects) 적는다.
#
# 입력: dev `python -m agents.platform.evaluation_export`가 꺼낸 폴더(load_export로 읽음), 시나리오 정책(기본은 Run이 시작할 때
#       고정한 정책이라 회원가입 밖의 시나리오도 그대로 된다), (선택) 최종 독립 평가 보고서(D5 output['report']).
#       보고서가 없으면 그것이 필요한 칸은 미검증이다.
# 이 파일은 기록을 읽기만 한다. 생성 코드·테스트를 실행하지 않고, 판정·점수(D5)와 Orchestrator 판정을 바꾸지 않는다.
# Single 구조는 에이전트가 하나라서 단계의 역할(phase: 기획·구현·수정·QA·Security)로 같은 표를 만든다. 평가 기준이 없는
# 새 phase(예: 검토 중인 팀장 보조 에이전트)는 다른 역할로 채점하지 않고 미검증으로 두되, 시간·오류는 잰다.
# 실험처럼 Run이 여러 개면 summarize가 (시나리오, 구조)별로 세 표를 모은다(발표의 Single·Multi 비교용).
#
# [처음 읽는 분을 위한 용어]
# - 단계(step): Orchestrator가 에이전트에게 맡긴 일 하나(A2A Task 하나). 예) "코드 버전 1을 QA 검증".
# - Artifact: 에이전트가 낸 결과물 기록(요구사항 목록·Snapshot·QA 보고서 등). 다음 단계에 ID로 넘어간다.
# - 받은 입력(received): 에이전트 작업 저장소에 남은 A2A 요청의 metadata(requirementIds·projectArtifactIds·codeVersion).
import json  # 내보낸 JSON 파일 읽기
import re  # Host 비용 글자 형식 확인
from datetime import datetime  # 사건 시각 차이 계산
from decimal import Decimal, InvalidOperation  # 비용을 정확히 더하기(Host는 십진 문자열로 준다)
from math import isfinite
from pathlib import Path  # 파일 경로 다루기
from statistics import median  # 여러 Run 모을 때 중앙값
from typing import Mapping, Optional  # 자료형 이름표(실행에는 영향 없음)

from .outcomes import _number, semantic_reviews  # 의미 검토 기록(최신 dev)·숫자 표기 맞춤
from .scoring import exam_score  # 최종 시험 점수(요구사항별 같은 비중)
from .summary import UNKNOWN, _cell, _seconds  # 요약 문서와 같은 표 칸 규칙("미확인", 초 단위)

ROLES = ('PLANNER', 'DEVELOPER', 'QA', 'SECURITY')
# 단계의 역할 이름(phase) → 평가표의 행. Single도 같은 이름을 쓴다(dev evaluation_export의 handoff.steps).
PHASE_ROLE = {'PLANNING': 'PLANNER', 'IMPLEMENTING': 'DEVELOPER', 'FIXING': 'DEVELOPER', 'QA': 'QA', 'SECURITY': 'SECURITY'}
EXPORT_FILES = ('bundle.json', 'trace.json', 'a2a-messages.json', 'handoff.json')  # 평가표에 쓰는 내보내기 파일
TERMINAL_TASK_STATES = ('TASK_STATE_COMPLETED', 'TASK_STATE_FAILED', 'TASK_STATE_CANCELED', 'TASK_STATE_REJECTED')
AGENT_ERROR_STATES = ('TASK_STATE_FAILED', 'TASK_STATE_REJECTED')  # 에이전트 오류로 세는 끝 상태(취소는 사용자 조작이라 뺌)


def load_export(folder) -> dict:
    # 내보내기 폴더에서 평가표에 쓰는 파일 네 개를 읽는다. 서로 다른 Run의 파일이 섞였으면 멈춘다.
    folder = Path(folder)
    names = {'bundle.json': 'bundle', 'trace.json': 'trace', 'a2a-messages.json': 'a2a', 'handoff.json': 'handoff'}
    export = {key: json.loads((folder / name).read_text(encoding='utf-8')) for name, key in names.items()}
    run_ids = {export['bundle']['run']['runId'], export['trace']['runId'], export['a2a']['runId'], export['handoff']['runId']}
    if len(run_ids) != 1:
        raise ValueError('Export files belong to different Runs')
    return export


def _time(value):
    # ISO 시각 글자 → 시각 값(끝의 Z는 +00:00으로 바꿔 3.10에서도 읽게 한다). 없으면 None.
    return None if not value else datetime.fromisoformat(str(value).replace('Z', '+00:00'))


def _ms(start, end):
    # 두 시각 사이의 밀리초. 어느 하나라도 없거나 거꾸로면 None(모름).
    return None if start is None or end is None or end < start else int((end - start).total_seconds() * 1000)


def _row(status, value=None, detail=None):
    # 표의 칸 하나: 판정(PASS/FAIL/UNVERIFIED/NOT_APPLICABLE), 보여 줄 값, 근거(무엇이 빠졌는지 등)
    return {'status': status, 'value': value, 'detail': detail}


class _Run:
    # 내보내기 문서에서 평가표가 자주 찾는 것(단계·Artifact·요구사항·A2A 기록)을 미리 정리해 둔다.
    def __init__(self, export: Mapping, scenario_contract: Mapping):
        self.export = export
        self.bundle, self.trace, self.a2a, self.handoff = (export['bundle'], export['trace'], export['a2a'], export['handoff'])
        self.run_id = self.bundle['run']['runId']
        self.architecture = self.handoff.get('architecture')
        # 시나리오 요구사항: ID → {key, validators}. 역할별 담당 요구사항은 validators로 정한다.
        self.requirements = {str(item['requirementId']): item for item in scenario_contract.get('requirements', [])}
        self.role_requirements = {role: {key for key, item in self.requirements.items() if role in item.get('validators', ())}
                                  for role in ('QA', 'SECURITY')}
        # 단계: handoff.steps(만든 순서). 역할은 phase로 정한다(Single도 같은 표가 되게). 평가 기준이 없는 새 phase
        # (예: 검토 중인 팀장 보조 에이전트)는 phase 이름을 그대로 역할로 두고 mapped=False로 표시한다(다른 역할로 채점하지 않음).
        self.steps = [{**step, 'role': PHASE_ROLE.get(step.get('phase')) or str(step.get('phase') or 'UNKNOWN'),
                       'mapped': step.get('phase') in PHASE_ROLE} for step in self.handoff.get('steps', [])]
        self.unmapped_roles = sorted({step['role'] for step in self.steps if not step['mapped']})
        # Artifact: RUN_CONFIGURATION을 뺀 프로젝트 Artifact를 단계별로 모은다.
        self.artifacts = [item for item in self.trace.get('artifacts', []) if item.get('artifactType') != 'RUN_CONFIGURATION']
        self.by_step = {}
        for item in self.artifacts:
            self.by_step.setdefault(item.get('workflowStepId'), []).append(item)
        self.by_id = {item['artifactId']: item for item in self.artifacts}
        self.step_agent = {step['workflowStepId']: step.get('agentIdentity') for step in self.steps}
        # A2A 기록: (작업 주체, Task 번호) → Task 줄과 상태 사건(시각 순서)
        self.tasks, self.task_events = {}, {}
        for identity, rows in (self.a2a.get('roles') or {}).items():
            for row in rows.get('tasks', []):
                self.tasks[(identity, row['taskId'])] = row
            for row in rows.get('events', []):
                status = (row.get('event') or {}).get('status') or {}
                if row.get('eventType') == 'TaskStatusUpdateEvent' and status.get('state'):
                    self.task_events.setdefault((identity, row['taskId']), []).append((status['state'], _time(status.get('timestamp'))))
        self.usage = ((self.bundle.get('usage') or {}).get('records')) if self.bundle.get('usage') else None
        self.telemetry = self.trace.get('telemetry')
        # 메시지 접수 기록(receipts): 에이전트 저장소가 요청을 받을 때 남긴 줄. 이 목록이 아예 없는 예전 내보내기는 대조하지 않는다.
        self.receipts, self.receipt_identities = {}, set()
        for identity, rows in (self.a2a.get('roles') or {}).items():
            if isinstance(rows.get('receipts'), list):
                self.receipt_identities.add(identity)
            for row in rows.get('receipts') or []:
                self.receipts.setdefault((identity, row.get('taskId')), []).append(row)
        # 의미 검토 호출 번호(최신 dev). 원격 측정 기록이 없거나 번호를 모르는 검토가 있으면 None(검토 호출을 가를 수 없음).
        reviews = semantic_reviews(self.trace)
        self.review_sequences = (None if reviews is None or any(item['modelCallSequences'] is None for item in reviews)
                                 else {key for item in reviews for key in item['modelCallSequences']})

    def outputs(self, step, kind):
        # 그 단계가 낸 Artifact 중 한 종류(없으면 None, 같은 종류가 여럿이면 마지막 것)
        found = [item for item in self.by_step.get(step['workflowStepId'], []) if item.get('artifactType') == kind]
        return found[-1] if found else None

    def received(self, step):
        # 그 단계 에이전트가 실제로 받은 입력(A2A 요청 metadata). 작업 저장소에 Task가 없으면 None.
        row = self.tasks.get((step.get('agentIdentity'), step.get('a2aTaskId')))
        return None if row is None else (row.get('metadata') or {})

    def planner_requirement_ids(self):
        # Planner가 낸 요구사항 목록(REQUIREMENT Artifact)의 ID. 없으면 None.
        artifact = next((item for item in self.artifacts if item.get('artifactType') == 'REQUIREMENT'), None)
        if artifact is None:
            return None
        return [str(item.get('requirementId')) for item in (artifact.get('payload') or {}).get('requirements', [])]

    def latest(self, kind, version=None):
        # 한 종류의 Artifact 중 (그 버전의) 마지막 것
        found = [item for item in self.artifacts if item.get('artifactType') == kind
                 and (version is None or item.get('codeVersion') == version)]
        return found[-1] if found else None


# ---------------------------------------------------------------- ② 정보 전달

def _expected_inputs(run: _Run, step):
    # 규칙상 이 단계가 받아야 할 입력: (Artifact ID 집합, 요구사항 ID 집합, 부분집합만 확인할지). 모르면 None.
    role, version = step['role'], step.get('codeVersion')
    planned = run.planner_requirement_ids()
    if role == 'PLANNER':
        return set(), set(), False
    if step.get('phase') == 'IMPLEMENTING':
        requirement = next((item for item in run.artifacts if item.get('artifactType') == 'REQUIREMENT'), None)
        return (None if requirement is None or planned is None
                else ({requirement['artifactId']}, set(planned), False))
    if step.get('phase') == 'FIXING':
        # 수정 단계: 직전 버전 Snapshot과, 이 버전으로 고치라고 한 Issue의 보고서는 반드시 있어야 한다(더 있어도 됨).
        source = run.latest('SOURCE', (version or 0) - 1)
        issues = [item for item in run.bundle.get('issues', []) if item.get('newCodeVersion') == version]
        if source is None or planned is None:
            return None
        return {source['artifactId'], *(str(item['reportArtifactId']) for item in issues if item.get('reportArtifactId'))}, set(planned), True
    source = run.latest('SOURCE', version)
    return None if source is None else ({source['artifactId']}, run.role_requirements.get(role, set()), False)


def _delivery(run: _Run, step):
    # 전달 완전성: 받은 입력 = 받아야 할 입력인가?
    received = run.received(step)
    if received is None:
        return _row('UNVERIFIED', None, 'A2A_TASK_NOT_IN_EXPORT')
    expected = _expected_inputs(run, step)
    if expected is None:
        return _row('UNVERIFIED', None, 'EXPECTED_INPUT_UNKNOWN')
    artifacts, requirements, subset = expected
    got_artifacts = set(map(str, received.get('projectArtifactIds') or ()))
    got_requirements = set(map(str, received.get('requirementIds') or ()))
    missing = sorted((artifacts - got_artifacts) | (requirements - got_requirements))
    extra = [] if subset else sorted((got_artifacts - artifacts) | (got_requirements - requirements))
    wrong_version = received.get('codeVersion') != step.get('codeVersion')
    value = f'Artifact {len(got_artifacts)}개 · 요구사항 {len(got_requirements)}개'
    if missing or extra or wrong_version:
        return _row('FAIL', value, {'missing': missing, 'extra': extra, 'codeVersionMismatch': wrong_version})
    return _row('PASS', value)


def _interpretation(run: _Run, step):
    # 해석 정확성: 받은 입력에 맞는 결과를 냈는가?
    role, received = step['role'], run.received(step)
    if role == 'PLANNER':
        return _plan_check(run)  # 받은 시나리오 요구사항을 계획 작업에 모두 연결했는가
    if received is None:
        return _row('UNVERIFIED', None, 'A2A_TASK_NOT_IN_EXPORT')
    wanted = set(map(str, received.get('requirementIds') or ()))
    if role == 'DEVELOPER':
        source = run.outputs(step, 'SOURCE')
        if source is None:
            return _row('UNVERIFIED', None, 'SOURCE_NOT_PRODUCED')
        missing = sorted(wanted - set(map(str, source.get('requirementIds') or ())))
        wrong = source.get('codeVersion') != received.get('codeVersion')
        detail = {'missingRequirements': missing, 'codeVersionMismatch': wrong}
        return _row('FAIL' if missing or wrong else 'PASS', f'코드 버전 {source.get("codeVersion")}', detail if missing or wrong else None)
    kind = 'QA_REPORT' if role == 'QA' else 'SECURITY_REPORT'
    report = run.outputs(step, kind)
    if report is None:
        return _row('UNVERIFIED', None, kind + '_NOT_PRODUCED')
    rows = report.get('tests') if role == 'QA' else report.get('requirementResults')
    covered = {str(item.get('requirementId')) for item in rows or ()}
    tested = str(((report.get('executionManifest') or {}).get('projectArtifactId')) or '')
    given = set(map(str, received.get('projectArtifactIds') or ()))
    missing, extra = sorted(wanted - covered), sorted(covered - wanted)
    wrong_source = tested not in given
    if missing or extra or wrong_source:
        return _row('FAIL', f'요구사항 {len(covered & wanted)}/{len(wanted)}',
                    {'missingRequirements': missing, 'extraRequirements': extra, 'testedOtherSnapshot': wrong_source})
    return _row('PASS', f'요구사항 {len(covered)}/{len(wanted)} · 받은 Snapshot 검사')


def _transfer(run: _Run, step):
    # 전달 경로: 받은 Artifact를 만든 에이전트가 이 단계 에이전트와 다르면 AGENT_TO_AGENT, 모두 같으면 SAME_AGENT.
    # 받은 Artifact가 없으면(기획) NONE, 만든 단계를 모르면 None.
    received = run.received(step)
    if received is None:
        return None
    producers = set()
    for artifact_id in map(str, received.get('projectArtifactIds') or ()):
        artifact = run.by_id.get(artifact_id)
        producer = None if artifact is None else run.step_agent.get(artifact.get('workflowStepId'))
        if producer is None:
            return None
        producers.add(producer)
    if not producers:
        return 'NONE'
    return 'SAME_AGENT' if producers == {step.get('agentIdentity')} else 'AGENT_TO_AGENT'


def _has_cycle(graph) -> bool:
    # 작업 의존관계(작업 → 먼저 끝나야 할 작업들)에 순환이 있는가
    state = {}

    def visit(node):
        if state.get(node) == 1:
            return True
        if state.get(node) == 2:
            return False
        state[node] = 1
        if any(visit(next_node) for next_node in graph.get(node, ())):
            return True
        state[node] = 2
        return False

    return any(visit(node) for node in graph)


def _plan_check(run: _Run):
    # Planner 계획(implementationPlan) 확인: 시나리오 요구사항이 모두 어떤 작업에 연결됐는가, 의존관계가 아는 다른 작업만
    # 가리키고 순환이 없는가. dev가 계획을 받을 때 같은 규칙을 강제하므로 받아들여진 계획은 보통 PASS다(기록 확인용이지
    # Single·Multi 능력 차이를 가르는 항목이 아님). 요구사항 목록(requirements)은 Host가 시나리오에서 채우는 값이라
    # 시나리오와 다르면 Planner가 아니라 기록·Host 문제로 따로 알린다(requirementListMismatch).
    artifact = next((item for item in run.artifacts if item.get('artifactType') == 'REQUIREMENT'), None)
    if artifact is None:
        return _row('UNVERIFIED', None, 'REQUIREMENT_ARTIFACT_MISSING')
    tasks = (artifact.get('payload') or {}).get('implementationPlan')
    if not tasks:
        return _row('UNVERIFIED', None, 'PLAN_MISSING')
    expected = set(run.requirements)
    task_ids = [str(task.get('taskId')) for task in tasks]
    linked = {str(item) for task in tasks for item in task.get('requirementIds') or ()}
    key = lambda ids: sorted(run.requirements.get(item, {}).get('key', item) for item in ids)
    bad_dependencies = sorted({str(dep) for task in tasks for dep in task.get('dependsOn') or ()
                               if str(dep) not in task_ids or str(dep) == str(task.get('taskId'))})
    graph = {str(task.get('taskId')): {str(dep) for dep in task.get('dependsOn') or ()} & set(task_ids) for task in tasks}
    listed = run.planner_requirement_ids() or []
    detail = {'uncoveredRequirements': key(expected - linked), 'unknownRequirements': sorted(linked - expected),
              'badDependencies': bad_dependencies, 'duplicateTasks': len(task_ids) != len(set(task_ids)),
              'dependencyCycle': _has_cycle(graph),
              'requirementListMismatch': set(listed) != expected or len(listed) != len(set(listed))}
    value = f'작업 {len(tasks)}개 · 요구사항 {len(expected & linked)}/{len(expected)} 연결'
    return _row('FAIL', value, detail) if any(detail.values()) else _row('PASS', value)


RECEIPT_KEYS = ('runId', 'scenarioId', 'workflowStepId', 'attempt', 'codeVersion', 'requirementIds', 'projectArtifactIds')


def _receipt(run: _Run, step):
    # 수신 기록 대조: 에이전트 저장소의 첫 메시지 접수 기록이 그 단계 Task의 입력 metadata·Context와 같은가?
    # 접수 기록은 "받았다"는 증거일 뿐 내용 이해·도구 실행·제품 성공을 증명하지 않는다. 기록이 없으면 FAIL이 아니라 미검증이다.
    identity, task_id = step.get('agentIdentity'), step.get('a2aTaskId')
    if identity not in run.receipt_identities:
        return _row('UNVERIFIED', None, 'RECEIPTS_NOT_IN_EXPORT')
    task = run.tasks.get((identity, task_id))
    if task is None:
        return _row('UNVERIFIED', None, 'A2A_TASK_NOT_IN_EXPORT')
    rows = run.receipts.get((identity, task_id), [])
    if not rows:
        return _row('UNVERIFIED', '접수 기록 0건', 'NO_RECEIPT_FOR_TASK')
    metadata, first = task.get('metadata') or {}, rows[0]
    received = first.get('metadata') or {}
    mismatched = sorted(key for key in RECEIPT_KEYS if (key in metadata or key in received)
                        and _number(received.get(key)) != _number(metadata.get(key)))
    if first.get('contextId') != task.get('contextId'):
        mismatched.append('contextId')
    value = f'접수 기록 {len(rows)}건'
    return _row('FAIL', value, {'mismatchedFields': mismatched}) if mismatched else _row('PASS', value)


def handoff_table(run: _Run) -> list:
    from .model_input import model_input_assessment
    rows = []
    for step in run.steps:
        # 평가 기준이 없는 phase는 받아야 할 입력·결과를 모르므로 두 칸 모두 미검증이다. 수신 기록 대조는 역할 규칙이 필요 없어 그대로 한다.
        unmapped = _row('UNVERIFIED', None, 'PHASE_NOT_MAPPED')
        rows.append({'workflowStepId': step['workflowStepId'], 'role': step['role'], 'phase': step.get('phase'),
                     'agentIdentity': step.get('agentIdentity'), 'transfer': _transfer(run, step),
                     'codeVersion': step.get('codeVersion'), 'stepStatus': step.get('status'),
                     'delivery': _delivery(run, step) if step['mapped'] else unmapped,
                     'interpretation': _interpretation(run, step) if step['mapped'] else unmapped,
                     'receipt': _receipt(run, step), 'modelInput': model_input_assessment(run, step)})
    return rows


# ---------------------------------------------------------------- ① 작업 정확성

def _report_verdicts(agent_report, role):
    # 에이전트 보고서 하나의 요구사항별 판정(하나라도 FAIL이면 FAIL, 모두 PASS면 PASS, 그 밖은 UNVERIFIED)
    rows = agent_report.get('tests') if role == 'QA' else agent_report.get('requirementResults')
    outcomes = {}
    for item in rows or ():
        outcomes.setdefault(str(item.get('requirementId')), []).append(item.get('outcome'))
    return {key: ('FAIL' if 'FAIL' in values else 'PASS' if all(value == 'PASS' for value in values) else 'UNVERIFIED')
            for key, values in outcomes.items()}


def _evaluation_verdicts(report: Optional[Mapping], role):
    # 독립 평가 보고서에서 그 관점(QA·SECURITY) 검사만으로 본 요구사항별 결과
    if report is None:
        return None
    statuses = {}
    for check in report.get('checks', []):
        if check.get('perspective') == role and check.get('required', True):
            statuses.setdefault(str(check.get('requirementId')), []).append(check.get('status'))
    return {key: ('FAIL' if 'FAIL' in values else 'PASS' if all(value == 'PASS' for value in values) else 'UNVERIFIED')
            for key, values in statuses.items()}


def _evaluation_binding(run, report):
    # 보고서의 PASS 문구만으로 이 Run의 성과를 인정하지 않는다.
    if report.get('sourceRunId') != run.run_id:
        return 'EVALUATION_RUN_IDENTITY_UNKNOWN'
    if report.get('architecture') not in (None, run.architecture):
        return 'EVALUATION_ARCHITECTURE_MISMATCH'
    if ((report.get('manifestCheck') or {}).get('metadataMatches') is not True
            or (report.get('manifestCheck') or {}).get('errorCode')):
        return 'EVALUATION_MANIFEST_NOT_VERIFIED'
    return None


def _manifest_difference(evaluated, actual):
    # 옛 내보내기의 Artifact ID 결합도 지원하되, 기록에 있는 다른 코드·환경 정보와 모순되면 사용하지 않는다.
    for field, reason in (('snapshotSha256', 'SNAPSHOT_MISMATCH'),
                          ('projectArtifactId', 'PROJECT_ARTIFACT_MISMATCH'),
                          ('repositoryId', 'EVALUATION_ENVIRONMENT_MISMATCH'),
                          ('commitHash', 'EVALUATION_ENVIRONMENT_MISMATCH'),
                          ('gitObjectFormat', 'EVALUATION_ENVIRONMENT_MISMATCH'),
                          ('treeHash', 'EVALUATION_ENVIRONMENT_MISMATCH'),
                          ('containerImageDigest', 'EVALUATION_ENVIRONMENT_MISMATCH'),
                          ('dependencyLockHash', 'EVALUATION_ENVIRONMENT_MISMATCH')):
        if evaluated.get(field) and actual.get(field) and evaluated[field] != actual[field]:
            return reason
    if not any(evaluated.get(field) and evaluated.get(field) == actual.get(field)
               for field in ('snapshotSha256', 'projectArtifactId')):
        return 'SNAPSHOT_IDENTITY_UNKNOWN'
    return None


def _independent_checks_problem(run, report):
    # dict의 마지막 중복 행이나 다른 요구사항의 PASS를 재사용해 점수를 만들 수 없게 한다.
    checks, requirements = report.get('checks'), report.get('requirements')
    if not isinstance(checks, list) or not checks or not isinstance(requirements, list) or not requirements:
        return 'INDEPENDENT_CHECKS_INCOMPLETE'
    by_id, linked, requirement_ids = {}, set(), set()
    for check in checks:
        if not isinstance(check, Mapping):
            return 'INDEPENDENT_CHECKS_INVALID'
        identity, requirement, role = check.get('testId'), str(check.get('requirementId')), check.get('perspective')
        if (not isinstance(identity, str) or not identity or identity in by_id
                or not isinstance(role, str) or role not in run.role_requirements or requirement not in run.role_requirements[role]
                or type(check.get('required', True)) is not bool
                or check.get('status') not in ('PASS', 'FAIL', 'UNVERIFIED', 'ERROR', 'NOT_RUN', 'NOT_APPLICABLE')):
            return 'INDEPENDENT_CHECKS_INVALID'
        if check.get('status') == 'NOT_APPLICABLE' and not (check.get('exclusionApprovalRef') and check.get('exclusionReason')):
            return 'INDEPENDENT_CHECKS_INVALID'
        by_id[identity] = check
    for requirement in requirements:
        if not isinstance(requirement, Mapping):
            return 'INDEPENDENT_CHECKS_INVALID'
        identity, check_ids = str(requirement.get('requirementId')), requirement.get('checkIds')
        if identity in requirement_ids or not isinstance(check_ids, list):
            return 'INDEPENDENT_CHECKS_INVALID'
        requirement_ids.add(identity)
        if not check_ids:
            return 'INDEPENDENT_CHECKS_INCOMPLETE'
        for check_id in check_ids:
            if not isinstance(check_id, str) or check_id in linked:
                return 'INDEPENDENT_CHECKS_INVALID'
            if check_id not in by_id:
                return 'INDEPENDENT_CHECKS_INCOMPLETE'
            if str(by_id[check_id].get('requirementId')) != identity:
                return 'INDEPENDENT_CHECKS_INVALID'
            linked.add(check_id)
    return None if linked == set(by_id) else 'INDEPENDENT_CHECKS_INVALID'


def _judgement_accuracy(run: _Run, role, report):
    # QA·Security 판정을 독립 평가와 대조한다. FAIL/PASS 불일치만으로 에이전트의 오탐이라고 단정하지 않는다.
    # 같은 코드 버전(같은 Snapshot)끼리만 비교한다. 수정 전 버전의 FAIL을 수정 후 버전의 평가 PASS와 비교하면, 결함을
    # 제대로 찾은 판정을 "잘못된 FAIL"로 세게 되기 때문이다. 평가한 버전은 보고서의 expectedManifest.codeVersion이고,
    # 그 버전의 에이전트 보고서가 없으면 다른 버전으로 대신하지 않고 미검증이다.
    kind = 'QA_REPORT' if role == 'QA' else 'SECURITY_REPORT'
    manifest = (report or {}).get('expectedManifest') or {}
    version = run.bundle['run'].get('codeVersion') if report is None else manifest.get('codeVersion')
    if version is None:
        return _row('UNVERIFIED', None, 'EVALUATED_VERSION_UNKNOWN')
    agent_report = run.latest(kind, version)
    if agent_report is None:
        return _row('UNVERIFIED', f'v{version}', role + '_REPORT_FOR_EVALUATED_VERSION_MISSING')
    if report is None:
        return _row('UNVERIFIED', None, 'INDEPENDENT_EVALUATION_MISSING')
    problem = _evaluation_binding(run, report)
    if problem:
        return _row('UNVERIFIED', f'v{version}', problem)
    agent_manifest = agent_report.get('executionManifest') or {}
    evaluated, tested = manifest.get('snapshotSha256'), agent_manifest.get('snapshotSha256')
    problem = _manifest_difference(manifest, agent_manifest)
    if problem:
        return _row('UNVERIFIED', f'v{version}', problem)
    if not report.get('checks'):
        return _row('UNVERIFIED', f'v{version}', 'INDEPENDENT_CHECKS_MISSING')
    problem = _independent_checks_problem(run, report)
    if problem:
        return _row('UNVERIFIED', f'v{version}', problem)
    own, truth = _report_verdicts(agent_report, role), _evaluation_verdicts(report, role)
    if not truth:
        return _row('UNVERIFIED', f'v{version}', 'INDEPENDENT_CHECKS_MISSING')
    keys = lambda ids: sorted(run.requirements.get(item, {}).get('key', item) for item in ids)
    expected = run.role_requirements[role]
    shared = [key for key in expected if key in own and truth.get(key) in ('PASS', 'FAIL') and own[key] in ('PASS', 'FAIL')]
    missed = keys(key for key in shared if own[key] == 'PASS' and truth[key] == 'FAIL')
    false_alarm = keys(key for key in shared if own[key] == 'FAIL' and truth[key] == 'PASS')
    unknown = keys(key for key in expected if key not in shared)
    unexpected = keys(set(truth) - expected)
    agreed = len(shared) - len(missed) - len(false_alarm)
    # 판정을 낸 도구(에이전트가 쓴 테스트를 돌린 도구·공통 스캐너 등). 도구 결과와 에이전트 기여를 나눠 읽게 함께 적는다.
    rows = agent_report.get('tests') if role == 'QA' else agent_report.get('requirementResults')
    tools = sorted({str((item.get('toolEvidence') or {}).get('toolName')) for item in rows or ()
                    if (item.get('toolEvidence') or {}).get('toolName')})
    detail = {'codeVersion': version, 'missedDefects': missed, 'agentFailExternalPass': false_alarm,
              'notComparable': unknown, 'evidenceTools': tools,
              'identityBasis': 'SNAPSHOT_SHA256' if evaluated and tested else 'PROJECT_ARTIFACT_ID',
              'interpretation': 'DISAGREEMENT_REQUIRES_REVIEW' if missed or false_alarm else 'BOUNDED_CHECK_AGREEMENT'}
    if unexpected:
        detail['unexpectedRequirements'] = unexpected
    status = 'FAIL' if missed or false_alarm else 'UNVERIFIED' if unknown or unexpected or not expected else 'PASS'
    return _row(status, f'v{version} 일치 {agreed}/{len(expected)}', detail)


def _issue_grounding(run: _Run, role):
    # 그 역할이 만든 Issue가 실제로 FAIL인 자기 검사를 가리키는가
    issues = [item for item in run.bundle.get('issues', []) if item.get('reporter') == role]
    if not issues:
        return _row('NOT_APPLICABLE', 'Issue 없음')
    grounded = 0
    for issue in issues:
        report = run.by_id.get(str(issue.get('reportArtifactId')))
        rows = (report or {}).get('tests' if role == 'QA' else 'requirementResults') or ()
        failing = {str(item.get('requirementId')) for item in rows if item.get('outcome') == 'FAIL'}
        grounded += bool(failing) and set(map(str, issue.get('requirementIds') or ())) <= failing
    return _row('PASS' if grounded == len(issues) else 'FAIL', f'{grounded}/{len(issues)}')


def _builds(run: _Run):
    builds = [item for item in run.artifacts if item.get('artifactType') == 'BUILD_REPORT']
    if not builds:
        return _row('UNVERIFIED', None, 'BUILD_REPORT_MISSING')
    version = run.bundle['run'].get('codeVersion')
    if type(version) is not int or version < 1:
        return _row('UNVERIFIED', None, 'BUILD_VERSION_SCOPE_UNKNOWN')
    expected = set(range(1, version + 1))
    by_version = {number: [item for item in builds if item.get('codeVersion') == number] for number in expected}
    passed = sum(len(items) == 1 and items[0].get('executionOutcome') == 'PASS' for items in by_version.values())
    failed = any(item.get('executionOutcome') == 'FAIL' for item in builds)
    complete = passed == len(expected) and len(builds) == len(expected)
    return _row('FAIL' if failed else 'PASS' if complete else 'UNVERIFIED', f'{passed}/{len(expected)} 버전')


def _final_product(run, report):
    if report is None:
        return _row('UNVERIFIED', None, 'INDEPENDENT_EVALUATION_MISSING')
    if (report.get('expectedManifest') or {}).get('codeVersion') != run.bundle['run'].get('codeVersion'):
        return _row('UNVERIFIED', None, 'FINAL_VERSION_NOT_EVALUATED')
    problem = _evaluation_binding(run, report)
    if problem:
        return _row('UNVERIFIED', None, problem)
    source = run.latest('SOURCE', run.bundle['run'].get('codeVersion'))
    if source is None:
        return _row('UNVERIFIED', None, 'FINAL_SOURCE_MISSING')
    problem = _manifest_difference(report.get('expectedManifest') or {},
                                   {**source, 'projectArtifactId': source.get('artifactId')})
    if problem:
        return _row('UNVERIFIED', None, problem)
    problem = _independent_checks_problem(run, report)
    if problem:
        return _row('UNVERIFIED', None, problem)
    score = exam_score(report)
    verdict = report.get('evaluationVerdict')
    value = (None if score['score'] is None or score['completionRate'] is None
             else f'점수 {score["score"]:.2f} · 완료율 {score["completionRate"]:.2f}')
    # 누락된 검사/요구사항으로 만들어진 PASS나 완료율 0의 PASS를 그대로 전파하지 않는다.
    expected = set().union(*run.role_requirements.values())
    requirements = [str(item.get('requirementId')) for item in report.get('requirements', [])]
    if set(requirements) != expected or len(requirements) != len(expected):
        return _row('UNVERIFIED', value, 'INDEPENDENT_REQUIREMENT_SCOPE_MISMATCH')
    if (set(score['requirementShares']) != expected
            or any(run.role_requirements[role] - set(_evaluation_verdicts(report, role)) for role in run.role_requirements)
            or verdict == 'PASS' and (score['score'] != 1 or score['completionRate'] != 1)):
        return _row('UNVERIFIED', value, 'INDEPENDENT_CHECKS_INCOMPLETE')
    return _row(verdict if verdict in ('PASS', 'FAIL') else 'UNVERIFIED', value,
                {'securityViolations': score['securityViolations']} if score['securityViolations'] else None)


def _fixes(run: _Run):
    # 수정 요청(Issue)마다 고친 버전에서 같은 역할의 판정이 PASS가 됐는가
    issues = [item for item in run.bundle.get('issues', []) if item.get('newCodeVersion')]
    if not issues:
        return _row('NOT_APPLICABLE', '수정 요청 없음')
    fixed, unknown, failed, details = 0, 0, 0, []
    for issue in issues:
        kind = {'QA': 'QA_REPORT', 'SECURITY': 'SECURITY_REPORT'}.get(issue.get('reporter'), 'BUILD_REPORT')
        report = run.latest(kind, issue['newCodeVersion'])
        if report is None:
            unknown += 1
            details.append({'issueId': issue.get('issueId'), 'status': 'UNVERIFIED', 'reason': 'REVALIDATION_REPORT_MISSING'})
            continue
        if kind == 'BUILD_REPORT':
            outcome = report.get('executionOutcome')
            fixed += outcome == 'PASS'
            failed += outcome == 'FAIL'
            unknown += outcome not in ('PASS', 'FAIL')
            details.append({'issueId': issue.get('issueId'), 'status': outcome if outcome in ('PASS', 'FAIL') else 'UNVERIFIED'})
            continue
        rows = report.get('tests') if kind == 'QA_REPORT' else report.get('requirementResults')
        wanted = set(map(str, issue.get('requirementIds') or ()))
        observed = {}
        for item in rows or ():
            requirement = str(item.get('requirementId'))
            if requirement in wanted:
                observed.setdefault(requirement, []).append(item.get('outcome'))
        missing = sorted(wanted - observed.keys())
        uncertain = sorted(key for key, values in observed.items() if any(value not in ('PASS', 'FAIL') for value in values))
        confirmed = sorted(key for key, values in observed.items() if 'FAIL' in values)
        unverified = not wanted or bool(missing or uncertain)
        unknown += unverified
        failed += bool(confirmed)
        fixed += not unverified and not confirmed
        details.append({'issueId': issue.get('issueId'), 'status': 'UNVERIFIED' if unverified else 'FAIL' if confirmed else 'PASS',
                        'missingRequirements': missing, 'unverifiedRequirements': uncertain,
                        'confirmedFailedRequirements': confirmed})
    status = 'UNVERIFIED' if unknown else 'PASS' if fixed == len(issues) else 'FAIL'
    return _row(status, f'{fixed}/{len(issues)}', {'issues': details, 'confirmedFailedIssues': failed})


def accuracy_table(run: _Run, report: Optional[Mapping]) -> list:
    return [
        {'role': 'PLANNER', 'item': '계획이 요구사항을 모두 작업에 연결(의존관계 모순 없음)', **_plan_check(run)},
        # 조건 보존·작업 구체성은 기록만으로 판정할 수 없어 사람 검토 전까지 미검증이다.
        {'role': 'PLANNER', 'item': '계획 내용 타당성(조건 보존·작업 구체성)', **_row('UNVERIFIED', None, 'HUMAN_REVIEW_REQUIRED')},
        {'role': 'DEVELOPER', 'item': '버전마다 빌드 성공', **_builds(run)},
        {'role': 'DEVELOPER', 'item': '수정 요청을 고침(고친 버전에서 PASS)', **_fixes(run)},
        {'role': 'QA', 'item': 'QA 판정이 독립 평가와 일치', **_judgement_accuracy(run, 'QA', report)},
        {'role': 'QA', 'item': 'Issue가 실제 FAIL 검사를 가리킴', **_issue_grounding(run, 'QA')},
        {'role': 'SECURITY', 'item': 'Security 판정이 독립 평가와 일치', **_judgement_accuracy(run, 'SECURITY', report)},
        {'role': 'SECURITY', 'item': 'Issue가 실제 FAIL 검사를 가리킴', **_issue_grounding(run, 'SECURITY')},
        {'role': 'SYSTEM', 'item': '최종 결과물이 독립 평가 통과', **_final_product(run, report)},
    ] + [{'role': role, 'item': '평가 기준이 정해지지 않은 에이전트 단계', **_row('UNVERIFIED', None, 'PHASE_NOT_MAPPED')}
         for role in run.unmapped_roles]


# ---------------------------------------------------------------- ③ 처리시간·오류

def _step_times(run: _Run, step):
    # A2A Task 상태 사건으로 잰 (처리 시간, 대기 시간, 마지막 상태). 접수(SUBMITTED)→끝, 접수→작업 시작(WORKING)
    events = run.task_events.get((step.get('agentIdentity'), step.get('a2aTaskId')), [])
    first = {state: stamp for state, stamp in reversed(events)}
    end = next((stamp for state, stamp in reversed(events) if state in TERMINAL_TASK_STATES), None)
    final = events[-1][0] if events else None
    return _ms(first.get('TASK_STATE_SUBMITTED'), end), _ms(first.get('TASK_STATE_SUBMITTED'), first.get('TASK_STATE_WORKING')), final


def _usage_evidence(run: _Run, step_role):
    """역할 합계에 쓰기 전에 전체 영수증 수·고유 호출·배정을 확인한다. 부분 기록도 진단으로 보존한다."""
    usage = run.bundle.get('usage') or {}
    records, summary = usage.get('records'), usage.get('summary') or {}
    reasons, found, duplicates = [], {}, set()
    if not isinstance(records, list):
        return [], False, ['USAGE_RECORDS_MISSING']
    if type(usage.get('total')) is not int or usage['total'] != len(records):
        reasons.append('USAGE_RECORD_COUNT_UNVERIFIED')
    if usage.get('recordsConsistent', True) is not True:
        reasons.append('USAGE_RECORDS_INCONSISTENT')
    if (summary.get('modelCalls') != len(records) or summary.get('pendingModelSequences')
            or summary.get('missingUsageSequences')):
        reasons.append('USAGE_CALL_COUNT_UNVERIFIED')
    if usage.get('runId', run.run_id) != run.run_id:
        return [], False, ['USAGE_RUN_MISMATCH']
    for item in records:
        if not isinstance(item, dict) or not isinstance(item.get('detail'), dict) or not isinstance(item.get('binding'), dict):
            reasons.append('USAGE_RECORD_INVALID')
            continue
        detail, binding = item['detail'], item['binding']
        sequence = _number(detail.get('sequence', item.get('sequence')))
        if type(sequence) is not int or sequence < 1:
            reasons.append('USAGE_SEQUENCE_INVALID')
            continue
        if sequence in found:
            reasons.append('USAGE_SEQUENCE_DUPLICATED')
            duplicates.add(sequence)
        if any(key in item and _number(item[key]) != _number(detail.get(key)) for key in
               ('sequence', 'inputTokens', 'outputTokens', 'totalTokens', 'usageKnown', 'costUsd', 'durationMs')):
            reasons.append('USAGE_VALUE_MISMATCH')
            duplicates.add(sequence)  # 어느 값이 맞는지 정하지 않는다.
        step = binding.get('workflowStepId')
        if (binding.get('runId', run.run_id) != run.run_id or step not in step_role
                or binding.get('role') not in (None, step_role.get(step), run.step_agent.get(step))):
            reasons.append('USAGE_ROLE_UNASSIGNED')
            duplicates.add(sequence)
        found[sequence] = item
    if set(found) != set(range(1, len(records) + 1)):
        reasons.append('USAGE_SEQUENCE_INCOMPLETE')
    return [item for sequence, item in found.items() if sequence not in duplicates], not reasons, sorted(set(reasons))


def timing_table(run: _Run) -> list:
    step_role = {step['workflowStepId']: step['role'] for step in run.steps}
    usable_usage, usage_complete, usage_reasons = _usage_evidence(run, step_role)
    tokens_confirmed, cost_confirmed, measurement_reasons = _measurement_summary(run.bundle.get('usage') or {}, usable_usage)
    usage_reasons = sorted(set(usage_reasons + measurement_reasons))
    rows = []
    for role in ROLES + tuple(run.unmapped_roles):  # 평가 기준이 없는 단계도 시간·오류는 잰다(숨기지 않음)
        steps = [step for step in run.steps if step['role'] == role]
        times = [_step_times(run, step) for step in steps]
        durations = [item[0] for item in times]
        errors = [f'{step.get("phase")} v{step.get("codeVersion")}: 단계 {step.get("status")}' for step in steps
                  if step.get('status') != 'SUCCEEDED']
        # 에이전트 오류는 실패·거부로 끝난 Task만 센다. 끝 상태 기록이 없는 Task는 오류가 아니라 기록 부족(시간 None)이다.
        errors += [f'{step.get("phase")} v{step.get("codeVersion")}: A2A {final}' for step, (_d, _w, final) in zip(steps, times)
                   if final in AGENT_ERROR_STATES]
        measured = [item for item in usable_usage if step_role.get(item['binding'].get('workflowStepId')) == role]
        llm = measured if usage_complete else None
        tools = None if run.telemetry is None else _tool_calls(run.telemetry, step_role, role)
        errors += [f'LLM 호출 {item["detail"].get("outcome")}' for item in measured
                   if item['detail'].get('outcome') not in (None, 'completed')]
        tool_outcomes = None if tools is None else sorted({str(item['outcome']) for item in tools})
        # 의미 검토 호출(최신 dev): 같은 역할·같은 예산으로 기록되므로 LLM 호출 수에 들어 있다. 작업 호출과 따로 보이려고 뽑는다.
        review = (None if llm is None or run.review_sequences is None
                  else [item for item in llm if _number((item.get('detail') or {}).get('sequence')) in run.review_sequences])
        known = None not in durations and bool(durations)
        rows.append({
            'role': role, 'steps': len(steps), 'agents': sorted({str(step.get('agentIdentity')) for step in steps}),
            'agentMs': sum(durations) if known else None,                       # 접수부터 끝까지(단계 합)
            'queueMs': sum(item[1] for item in times) if times and None not in [item[1] for item in times] else None,
            'llmCalls': None if llm is None else len(llm),
            'llmMs': None if llm is None else _total(llm, 'durationMs'),
            'inputTokens': None if llm is None or not tokens_confirmed else _total(llm, 'inputTokens'),
            'outputTokens': None if llm is None or not tokens_confirmed else _total(llm, 'outputTokens'),
            'costUsd': None if llm is None or not cost_confirmed else _total(llm, 'costUsd', money=True),
            'reviewLlmCalls': None if review is None else len(review),          # 위 LLM 호출 중 의미 검토 호출
            'reviewLlmMs': None if review is None else _total(review, 'durationMs'),
            'reviewInputTokens': None if review is None or not tokens_confirmed else _total(review, 'inputTokens'),
            'reviewOutputTokens': None if review is None or not tokens_confirmed else _total(review, 'outputTokens'),
            'reviewCostUsd': None if review is None or not cost_confirmed else _total(review, 'costUsd', money=True),
            'toolCalls': None if tools is None else len(tools),
            'toolMs': (None if tools is None or any(item['ms'] is None for item in tools)
                       else sum(item['ms'] for item in tools)),
            'toolRetries': None if tools is None else sum(item['retry'] for item in tools),
            'toolOutcomes': tool_outcomes,
            'callRecordsComplete': usage_complete,
            'usageComplete': usage_complete and not measurement_reasons, 'usageWarnings': usage_reasons,
            'knownLlmCalls': len(measured) if measured or usage_complete else None,
            'knownCostUsd': _known_total(measured, 'costUsd', money=True, verified_empty=usage_complete),
            'knownInputTokens': _known_total(measured, 'inputTokens', verified_empty=usage_complete),
            'knownOutputTokens': _known_total(measured, 'outputTokens', verified_empty=usage_complete),
            'errors': errors,
            'status': 'FAIL' if errors else 'PASS' if steps and known and llm is not None and not measurement_reasons
                      and all(item['detail'].get('outcome') is not None for item in llm)
                      else 'UNVERIFIED' if steps else 'NOT_APPLICABLE',
        })
    return rows


def _measurement_summary(usage, records):
    """호출 수 확인과 별개로 Host 사용량 요약과 영수증 합을 대조한다."""
    summary = usage.get('summary') or {}
    tokens, cost, reasons = True, True, []
    if 'usageComplete' in summary and summary['usageComplete'] is not True:
        tokens = cost = False
        reasons.append('USAGE_TOKEN_SUMMARY_UNVERIFIED')
    if 'costComplete' in summary and summary['costComplete'] is not True:
        cost = False
        # 단가 미승인 등으로 영수증에도 비용이 없는 경우는 기존의 값 없음(None)으로 충분하다.
        if any(_measurement(row, 'costUsd', money=True) is not None for row in records):
            reasons.append('USAGE_COST_SUMMARY_UNVERIFIED')
    token_parts = []
    for row in records:
        incoming, outgoing, total = (_measurement(row, key) for key in ('inputTokens', 'outputTokens', 'totalTokens'))
        if incoming is not None and outgoing is not None and total is not None and incoming + outgoing != total:
            tokens = cost = False
            reasons.append('USAGE_TOKEN_VALUE_MISMATCH')
        token_parts.append(incoming + outgoing if incoming is not None and outgoing is not None else None)
    for key in ('totalTokens', 'knownTotalTokens'):
        if key not in summary:
            continue  # 해당 합계가 없던 이전 인계 형식은 개별 영수증 검사만 적용한다.
        parts = token_parts
        measured = sum(value for value in parts if value is not None)
        expected = _number(summary[key])
        if expected is None:
            if key == 'totalTokens':
                tokens = False
                if summary.get('usageComplete') is True:
                    reasons.append('USAGE_TOKEN_SUMMARY_MISMATCH')
        elif type(expected) is not int or expected < 0 or expected != measured or (
                key == 'totalTokens' and any(value is None for value in parts)):
            tokens = cost = False
            reasons.append('USAGE_TOKEN_SUMMARY_MISMATCH')
    for key in ('costUsd', 'knownCostUsd'):
        if key not in summary:
            continue
        parts = [_measurement(row, 'costUsd', money=True) for row in records]
        expected = _money(summary[key])
        if expected is None:
            if summary[key] is not None:
                cost = False
                reasons.append('USAGE_COST_SUMMARY_MISMATCH')
            if key == 'costUsd':
                cost = False
                if summary.get('costComplete') is True:
                    reasons.append('USAGE_COST_SUMMARY_MISMATCH')
        elif expected != sum((value for value in parts if value is not None), Decimal(0)) or (
                key == 'costUsd' and any(value is None for value in parts)):
            cost = False
            reasons.append('USAGE_COST_SUMMARY_MISMATCH')
    return tokens, cost, sorted(set(reasons))


def _tool_calls(telemetry, step_role, role):
    # 그 역할의 도구 호출(시도) 목록. 호출(MCP_TOOL_CALLED)과 끝(MCP_TOOL_FINISHED)을 시도 번호(attemptId)로 짝지어
    # 시간을 잰다. 끝 사건이 없으면 시간은 None(모름), 결과는 'NOT_FINISHED'.
    started, finished = {}, {}
    for item in telemetry:
        binding, detail = item.get('binding') or {}, item.get('detail') or {}
        if step_role.get(binding.get('workflowStepId')) != role or not detail.get('attemptId'):
            continue
        if binding.get('eventType') == 'MCP_TOOL_CALLED':
            started[detail['attemptId']] = (_time(binding.get('occurredAt')), detail)
        elif binding.get('eventType') == 'MCP_TOOL_FINISHED':
            finished[detail['attemptId']] = (_time(binding.get('occurredAt')), detail)
    calls = []
    for attempt, (start, detail) in started.items():
        end, done = finished.get(attempt, (None, None))
        calls.append({'ms': _ms(start, end), 'outcome': (done or {}).get('outcome', 'NOT_FINISHED'),
                      'retry': (detail.get('toolAttempt') or 0) > 0})
    return calls


_COST_TEXT = re.compile(r'(?:0|[1-9][0-9]*)(?:\.[0-9]+)?')  # dev pricing.UsageRecord.cost_usd와 같은 형식


def _money(value):
    # 비용 한 건을 Decimal로. dev Host는 십진 문자열("0.001")을 주고, 예전 시험 대역은 숫자를 준다.
    # 형식이 틀리거나 음수·무한·참거짓이면 None(모름)이다. 0으로 바꾸지 않는다.
    if type(value) is bool:
        return None
    if isinstance(value, str):
        return Decimal(value) if _COST_TEXT.fullmatch(value) else None
    if isinstance(value, (int, float)):
        try:
            amount = Decimal(str(value))
        except InvalidOperation:
            return None
        return amount if amount.is_finite() and amount >= 0 else None
    return None


def _total(records, key, money=False):
    # 합계. 하나라도 모르면(None) 합계도 모름이다(0으로 채우지 않는다). 비용은 Decimal로 정확히 더한다.
    values = [_measurement(item, key, money) for item in records]
    if not values or any(value is None for value in values):
        return None if values else 0
    return float(round(sum(values, Decimal(0)), 6)) if money else sum(values)


def _measurement(item, key, money=False):
    detail = item.get('detail') or {}
    # 실패 호출의 usageKnown=False를 숫자 0으로 받은 경우도 실제 무료·0토큰이라고 확정하지 않는다.
    if key != 'durationMs' and detail.get('usageKnown', item.get('usageKnown')) is False:
        return None
    value = detail.get(key)
    if money:
        return _money(value)
    value = _number(value)
    if key == 'durationMs':
        return value if type(value) in (int, float) and isfinite(value) and value >= 0 else None
    return value if type(value) is int and value >= 0 else None


def _known_total(records, key, money=False, verified_empty=False):
    values = [_measurement(item, key, money) for item in records]
    known = [value for value in values if value is not None]
    if not known:
        return 0 if not records and verified_empty else None
    return float(round(sum(known, Decimal(0)), 6)) if money else sum(known)


# ---------------------------------------------------------------- 묶기

def run_contract(export: Mapping, scenario_contract: Optional[Mapping] = None) -> Mapping:
    # 이 Run에 쓸 시나리오 정책. 기본은 Run이 시작할 때 고정한 정책(bundle.configuration.scenarioContract)이고,
    # 없으면 Run의 scenarioId로 dev에 등록된 시나리오를 찾는다. 그래서 회원가입 밖의 시나리오(예: 전자문서)도 그대로 된다.
    # 정책을 따로 줬는데 Run이 고정한 정책과 요구사항이 다르면(다른 시나리오) 멈춘다.
    bundle = export['bundle']
    frozen = (bundle.get('configuration') or {}).get('scenarioContract')
    if frozen is None and scenario_contract is None and bundle['run'].get('scenarioId'):
        from uuid import UUID
        from orchestrator.domain.scenario_registry import get_scenario  # dev 등록 시나리오(필요할 때만 읽음)
        found = get_scenario(UUID(str(bundle['run']['scenarioId'])))
        frozen = None if found is None else found.planner_contract()
    if scenario_contract is None:
        if frozen is None:
            raise ValueError('Scenario contract not found in the export or the scenario registry')
        return frozen
    ids = lambda contract: {str(item['requirementId']) for item in contract.get('requirements', [])}
    if frozen is not None and ids(frozen) != ids(scenario_contract):
        raise ValueError("Scenario contract differs from the Run's frozen contract")
    return scenario_contract


def agent_tables(export: Mapping, scenario_contract: Optional[Mapping] = None,
                 evaluation_report: Optional[Mapping] = None, *, planner_review=None) -> dict:
    # 평가표 세 개와 오류 발생 지점을 만든다. evaluation_report는 같은 Run의 D5 독립 평가 보고서여야 한다.
    # scenario_contract를 주지 않으면 Run이 고정한 시나리오 정책을 쓴다(run_contract).
    scenario_contract = run_contract(export, scenario_contract)
    run = _Run(export, scenario_contract)
    source_run = ((evaluation_report or {}).get('sourceRun') or {}).get('runId')
    if evaluation_report is not None and (source_run not in (None, run.run_id)
            or evaluation_report.get('sourceRunId') not in (None, run.run_id)):
        raise ValueError('Evaluation report belongs to another Run')
    handoffs, timing = handoff_table(run), timing_table(run)
    first = None
    for row in handoffs:  # 단계 순서대로 처음 문제가 난 곳(전달 → 해석 → 수신 기록 순). 수신 기록은 불일치(FAIL)만 센다.
        for part in ('delivery', 'interpretation', 'receipt', 'modelInput'):
            if row[part]['status'] == 'FAIL' or row[part]['status'] == 'UNVERIFIED' and part not in ('receipt', 'modelInput'):
                first = {'kind': part, 'role': row['role'], 'phase': row['phase'], 'codeVersion': row['codeVersion'],
                         'status': row[part]['status'], 'detail': row[part]['detail']}
                break
        if first:
            break
    if first is None:
        errored = next((row for row in timing if row['errors']), None)
        if errored:
            first = {'kind': 'agentError', 'role': errored['role'], 'status': 'FAIL', 'detail': errored['errors'][0]}
    defects = [{'reporter': item.get('reporter'), 'codeVersion': item.get('codeVersion'), 'category': item.get('category'),
                'requirements': sorted(run.requirements.get(str(key), {}).get('key', str(key)) for key in item.get('requirementIds') or ()),
                'fixedIn': item.get('newCodeVersion')} for item in run.bundle.get('issues', [])]
    accuracy = accuracy_table(run, evaluation_report)
    if planner_review is not None:
        from .planner_review import assess_review
        review_row = assess_review(export, planner_review)
        next(row for row in accuracy if row['role'] == 'PLANNER' and row['item'].startswith('계획 내용 타당성')).update(review_row)
    return {'schemaVersion': 1, 'runId': run.run_id, 'architecture': run.architecture,
            'scenarioId': str(scenario_contract.get('scenarioId') or run.bundle['run'].get('scenarioId') or '') or None,
            'scenarioKey': scenario_contract.get('scenarioKey'),
            'runStatus': run.handoff.get('runStatus'), 'orchestratorVerdict': run.handoff.get('orchestratorVerdict'),
            'exportCoverage': run.handoff.get('coverage'), 'evaluationReport': evaluation_report is not None,
            'accuracy': accuracy, 'handoffs': handoffs, 'timing': timing,
            'firstProblem': first, 'productDefects': defects}


# ---------------------------------------------------------------- 보여 주기(Markdown)

STATUS_TEXT = {'PASS': 'PASS', 'FAIL': 'FAIL', 'UNVERIFIED': '미검증', 'NOT_APPLICABLE': '해당 없음'}
ROLE_TEXT = {'PLANNER': 'Planner', 'DEVELOPER': 'Developer', 'QA': 'QA', 'SECURITY': 'Security', 'SYSTEM': '전체 시스템'}
# Single은 에이전트가 하나라서 행 이름을 역할(에이전트)이 아니라 단계로 적는다. 여러 Run 모으기는 두 구조를 같은 행에 두므로 단계 이름.
STAGE_TEXT = {'PLANNER': '기획', 'DEVELOPER': '구현·수정', 'QA': 'QA 검증', 'SECURITY': 'Security 검증', 'SYSTEM': '전체 시스템'}
TRANSFER_TEXT = {'AGENT_TO_AGENT': '에이전트 간', 'SAME_AGENT': '같은 에이전트', 'NONE': '-'}
PHASE_TEXT = {'PLANNING': '기획', 'IMPLEMENTING': '구현', 'FIXING': '수정', 'QA': 'QA 검증', 'SECURITY': 'Security 검증'}


def _role(role, single=False) -> str:
    # 역할 표시 이름. Single은 단계 이름(예: "기획 단계"), 평가 기준이 없는 phase는 그 이름 그대로 적는다.
    if single and role in STAGE_TEXT and role != 'SYSTEM':
        return STAGE_TEXT[role] + ' 단계'
    return ROLE_TEXT.get(role, _cell(role))


def _status(row) -> str:
    # 판정 글자와 값(있으면). 근거(detail)는 표가 길어지지 않게 코드만 적는다.
    text = STATUS_TEXT.get(row['status'], _cell(row['status']))
    value = '' if row.get('value') is None else f' ({_cell(row["value"])})'
    detail = row.get('detail')
    reason = '' if not detail or row['status'] == 'PASS' else ' · ' + _cell(
        detail if isinstance(detail, str) else ', '.join(f'{key}={value}' for key, value in detail.items() if value))
    return text + value + reason


def render_markdown(tables: Mapping) -> str:
    # 회의·발표용으로 평가표 세 개를 Markdown 표로 적는다. 에이전트가 쓴 자유 글은 넣지 않는다.
    lines = [f'# Agent별 평가표 — Run {tables["runId"]}', '',
             f'시나리오 {_cell(tables.get("scenarioKey") or tables.get("scenarioId"))} · '
             f'구조 {_cell(tables["architecture"])} · Run 상태 {_cell(tables["runStatus"])} · '
             f'Orchestrator 판정 {_cell(tables["orchestratorVerdict"])} · '
             f'독립 평가 보고서 {"있음" if tables["evaluationReport"] else "없음(그 칸은 미검증)"}', '',
             '## ① 작업 정확성', '', '| 에이전트 | 항목 | 결과 |', '|---|---|---|']
    lines += [f'| {_role(row["role"])} | {row["item"]} | {_status(row)} |' for row in tables['accuracy']]
    lines += ['', '## ② 정보 전달', '', '| 단계 | 에이전트 | 버전 | 전달 완전성 | 해석 정확성 | 수신 기록 대조 | 모델 입력 대조 |',
              '|---|---|---|---|---|---|---|']
    lines += [f'| {PHASE_TEXT.get(row["phase"], _cell(row["phase"]))} | {_role(row["role"])} | '
              f'{"-" if row["codeVersion"] is None else "v" + str(row["codeVersion"])} | {_status(row["delivery"])} | '
              f'{_status(row["interpretation"])} | {_status(row["receipt"]) if row.get("receipt") else UNKNOWN} | '
              f'{_status(row["modelInput"]) if row.get("modelInput") else UNKNOWN} |'
              for row in tables['handoffs']]
    lines += ['', '## ③ 처리시간·오류', '',
              '| 에이전트 | 작업 | 처리 시간 | LLM 호출 | 토큰(입력/출력) | 도구 호출(재시도) | 도구 시간 | 비용 | 오류 | 그중 의미 검토 호출 |',
              '|---|---|---|---|---|---|---|---|---|---|']
    for row in tables['timing']:
        tokens = UNKNOWN if row['inputTokens'] is None else f'{row["inputTokens"]}/{row["outputTokens"]}'
        tools = UNKNOWN if row['toolCalls'] is None else f'{row["toolCalls"]}({row["toolRetries"]})'
        cost = UNKNOWN if row['costUsd'] is None else f'${row["costUsd"]}'
        errors = '없음' if not row['errors'] else _cell('; '.join(row['errors']))
        lines.append(f'| {_role(row["role"])} | {row["steps"]} | {_seconds(row["agentMs"])} | {_cell(row["llmCalls"])} | '
                     f'{tokens} | {tools} | {_seconds(row["toolMs"])} | {cost} | {errors} | {_cell(row.get("reviewLlmCalls"))} |')
    first = tables['firstProblem']
    lines += ['', '## 오류 발생 지점', '',
              '없음(전달·해석·에이전트 오류 모두 없음)' if first is None else
              f'{_role(first["role"])} · {PHASE_TEXT.get(first.get("phase"), "")} '
              f'{"" if first.get("codeVersion") is None else "v" + str(first["codeVersion"])} · {first["kind"]} '
              f'{STATUS_TEXT[first["status"]]} · {_cell(first["detail"])}']
    lines += ['', '## 발견된 제품 결함(에이전트 오류 아님)', '']
    lines += [f'- {_role(item["reporter"])}가 v{item["codeVersion"]}에서 '
              f'{", ".join(item["requirements"])} 결함 발견({_cell(item["category"])}) → '
              f'{"v" + str(item["fixedIn"]) + "에서 수정 요청" if item["fixedIn"] else "수정 요청 없음"}'
              for item in tables['productDefects']] or ['- 없음']
    return '\n'.join(lines) + '\n'


# ---------------------------------------------------------------- 여러 Run 모으기(발표·비교용)
# 실제 실험은 Run이 여러 개라서 Run마다 표를 보지 않고 (시나리오, 구조)별로 모아 본다: 항목별 PASS/FAIL/미검증 Run 수,
# 역할별 전달·해석 실패 단계 수, 처리시간·LLM 사용량의 중앙값(모르는 Run은 빼고 몇 Run에서 알았는지 함께), 오류 발생 지점 분포.

SUMMARY_STATUSES = ('PASS', 'FAIL', 'UNVERIFIED', 'NOT_APPLICABLE')


def _counts(statuses) -> dict:
    return {status: statuses.count(status) for status in SUMMARY_STATUSES}


def _median_known(values) -> dict:
    # 아는 값만의 중앙값과, 몇 Run 중 몇 Run에서 알았는지(모르는 값을 0으로 넣지 않는다)
    known = [value for value in values if value is not None]
    return {'median': median(known) if known else None, 'known': len(known), 'runs': len(values)}


def _summary_group(scenario, architecture, items) -> dict:
    accuracy, handoffs, timing, firsts = {}, {}, {}, {}
    for tables in items:
        for row in tables['accuracy']:
            accuracy.setdefault((row['role'], row['item']), []).append(row['status'])
        for row in tables['handoffs']:
            entry = handoffs.setdefault(row['role'], {'delivery': [], 'interpretation': [], 'receipt': [], 'modelInput': []})
            entry['delivery'].append(row['delivery']['status'])
            entry['interpretation'].append(row['interpretation']['status'])
            # 예전 평가표(수신 기록 칸 이전)는 미검증으로 센다
            entry['receipt'].append((row.get('receipt') or {}).get('status', 'UNVERIFIED'))
            entry['modelInput'].append((row.get('modelInput') or {}).get('status', 'UNVERIFIED'))
        for row in tables['timing']:
            if row['steps']:  # 그 Run에서 이 역할이 일하지 않았으면(예: 중간에 끝남) 세지 않는다
                tokens = (None if row['inputTokens'] is None or row['outputTokens'] is None
                          else row['inputTokens'] + row['outputTokens'])
                entry = timing.setdefault(row['role'], {'agentMs': [], 'llmCalls': [], 'reviewLlmCalls': [], 'tokens': [],
                                                        'costUsd': [], 'errors': 0})
                for name, value in (('agentMs', row['agentMs']), ('llmCalls', row['llmCalls']), ('tokens', tokens),
                                    ('costUsd', row['costUsd']), ('reviewLlmCalls', row.get('reviewLlmCalls'))):
                    entry[name].append(value)
                entry['errors'] += bool(row['errors'])
        first = tables.get('firstProblem')
        where = '없음' if first is None else f'{_role(first["role"])} · {PHASE_TEXT.get(first.get("phase"), "") or "-"} · {first["kind"]}'
        firsts[where] = firsts.get(where, 0) + 1
    return {
        'scenario': scenario, 'architecture': architecture, 'runs': len(items),
        'runIds': [tables['runId'] for tables in items],
        'evaluationReports': sum(bool(tables.get('evaluationReport')) for tables in items),
        'runStatus': _tally(tables.get('runStatus') for tables in items),
        'orchestratorVerdict': _tally(tables.get('orchestratorVerdict') for tables in items),
        'accuracy': [{'role': role, 'item': item, 'counts': _counts(statuses)} for (role, item), statuses in accuracy.items()],
        'handoffs': [{'role': role, 'steps': len(entry['delivery']), 'delivery': _counts(entry['delivery']),
                      'interpretation': _counts(entry['interpretation']), 'receipt': _counts(entry['receipt']),
                      'modelInput': _counts(entry['modelInput'])}
                     for role, entry in handoffs.items()],
        'timing': [{'role': role, 'runs': len(entry['agentMs']), 'runsWithErrors': entry['errors'],
                    **{name: _median_known(entry[name]) for name in ('agentMs', 'llmCalls', 'reviewLlmCalls', 'tokens', 'costUsd')}}
                   for role, entry in timing.items()],
        'firstProblems': [{'where': where, 'runs': count} for where, count in sorted(firsts.items(), key=lambda kv: -kv[1])],
    }


def _tally(values) -> dict:
    found = {}
    for value in values:
        found[str(value)] = found.get(str(value), 0) + 1
    return found


def summarize(tables_list) -> dict:
    # 여러 Run의 agent_tables 결과를 (시나리오, 구조)별로 모은다. 같은 Run이 두 번 들어오면 멈춘다(한 Run을 두 번 세지 않게).
    groups, seen = {}, set()
    for tables in tables_list:
        if tables.get('schemaVersion') != 1:
            raise ValueError('Unsupported agent table schema')
        if tables['runId'] in seen:
            raise ValueError('The same Run was given twice')
        seen.add(tables['runId'])
        key = (tables.get('scenarioKey') or tables.get('scenarioId'), tables.get('architecture'))
        groups.setdefault(key, []).append(tables)
    order = lambda key: (str(key[0]), {'SINGLE_AGENT': 0, 'MULTI_AGENT': 1}.get(key[1], 2), str(key[1]))
    return {'schemaVersion': 1, 'runs': len(seen),
            'groups': [_summary_group(scenario, architecture, groups[(scenario, architecture)])
                       for scenario, architecture in sorted(groups, key=order)]}


def _count_cell(counts) -> str:
    # "PASS 3 · FAIL 1 · 미검증 2"처럼 0이 아닌 것만
    parts = [f'{STATUS_TEXT[status]} {counts[status]}' for status in SUMMARY_STATUSES if counts.get(status)]
    return ' · '.join(parts) or '-'


def _median_cell(entry, show) -> str:
    # 중앙값(모르는 Run이 있으면 "알려진 k/n Run")
    text = UNKNOWN if entry['median'] is None else show(entry['median'])
    return text if entry['known'] == entry['runs'] else f'{text} (알려진 {entry["known"]}/{entry["runs"]} Run)'


def render_summary_markdown(summary: Mapping) -> str:
    lines = [f'# Agent별 평가 요약 — Run {summary["runs"]}개', '',
             '모르는 값은 0으로 채우지 않고 뺐다(괄호의 "알려진 k/n Run"). 시간·사용량은 Run별 합의 중앙값이다.']
    for group in summary['groups']:
        lines += ['', f'## {_cell(group["scenario"])} · {_cell(group["architecture"])} (Run {group["runs"]}개, '
                      f'독립 평가 보고서 {group["evaluationReports"]}개)', '',
                  'Run 상태 ' + ', '.join(f'{key} {value}' for key, value in group['runStatus'].items()) +
                  ' · Orchestrator 판정 ' + ', '.join(f'{key} {value}' for key, value in group['orchestratorVerdict'].items()),
                  '', '### ① 작업 정확성(Run 수)', '', '| 에이전트 | 항목 | 결과 |', '|---|---|---|']
        lines += [f'| {_role(row["role"])} | {row["item"]} | {_count_cell(row["counts"])} |' for row in group['accuracy']]
        lines += ['', '### ② 정보 전달(단계 수)', '', '| 에이전트 | 단계 | 전달 완전성 | 해석 정확성 | 수신 기록 대조 | 모델 입력 대조 |',
                  '|---|---|---|---|---|---|']
        lines += [f'| {_role(row["role"])} | {row["steps"]} | {_count_cell(row["delivery"])} | {_count_cell(row["interpretation"])} | '
                  f'{_count_cell(row.get("receipt") or {})} | {_count_cell(row.get("modelInput") or {})} |' for row in group['handoffs']]
        lines += ['', '### ③ 처리시간·오류(Run별 합의 중앙값)', '',
                  '| 에이전트 | Run | 처리 시간 | LLM 호출 | 토큰 | 비용 | 오류 난 Run | 그중 의미 검토 호출 |',
                  '|---|---|---|---|---|---|---|---|']
        number = lambda value: f'{value:g}'
        lines += [f'| {_role(row["role"])} | {row["runs"]} | {_median_cell(row["agentMs"], _seconds)} | '
                  f'{_median_cell(row["llmCalls"], number)} | {_median_cell(row["tokens"], number)} | '
                  f'{_median_cell(row["costUsd"], lambda value: f"${value:.4f}")} | {row["runsWithErrors"]} | '
                  f'{_median_cell(row["reviewLlmCalls"], number) if row.get("reviewLlmCalls") else UNKNOWN} |'
                  for row in group['timing']]
        lines += ['', '### 오류 발생 지점(처음 문제 난 곳)', '', '| 위치 | Run |', '|---|---|']
        lines += [f'| {row["where"]} | {row["runs"]} |' for row in group['firstProblems']]
    return '\n'.join(lines) + '\n'


def _agent_table_files(paths):
    # 파일이면 그대로, 폴더면 그 아래의 agent_tables.json을 모두(정렬해서) 찾는다.
    for path in paths:
        yield from (sorted(path.rglob('agent_tables.json')) if path.is_dir() else [path])


def main(argv=None):
    # 한 Run: python -m evaluation.agent_eval --export <evaluation_export 폴더> [--report <D5 output.json>] [--contract <정책 JSON>] --out <폴더>
    # 여러 Run: python -m evaluation.agent_eval --summarize <agent_tables.json 또는 폴더> ... --out <폴더>
    import argparse
    parser = argparse.ArgumentParser(prog='python -m evaluation.agent_eval', description='Agent별 평가표 세 개 만들기·모으기')
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument('--export', type=Path, help='dev evaluation_export가 꺼낸 폴더(Run 하나)')
    source.add_argument('--summarize', type=Path, nargs='+', help='agent_tables.json 파일 또는 그것이 든 폴더(여러 Run)')
    parser.add_argument('--report', type=Path, help='같은 Run의 D5 결과(output.json) 또는 독립 평가 보고서 JSON')
    parser.add_argument('--contract', type=Path, help='시나리오 정책 JSON(없으면 Run이 고정한 정책)')
    parser.add_argument('--planner-review', type=Path, help='같은 Run·계획 해시에 연결된 독립 사람 검토 JSON')
    parser.add_argument('--out', required=True, type=Path, help='결과 JSON·Markdown을 쓸 폴더')
    args = parser.parse_args(argv)
    if args.summarize and args.planner_review:
        parser.error('--planner-review requires --export')
    if args.summarize:
        if args.report or args.contract:
            parser.error('--report/--contract are for --export (one Run)')
        files = list(_agent_table_files(args.summarize))
        if not files:
            parser.error('agent_tables.json not found')
        summary = summarize(json.loads(path.read_text(encoding='utf-8')) for path in files)
        args.out.mkdir(parents=True, exist_ok=True)
        (args.out / 'agent_summary.json').write_text(json.dumps(summary, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
        (args.out / 'agent_summary.md').write_text(render_summary_markdown(summary), encoding='utf-8')
        print(args.out / 'agent_summary.md')
        return 0
    contract = json.loads(args.contract.read_text(encoding='utf-8')) if args.contract else None
    report = None
    if args.report:
        loaded = json.loads(args.report.read_text(encoding='utf-8'))
        report = loaded.get('report', loaded)  # D5 결과 전체를 줘도, 보고서만 줘도 된다
    review = json.loads(args.planner_review.read_text(encoding='utf-8')) if args.planner_review else None
    tables = agent_tables(load_export(args.export), contract, report, planner_review=review)
    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / 'agent_tables.json').write_text(json.dumps(tables, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    (args.out / 'agent_tables.md').write_text(render_markdown(tables), encoding='utf-8')
    print(args.out / 'agent_tables.md')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
