# A2A Orchestrator

Python 3.10+와 FastAPI를 사용하는 Orchestrator 서비스입니다.

## 로컬 실행

`uv`가 설치되어 있다면 프로젝트 루트에서 다음을 실행합니다.

```bash
uv sync
uv run uvicorn orchestrator.main:app --app-dir src --reload
```

기본 주소는 `http://127.0.0.1:8000`입니다.

```text
GET /health
POST /api/v1/runs
GET /api/v1/runs/{runId}
GET /api/v1/runs/{runId}/steps
GET /api/v1/runs/{runId}/events
GET /docs
```

설정값은 환경변수 또는 프로젝트 루트의 `.env` 파일에서 읽습니다. 시작값은 `.env.example`을 참고하세요.

현재 구현된 기능은 Workflow 상태·Trace 저장, A2A 1.0 Task 실행, Planner/Developer 출력 검증, Build PASS 시 같은 Snapshot을 QA/Security에 전달하고 두 Report를 검증·저장하는 로컬 Orchestrator 흐름입니다. 고정 Scenario/Requirement Registry가 없어 자동 `SUCCESS`는 보류되며, 자동 Developer 수정·재검증과 실제 Artifact bytes/ACL은 아직 연결되지 않았습니다. Build Tool은 Developer Agent가 MCP로 실행하며 Orchestrator가 MCP를 직접 호출하지 않습니다. 통합 계약은 [10번 Planner/Developer 계약](docs/10-planner-output-developer-dispatch.md), [11번 Snapshot/Build handoff](docs/11-developer-snapshot-build.md), [12번 QA/Security 판정](docs/12-validation-results-verdict.md)을 참고하세요.

자동 QA/Security dispatch를 시험하려면 `.env`에 `ORCHESTRATOR_PLANNER_AGENT_URL`, `ORCHESTRATOR_DEVELOPER_AGENT_URL`, `ORCHESTRATOR_QA_AGENT_URL`, `ORCHESTRATOR_SECURITY_AGENT_URL`을 모두 설정합니다. QA/Security 중 하나라도 설정되지 않으면 Snapshot과 pending Step은 보존하고 Run을 `HUMAN_REVIEW`로 전환합니다.

기본 DB 경로는 `.data/orchestrator.sqlite3`이며 `ORCHESTRATOR_DATABASE_PATH` 환경변수로 바꿀 수 있습니다. `.data/`는 Git에서 제외됩니다.

## 테스트

```bash
PYTHONPATH=src uv run python -m unittest discover -s tests -v
```
