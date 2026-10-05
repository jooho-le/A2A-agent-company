"""개발 순환이 끝난 결과물을 보호 테스트로 채점한 독립 평가 보고서(INDEPENDENT_EVALUATION)를 만든다.

에이전트 호환 보고서(reporting.py)와 달리 A2A ID가 필요 없고, 한 요구사항에 검사를 여러 개 연결할 수 있으며,
요구사항별·관점별(QA/SECURITY) 요약을 함께 만든다. 테스트 실행과 suite 해시 계산은 하지 않는다.
"""
from dataclasses import dataclass
from datetime import datetime
import re
from typing import Iterable

from .aggregation import CheckPlan, CheckResult, _uuid4
from .manifest import ExecutionManifest, aggregate_report
from .reporting import validate_contract


@dataclass(frozen=True)
class EvaluationBinding:
    """검사 하나를 요구사항 UUID, 관점("QA" 또는 "SECURITY"), 제목에 연결한다."""

    requirement_id: str
    perspective: str
    title: str


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
) -> dict:
    """독립 평가 보고서 dict를 만든다.

    - evaluation_id(채점 실행)와 source_run_id(대상 개발 실행)는 서로 다른 UUIDv4.
    - suite_sha256: 보호 테스트 파일 묶음 해시. 이름이 같아도 같은 시험지인지 해시로 증명한다.
    - created_at: 시간대 포함 ISO 시각(예: 2026-10-06T12:00:00+09:00).
    - bindings는 계획 전체를 덮고, 요구사항 집합이 required_requirement_ids와 같아야 하며,
      필수 요구사항마다 필수 검사가 하나 이상 있어야 한다.
    - evaluationVerdict는 Orchestrator의 finalVerdict와 별개다.
      검사 통과율과 요구사항 통과율을 따로 계산하며, 검사가 없는 관점은 None이다.
    """
    # 1) 식별 정보
    _uuid4(evaluation_id)
    _uuid4(source_run_id)
    if evaluation_id == source_run_id:
        raise ValueError('Independent evaluation must have its own execution ID')
    if architecture not in ('SINGLE_AGENT', 'MULTI_AGENT'):
        raise ValueError('Unsupported architecture')
    for name, value in (('suite_id', suite_id), ('suite_version', suite_version)):
        if not isinstance(value, str) or not value.strip() or value != value.strip():
            raise ValueError(f'{name} must be nonempty trimmed text')
    if not isinstance(suite_sha256, str) or not re.fullmatch(r'[0-9a-f]{64}', suite_sha256):
        raise ValueError('suite_sha256 must be a SHA-256 hex digest')
    if not isinstance(created_at, str):
        raise ValueError('created_at must be an ISO timestamp')
    # Python 3.10의 fromisoformat은 "Z"를 못 읽어 "+00:00"으로 바꿔 해석한다.
    timestamp = datetime.fromisoformat(created_at.replace('Z', '+00:00'))
    if timestamp.utcoffset() is None:
        raise ValueError('created_at must include a timezone')

    # 2) 시험 범위
    checks, received = tuple(plan), tuple(results)
    secrets = tuple(sensitive_values)
    required_ids = tuple(required_requirement_ids)
    if not required_ids or len(set(required_ids)) != len(required_ids):
        raise ValueError('Required requirement IDs must be nonempty and unique')
    for requirement_id in required_ids:
        _uuid4(requirement_id)
    if set(bindings) != {check.test_id for check in checks}:
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
        raise ValueError('Bindings must cover the exact requirement scope')
    # 선택 검사만 붙여 필수 요구사항을 통과로 보이게 하는 것을 막는다.
    required_coverage = {bindings[c.test_id].requirement_id for c in checks if c.required}
    if required_coverage != set(required_ids):
        raise ValueError('Each required requirement needs a required check')

    # 3) 전체 집계 + 검사별 행에 요구사항·관점·제목 추가
    summary = aggregate_report(checks, received, expected=expected, observed=observed,
                               sensitive_values=secrets)
    for row in summary['results']:
        binding = bindings[row['testId']]
        row.update(requirementId=binding.requirement_id, perspective=binding.perspective,
                   title=binding.title)

    def summarize_subset(selected):
        # 부분 요약도 전체와 같은 Manifest·집계 규칙을 적용한다.
        ids = {check.test_id for check in selected}
        group = aggregate_report(selected, (r for r in received if r.test_id in ids),
                                 expected=expected, observed=observed,
                                 sensitive_values=secrets)
        return {key: group[key] for key in (
            'validationVerdict', 'summary', 'confirmedFailureIds', 'unverifiedTestIds',
            'optionalConfirmedFailureIds', 'optionalUnverifiedTestIds', 'requiredPassRate')}

    # 4) 요구사항별·관점별 요약
    requirements = []
    for requirement_id in required_ids:
        selected = [c for c in checks if bindings[c.test_id].requirement_id == requirement_id]
        requirements.append(dict(requirementId=requirement_id,
                                 checkIds=[c.test_id for c in selected],
                                 **summarize_subset(selected)))
    perspectives = {}
    for perspective in ('QA', 'SECURITY'):
        selected = [c for c in checks if bindings[c.test_id].perspective == perspective]
        perspectives[perspective] = summarize_subset(selected) if selected else None
    passed = sum(row['validationVerdict'] == 'PASS' for row in requirements)

    report = {
        'schemaVersion': '1.0',
        'reportType': 'INDEPENDENT_EVALUATION',
        'evaluationId': evaluation_id,
        'sourceRunId': source_run_id,
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
        'requiredCheckPassRate': summary['requiredPassRate'],
        # Manifest가 맞지 않아 아무것도 측정하지 못했으면 0%가 아니라 None이다.
        'requiredRequirementPassRate': (None if summary['manifestCheck']['errorCode']
                                        else passed / len(requirements)),
        'requirements': requirements,
        'perspectives': perspectives,
        'checks': summary['results'],
    }
    # 필드·자료형·허용값을 스키마로 확인한다. 검사 규칙은 위에서 이미 확인했다.
    validate_contract(report, 'independent_evaluation')
    return report
