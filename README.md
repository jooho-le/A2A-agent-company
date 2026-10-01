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

현재 구현된 기능은 서비스 시작, Workflow 도메인 모델·상태 전이·재시도 정책, Snapshot/Artifact 인계 계약, A2A 1.0 Client와 Task lifecycle, SQLite Workflow 저장소, Run/Step/Trace API입니다. Run API는 요청을 `RECEIVED`로 저장하고 Planner Step을 만들지만 Agent dispatch는 아직 연결하지 않습니다. Artifact Registry/Object Store와 실제 Mock/팀원 Agent 연결은 후속 작업입니다.

기본 DB 경로는 `.data/orchestrator.sqlite3`이며 `ORCHESTRATOR_DATABASE_PATH` 환경변수로 바꿀 수 있습니다. `.data/`는 Git에서 제외됩니다.

## 테스트

```bash
PYTHONPATH=src uv run python -m unittest discover -s tests -v
```
