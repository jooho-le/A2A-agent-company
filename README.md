# A2A Orchestrator

Python 3.10+와 FastAPI를 사용하는 Orchestrator 서비스입니다.

## 로컬 실행

`uv`가 설치되어 있다면 프로젝트 루트에서 다음을 실행합니다.

```bash
uv sync --frozen
uv run uvicorn orchestrator.main:app --app-dir src --reload
```

기본 주소는 `http://127.0.0.1:8000`입니다.

```text
GET /health
GET /api/v1/scenarios
POST /api/v1/runs
GET /api/v1/runs/{runId}
GET /api/v1/runs/{runId}/steps
GET /api/v1/runs/{runId}/events
GET /api/v1/runs/{runId}/artifacts
GET /api/v1/runs/{runId}/artifacts/{artifactId}
GET /api/v1/runs/{runId}/issues
GET /api/v1/runs/{runId}/configuration
GET /api/v1/runs/{runId}/workspace
GET /api/v1/runs/{runId}/tool-attempts
POST /api/v1/runs/{runId}/resume
POST /api/v1/runs/{runId}/recover
POST /api/v1/runs/{runId}/cancel
GET /docs
```

설정값은 환경변수 또는 프로젝트 루트의 `.env` 파일에서 읽습니다. 시작값은 `.env.example`을 참고하세요.

현재 구현은 SCN-001 고정 기준, Planner → Developer → QA/Security 실행, 최대 3회 수정·새 Snapshot 재검증, 불변 Artifact/Issue/Trace 저장 및 최종 판정입니다. Run 생성 때 workspace ID와 Scenario 정책·실행 설정을 동결하고, Build/QA/Security의 동일 Manifest 및 MCP 실행·재시도 근거가 확인되어야 `SUCCESS` 또는 `UNVERIFIED`를 확정합니다. 비밀정보는 전송·저장·로그 경계에서 마스킹하고 Agent 인증은 HTTP 헤더로만 전달합니다. 최신 변경과 제한은 [14번 정의서 준수 보완](docs/14-orchestrator-contract-compliance.md)을 참고하세요.

현재 등록된 `scenarioId`는 `f7f9e5c3-ffc3-4b3f-918b-21e1b956ce76`입니다. 다른 UUID와 공백뿐인 `requestText`는 422를 반환합니다. `/api/v1/scenarios`에서 등록 기준을 확인할 수 있습니다.

재개는 저장된 Task/Context를 이어 사용하며 수정 횟수를 늘리지 않습니다. 전송됐으나 Task ID가 확인되지 않은 요청은 자동 재전송하지 않습니다. 진행 중인 다른 제어 작업과 충돌하면 409이며, 원격 Task 취소는 실제 `CANCELED` 확인 후 기록합니다. PID 기반 잠금은 단일 호스트 MVP용입니다.

Build/Test/Scan의 실제 MCP 호출은 Agent/MCP 담당 범위입니다. Orchestrator는 전달된 실행 근거를 검증·저장하며, Artifact 원본 bytes·READ_ONLY ACL·실제 회원가입 코드의 성공·비교 실험 완료는 아직 주장하지 않습니다.

자동 QA/Security dispatch를 시험하려면 `.env`에 `ORCHESTRATOR_PLANNER_AGENT_URL`, `ORCHESTRATOR_DEVELOPER_AGENT_URL`, `ORCHESTRATOR_QA_AGENT_URL`, `ORCHESTRATOR_SECURITY_AGENT_URL`을 모두 설정합니다. QA/Security 중 하나라도 설정되지 않으면 Snapshot과 pending Step은 보존하고 Run을 `HUMAN_REVIEW`로 전환합니다.

기본 DB 경로는 `.data/orchestrator.sqlite3`이며 `ORCHESTRATOR_DATABASE_PATH` 환경변수로 바꿀 수 있습니다. `.data/`는 Git에서 제외됩니다.

## 테스트

```bash
PYTHONPATH=src uv run --frozen python -m unittest discover -s tests -v
```

## 평가 모듈

평가 모듈은 `src/evaluation/`에 있으며 공통 `pyproject.toml`, `uv.lock`, `.venv`를 사용합니다.
[평가 안내](src/evaluation/README.md)와 [평가 개발진행사항](docs/evaluation/development_progress.md)을 참고하세요.

```bash
uv sync --frozen
uv run --frozen python -m pytest
uv run --frozen python -m evaluation.report_demo
```
