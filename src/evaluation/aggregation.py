# 미리 정한 "검사 계획"과 실제로 받은 "검사 결과"를 맞춰 보고, 통과·실패가 몇 개인지 세어 요약하는 파일이다.
#
# 아래 세 경우는 "합격"으로 인정하지 않는다.
#   1) 결과가 아예 없는 검사
#   2) 증거 없이 "합격/불합격"이라고만 적힌 검사
#   3) 승인 없이 "이번에는 검사하지 않아도 된다"고 주장한 검사
# 이 파일은 시험을 직접 돌리거나, 증거 파일을 열어 보거나, 시스템의 최종 판정(finalVerdict)을 내리지 않는다.
# 이미 나온 결과를 받아서 "세고 요약하는" 일만 한다.
#
# [처음 읽는 분을 위한 용어]
# - 검사(test/check): 요구사항이 지켜지는지 확인하는 작은 시험 하나.
# - 계획(plan): 시험을 보기 "전에" 정해 두는 검사 목록. 무엇을 반드시 봐야 하는지가 여기서 정해진다.
# - 결과(result): 시험을 본 "뒤에" 받은 점수표. 계획에는 있는데 결과가 없으면 "안 본 것"이다.
# - 증거(evidence): 결과를 믿을 수 있게 해 주는 기록의 번호표. 증거 없는 합격/불합격은 믿지 않는다.
# - 필수(required) / 선택(optional): 필수 검사만 전체 합격·불합격 판정에 쓰이고, 선택 검사는 참고용이다.
from dataclasses import dataclass  # 값을 담는 "데이터 상자" 클래스를 짧게 만들어 주는 도구
from enum import Enum  # 정해진 몇 개의 값 중 하나만 갖게 하는 "열거형"을 만드는 도구
from typing import Iterable, Optional  # 자료형을 설명하는 이름표(실행에는 영향 없음)
from uuid import UUID  # 겹치지 않는 고유 번호(UUID)를 읽고 확인하는 도구


# 검사 하나가 가질 수 있는 상태 다섯 가지. 이 다섯 값 외에는 쓸 수 없다.
# (str도 같이 상속했기 때문에 "PASS" 같은 글자로 바로 비교하거나 JSON에 넣을 수 있다.)
class Status(str, Enum):
    # ERROR와 NOT_RUN은 둘 다 "제대로 확인하지 못함"이라는 뜻이어서,
    # 요약할 때는 하나로 묶어 미검증(UNVERIFIED)으로 센다.
    PASS = "PASS"                      # 시험을 봤고 요구를 만족함
    FAIL = "FAIL"                      # 시험을 봤고 요구를 어김
    ERROR = "ERROR"                    # 도구나 환경 문제로 시험을 제대로 못 봄
    NOT_RUN = "NOT_RUN"                # 시험을 아예 안 봄
    NOT_APPLICABLE = "NOT_APPLICABLE"  # 승인을 받아 이번에는 적용하지 않기로 한 검사


# @dataclass(frozen=True): 값을 담는 상자를 만들되, 만든 뒤에는 값을 바꿀 수 없게(frozen) 한다.
# 계획이 실행 도중에 몰래 바뀌는 일을 막으려는 것이다.
@dataclass(frozen=True)
class CheckPlan:
    # 시험을 보기 전에 정해 두는 검사 항목 하나.
    # 필수인지, 제외할 것인지는 "계획"에서만 정할 수 있고, 결과를 보내는 쪽이 바꿀 수 없다.
    #
    # 검사를 제외하려면 "승인한 근거"(exclusion_approval_ref)와 "이유"(exclusion_reason)가 둘 다 있어야 한다.
    test_id: str                                  # 검사의 고유 번호(UUIDv4 문자열)
    required: bool = True                         # True면 필수 검사(판정에 반영), False면 선택 검사
    exclusion_approval_ref: Optional[str] = None  # 제외를 승인한 근거(없으면 None)
    exclusion_reason: Optional[str] = None        # 제외하는 이유(없으면 None)


@dataclass(frozen=True)
class CheckResult:
    # 검사 하나의 결과. PASS나 FAIL을 적으려면 증거 번호(evidence_ids)가 반드시 있어야 한다.
    #
    # 아래 "설명 글" 필드들은 적어도 되고 안 적어도 된다. 다만 적었다면 빈 글자("")는 안 된다.
    #   expected_result     : 이렇게 동작해야 한다고 기대한 내용
    #   actual_result       : 실제로 관찰한 내용
    #   reason              : 이런 판정이 나온 이유
    #   normalized_location : 문제가 있는 위치(예: 파일 이름과 줄 번호)
    #   error_code          : 원인을 구분하는 짧은 코드
    # 증거 번호가 올바른 UUID 모양인지, 같은 번호가 두 번 들어 있지 않은지는 확인한다.
    # 하지만 그 번호가 가리키는 실제 증거 파일의 내용까지 열어 보지는 않는다.
    test_id: str                                   # 어느 검사의 결과인지(계획의 test_id와 같아야 함)
    status: Status                                 # 위 다섯 상태 중 하나
    evidence_ids: tuple[str, ...] = ()             # 증거 번호 목록(비어 있을 수 있음)
    expected_result: Optional[str] = None
    actual_result: Optional[str] = None
    reason: Optional[str] = None
    normalized_location: Optional[str] = None
    error_code: Optional[str] = None


def _uuid4(value: str) -> None:
    # 값이 "정확한 형식의 UUIDv4 문자열"인지 확인한다. 아니면 ValueError(값이 잘못됐다는 오류)를 낸다.
    # UUIDv4는 "f2bf2881-d327-4e67-aa24-45fda73e34a9"처럼 겹치지 않게 무작위로 만든 고유 번호다.
    # "REQ-001" 같은 사람이 읽기 쉬운 이름은 번호표로 쓸 수 없으므로 여기서 걸러낸다.
    # 이름이 _ 로 시작하는 것은 "이 모듈 안에서 쓰려고 만든 도우미"라는 파이썬 관례이다.
    if not isinstance(value, str):
        raise ValueError("Identifiers must be UUIDv4 strings")
    try:
        parsed = UUID(value)
    except ValueError as exc:
        # UUID로 읽을 수 없는 글자면, 더 알아보기 쉬운 우리 쪽 오류 메시지로 바꿔서 다시 낸다.
        raise ValueError("Identifiers must be UUIDv4 strings") from exc
    # 버전이 4가 아니거나, 다시 글자로 바꿨을 때 원래 값과 다르면(대문자·중괄호가 섞인 경우 등) 거부한다.
    if parsed.version != 4 or str(parsed) != value:
        raise ValueError("Identifiers must be canonical UUIDv4 strings")


def aggregate(plan: Iterable[CheckPlan], results: Iterable[CheckResult], *,
              sensitive_values: Iterable[str] = ()) -> dict:
    # 검사 "계획"을 기준으로 "결과"를 맞춰 보고 요약(사전 dict)을 만든다.
    #
    # 이 함수가 하는 일:
    #  - 결과가 없는 검사는 NOT_RUN(안 봄)으로 적는다. (오류 코드: RESULT_MISSING)
    #  - 증거 없이 PASS·FAIL이라고만 한 결과는 ERROR로 바꾼다. (EVIDENCE_MISSING)
    #  - 승인 없이 NOT_APPLICABLE(적용 안 함)이라고 한 결과는 ERROR로 바꾼다. (EXCLUSION_NOT_APPROVED)
    #  - 전체 판정(validationVerdict)은 "필수 검사"만 본다.
    #      필수 검사 중 못 본 것이 있거나 적용할 검사가 하나도 없으면 UNVERIFIED(판정 불가),
    #      그렇지 않고 FAIL이 하나라도 있으면 FAIL, 모두 통과하면 PASS다.
    #      UNVERIFIED여도 이미 확인된 FAIL 검사 번호는 따로 남겨 둔다.
    #  - 실패·미검증 목록은 필수 검사와 선택(optional...) 검사를 나눠서 담는다.
    #  - sensitive_values에 적은 글자(예: 시험용 비밀번호)는 설명 글에서 "[REDACTED]"로 바꿔 가린다.
    #      가리는 대상은 errorCode, expectedResult, actualResult, reason, normalizedLocation 다섯 곳뿐이다.
    #      민감한 글자를 자동으로 찾아내지는 않으므로, 다른 곳은 값을 넘기는 쪽이 미리 정리해야 한다.
    #  - 입력이 잘못되었으면 ValueError로 멈춘다.
    #
    # 함수 모양 설명: 매개변수 목록의 `*,`는 "이 뒤의 값은 반드시 이름을 붙여서(sensitive_values=...) 넘겨라"라는 뜻이다.
    # Iterable은 for문으로 하나씩 꺼낼 수 있는 모음(리스트, 튜플 등)이다.
    # 한 번 꺼내면 사라지는 모음일 수도 있어서, 아래에서 tuple()로 한 번에 고정해 둔다.
    secrets = tuple(sensitive_values)
    if any(not isinstance(value, str) or not value for value in secrets):
        # 가릴 글자 목록에 빈 글자("")가 있으면 모든 글자 사이에 [REDACTED]가 끼어들기 때문에 막는다.
        raise ValueError("Sensitive values must be nonempty strings")

    # 설명 글에서 가릴 글자를 [REDACTED]로 바꾼다. 값이 없으면(None) 그대로 None을 돌려준다.
    def safe_text(value):
        # 긴 글자부터 바꿔야 한다. 짧은 글자를 먼저 바꾸면 긴 글자가 일부만 가려질 수 있기 때문이다.
        # 예: "abc"와 "abcdef"를 가릴 때 "abc"를 먼저 바꾸면 "abcdef"가 "[REDACTED]def"로 일부만 남는다.
        if value is None:
            return None
        for secret in sorted(set(secrets), key=len, reverse=True):
            value = value.replace(secret, "[REDACTED]")
        return value

    # 1) 계획 확인: 계획이 비었거나, 번호가 겹치거나, 필수 여부·제외 정보가 이상하면 거부한다.
    checks = tuple(plan)
    if not checks:
        # 계획이 비어 있으면 "검사할 게 없으니 통과"라는 착각이 생기므로 오류로 멈춘다.
        raise ValueError("The fixed test plan must not be empty")
    planned = {}  # {검사 번호: 계획} 사전. 번호로 계획을 바로 찾고, 같은 번호가 겹치는지도 잡아낸다.
    for check in checks:
        _uuid4(check.test_id)
        if type(check.required) is not bool:
            # True/False만 허용한다(예: 숫자 1이나 글자 "yes"는 안 된다).
            raise ValueError("required must be boolean")
        if check.test_id in planned:
            raise ValueError("Duplicate test ID in plan")
        for field in (check.exclusion_approval_ref, check.exclusion_reason):
            if field is not None and (not isinstance(field, str) or not field.strip()):
                # 값을 적었다면 공백만 있는 글자는 안 된다.
                raise ValueError("Exclusion approval and reason must be nonempty")
        if bool(check.exclusion_approval_ref) != bool(check.exclusion_reason):
            # 승인 근거와 이유는 "둘 다 적거나 둘 다 비워야" 한다.
            raise ValueError("Exclusion needs both approval reference and reason")
        planned[check.test_id] = check

    # 2) 결과 확인: 계획에 없는 검사의 결과, 같은 검사의 결과가 두 번, 잘못된 모양의 값을 거부한다.
    received = {}  # {검사 번호: 결과} 사전
    for result in results:
        _uuid4(result.test_id)
        if result.test_id not in planned:
            # 계획에 없던 검사의 결과가 끼어드는 것을 막는다.
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
            # set(집합)은 중복을 없앤다. 길이가 달라졌다면 같은 증거 번호가 두 번 들어 있었다는 뜻이다.
            raise ValueError("Duplicate evidence ID")
        for name in ("expected_result", "actual_result", "reason", "normalized_location", "error_code"):
            value = getattr(result, name)  # 필드 이름을 글자로 주고 값을 꺼낸다(result.reason 과 같은 뜻).
            if value is not None and (not isinstance(value, str) or not value.strip()):
                raise ValueError(f"{name} must be nonempty text when supplied")
        received[result.test_id] = result

    # 3) 검사별 한 줄 만들기: "결과"가 아니라 "계획"을 하나씩 돌아야, 결과가 오지 않은 검사도 줄이 남는다.
    rows = []  # 검사 하나당 한 줄(사전)로 만든 결과표
    for check in checks:
        result = received.get(check.test_id)  # 결과가 없으면 None
        status = result.status if result else Status.NOT_RUN
        evidence = result.evidence_ids if result else ()
        error = "RESULT_MISSING" if result is None else result.error_code
        if status in (Status.PASS, Status.FAIL) and not evidence:
            # 증거 없는 합격/불합격은 믿을 수 없으므로 ERROR(미검증)로 바꾼다.
            status, error = Status.ERROR, "EVIDENCE_MISSING"
        if status == Status.NOT_APPLICABLE and not check.exclusion_approval_ref:
            # 승인 없이 "적용 안 함"이라고 주장한 결과도 인정하지 않는다.
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

    # 4) 전체 판정: 필수 검사만으로 정한다. 승인받아 제외한 검사는 통과율 계산(분모)에서 뺀다.
    # 아래 [ ... for r in rows if ... ] 모양은 "조건에 맞는 것만 골라 새 목록을 만드는" 파이썬 문법이다.
    required = [r for r in rows if r["required"]]
    optional = [r for r in rows if not r["required"]]
    applicable = [r for r in required if r["status"] != "NOT_APPLICABLE"]       # 실제로 적용되는 필수 검사
    incomplete = [r for r in required if r["status"] in ("ERROR", "NOT_RUN")]   # 아직 제대로 못 본 필수 검사
    if not applicable or incomplete:
        # 판정할 대상이 없거나, 못 본 필수 검사가 하나라도 있으면 "판정 불가"다.
        verdict = "UNVERIFIED"
    elif any(r["status"] == "FAIL" for r in required):
        verdict = "FAIL"
    else:
        verdict = "PASS"

    # 상태별 개수를 센다. 결과 사전의 이름은 보고서에서 쓰는 이름(pass, fail, notRun 등)이다.
    def counts(items):
        # 사전의 왼쪽은 상태 이름, 오른쪽은 보고서에 쓸 필드 이름이다.
        names = {"PASS": "pass", "FAIL": "fail", "ERROR": "error",
                 "NOT_RUN": "notRun", "NOT_APPLICABLE": "notApplicable"}
        # sum(...)은 조건이 맞는 항목을 1씩 더해 "해당 상태인 검사의 수"를 구한다.
        return {name: sum(r["status"] == status for r in items)
                for status, name in names.items()}

    # 실패(FAIL)가 확인된 검사들의 번호 목록을 돌려준다.
    def failed(items):
        return [r["testId"] for r in items if r["status"] == "FAIL"]

    # 제대로 확인하지 못한(ERROR 또는 결과 없음) 검사들의 번호 목록을 돌려준다.
    def unverified(items):
        return [r["testId"] for r in items if r["status"] in ("ERROR", "NOT_RUN")]

    # 여기서 만든 판정(validationVerdict)은 이 파일 안에서 계산한 "요약"일 뿐, 시스템의 최종 판정(finalVerdict)이 아니다.
    # requiredPassRate(필수 검사 통과율) = 통과한 적용 필수 검사 수 ÷ 적용 필수 검사 수.
    # 적용할 검사가 하나도 없으면 0으로 나눌 수 없으므로 None을 돌려준다.
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
