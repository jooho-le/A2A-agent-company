# 에이전트 호환 보고서(QA_REPORT / SECURITY_REPORT)의 "가짜 데이터 예시"를 만드는 파일이다.
# 터미널에서 `python -m evaluation.report_demo` 로 실행하면 JSON으로 출력된다.
#
# scripts/check_dev_reports.py가 이 예시를 가져다가 "팀 코드(dev)의 보고서 모델이 우리가 만든 보고서를 받아들이는지" 확인한다.
# 여기 있는 값은 전부 가짜이고 실제 서비스 결과가 아니다.
import json
from uuid import UUID
from .aggregation import CheckPlan, CheckResult, Status
from .manifest import ExecutionManifest
from .reporting import TestBinding, build_report


def examples():
    # QA와 SECURITY 두 역할 × PASS·FAIL·ERROR·NOT_RUN 네 상태 = 보고서 8개를 하나씩 만들어 돌려준다.
    # 돌려주는 모양: {"mock": True, "case": 사례 이름, "report": 보고서}
    # (def 안에 yield가 있으면 "값을 하나씩 내보내는 함수(제너레이터)"가 된다.)
    #
    # uid(n): 같은 숫자를 주면 항상 같은 예제용 UUID를 만들어 준다. (lambda는 이름 없는 한 줄짜리 함수)
    uid = lambda n: str(UUID(int=n, version=4))
    # 가짜 신분증(Manifest). 해시 자리에는 'a'*40 처럼 같은 글자를 반복해 길이만 맞춘 가짜 값을 넣었다.
    manifest = ExecutionManifest('synthetic-repository', 1, uid(1), 'sha1',
                                 'a'*40, 'b'*40, 'c'*64, 'sha256:'+'d'*64, 'sha256:'+'e'*64)
    for role in ('QA', 'SECURITY'):
        for status in (Status.PASS, Status.FAIL, Status.ERROR, Status.NOT_RUN):
            # 보고서에 들어갈 식별 정보(가짜)
            context = dict(artifactId=uid(2), artifactVersion=1, previousArtifactId=None,
                           runId=uid(3), workflowStepId=uid(4), a2aTaskId='synthetic-task',
                           a2aArtifactId='synthetic-artifact', createdAt='2026-10-04T12:00:00Z',
                           requirementIds=[uid(5)])
            # 도구 실행 증거(가짜). 제품 검사가 FAIL이어도 도구가 끝까지 정상으로 돌았다면 "도구의 결과(outcome)"는 PASS다.
            # (도구가 고장 난 것과, 도구가 정상으로 돌아서 제품의 결함을 찾아낸 것은 다른 일이다.)
            evidence = dict(toolName='run_unit_tests' if role == 'QA' else 'run_security_scan',
                            executionId=uid(8), executionManifest=manifest.to_payload(),
                            evidenceRef='artifact://synthetic/report', attempts=[dict(
                                attempt=0, outcome='PASS' if status in (Status.PASS, Status.FAIL)
                                else 'UNVERIFIED', evidenceRef='artifact://synthetic/attempt')])
            report = build_report(role=role, context=context, expected=manifest, observed=manifest,
                plan=[CheckPlan(uid(6))], results=[CheckResult(uid(6), status, (uid(7),),
                    expected_result='Synthetic expected result', actual_result=status.value,
                    reason='Synthetic diagnostic', normalized_location='synthetic/check')],
                bindings={uid(6): TestBinding('SYNTHETIC-001', uid(5), 'Synthetic check')},
                tool_evidence={uid(6): evidence}, findings=[] if role == 'SECURITY' else None)
            yield dict(mock=True, case=role + '_' + status.value, report=report)


# 이 파일을 직접 실행했을 때만 예시 8개를 한 줄씩 출력한다.
if __name__ == '__main__':
    for example in examples():
        print(json.dumps(example, ensure_ascii=False))  # ensure_ascii=False: 한글이 \uXXXX로 깨지지 않게 출력
