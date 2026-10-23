# 평가 개발 환경

최종 갱신: 2026-10-17. Python 3.13을 사용한다(설치할 때 `--python 3.13`으로 직접 지정). dev의 공통 프로젝트 설정을 사용한다.

Python 3.10은 dev 테스트(`Task.cancelling`)와 Security 승인 함수(`co_qualname`)가 지원하지 않아 실패하고, 3.14는 dev 보안 승인 테스트 6개가 실패했다. 루트 `pyproject.toml`의 `requires-python = ">=3.10"`은 팀 공유 파일이라 바꾸지 않았다. 팀 Python 버전 통일은 회의 안건이다(OPEN-15).

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

기본 pytest는 `tests/`만 수집한다. 테스트 수와 결과는 수정 시점에 따라 달라지므로 [개발진행사항 7절](development_progress.md#7-검증-기록)에서 날짜·검증 대상과 함께 확인한다. 2026-10-16 5차 수정 시점의 기록은 평가 494개, 저장소 전체 3802개 통과이며 현재 코드의 전체 통과를 뜻하지 않는다. 저장소 전체는 dev 테스트가 많아 20분 넘게 걸린다. dev의 `test_planner_developer_continuation`에는 과거 수정 전 커밋에서도 재현된 간헐 실패가 있으므로 새 실패는 실제 원인과 변경 범위를 다시 확인한다.
평가 테스트만 실행하려면 `uv run --frozen python -m pytest tests/evaluation`,
환경 확인은 `uv run --frozen python -m pytest checks`로 따로 실행한다.
평가 자체 테스트와 보고서 모델 호환 검사는 실제 A2A/MCP/회원가입 서비스 통합 검증이 아니다.
보고서 데모는 mock이다.

`checks/test_setup.py`는 프로젝트 가상환경 안에서 실행 중인지 확인한다. 가상환경 없는 컨테이너에서는
실패하므로 기본 실행 대상에서 뺐다. PyCharm 설정을 확인할 때만 따로 실행한다.

## 공통 계약 및 설정

- `schemas/project/`가 공통 스키마 원본이다.
- `src/evaluation/contracts/`의 QA·Security 보고서, Manifest, 도구 실행 근거 스키마는 `schemas/project/`의 고정 사본(현재 원본과 동일)이며 독립 규격이 아니다. `independent_evaluation.schema.json`만 평가 모듈 전용이다.
- 공통 계약 변경 시 사본과 보고서 모델 호환 검사도 함께 갱신한다.
- Agent URL과 토큰은 루트 `.env.example`을 참고한다.
- 실행환경 lock은 루트 `uv.lock`이며 실제 검사 대상 서비스의 lock과 구분한다.
- 평가 공통 스키마는 package-data로 wheel에 포함한다. `security/scn001_security_criteria.json`은 아직 wheel에 들어가지 않는다(루트 `pyproject.toml`은 팀 공유 파일이라 주호 님 승인 후 반영). 보안 판정 구현을 변경하면 코드 동결 후 승인 해시를 다시 생성해야 한다.

## 문제 확인

- import 실패: 루트에서 `uv sync --frozen`을 다시 실행하고 PyCharm 인터프리터를 확인한다.
- 계약 오류: ID 매핑, Manifest, Tool 증거와 필수 검사 계획을 확인한다.
- 스키마·모델 통과만으로 실제 증거 진위나 서비스 성공이 보장되지는 않는다.
- Security 판정이 모든 요구사항을 이유 없이 UNVERIFIED로 내면 `evaluation.security.evaluate(request)`를 직접 호출해 예외를 확인한다(`assess`는 오류를 `None`으로만 알린다).
- 독립 평가 보고서의 `created_at` 오류: 시간대를 붙인 `2026-10-14T12:00:00+09:00` 형식을 사용한다.
  3.10에서는 소수점 초 등 일부 ISO 표기를 읽지 못해 코드는 3.10 호환을 위해 `Z`를 `+00:00`으로 바꿔 읽는다.
