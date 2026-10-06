"""설치된 개발 기준 환경의 파이썬으로 실제 보고서 모델과의 호환성을 검사한다.

사용법: python scripts/check_dev_reports.py /path/to/dev/.venv/bin/python
보고서 모델만 검사하며 에이전트 간 전송, 작업 소유 관계, 제품 실행은 검사하지 않는다."""
import json
import subprocess
import sys
from evaluation.report_demo import examples

# 지정한 별도 인터프리터에서 실행할 최소 검사 코드. JSON은 stdin으로만 전달한다.
validator = '''
import json, sys
from orchestrator.domain.validation_artifacts import QAReportArtifact, SecurityReportArtifact
rows = json.load(sys.stdin)
for row in rows:
    payload = row['report']
    model = QAReportArtifact if payload['artifactType'] == 'QA_REPORT' else SecurityReportArtifact
    model.model_validate(payload)
print(f"Native dev models accepted {len(rows)} synthetic reports")
'''
if __name__ == '__main__':
    if len(sys.argv) != 2:
        raise SystemExit('Provide the Python executable of an installed dev checkout')
    # shell을 사용하지 않으므로 경로나 보고서 문자열이 셸 명령으로 실행되지 않는다.
    subprocess.run([sys.argv[1], '-c', validator], input=json.dumps(list(examples())),
                   text=True, check=True)
