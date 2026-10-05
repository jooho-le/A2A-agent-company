"""Offline synthetic aggregation demo: python -m evaluation."""
import json
from uuid import UUID
from .aggregation import CheckPlan, CheckResult, Status, aggregate


def main() -> None:
    # Synthetic IDs only. No real Registry, evidence store or service is used.
    ids = [str(UUID(int=i, version=4)) for i in (1, 2)]
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
            [CheckResult(ids[i], status, (evidence,) if status in
                         (Status.PASS, Status.FAIL) else ())
             for i, status in enumerate(statuses)],
        )
        print(json.dumps({"mock": True, "case": name, "localSummary": summary}))


if __name__ == "__main__":
    main()
