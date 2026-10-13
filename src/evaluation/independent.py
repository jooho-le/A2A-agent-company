# "독립 평가 보고서"를 만드는 파일이다. 이 프로젝트에서 가장 중요한 결과물이다.
#
# 독립 평가란?
#   에이전트들이 코드를 만들고 고치는 과정(개발 순환)이 끝난 뒤, 그 과정 "밖"에서
#   최종 코드를 따로 시험(보호 테스트)해 점수를 매기는 것이다. 에이전트는 이 시험 문제와 결과를 볼 수 없다.
#
# reporting.py의 "에이전트 호환 보고서"와 다른 점:
#   - 실제 A2A 작업 번호가 필요 없다.
#   - 요구사항 하나에 검사를 여러 개 연결할 수 있다.
#   - 요구사항별 요약과 관점별(QA / SECURITY) 요약을 함께 만든다.
#
# 이 파일은 시험을 직접 실행하지 않고, 시험 묶음의 해시(지문)도 계산하지 않는다.
# "이미 나온 결과를 받아서 검증하고 보고서로 정리"하는 일만 한다.
#
# [용어]
# - suite(시험 묶음): 함께 쓰는 시험 파일들의 모음. 이름이 같아도 내용이 바뀌었을 수 있어 해시(지문)로 같은지 확인한다.
# - 요구사항(requirement): 만들어진 프로그램이 지켜야 할 약속 한 줄. 예) "비밀번호는 8자 이상이어야 한다".
# - 관점(perspective): QA(기능이 맞게 동작하는가)와 SECURITY(보안이 지켜지는가) 두 가지로 나눠 본다.
from dataclasses import dataclass  # 값을 담는 "데이터 상자" 클래스를 쉽게 만드는 도구
import re  # 글자 모양을 검사하는 정규식 도구
from typing import Iterable

from .aggregation import CheckPlan, CheckResult, _uuid4  # 검사 계획·결과 상자, UUID 검사 도구
from .common import ARCHITECTURES, aware_time  # 구조 이름 목록, 시각 읽기 도구
from .manifest import ExecutionManifest, aggregate_report  # 코드·환경 신분증, 신분증 확인 후 집계
from .reporting import validate_contract  # 보고서가 약속된 JSON 모양인지 확인하는 도구


@dataclass(frozen=True)
class EvaluationBinding:
    # 검사 하나를 "어느 요구사항의 것인지", "어느 관점인지", "무슨 검사인지(제목)"와 이어 주는 꼬리표.
    # 예) 검사 번호 abc... → 요구사항 REQ-005, 관점 SECURITY, 제목 "비밀번호 해시 저장"

    requirement_id: str  # 요구사항 고유 번호(UUIDv4)
    perspective: str     # "QA" 또는 "SECURITY"
    title: str           # 사람이 읽을 검사 제목


# 1번 담당(Orchestrator)이 쓰는 "실행 상태"와 "최종 판정" 값 목록. 팀 코드(dev)의 states.py와 똑같이 맞춰 둔 것이다.
WORKFLOW_STATUSES = ('RECEIVED', 'PLANNING', 'WAITING_INPUT', 'IMPLEMENTING', 'SNAPSHOT_READY',
                     'VALIDATING', 'FIX_REQUIRED', 'FIXING', 'REVALIDATING', 'HUMAN_REVIEW',
                     'FINISHED', 'ABORTED')
FINAL_VERDICTS = ('SUCCESS', 'FAIL', 'UNVERIFIED', 'HUMAN_REVIEW')
# 이 상태가 되면 실행이 "완전히 끝난 것"으로 보고, 마지막 갱신 시각(updatedAt)을 끝난 시각으로 쓴다.
# HUMAN_REVIEW(사람 검토 중)는 사람이 확인한 뒤 다시 이어서 실행할 수 있는 "잠시 멈춘 상태"이므로
# 끝난 것으로 보지 않는다. 그래서 여기에 넣지 않았다.
TERMINAL_STATUSES = ('FINISHED', 'ABORTED')


@dataclass(frozen=True)
class SourceRunSummary:
    # 채점 대상이 된 "개발 실행(Run)"에 대해 1번 담당 시스템이 남긴 기록의 요약이다.
    # 채점 결과 옆에 같이 적어 두면 "시스템은 성공이라 했는데 우리 시험은 실패" 같은 차이를 한눈에 볼 수 있고,
    # 수정을 몇 번 했는지, 얼마나 걸렸는지도 보고서 한 장에서 확인할 수 있다.
    run_id: str                  # 개발 실행의 고유 번호
    workflow_status: str         # 실행 상태(WORKFLOW_STATUSES 중 하나)
    final_verdict: str | None    # 시스템이 내린 최종 판정. 아직 없으면 None
    code_version: int | None     # 최종 코드 버전(1~4). 아직 없으면 None
    fix_attempt: int             # 코드를 고친 횟수(0~3)
    started_at: str              # 실행을 시작한 시각
    finished_at: str | None      # 실행이 끝난 시각. 끝나지 않았으면 None

    @classmethod  # 객체를 만들기 전에 SourceRunSummary.from_run_status(...)처럼 부르는 "만들기 도우미" 함수
    def from_run_status(cls, payload: dict) -> "SourceRunSummary":
        # 시스템 API(GET /api/v1/runs/{runId})가 돌려준 JSON(payload)에서 필요한 값만 꺼내 요약 상자를 만든다.
        # "끝난 상태"일 때만 updatedAt을 끝난 시각으로 쓰고, 진행 중이면 None으로 둔다.
        if not isinstance(payload, dict):
            raise ValueError('Run status must be a JSON object')
        status = payload.get('status')
        # payload.get('키')는 키가 없어도 오류 없이 None을 돌려준다(없는 값은 아래 __post_init__에서 걸러진다).
        return cls(run_id=payload.get('runId'), workflow_status=status,
                   final_verdict=payload.get('verdict'), code_version=payload.get('codeVersion'),
                   fix_attempt=payload.get('fixAttempt'), started_at=payload.get('createdAt'),
                   finished_at=payload.get('updatedAt') if status in TERMINAL_STATUSES else None)

    def __post_init__(self):
        # 상자가 만들어진 직후 파이썬이 자동으로 부르는 함수. 값의 모양과 상태 규칙이 맞는지 검사한다.
        _uuid4(self.run_id)
        if self.workflow_status not in WORKFLOW_STATUSES:
            raise ValueError('Unknown workflow status')
        if self.final_verdict is not None and self.final_verdict not in FINAL_VERDICTS:
            raise ValueError('Unknown final verdict')
        # 팀 코드(dev)의 규칙: 상태에 따라 가질 수 있는 판정이 정해져 있다.
        #  - FINISHED(끝남): 판정이 반드시 있어야 한다.
        #  - HUMAN_REVIEW(사람 검토): 판정이 없거나 HUMAN_REVIEW 하나뿐이다.
        #  - 그 밖(진행 중, 중단됨): 판정이 없어야 한다.
        if self.workflow_status == 'FINISHED':
            allowed = FINAL_VERDICTS
        elif self.workflow_status == 'HUMAN_REVIEW':
            allowed = (None, 'HUMAN_REVIEW')
        else:
            allowed = (None,)
        if self.final_verdict not in allowed:
            raise ValueError('Final verdict does not match the workflow status')
        if self.code_version is not None and (
                type(self.code_version) is not int or not 1 <= self.code_version <= 4):
            raise ValueError('code_version must be an integer from 1 to 4')
        if type(self.fix_attempt) is not int or not 0 <= self.fix_attempt <= 3:
            raise ValueError('fix_attempt must be an integer from 0 to 3')
        start = aware_time(self.started_at, 'started_at')
        if self.finished_at is not None and aware_time(self.finished_at, 'finished_at') < start:
            # 끝난 시각이 시작 시각보다 빠르면 자료가 잘못된 것이다.
            raise ValueError('finished_at is earlier than started_at')

    def to_payload(self, has_confirmed_failure: bool) -> dict:
        # 보고서의 "sourceRun" 항목(JSON으로 쓸 사전)을 만든다.
        # durationMs = 걸린 시간(밀리초). 시작~끝 시각의 차이이고, 아직 끝나지 않았으면 None이다.
        # missedDefect = "놓친 결함"이 있었는가.
        #   시스템은 SUCCESS(성공)로 끝냈는데 우리 시험에서 필수 검사 FAIL이 하나라도 확인된 경우 True다.
        #   다른 검사가 미검증이라 전체 판정이 UNVERIFIED여도, 이미 확인된 FAIL은 놓친 결함으로 센다.
        duration = None
        if self.finished_at is not None:
            delta = (aware_time(self.finished_at, 'finished_at')
                     - aware_time(self.started_at, 'started_at'))  # 시각끼리 빼면 시간 간격이 나온다.
            duration = int(delta.total_seconds() * 1000)  # 초 → 밀리초(1초 = 1000밀리초)
        return {
            'workflowStatus': self.workflow_status,
            'finalVerdict': self.final_verdict,
            'codeVersion': self.code_version,
            'fixAttempt': self.fix_attempt,
            'startedAt': self.started_at,
            'finishedAt': self.finished_at,
            'durationMs': duration,
            'missedDefect': self.final_verdict == 'SUCCESS' and has_confirmed_failure,
        }


def build_evaluation_report(
    *,
    evaluation_id: str,
    source_run_id: str,
    architecture: str,
    suite_id: str,
    suite_version: str,
    suite_sha256: str,
    created_at: str,
    required_requirement_ids: Iterable[str],
    expected: ExecutionManifest,
    observed: ExecutionManifest | None,
    plan: Iterable[CheckPlan],
    results: Iterable[CheckResult],
    bindings: dict[str, EvaluationBinding],
    sensitive_values: Iterable[str] = (),
    source_run: SourceRunSummary | None = None,
) -> dict:
    # 독립 평가 보고서(사전 dict)를 만들어 돌려준다. 입력이 하나라도 이상하면 ValueError로 멈춘다.
    # (매개변수 앞의 `*,`는 "모든 값을 이름을 붙여서 넘겨라"라는 뜻이다. 순서를 헷갈리지 않게 하려는 장치다.)
    #
    # 입력값 설명:
    # - evaluation_id : 이번 "채점" 작업의 번호. source_run_id(채점 대상인 "개발 실행"의 번호)와 달라야 한다.
    # - architecture  : SINGLE_AGENT 또는 MULTI_AGENT. 어느 구조가 만든 코드를 채점하는지.
    # - suite_*       : 시험 묶음의 이름·버전·해시(지문). 해시는 부르는 쪽에서 계산해 넘긴다.
    #                   이 함수는 해시가 올바른 "모양"인지만 보고, 실제 파일과 맞는지는 확인하지 않는다.
    # - created_at    : 보고서를 만든 시각. 시간대를 포함해야 한다. 예) 2026-10-14T12:00:00+09:00
    # - expected      : 믿을 수 있는 설정에서 받은 "채점하려던 코드·환경"의 신분증(Manifest).
    # - observed      : 결과가 실제로 나온 코드·환경의 신분증. 없거나 expected와 다르면 결과를 쓰지 않는다(미검증).
    # - plan, results : 검사 계획과 받은 결과.
    # - bindings      : {검사 번호: EvaluationBinding}. 각 검사가 어느 요구사항·관점·제목인지 알려 주는 표.
    #                   계획에 있는 모든 검사를 빠짐없이 덮어야 하고, 필수 요구사항마다 필수 검사가 하나 이상 있어야 한다.
    # - sensitive_values : 설명 글에서 가려야 할 글자(시험용 비밀번호 등).
    # - source_run    : (선택) 개발 실행의 기록. 주면 runId와 코드 버전이 맞는지 확인하고 보고서에 함께 적는다.
    #
    # evaluationVerdict(독립 평가 판정)는 시스템의 finalVerdict(최종 판정)와 별개의 값이다. 서로 덮어쓰지 않는다.
    # 통과율은 "검사 기준"과 "요구사항 기준" 두 가지를 따로 계산한다. 검사가 없는 관점은 None이다.

    # 1) 식별 정보 확인: 번호·이름·시각이 올바른 모양인지 먼저 본다.
    _uuid4(evaluation_id)
    _uuid4(source_run_id)
    if evaluation_id == source_run_id:
        # 채점 작업과 개발 실행이 같은 번호면 서로 다른 일이라는 구분이 무너진다.
        raise ValueError('Independent evaluation must have its own execution ID')
    if architecture not in ARCHITECTURES:
        raise ValueError('Unsupported architecture')
    for name, value in (('suite_id', suite_id), ('suite_version', suite_version)):
        # 이름·버전은 비어 있지 않아야 하고 앞뒤 공백이 없어야 한다.
        if not isinstance(value, str) or not value.strip() or value != value.strip():
            raise ValueError(f'{name} must be nonempty trimmed text')
    if not isinstance(suite_sha256, str) or not re.fullmatch(r'[0-9a-f]{64}', suite_sha256):
        raise ValueError('suite_sha256 must be a SHA-256 hex digest')
    aware_time(created_at, 'created_at')  # 시간대가 있는 올바른 시각인지만 확인한다(결과값은 쓰지 않음).
    if source_run is not None:
        if not isinstance(source_run, SourceRunSummary):
            raise ValueError('Expected a SourceRunSummary')
        if source_run.run_id != source_run_id:
            raise ValueError('Source run summary belongs to another run')
        # 개발 실행의 "최종 코드"가 아닌 다른 버전을 채점했다면 시스템 판정과 비교할 수 없다.
        if source_run.code_version is not None and source_run.code_version != expected.code_version:
            raise ValueError('Evaluated code version differs from the run\'s final code version')

    # 2) 시험 범위 확인: 요구사항·검사·꼬리표가 서로 빈틈없이 맞물리는지 본다.
    # 받은 목록들을 tuple로 한 번에 고정해 둔다. (한 번 꺼내면 사라지는 목록이 와도 여러 번 쓸 수 있게)
    checks, received = tuple(plan), tuple(results)
    secrets = tuple(sensitive_values)
    required_ids = tuple(required_requirement_ids)
    if not required_ids or len(set(required_ids)) != len(required_ids):
        # 요구사항 목록이 비었거나 같은 번호가 두 번 들어 있으면 안 된다.
        raise ValueError('Required requirement IDs must be nonempty and unique')
    for requirement_id in required_ids:
        _uuid4(requirement_id)
    if set(bindings) != {check.test_id for check in checks}:
        # 꼬리표(bindings)가 계획의 모든 검사를 정확히 덮어야 한다. 빠지거나 남는 검사가 있으면 안 된다.
        raise ValueError('Bindings must match the exact protected test plan')
    for binding in bindings.values():
        if not isinstance(binding, EvaluationBinding):
            raise ValueError('Expected an EvaluationBinding')
        _uuid4(binding.requirement_id)
        if binding.perspective not in ('QA', 'SECURITY'):
            raise ValueError('Unsupported evaluation perspective')
        if not isinstance(binding.title, str) or not binding.title.strip():
            raise ValueError('Check title must not be blank')
    if {b.requirement_id for b in bindings.values()} != set(required_ids):
        # 검사들이 연결한 요구사항 집합이, 이번에 평가하기로 한 요구사항 집합과 정확히 같아야 한다.
        raise ValueError('Bindings must cover the exact requirement scope')
    # 선택(참고용) 검사만 붙여 놓고 필수 요구사항이 통과한 것처럼 보이게 하는 편법을 막는다.
    required_coverage = {bindings[c.test_id].requirement_id for c in checks if c.required}
    if required_coverage != set(required_ids):
        raise ValueError('Each required requirement needs a required check')

    # 3) 전체 집계: Manifest(신분증)가 맞을 때만 결과를 집계하고, 검사별 줄에 요구사항·관점·제목을 덧붙인다.
    summary = aggregate_report(checks, received, expected=expected, observed=observed,
                               sensitive_values=secrets)
    for row in summary['results']:
        binding = bindings[row['testId']]
        row.update(requirementId=binding.requirement_id, perspective=binding.perspective,
                   title=binding.title)

    def summarize_subset(selected):
        # 검사의 일부(한 요구사항 또는 한 관점)만 따로 모아 요약한다.
        # 전체 집계와 똑같은 신분증 확인·집계 규칙을 쓰므로 부분 요약이 전체와 모순되지 않는다.
        ids = {check.test_id for check in selected}
        group = aggregate_report(selected, (r for r in received if r.test_id in ids),
                                 expected=expected, observed=observed,
                                 sensitive_values=secrets)
        # 집계 결과 중 요약에 필요한 항목만 골라서 돌려준다.
        return {key: group[key] for key in (
            'validationVerdict', 'summary', 'confirmedFailureIds', 'unverifiedTestIds',
            'optionalConfirmedFailureIds', 'optionalUnverifiedTestIds', 'requiredPassRate')}

    # 4) 요구사항별·관점별 요약
    requirements = []
    for requirement_id in required_ids:
        # 이 요구사항에 연결된 검사만 골라 요약한다. (한 요구사항에 검사가 여러 개일 수 있다.)
        selected = [c for c in checks if bindings[c.test_id].requirement_id == requirement_id]
        requirements.append(dict(requirementId=requirement_id,
                                 checkIds=[c.test_id for c in selected],
                                 **summarize_subset(selected)))  # ** 는 사전의 내용을 풀어서 이름 붙은 값으로 넘긴다.
    perspectives = {}
    for perspective in ('QA', 'SECURITY'):
        selected = [c for c in checks if bindings[c.test_id].perspective == perspective]
        # 그 관점의 검사가 하나도 없으면 "통과"로 꾸미지 않고 None으로 둔다.
        perspectives[perspective] = summarize_subset(selected) if selected else None
    passed = sum(row['validationVerdict'] == 'PASS' for row in requirements)  # PASS인 요구사항의 수

    report = {
        'schemaVersion': '1.0',                 # 보고서 형식의 버전
        'reportType': 'INDEPENDENT_EVALUATION', # 이 보고서의 종류 이름
        'evaluationId': evaluation_id,
        'sourceRunId': source_run_id,
        'sourceRun': (source_run.to_payload(bool(summary['confirmedFailureIds']))
                      if source_run is not None else None),
        'architecture': architecture,
        'createdAt': created_at,
        'suite': {'id': suite_id, 'version': suite_version, 'sha256': suite_sha256},
        'expectedManifest': expected.to_payload(),
        'observedManifest': observed.to_payload() if observed is not None else None,
        'evaluationVerdict': summary['validationVerdict'],
        'manifestCheck': summary['manifestCheck'],
        'summary': summary['summary'],
        'confirmedFailureIds': summary['confirmedFailureIds'],
        'unverifiedTestIds': summary['unverifiedTestIds'],
        'optionalConfirmedFailureIds': summary['optionalConfirmedFailureIds'],
        'optionalUnverifiedTestIds': summary['optionalUnverifiedTestIds'],
        # 검사 통과율 = 통과한 필수 검사 수 ÷ 적용된 필수 검사 수(승인받아 제외한 검사는 분모에서 뺀다).
        'requiredCheckPassRate': summary['requiredPassRate'],
        # 요구사항 통과율 = PASS인 요구사항 수 ÷ 전체 필수 요구사항 수.
        # 신분증(Manifest)이 없거나 달라서 아무것도 측정하지 못했다면 0%(전부 실패)가 아니라 None(측정 못 함)이다.
        'requiredRequirementPassRate': (None if summary['manifestCheck']['errorCode']
                                        else passed / len(requirements)),
        'requirements': requirements,
        'perspectives': perspectives,
        'checks': summary['results'],
    }
    # 마지막으로, 보고서가 약속된 JSON 모양(스키마)과 맞는지 한 번 더 확인하고 돌려준다.
    # (위에서 검사한 "규칙"과 별개로, 필드 이름·자료형·허용 값을 기계적으로 확인하는 단계다.)
    validate_contract(report, 'independent_evaluation')
    return report
