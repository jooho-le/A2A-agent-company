"""에이전트 호환 보고서 합성 예시. 실행: python -m evaluation.report_demo

scripts/check_dev_reports.py가 이 예시로 dev 보고서 모델 호환을 확인한다. 모든 값은 가짜다.
"""
import json
from uuid import UUID
from .aggregation import CheckPlan, CheckResult, Status
from .manifest import ExecutionManifest
from .reporting import TestBinding, build_report


def examples():
    """QA·SECURITY × PASS·FAIL·ERROR·NOT_RUN 보고서 8개를 {"mock", "case", "report"} 형태로 돌려준다."""
    uid = lambda n: str(UUID(int=n, version=4))
    manifest = ExecutionManifest('synthetic-repository', 1, uid(1), 'sha1',
                                 'a'*40, 'b'*40, 'c'*64, 'sha256:'+'d'*64, 'sha256:'+'e'*64)
    for role in ('QA', 'SECURITY'):
        for status in (Status.PASS, Status.FAIL, Status.ERROR, Status.NOT_RUN):
            context = dict(artifactId=uid(2), artifactVersion=1, previousArtifactId=None,
                           runId=uid(3), workflowStepId=uid(4), a2aTaskId='synthetic-task',
                           a2aArtifactId='synthetic-artifact', createdAt='2026-10-04T12:00:00Z',
                           requirementIds=[uid(5)])
            # 제품 FAIL이어도 도구가 끝까지 돌았으면 도구 outcome은 PASS다.
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


if __name__ == '__main__':
    for example in examples():
        print(json.dumps(example, ensure_ascii=False))
