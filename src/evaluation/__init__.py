# 이 폴더(evaluation)는 "4번 담당 평가 모듈"이다. 파이썬에서는 __init__.py가 있는 폴더를 하나의 "패키지"로 본다.
# (이 파일은 일부러 비워 두었고, 안내 주석만 있다.)
#
# 에이전트가 만든 결과물을 채점하고, 그 결과를 믿을 수 있는지 확인해서 보고서로 만드는 일을 한다.
# 파일별 역할은 아래와 같다.
#   aggregation.py   검사 계획과 결과를 맞춰 보고 통과·실패를 센다.
#   manifest.py      "어떤 코드·환경을 검사했는가"(신분증)가 기준과 같을 때만 결과를 인정한다.
#   independent.py   독립 평가 보고서를 만든다.(기본 경로)
#   reporting.py     시스템이 받는 QA·SECURITY 보고서 모양으로 바꾼다.(보조 경로)
#   security/        회원가입 코드의 보안 요구사항을 코드만 읽어서 판정한다.
#   protected_tests/ 회원가입 보호 테스트 묶음(D2)과, 그것을 dev 설정·독립 평가 입력으로 바꾸는 도구
#   check_adapters.py unittest 보고서와 정적 판정 결과를 검사 결과(CheckResult)로 바꾼다.(결과 어댑터)
#   run_metrics.py   시스템의 실행 기록을 평가 숫자(지표)로 바꾸고, 두 구조를 비교해도 되는지 확인한다.
#   trace_checks.py  실행 기록(Trace)에서 비밀번호·해시 노출(REQ-006)과 추적 연결(REQ-008)을 확인한다.
#   final_evaluation.py 최종 Snapshot을 받아 보호 테스트(컨테이너)·정적 판정으로 채점하고 독립 평가 보고서를 만든다.(D5)
#   summary.py       독립 평가 결과를 회의·발표용 Markdown 요약으로 만든다.
#   demo_index.py    시연 자료(로그·생성 파일·화면)의 목록표를 만든다.
#   common.py        여러 파일이 함께 쓰는 구조 이름(SINGLE_AGENT/MULTI_AGENT)과 시각 읽기 도구
#   report_demo.py, __main__.py   가짜 데이터로 돌려 보는 예시
#   contracts/       보고서의 JSON 모양을 정한 약속 문서(스키마) 모음
# 시험을 실행하는 곳은 final_evaluation의 실행기 하나뿐이다. 실제 생성 코드는 DockerRunner로 컨테이너에서만 실행하고
# (LocalRunner는 합성 표본 시험용), 나머지 모듈은 이미 나온 결과를 다룬다. 네트워크로 무언가를 보내지 않는다.
