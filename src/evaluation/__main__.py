# 집계 기능을 눈으로 확인해 보는 "가짜 데이터 예시"이다.
# 터미널에서 `python -m evaluation` 으로 실행한다. (파일 이름이 __main__.py면 이렇게 패키지 이름만으로 실행된다.)
# 인터넷이나 실제 서비스에 접근하지 않고, 출력에는 mock(가짜)이라고 표시한다.
import json  # 파이썬 사전을 JSON 글자로 바꿔 출력하는 도구
from uuid import UUID  # 고유 번호(UUID)를 만드는 도구
from .aggregation import CheckPlan, CheckResult, Status, aggregate


def main() -> None:
    # 검사 2개짜리 계획으로 다섯 가지 상황을 집계해서 출력한다.
    #   정상 / QA 실패 / 보안 실패 / 환경 오류 / 실패 + 결과 누락
    # UUID(int=1, version=4)처럼 숫자로 만들면 실행할 때마다 같은 가짜 번호가 나와서 예시 결과가 매번 같다.
    ids = [str(UUID(int=i, version=4)) for i in (1, 2)]
    # 증거가 "있다"는 조건을 흉내 내기 위한 가짜 번호이다. 실제 증거 파일을 가리키지 않는다.
    evidence = str(UUID(int=100, version=4))
    # 첫 번째 검사는 QA 검사, 두 번째 검사는 보안 검사라고 가정한다. 실제 시험은 실행하지 않는다.
    # 각 항목: 사례 이름 → 검사별 결과 상태 목록(순서대로 첫 번째·두 번째 검사의 결과)
    cases = {
        "normal": [Status.PASS, Status.PASS],
        "qa_failure": [Status.FAIL, Status.PASS],
        "security_failure": [Status.PASS, Status.FAIL],
        "environment_error": [Status.PASS, Status.ERROR],
        # 두 번째 결과를 일부러 뺐다. "계획에는 있는데 결과가 없는 검사"가 NOT_RUN이 되는지 보여 준다.
        "failure_and_missing": [Status.FAIL],
    }
    for name, statuses in cases.items():
        summary = aggregate(
            [CheckPlan(ids[0]), CheckPlan(ids[1])],
            # 증거 없는 PASS·FAIL은 ERROR로 바뀌어 버리므로, PASS·FAIL인 결과에만 증거 번호를 붙인다.
            [CheckResult(ids[i], status, (evidence,) if status in
                         (Status.PASS, Status.FAIL) else ())
             for i, status in enumerate(statuses)],
        )
        # mock = 가짜 예시라는 표시, case = 사례 이름, localSummary = 집계 결과 요약
        print(json.dumps({"mock": True, "case": name, "localSummary": summary}))


# 이 파일을 직접 실행했을 때만(`python -m evaluation`) main()을 부른다. 다른 파일이 import할 때는 실행되지 않는다.
if __name__ == "__main__":
    main()
