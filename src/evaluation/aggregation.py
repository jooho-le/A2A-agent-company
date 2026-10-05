"""검사 계획과 결과를 대조해 상태별 개수·통과율·실패/미검증 목록을 계산한다.

빠진 결과, 증거 없는 판정, 승인 안 된 적용 제외는 합격으로 치지 않는다.
테스트 실행·증거 확인·최종 판정(finalVerdict)은 이 모듈의 일이 아니다.
"""
from dataclasses import dataclass
from enum import Enum
from typing import Iterable, Optional
from uuid import UUID


class Status(str, Enum):
    """검사 하나의 상태. ERROR·NOT_RUN은 집계에서 미검증(UNVERIFIED)으로 다룬다."""
    PASS = "PASS"
    FAIL = "FAIL"
    ERROR = "ERROR"
    NOT_RUN = "NOT_RUN"
    NOT_APPLICABLE = "NOT_APPLICABLE"


@dataclass(frozen=True)
class CheckPlan:
    """실행 전에 정하는 검사 항목. 결과 쪽에서 필수 여부나 제외 여부를 바꾸지 못하게 계획에서만 정한다.

    적용 제외는 승인 참조(exclusion_approval_ref)와 사유(exclusion_reason)가 둘 다 있어야 인정된다.
    """
    test_id: str
    required: bool = True
    exclusion_approval_ref: Optional[str] = None
    exclusion_reason: Optional[str] = None


@dataclass(frozen=True)
class CheckResult:
    """검사 하나의 결과. PASS·FAIL에는 증거 ID(evidence_ids)가 있어야 한다.

    expected_result ~ error_code는 실패 원인을 남기는 선택 정보다(값을 주면 빈 문자열 불가).
    """
    test_id: str
    status: Status
    evidence_ids: tuple[str, ...] = ()
    expected_result: Optional[str] = None
    actual_result: Optional[str] = None
    reason: Optional[str] = None
    normalized_location: Optional[str] = None
    error_code: Optional[str] = None


def _uuid4(value: str) -> None:
    """정규 형식(소문자) UUIDv4 문자열이 아니면 ValueError. "REQ-001" 같은 표시 이름을 걸러낸다."""
    if not isinstance(value, str):
        raise ValueError("Identifiers must be UUIDv4 strings")
    try:
        parsed = UUID(value)
    except ValueError as exc:
        raise ValueError("Identifiers must be UUIDv4 strings") from exc
    if parsed.version != 4 or str(parsed) != value:
        raise ValueError("Identifiers must be canonical UUIDv4 strings")


def aggregate(plan: Iterable[CheckPlan], results: Iterable[CheckResult], *,
              sensitive_values: Iterable[str] = ()) -> dict:
    """계획 기준으로 결과를 집계한다.

    - 결과 없음 → NOT_RUN(RESULT_MISSING), 증거 없는 PASS·FAIL → ERROR(EVIDENCE_MISSING),
      승인 없는 NOT_APPLICABLE → ERROR(EXCLUSION_NOT_APPROVED)
    - validationVerdict는 필수 검사만 본다: 미검증이 있거나 적용 대상이 없으면 UNVERIFIED,
      아니면 FAIL이 있으면 FAIL, 그 밖에는 PASS. UNVERIFIED여도 확인된 FAIL ID는 남긴다.
    - 실패·미검증 ID 목록도 필수와 선택(optional...)으로 나눈다.
    - sensitive_values의 문자열은 진단 필드에서 "[REDACTED]"로 바꾼다.
    - 입력이 잘못되면 ValueError로 멈춘다.
    """
    secrets = tuple(sensitive_values)
    if any(not isinstance(value, str) or not value for value in secrets):
        raise ValueError("Sensitive values must be nonempty strings")

    def safe_text(value):
        # 긴 값부터 바꿔야 짧은 값이 긴 값의 일부만 가리는 일이 없다.
        if value is None:
            return None
        for secret in sorted(set(secrets), key=len, reverse=True):
            value = value.replace(secret, "[REDACTED]")
        return value

    # 1) 계획 검사: 빈 계획, 중복 ID, 잘못된 required·제외 정보를 거부한다.
    checks = tuple(plan)
    if not checks:
        raise ValueError("The fixed test plan must not be empty")
    planned = {}
    for check in checks:
        _uuid4(check.test_id)
        if type(check.required) is not bool:
            raise ValueError("required must be boolean")
        if check.test_id in planned:
            raise ValueError("Duplicate test ID in plan")
        for field in (check.exclusion_approval_ref, check.exclusion_reason):
            if field is not None and (not isinstance(field, str) or not field.strip()):
                raise ValueError("Exclusion approval and reason must be nonempty")
        if bool(check.exclusion_approval_ref) != bool(check.exclusion_reason):
            raise ValueError("Exclusion needs both approval reference and reason")
        planned[check.test_id] = check

    # 2) 결과 검사: 계획에 없는 검사, 중복 결과, 잘못된 형식을 거부한다.
    received = {}
    for result in results:
        _uuid4(result.test_id)
        if result.test_id not in planned:
            raise ValueError("Result test ID is absent from the fixed plan")
        if result.test_id in received:
            raise ValueError("Duplicate result for a test ID")
        if not isinstance(result.status, Status):
            raise ValueError("Result status must be a Status enum")
        if not isinstance(result.evidence_ids, tuple):
            raise ValueError("evidence_ids must be a tuple")
        for evidence_id in result.evidence_ids:
            _uuid4(evidence_id)
        if len(set(result.evidence_ids)) != len(result.evidence_ids):
            raise ValueError("Duplicate evidence ID")
        for name in ("expected_result", "actual_result", "reason", "normalized_location", "error_code"):
            value = getattr(result, name)
            if value is not None and (not isinstance(value, str) or not value.strip()):
                raise ValueError(f"{name} must be nonempty text when supplied")
        received[result.test_id] = result

    # 3) 검사별 행: 결과가 아닌 계획을 순회해야 결과가 오지 않은 검사도 남는다.
    rows = []
    for check in checks:
        result = received.get(check.test_id)
        status = result.status if result else Status.NOT_RUN
        evidence = result.evidence_ids if result else ()
        error = "RESULT_MISSING" if result is None else result.error_code
        if status in (Status.PASS, Status.FAIL) and not evidence:
            status, error = Status.ERROR, "EVIDENCE_MISSING"
        if status == Status.NOT_APPLICABLE and not check.exclusion_approval_ref:
            status, error = Status.ERROR, "EXCLUSION_NOT_APPROVED"
        rows.append({
            "testId": check.test_id, "required": check.required,
            "status": status.value, "evidenceIds": list(evidence),
            "errorCode": safe_text(error),
            "expectedResult": safe_text(result.expected_result) if result else None,
            "actualResult": safe_text(result.actual_result) if result else None,
            "reason": safe_text(result.reason) if result else None,
            "normalizedLocation": safe_text(result.normalized_location) if result else None,
            "exclusionApprovalRef": (check.exclusion_approval_ref
                                     if status == Status.NOT_APPLICABLE else None),
            "exclusionReason": (check.exclusion_reason
                                if status == Status.NOT_APPLICABLE else None),
        })

    # 4) 판정은 필수 검사로만. 승인된 제외는 통과율 분모에서 뺀다.
    required = [r for r in rows if r["required"]]
    optional = [r for r in rows if not r["required"]]
    applicable = [r for r in required if r["status"] != "NOT_APPLICABLE"]
    incomplete = [r for r in required if r["status"] in ("ERROR", "NOT_RUN")]
    if not applicable or incomplete:
        verdict = "UNVERIFIED"
    elif any(r["status"] == "FAIL" for r in required):
        verdict = "FAIL"
    else:
        verdict = "PASS"

    def counts(items):
        names = {"PASS": "pass", "FAIL": "fail", "ERROR": "error",
                 "NOT_RUN": "notRun", "NOT_APPLICABLE": "notApplicable"}
        return {name: sum(r["status"] == status for r in items)
                for status, name in names.items()}

    def failed(items):
        return [r["testId"] for r in items if r["status"] == "FAIL"]

    def unverified(items):
        return [r["testId"] for r in items if r["status"] in ("ERROR", "NOT_RUN")]

    # 로컬 요약이며 finalVerdict가 아니다. 적용 대상이 없으면 통과율은 None.
    return {
        "validationVerdict": verdict,
        "summary": {"required": counts(required), "optional": counts(optional)},
        "confirmedFailureIds": failed(required),
        "unverifiedTestIds": unverified(required),
        "optionalConfirmedFailureIds": failed(optional),
        "optionalUnverifiedTestIds": unverified(optional),
        "requiredPassRate": (sum(r["status"] == "PASS" for r in applicable)
                             / len(applicable) if applicable else None),
        "results": rows,
    }
