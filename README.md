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
POST /api/v1/runs/{runId}/workspace/provision
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

## Agent/MCP 개발 기반 — 15~17번

담당 범위를 1번 Orchestrator와 2번 Agent/MCP로 확장했습니다. `src/agents/`에는 공통 A2A 서버·설정이, `src/mcp_tools/`에는 설정·역할별 Tool 정책 선언이 있습니다. 실제 역할별 LLM 작업, MCP 실행, Sandbox·파일 권한 강제는 아직 구현하지 않았습니다. 3번 웹 서비스와 4번 평가 모듈은 변경하지 않습니다.

설정 접두사는 `ORCHESTRATOR_`, `AGENT_`, `MCP_`로 분리합니다. 각 Agent는 하나의 역할을 명시하며 기본 주소는 Planner `127.0.0.1:8101`, Developer `:8102`, QA `:8103`, Security `:8104`입니다. 기존 Orchestrator의 Agent URL은 실제 역할 구현·연결 전까지 미설정 상태로 유지하세요. 지금 연결하면 기본 런타임이 요청을 `TASK_STATE_REJECTED`로 거절하며 제품을 개발하지 않습니다.

기존 의존성을 설치한 환경에서는 다음 코드로 설정 객체만 확인할 수 있습니다. 서버·DB·LLM·MCP subprocess를 실행하지 않습니다.

```python
from agents.core.config import AgentSettings
from mcp_tools.core.config import MCPSettings
from orchestrator.domain.states import AgentRole

agent = AgentSettings(role=AgentRole.DEVELOPER, _env_file=None)
tools = MCPSettings(role=agent.role, _env_file=None)
print(agent.agent_base_url)  # http://127.0.0.1:8102
print(tools.allowed_tool_names)  # 실제 Tool 실행이 아닌 정책 선언
```

`_env_file=None`은 `.env` 파일만 생략하며 프로세스 환경변수는 계속 적용됩니다. 모델·Provider는 기본 미설정이며, 인증값은 설정 객체의 일반 출력/직렬화에서 제외합니다. 상세 변경·정의서 점검·후속 번호는 [15번 Agent/MCP 개발 기반](docs/15-agent-mcp-bootstrap.md)을 참고하세요.

### 공통 Agent 서버 실행

기존 의존성이 설치된 `.venv`에서 프로젝트 루트를 기준으로 실행합니다. 역할에 따라 설정된 포트로 시작합니다.

```bash
AGENT_ROLE=PLANNER PYTHONPATH=src .venv/bin/python -m agents
```

Planner의 `/health`, `/.well-known/agent-card.json`, `/docs`는 `http://127.0.0.1:8101`에서 확인합니다. `/docs`의 `POST /message:send` 예시에 `A2A-Version: 1.0`을 넣어 실행한 뒤 반환된 `task.id`로 `GET /tasks/{id}`를 호출하면 `TASK_STATE_REJECTED`와 `AGENT_RUNTIME_NOT_CONFIGURED` 사유를 확인할 수 있습니다. `SUBMITTED`가 먼저 보일 수 있으므로 다시 조회하세요.

Task·Context·요청 기록은 `.data/agents/{역할소문자}.sqlite3`에 영속 저장합니다. 같은 `messageId`와 같은 요청을 재전송하면 기존 Task만 반환하며 다시 실행하지 않습니다. 재시작으로 중단된 진행 Task는 `FAILED / AGENT_EXECUTION_INTERRUPTED`로 기록하고 자동 재실행하지 않습니다. 입력/인증 대기 Task는 명시적 새 Message로 같은 Task를 이어갈 수 있습니다.

`AGENT_DATABASE_PATH`로 운영자 DB 경로를 바꿀 수 있지만 **같은 DB는 한 Agent 프로세스만 사용**합니다. 여러 worker나 서로 다른 역할이 DB를 공유하지 마세요. 제품/Orchestrator DB와도 분리합니다. `AGENT_BEARER_TOKEN`을 설정하면 Card·Task API에 HTTP Bearer 인증을 강제합니다. API Key·Token을 요청 본문에 넣지 않습니다. HTTP API는 [16번](docs/16-common-a2a-agent-server.md), 생명주기는 [17번](docs/17-agent-task-lifecycle.md), 최신 계약 보완·검증·남은 이슈는 [17번 점검 후 보완](docs/17-contract-audit-fixes.md)을 참고하세요.

### 역할별 Prompt와 출력 계약

Planner·Developer·QA·Security의 책임/금지/입출력/Tool 선언은 `agents.roles`에 있습니다. 신뢰된 서비스 역할로 `build_system_prompt()`를 선택하고 `prepare_role_prompt()`로 정적 System 규칙과 정제된 JSON 입력을 분리합니다. 완료 출력은 `validate_completed_role_output()`이 기존 A2A Artifact 파서와 보호된 기준·Snapshot·실행 근거를 확인합니다.

이 단계는 실제 LLM/MCP 실행이나 ACL 강제가 아닙니다. 기본 Agent는 여전히 미구현 작업을 거부합니다. 사용법·Schema·정의서 점검·검증 결과는 [18번 역할 Prompt와 출력 계약](docs/18-role-prompts-output-contracts.md)을 참고하세요.

### 공통 LLM 실행 엔진 — 19번

`agents.llm`에 Provider 인터페이스·선택 가능한 OpenAI Responses 어댑터·JSON 응답 검증·역할별 Tool Loop·공유 예산·메모리 사용량 기록을 추가했습니다. Provider/모델/temperature/API Key는 직접 설정해야 하며 자동 모델 선택·fallback·실행 재시도를 하지 않습니다. 실제 API 호출은 설정된 어댑터를 명시적으로 실행할 때만 발생합니다.

이 엔진은 아직 기본 A2A 서버에 연결하지 않았습니다. 역할별 Executor·실제 MCP·Workspace/Sandbox·영속 Trace는 후속 단계입니다. 설정·사용법·한계·정의서 점검은 [19번 공통 LLM 실행](docs/19-llm-runtime.md)을 참고하세요.

### 실제 Workspace와 경로 권한 — 20번

Run 생성은 기존처럼 ID·Metadata만 저장합니다. `POST /api/v1/runs/{runId}/workspace/provision`을 호출하면 서버에 등록된 해당 workspace를 실제로 준비합니다. 기존 Source/출력은 덮어쓰지 않으며, 준비되지 않았거나 소유권이 충돌하는 폴더는 Agent 접근 대상으로 삼지 않습니다.

`WorkspaceRegistry.bind()`의 신뢰된 역할별 접근기는 Developer의 `source/`, QA의 `outputs/qa/`, Security의 `outputs/security/`, Planner의 `planning/` 쓰기만 허용합니다. Snapshot 쓰기·Host 경로·Traversal·Secret 경로·Symlink 쓰기·Hardlink 및 비정규 파일 접근은 차단합니다. 실제 Snapshot 저장/불변성·Container/MCP 강제는 아직 후속 단계입니다. 상세 사용법·정의서 점검은 [20번 실제 Workspace Registry](docs/20-workspace-registry-permissions.md)를 참고하세요.

## 테스트

```bash
PYTHONPATH=src uv run --frozen python -m unittest discover -s tests -v
```

이미 의존성이 설치된 `.venv`에서는 `PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -v`로도 실행할 수 있습니다. 이 명령은 unittest 기반 Orchestrator/Agent/MCP 검증용이며, pytest 함수 기반 평가 테스트 전체를 실행하는 명령은 아닙니다.

## 평가 모듈

평가 모듈은 `src/evaluation/`에 있으며 공통 `pyproject.toml`, `uv.lock`, `.venv`를 사용합니다.
[평가 안내](src/evaluation/README.md)와 [평가 개발진행사항](docs/evaluation/development_progress.md)을 참고하세요.

```bash
uv sync --frozen
uv run --frozen python -m pytest
uv run --frozen python -m evaluation.report_demo
```
