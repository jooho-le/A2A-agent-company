# 평가 개발 환경

최종 갱신: 2026-10-05. Python 3.10 이상. dev의 공통 프로젝트 설정을 사용한다.

## 설치 및 PyCharm

1. 저장소 루트를 PyCharm 프로젝트로 연다.
2. uv를 설치한 환경에서 루트 기준 `uv sync --frozen`을 실행한다.
3. 인터프리터는 루트 `.venv/bin/python`으로 지정한다.
4. 테스트는 모듈 `pytest`, 작업 디렉터리는 저장소 루트로 실행한다.

루트 `pyproject.toml`과 `uv.lock`만 사용한다. Orchestrator와 평가 모듈은 모두
`src/`에서 설치되며 평가 전용 패키지 설치나 별도 가상환경은 필요 없다.
`jsonschema[format]`은 실행 의존성, `pytest`는 기본 설치되는 dev 의존성 그룹이다.
개인 `.idea` 설정은 Git에 올리지 않는다.

## 실행과 검증

```bash
uv sync --frozen
uv run --frozen uvicorn orchestrator.main:app --app-dir src --reload
uv run --frozen python -m pytest
uv run --frozen python -m evaluation
uv run --frozen python -m evaluation.report_demo
uv run --frozen python scripts/check_dev_reports.py .venv/bin/python
# 기존 Orchestrator unittest 실행 방식도 유지
PYTHONPATH=src uv run --frozen python -m unittest discover -s tests -v
```

pytest는 `checks/`와 `tests/`를 수집한다. 평가 자체 테스트와 보고서 모델 호환 검사는
실제 A2A/MCP/회원가입 서비스 통합 검증이 아니다. 보고서 데모는 mock이다.

## 공통 계약 및 설정

- `schemas/project/`가 공통 스키마 원본이다.
- `src/evaluation/contracts/`는 dev `2b4fc60`의 고정 사본이며 독립 규격이 아니다.
- 공통 계약 변경 시 사본과 보고서 모델 호환 검사도 함께 갱신한다.
- Agent URL과 토큰은 루트 `.env.example`을 참고한다.
- 실행환경 lock은 루트 `uv.lock`이며 실제 검사 대상 서비스의 lock과 구분한다.

## 문제 확인

- import 실패: 루트에서 `uv sync --frozen`을 다시 실행하고 PyCharm 인터프리터를 확인한다.
- 계약 오류: ID 매핑, Manifest, Tool 증거와 필수 검사 계획을 확인한다.
- 스키마·모델 통과만으로 실제 증거 진위나 서비스 성공이 보장되지는 않는다.
