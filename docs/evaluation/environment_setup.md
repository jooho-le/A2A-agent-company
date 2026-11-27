# 평가 개발 환경

최종 갱신: 2026-11-15. Python 3.13을 사용한다(설치할 때 `--python 3.13`으로 직접 지정). dev의 공통 프로젝트 설정을 사용한다.

최신 전체 검증은 [5,780개 통과 기록](precommit_verification_2026-11-15.md)을 따른다. 환경 준비와 실제 실험 설계는 구분한다. 현재 비교 대상은 Single 하나·Multi Ver1·Multi Ver2이며 기존 2×2 계획·제출·집계 정리가 남아 있다. 실제 제출 전 [실행 절차의 적용 범위](experiment_runbook.md)를 확인한다. 이번 문서 갱신에서 비교 실험·유료 모델 호출은 실행하지 않았다.

팀 선언은 `requires-python = ">=3.10"`이다. 평가 코드·보호 테스트와 dev 승인 함수가 3.10에서도 동작한다(승인 함수의 3.11 전용 속성은 2026-10-30에 고침). 3.14는 dev 보안 승인 테스트 6개가 실패했다. 내 개발 환경은 3.13이다. 실행 시험(D2 보호 테스트 변형, D5 합성 서비스)에 필요한 sqlalchemy·argon2-cffi는 dev 그룹에 있어 `uv run python -m pytest tests/evaluation`이 함께 돌린다. 실제 컨테이너 시험은 Docker를 켜고 `EVAL_DOCKER_IMAGE`(해시로 고정한 이미지)를 주면 돈다. 공식 실험용 공통 이미지는 아래 '실험 실행 환경' 절에서 준비한다.

내 컴퓨터 루트에는 `.python-version`(내용 3.13) 파일이 있지만 **Git에는 올리지 않았다.** 이 파일은 저장소를 쓰는 모두의 uv가 고르는 Python 버전을 바꾸기 때문이다. 그래서 다른 컴퓨터에서는 아래처럼 `--python 3.13`을 직접 적어야 3.13이 선택된다. 팀이 3.13으로 통일하기로 하면 팀장 승인을 받아 이 파일을 올리거나 `pyproject.toml`을 고친다.

## 설치 및 PyCharm

1. 저장소 루트를 PyCharm 프로젝트로 연다.
2. uv를 설치한 환경에서 루트 기준 `uv sync --frozen --python 3.13`을 실행한다. uv가 3.13 가상환경을 만든다(이미 3.10 `.venv`가 있으면 지우고 다시 만든다).
3. 인터프리터는 루트 `.venv/bin/python`으로 지정한다.
4. 테스트는 모듈 `pytest`, 작업 디렉터리는 저장소 루트로 실행한다.

루트 `pyproject.toml`과 `uv.lock`만 사용한다. Orchestrator와 평가 모듈은 모두
`src/`에서 설치되며 평가 전용 패키지 설치나 별도 가상환경은 필요 없다.
`jsonschema[format]`은 실행 의존성, `pytest`는 기본 설치되는 dev 의존성 그룹이다.
개인 `.idea` 설정은 Git에 올리지 않는다.

## 실행과 검증

```bash
uv sync --frozen --python 3.13
uv run --frozen uvicorn orchestrator.main:app --app-dir src --reload
uv run --frozen python -m pytest
uv run --frozen python -m evaluation
uv run --frozen python -m evaluation.report_demo
uv run --frozen python scripts/check_dev_reports.py .venv/bin/python
# 기존 Orchestrator unittest 실행 방식도 유지
PYTHONPATH=src uv run --frozen python -m unittest discover -s tests -v
```

기본 pytest는 `tests/`만 수집한다. 테스트 수와 결과는 수정 시점에 따라 달라지므로 [개발진행사항](development_progress.md) 최신 절에서 날짜·검증 대상과 함께 확인한다. 저장소 전체는 dev 테스트가 많아 40분 가까이 걸리고, dev의 `test_planner_developer_continuation`에는 수정 전 커밋에서도 재현된 간헐 실패가 있으므로 새 실패는 원인과 변경 범위를 다시 확인한다.
평가 테스트만 실행하려면 `uv run --frozen python -m pytest tests/evaluation`(dev 그룹에 sqlalchemy·argon2-cffi가 있어 보호 테스트·D5 실행 시험도 함께 돈다, 2026-10-30),
환경 확인은 `uv run --frozen python -m pytest checks`로 따로 실행한다.
Ver1·Ver2 실험 준비·채점·비교 명령(`python -m evaluation.experiment configure|fetch|score|record|compare`)과 dev 내보내기 명령(`agents.platform.snapshot_export`·`a2a_export`)은 [평가 모듈 README](../../src/evaluation/README.md)의 "Ver1·Ver2 실험 도구" 절에 있다.
평가 자체 테스트와 보고서 모델 호환 검사는 실제 A2A/MCP/회원가입 서비스 통합 검증이 아니다.
보고서 데모는 mock이다.

`checks/test_setup.py`는 프로젝트 가상환경 안에서 실행 중인지 확인한다. 가상환경 없는 컨테이너에서는
실패하므로 기본 실행 대상에서 뺐다. PyCharm 설정을 확인할 때만 따로 실행한다.

## 실험 실행 환경 (2026-11-15)

Single/Multi 측정 Run은 Docker 안에서 Host 도구(빌드·단위·브라우저·보안)를 실행하고, 같은 이미지에서 최종 채점(D5)을 한다. 준비와 확인은 `evaluation.environment`가 맡는다.

```bash
PYTHONPATH=src .venv/bin/python -m evaluation.environment status
```

- 준비된 값: 공통 이미지 `capstone-sandbox:scn001`(`sha256:c712fdf445fe01217001440459070bf1e2b0a8d94ad3b9103bacfedddf7dae5a`, Python 3.13 slim·Playwright 1.65.2·Bandit 1.9.4), wheel 33개 lock, 공통 시작 커밋(`.data/starting-source/scn001`), Ver1·Ver2 도구 설정(`configs/experiment/`). 받은 파일·로컬 설정은 Git 제외 위치에 있다.
- 다시 만들 때는 `docker/sandbox/prepared/`·`configs/owned-source.local.json`·`.data/starting-source/`를 지운 뒤 `environment all --allow-network`를 실행한다(다운로드 승인 필요).
- 실제 이미지로 평가 시험을 돌리려면 다음을 실행한다.

```bash
EVAL_DOCKER_IMAGE=sha256:c712fdf445fe01217001440459070bf1e2b0a8d94ad3b9103bacfedddf7dae5a .venv/bin/python -m pytest -q tests/evaluation/test_final_evaluation.py::test_real_container_run tests/evaluation/test_docker_evaluation_integration.py tests/evaluation/test_experiment_image_skeleton.py
```

- 사전점검: `python -m evaluation.preflight --plan <계획>`. 설정 대조와 함께 dev 샌드박스와 같은 옵션으로 실제 브라우저 도구를 한 번 돌린다.
- 환경 변수(값은 문서·커밋에 넣지 않음): `A2A_PLATFORM_LLM_API_KEY`(모델), `A2A_PLATFORM_<역할>_TOKEN`(Agent 사이), `A2A_HOST_API_TOKEN`(Host API, dev 83번부터 필수, 32자 이상, 다른 토큰과 달라야 함). 실행 순서는 [시범 실험 실행 절차](experiment_runbook.md)를 따른다.

## 공통 계약 및 설정

- `schemas/project/`가 공통 스키마 원본이다.
- `src/evaluation/contracts/`의 QA·Security 보고서, Manifest, 도구 실행 근거 스키마는 `schemas/project/`의 고정 사본(현재 원본과 동일)이며 독립 규격이 아니다. `independent_evaluation.schema.json`만 평가 모듈 전용이다.
- 공통 계약 변경 시 사본과 보고서 모델 호환 검사도 함께 갱신한다.
- Agent URL과 토큰은 루트 `.env.example`을 참고한다.
- 실행환경 lock은 루트 `uv.lock`이며 실제 검사 대상 서비스의 lock과 구분한다.
- 평가 공통 스키마, `security/scn001_security_criteria.json`, 보호 테스트 묶음(`protected_tests/suite.json`, `tests/signup/*.py`)을 package-data로 wheel에 포함한다(2026-10-30, 10.29 회의록의 테스트용 dev 수정으로 4번이 `pyproject.toml` 수정). 보안 판정 구현을 변경하면 코드 동결 후 승인 해시를 다시 생성해야 한다.

## 문제 확인

- import 실패: 루트에서 `uv sync --frozen`을 다시 실행하고 PyCharm 인터프리터를 확인한다.
- 계약 오류: ID 매핑, Manifest, Tool 증거와 필수 검사 계획을 확인한다.
- 스키마·모델 통과만으로 실제 증거 진위나 서비스 성공이 보장되지는 않는다.
- Security 판정이 모든 요구사항을 이유 없이 UNVERIFIED로 내면 `evaluation.security.evaluate(request)`를 직접 호출해 예외를 확인한다(`assess`는 오류를 `None`으로만 알린다).
- 독립 평가 보고서의 `created_at` 오류: 시간대를 붙인 `2026-10-14T12:00:00+09:00` 형식을 사용한다.
  3.10에서는 소수점 초 등 일부 ISO 표기를 읽지 못해 코드는 3.10 호환을 위해 `Z`를 `+00:00`으로 바꿔 읽는다.
