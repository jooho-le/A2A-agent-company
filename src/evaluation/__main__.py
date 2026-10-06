"""집계 합성 예시. 실행: python -m evaluation (외부 접근 없음, 출력에 mock 표시)"""
import json
from uuid import UUID
from .aggregation import CheckPlan, CheckResult, Status, aggregate


def main() -> None:
    """검사 2개 계획으로 정상·QA 실패·보안 실패·환경 오류·실패+누락 다섯 경우를 집계해 출력한다."""
    ids = [str(UUID(int=i, version=4)) for i in (1, 2)]  # 매번 같은 가짜 UUID
    evidence = str(UUID(int=100, version=4))
    cases = {
        "normal": [Status.PASS, Status.PASS],
        "qa_failure": [Status.FAIL, Status.PASS],
        "security_failure": [Status.PASS, Status.FAIL],
        "environment_error": [Status.PASS, Status.ERROR],
        "failure_and_missing": [Status.FAIL],
    }
    for name, statuses in cases.items():
        summary = aggregate(
            [CheckPlan(ids[0]), CheckPlan(ids[1])],
            # 증거 없는 PASS·FAIL은 ERROR가 되므로 PASS·FAIL에만 증거를 붙인다.
            [CheckResult(ids[i], status, (evidence,) if status in
                         (Status.PASS, Status.FAIL) else ())
             for i, status in enumerate(statuses)],
        )
        print(json.dumps({"mock": True, "case": name, "localSummary": summary}))


if __name__ == "__main__":
    main()
