"""우리가 만든 가짜 보고서를 팀 코드(dev)의 "진짜 보고서 모델"이 받아들이는지 확인하는 스크립트이다.

사용법: python scripts/check_dev_reports.py /path/to/dev/.venv/bin/python
(두 번째 인자는 dev 프로젝트가 설치된 가상환경의 파이썬 실행 파일 경로이다.)

확인하는 것은 "보고서 모양이 맞는가"뿐이다. 에이전트끼리 보고서를 주고받는 전송, 작업의 주인이 누구인지,
제품을 실제로 실행하는 것은 확인하지 않는다."""
import json  # 사전 ↔ JSON 글자 변환
import subprocess  # 다른 프로그램(여기서는 dev의 파이썬)을 실행하는 도구
import sys  # 명령줄 인자(sys.argv)를 읽는 도구
from evaluation.report_demo import examples  # 가짜 보고서 8개를 만들어 주는 함수

# dev의 파이썬에서 실행할 짧은 검사 코드(글자로 적어 두었다가 그쪽 파이썬에 넘긴다).
# 보고서 JSON은 명령줄이 아니라 stdin(표준 입력)으로만 전달한다. 그래야 값이 명령어로 해석될 위험이 없다.
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
# 이 파일을 직접 실행했을 때만 아래가 동작한다. (다른 파일에서 import하면 실행되지 않는다.)
if __name__ == '__main__':
    if len(sys.argv) != 2:
        # 인자가 정확히 하나(파이썬 경로)가 아니면 사용법을 알려 주고 종료한다.
        raise SystemExit('Provide the Python executable of an installed dev checkout')
    # shell을 거치지 않고 직접 실행하므로, 경로나 보고서 글자가 셸 명령으로 실행되는 일이 없다.
    # check=True: dev의 검사가 실패하면(보고서를 거부하면) 이 스크립트도 오류로 끝난다.
    subprocess.run([sys.argv[1], '-c', validator], input=json.dumps(list(examples())),
                   text=True, check=True)
