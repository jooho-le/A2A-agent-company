# 환경 설정 및 실행 안내

최종 갱신: 2026-10-04. Python 3.10 이상, 현재 로컬 Python 3.10.2/arm64 사용.
평가 패키지는 `evaluation/pyproject.toml`, 고정 의존성은 `evaluation/uv.lock`으로 관리한다.

## PyCharm

1. 저장소 루트를 프로젝트로 연다.
2. 인터프리터는 기존 루트 `.venv/bin/python`으로 설정한다.
3. 실행 모듈을 `pytest`, 작업 디렉터리를 저장소 루트로 설정한다.
4. pytest는 `checks`와 `tests` 전체를 수집한다.

기존 가상환경을 다시 만들 필요는 없다. 이번에는 같은 가상환경에 평가 패키지와
스키마 검증 의존성을 lock 기준으로 설치했다. 개인 `.idea` 설정은 공유하지 않는다.

## 설치

현재 `seokmin-evaluation`은 dev에서 시작한 통합 환경이다.
저장소 루트에서 공통 의존성을 설치한 뒤 평가 패키지를 추가한다.
uv는 별도로 설치되어 있어야 한다.

```bash
uv sync --frozen
uv pip install --python .venv/bin/python -e "./evaluation[dev]"
```

평가용 `evaluation/uv.lock`은 독립 환경용이며 공통 `uv.lock`을 대체하지 않는다.
통합 환경에서 평가 lock만으로 sync하면 Orchestrator 의존성이 제거될 수 있다.

## 검사와 데모

```bash
.venv/bin/python -m pytest
.venv/bin/python -m evaluation
.venv/bin/python -m evaluation.report_demo
# 별도 dev 체크아웃에 설치된 Python을 인자로 전달
.venv/bin/python scripts/check_dev_reports.py /path/to/dev/.venv/bin/python
```

평가 자체 테스트 91개와 공통 모델 수용 검사 8개를 확인했다. 데모는 모두 mock이다.
`check_dev_reports.py`는 보고서 모델만 검사하며 A2A 전송·Task 소유 관계·실제 도구 실행을 검사하지 않는다.

## dev와 함께 사용할 때

- dev 기준은 `2b4fc6037e1a7d8296040c01404c56e8a2a0f22e`이며 Python >=3.10이다.
- dev 환경은 해당 체크아웃 루트에서 `uv sync --frozen`으로 준비한다.
- 실행: `uv run --frozen uvicorn orchestrator.main:app --app-dir src --reload`.
- 자체 검사: `PYTHONPATH=src uv run --frozen python -m unittest discover -s tests -v`.
- dev 배포는 src/만 탐색하므로 평가 패키지는 별도 설치한다. dev 루트 pyproject를 덮어쓰지 않는다.
- 통합 환경에서는 dev 의존성 설치 후 평가 wheel 또는 평가 `[dev]` 패키지를 추가 설치하고
  두 요구가 호환되는지 확인한다. 평가 lock만으로 통합 환경 전체를 sync하면 dev 전용 의존성이 제거될 수 있다.
- 두 lock을 합친 팀 공통 통합 lock은 아직 없다. 이번 검증은 별도 환경 간 JSON 보고서 교환으로 진행했다.
- Agent URL/토큰은 dev `.env.example`을 기준으로 담당자가 제공한 값만 설정한다.

## Git 제외와 문제 확인

`.venv/`, `.idea/`, `.env*`(example/template 제외), `.data/`, 캐시, results/,
build/dist/와 egg-info는 제외한다. 평가 pyproject·lock·공통 스키마는 포함한다.

- jsonschema import 실패: 위 평가 패키지 추가 설치 명령을 실행한다.
- dev import 실패: Orchestrator가 설치된 별도 Python 경로인지 확인한다.
- 공통 보고서 변환 거부: ID 대응표·Manifest·Tool 실행 근거·필수 검사 목록을 확인한다.
- schema/모델 검증 통과는 Registry 등록과 증거 진위를 보장하지 않는다.

## 변경 이력

| 날짜 | 변경 |
|---|---|
| 2026-10-04 | 루트 Python 환경 유지, pytest 전체 tests 수집, 실행 데이터 Git 제외 |
| 2026-10-04 | 평가 독립 패키지와 uv.lock 도입, jsonschema 및 개발 의존성 설치 |
| 2026-10-04 | 기존 local_setup.md를 환경 설정 문서로 통합 |
