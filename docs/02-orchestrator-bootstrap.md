# 2. Orchestrator 서비스 뼈대

> 상태: 구현 완료  
> 범위: FastAPI 앱, 환경 설정, 기본 로깅, Liveness API  
> 현재 계약·지원 API·남은 경계는 [14번 정의서 준수 보완](14-orchestrator-contract-compliance.md)을 따른다. 이 문서의 단계별 미완료 항목·검증 수치는 당시 이력이며, 실제 Agent/MCP 팀 통합은 별도 범위다.

## 결정 목적

Workflow 및 Agent 통신 기능을 추가하기 전에, 실행 가능한 Python API 서비스의 기본 구조를 마련한다. 이번 단계는 서비스 프로세스가 설정을 읽고 요청에 응답하는 기반만 제공한다.

## 구현 내용

| 경로 | 내용 |
| --- | --- |
| `pyproject.toml`, `uv.lock` | Python 3.10+, FastAPI, `a2a-sdk>=1.0.0`, Pydantic Settings 의존성 고정과 `src` 패키지 구조 |
| `.env.example` | 로컬 실행에 필요한 설정 이름과 기본값 예시 |
| `.gitignore` | `.env`, `.DS_Store`, 가상환경, Python 빌드·캐시 파일 제외 |
| `src/orchestrator/main.py` | FastAPI 앱 팩토리와 실행 가능한 `app` 객체 |
| `src/orchestrator/core/config.py` | `ORCHESTRATOR_` 접두어 환경 설정 및 기본값 |
| `src/orchestrator/core/logging.py` | 표준 Python 로깅의 최소 초기화 |
| `src/orchestrator/api/routes/health.py` | `GET /health` Liveness 응답 |
| `README.md` | 로컬 실행 및 API 확인 방법 |

## 설정 계약

| 환경변수 | 기본값 | 설명 |
| --- | --- | --- |
| `ORCHESTRATOR_APP_NAME` | `A2A Orchestrator` | FastAPI 제목 |
| `ORCHESTRATOR_ENVIRONMENT` | `local` | `local`, `development`, `test`, `production` 중 하나 |
| `ORCHESTRATOR_LOG_LEVEL` | `INFO` | `DEBUG`, `INFO`, `WARNING`, `ERROR`, `CRITICAL` 중 하나 |
| `ORCHESTRATOR_API_PREFIX` | `/api/v1` | 후속 업무 API용 기본 Prefix. 현재 Health 경로에는 적용하지 않음 |
| `ORCHESTRATOR_DATABASE_PATH` | `.data/orchestrator.sqlite3` | SQLite Workflow DB 경로 |
| `ORCHESTRATOR_PLANNER_AGENT_URL` | 설정 없음 | Planner Agent Card/A2A Base URL. 설정하면 Run 제출 후 자동 dispatch |
| `ORCHESTRATOR_DEVELOPER_AGENT_URL` | 설정 없음 | Developer Agent Card/A2A Base URL |
| `ORCHESTRATOR_QA_AGENT_URL` | 설정 없음 | QA Agent Card/A2A Base URL |
| `ORCHESTRATOR_SECURITY_AGENT_URL` | 설정 없음 | Security Agent Card/A2A Base URL |

환경변수는 프로세스 환경에서 읽거나 프로젝트 루트에 `.env` 파일을 두어 설정한다. Agent URL은 Agent Card를 조회할 Base URL이며 A2A interface path를 직접 넣지 않는다. Planner URL을 설정하지 않아도 Run 저장·조회 API는 쓸 수 있지만 POST 응답은 `dispatchStatus: NOT_CONFIGURED`를 알린다. 비밀정보는 예시 파일이나 로그에 넣지 않는다.

## Health API

### 요청

```http
GET /health
```

### 응답

```json
{
  "status": "ok"
}
```

이 API는 프로세스가 응답 가능한지 확인하는 Liveness 검사다. 데이터베이스나 Agent 연결 상태까지 검사하지 않는다. 해당 의존성이 추가될 때 별도의 Readiness 검사로 확장한다.

## 로컬 실행

```bash
cp .env.example .env
uv sync
uv run uvicorn orchestrator.main:app --app-dir src --reload
```

- Health: `http://127.0.0.1:8000/health`
- OpenAPI UI: `http://127.0.0.1:8000/docs`

## 완료 기준

- Python 3.10 이상에서 `uv.lock`에 고정된 의존성을 설치할 수 있다.
- A2A Protocol 버전(`1.0`)과 Python SDK 패키지 버전을 별도로 관리하며, SDK 해석 버전은 `uv.lock`에 고정한다.
- FastAPI 앱이 환경 설정을 읽어 실행된다.
- `GET /health`가 HTTP 200과 `{"status":"ok"}`를 반환한다.
- 이후 Workflow, API, A2A Client를 추가할 수 있도록 `src/orchestrator` 아래에 패키지 구조가 있다.

## 이번 단계에서 미포함

- DB 연결 및 저장소 모델
- Run 생성·조회 API
- A2A Client 및 Agent Card 조회
- Workflow 상태 머신
- Agent/MCP 서버
- 인증·배포 설정

## 검증

- 실행 명령: `PYTHONPATH=src uv run python -m unittest discover -s tests -p test_health.py -v`
- `GET /health`가 HTTP 200과 `{"status":"ok"}`를 반환하는지 확인.
- 실행 결과: 통과.

## 참고

- FastAPI 설치와 앱 실행은 [공식 FastAPI 튜토리얼](https://fastapi.tiangolo.com/tutorial/)을 따른다.
- 설정은 Pydantic Settings의 `BaseSettings`와 `env_prefix`를 사용한다. [공식 설정 문서](https://pydantic.dev/docs/validation/latest/concepts/pydantic_settings/)
