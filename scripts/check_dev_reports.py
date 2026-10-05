"""Run native dev model compatibility checks with an installed dev interpreter.

Usage: python scripts/check_dev_reports.py /path/to/dev/.venv/bin/python
This checks report models, not A2A transport, Task ownership or product execution.
"""
import json
import subprocess
import sys
from evaluation.report_demo import examples

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
    subprocess.run([sys.argv[1], '-c', validator], input=json.dumps(list(examples())),
                   text=True, check=True)
