# A2A Orchestrator

Python 3.10+와 FastAPI를 사용하는 Orchestrator 서비스입니다.

현재 역할 1·2 구현의 사용법·설정·API/Artifact 인계 기준은 [45번 플랫폼 인계 안내](docs/45-owned-platform-handoff.md)에 정리했습니다. **44번은 준비 점검만 완료했고 실제 LLM·Docker 최소 Run 실행은 아직 미수행**입니다. 문서 정리 완료와 실제 시연 성공은 구분합니다.

45번 이후 점검의 Source 비밀정보 반입, Python 3.10 deadline 호환성, RunConfiguration URL Credential, Agent DB WAL 시작 경쟁은 [46번 점검 보완](docs/46-platform-audit-hardening.md)에 기록했습니다. 기존 비밀정보 포함 불변 데이터는 자동 변경하지 않으며 새 검사에서 거부될 수 있습니다.

## 기본 Orchestrator 실행 — Bootstrap

`uv`가 설치되어 있다면 프로젝트 루트에서 다음을 실행합니다.

```bash
uv sync --frozen
uv run uvicorn orchestrator.main:app --app-dir src --reload
```

기본 주소는 `http://127.0.0.1:8000`입니다.

이 명령은 실제 네 Agent·승인 Tool·공유 예산을 조립하는 Owned Host 실행 명령이 아닙니다. 실제 Host는 [45번 실행 안내](docs/45-owned-platform-handoff.md)에 따라 승인 설정 검사 후 명시적으로 시작합니다.

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
GET /api/v1/runs/{runId}/usage
GET /api/v1/runs/{runId}/telemetry
POST /api/v1/runs/{runId}/resume
POST /api/v1/runs/{runId}/recover
POST /api/v1/runs/{runId}/cancel
GET /docs
```

기본 Orchestrator 설정은 환경변수 또는 프로젝트 루트의 `.env` 파일에서 읽습니다. 시작값은 `.env.example`을 참고하세요. Owned Host의 승인 설정은 이와 다르며 `.env`를 자동으로 읽거나 해당 환경변수로 덮어쓰지 않습니다.

현재 구현은 SCN-001 고정 기준, Planner → Developer → QA/Security 실행, 최대 3회 수정·새 Snapshot 재검증, 불변 Artifact/Issue/Trace 저장 및 최종 판정입니다. Run 생성 때 workspace ID와 Scenario 정책·실행 설정을 동결하고, Build/QA/Security의 동일 Manifest 및 MCP 실행·재시도 근거가 확인되어야 `SUCCESS` 또는 `UNVERIFIED`를 확정합니다. 비밀정보는 전송·저장·로그 경계에서 마스킹하고 Agent 인증은 HTTP 헤더로만 전달합니다. 최신 변경과 제한은 [14번 정의서 준수 보완](docs/14-orchestrator-contract-compliance.md)을 참고하세요.

현재 등록된 `scenarioId`는 `f7f9e5c3-ffc3-4b3f-918b-21e1b956ce76`입니다. 다른 UUID와 공백뿐인 `requestText`는 422를 반환합니다. `/api/v1/scenarios`에서 등록 기준을 확인할 수 있습니다.

재개는 저장된 Task/Context를 이어 사용하며 수정 횟수를 늘리지 않습니다. 전송됐으나 Task ID가 확인되지 않은 요청은 자동 재전송하지 않습니다. 진행 중인 다른 제어 작업과 충돌하면 409이며, 원격 Task 취소는 실제 `CANCELED` 확인 후 기록합니다. PID 기반 잠금은 단일 호스트 MVP용입니다.

Build/Test/Scan의 실제 MCP 호출은 Agent/MCP 담당 범위입니다. Orchestrator는 전달된 실행 근거를 검증·저장합니다. 20~22번에는 Workspace·Artifact·Container 실행 기반, 23번에는 MCP stdio 통신·Schema·역할 권한, 24번에는 실제 파일 Tool, 25번에는 Container Build, 26번에는 Python unittest, 27번에는 Playwright 브라우저 실행, 28번에는 Bandit 정적 보안 검사, 29번에는 실제 호출 원장·오류 분류·안전 Retry 어댑터를 추가했습니다. 30~33번 실제 네 Agent 실행기와 기존 Dispatcher의 명시적 Host 연결은 34번에서 추가했습니다. 기본 Bootstrap 명령은 그대로이며 실제 Docker·회원가입 코드의 성공·비교 실험 완료를 뜻하지 않습니다.

자동 QA/Security dispatch를 시험하려면 `.env`에 `ORCHESTRATOR_PLANNER_AGENT_URL`, `ORCHESTRATOR_DEVELOPER_AGENT_URL`, `ORCHESTRATOR_QA_AGENT_URL`, `ORCHESTRATOR_SECURITY_AGENT_URL`을 모두 설정합니다. QA/Security 중 하나라도 설정되지 않으면 Snapshot과 pending Step은 보존하고 Run을 `HUMAN_REVIEW`로 전환합니다.

기본 DB 경로는 `.data/orchestrator.sqlite3`이며 `ORCHESTRATOR_DATABASE_PATH` 환경변수로 바꿀 수 있습니다. `.data/`는 Git에서 제외됩니다.

## Agent/MCP 개발 기반 — 15~17번

아래 번호별 설명은 해당 단계의 작업 이력입니다. 초기 단계의 미연결 상태와 현재 실행 구성을 혼동하지 않도록 최신 사용법은 [45번](docs/45-owned-platform-handoff.md)을 기준으로 읽으세요.

담당 범위를 1번 Orchestrator와 2번 Agent/MCP로 확장했습니다. `src/agents/`에는 공통 A2A 서버·설정이, `src/mcp_tools/`에는 stdio 서버·Client·역할별 실제 Tool이 있습니다. 공통 LLM 엔진과 Workspace/Artifact/Sandbox를 바탕으로 Planner·Developer·QA·Security의 명시적 실행기와 34번 단일 Host 연결을 추가했습니다. 기본 Agent CLI는 Bootstrap을 유지합니다. 3번 웹 서비스와 4번 평가 모듈은 변경하지 않습니다.

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

`WorkspaceRegistry.bind()`의 신뢰된 역할별 접근기는 Developer의 `source/`, QA의 `outputs/qa/`, Security의 `outputs/security/`, Planner의 `planning/` 쓰기만 허용합니다. Snapshot 쓰기·Host 경로·Traversal·Secret 경로·Symlink 쓰기·Hardlink 및 비정규 파일 접근은 차단합니다. 24번의 실제 파일 Tool은 이 권한과 Schema를 사용해 읽고 씁니다. `write_test_file`은 QA의 `outputs/qa/tests/`만 허용합니다. 상세 사용법·정의서 점검은 [20번 실제 Workspace Registry](docs/20-workspace-registry-permissions.md)와 아래 24번을 참고하세요.

### 실제 Snapshot/Artifact 내용 저장 — 21번

`ArtifactStore`는 등록된 Workspace의 Git full Commit에서 실제 Tree/Blob을 읽어 정규화한 `source.tar`와 SHA-256을 만듭니다. 동결 Run 환경과 실제 Commit의 Dependency Lock Hash가 일치해야 저장합니다. 실제 Archive/보고서 JSON과 읽기 권한은 기존 SQLite의 불변 BLOB 테이블에 저장하고, 매 읽기마다 내용·Metadata Hash 및 Run/역할 권한을 검사합니다. Host 경로나 외부 Artifact URL을 다운로드하는 기능은 없습니다.

이 저장은 Build 전 후보 준비이며 기존 A2A 완료 등록·Workflow 성공 판정과 구분합니다. QA/Security는 같은 Snapshot을 읽기 전용으로 받습니다. Container 기반은 아래 22번에 추가했으며, MCP·역할 Executor·기존 Dispatch의 실제 연결은 아직 후속 작업입니다. 사용법과 정의서 점검은 [21번 실제 Snapshot/Artifact 저장소](docs/21-snapshot-artifact-store.md)를 참고하세요.

### Container Sandbox — 22번

`SandboxRuntime`은 실제 Run/Step·역할 권한·Source Hash를 확인하고, 검증된 Snapshot과 선택적 Host 입력만 Workspace 내부의 전용 실행 폴더에 준비합니다. Host가 선택한 고정 실행 Profile을 읽기 전용 Root/Source, 외부 Network 차단, 비특권 사용자, CPU/Memory/PID/시간/출력 제한이 적용된 일회용 Docker Container에서 실행하도록 구현했습니다. 실행 전 실제 Container 설정을 검사하며, 종료·타임아웃·취소 시 소유권이 확인된 Container와 준비 폴더만 정리합니다. 임의 Shell Tool이나 Host에서 생성 코드를 실행하는 fallback은 없습니다.

현재 환경에는 Docker가 없어 실제 Container 실행은 검증하지 못했습니다. 테스트는 실제 Git/SQLite/파일과 Fake Docker 통신을 사용하며, 제품 Build/Test PASS를 뜻하지 않습니다. Docker·고정 이미지가 준비되지 않으면 오류로 중단합니다. MCP `run_build`는 25번에서 연결했지만 기본 Agent 연결은 후속이며 자원 수치도 팀 확정 전 임시 제한입니다. 사용법·정의서 점검·검증 한계는 [22번 Container Sandbox](docs/22-container-sandbox.md)를 참고하세요.

### MCP stdio 통신·Tool 계약·역할 권한 — 23번

공식 Python MCP SDK v2를 사용해 고정된 `2026-07-28` 버전의 로컬 자식 프로세스와 실제 `server/discover` → `tools/list` → `tools/call` 통신을 구현했습니다. Host가 Agent 역할·Run·Workspace를 고정하며, 서버는 역할별 목록 필터뿐 아니라 직접 호출 권한·JSON Schema·Registry 소유권도 검사합니다. 10개 Tool의 입출력 계약과 실제 stdio CLI Handler를 24~28번에서 연결했습니다. 실행에 필요한 Host 설정/환경이 없으면 오류를 반환하며, 기본 Agent에 실제 역할 실행기를 연결한 것은 아닙니다.

실제 stdio 자식 프로세스의 네 역할 통신과 종료를 확인했습니다. 임의 Shell·자동 Retry·기본 Agent 연결·제품 개발/검증 완료는 포함하지 않습니다. 해당 단계의 사용법·정의서 점검·검증 결과는 [23번 MCP stdio 및 역할 권한](docs/23-mcp-stdio-role-enforcement.md)을 참고하세요.

### 실제 파일 Read/Write/Patch Tool — 24번

`read_project_file`, `write_source_file`, `write_test_file`, `apply_patch`에 실제 파일 처리를 연결했습니다. Developer는 Working Source를 읽고 쓰며, QA/Security의 Source 읽기는 Host가 지정한 불변 Artifact bytes에서만 수행합니다. 내부 symlink로 Working Source 읽기를 우회하는 경로도 차단합니다. 파일 Hash·크기·동일 내용 여부를 실제 bytes로 계산하고, expected Hash와 Patch 기준 Snapshot을 검증합니다.

쓰기와 Patch는 역할 제한·FD 기반 경로 검사·잠금·원자적 파일 교체를 사용합니다. 여러 파일 Patch는 사전 검증 및 일반 실패 rollback을 지원하지만 프로세스 crash까지 원자적으로 복구한다고 보장하지 않습니다. 실제 stdio 파일 호출을 검증했으며 제품 코드 실행이나 Git commit은 하지 않습니다. 상세 사용법·정의서 점검·한계는 [24번 파일 Tool](docs/24-file-read-write-patch.md)을 참고하세요.

### 실제 Build Tool·불변 실행 기록 — 25번

`run_build`를 실제 Snapshot·Container Sandbox와 연결했습니다. 명령·자원·로컬 Docker endpoint는 신뢰된 Host 설정으로 고정하며 Tool 입력으로 선택할 수 없습니다. 정상 실행의 종료 코드·시간·정제된 stdout/stderr·ExecutionManifest·실제 Profile을 SQLite에 함께 저장한 뒤 참조를 반환합니다. 저장 시점에도 현재 Run/Developer Step/Source/환경을 재검사하고 불변 기록의 덮어쓰기를 차단합니다.

컴파일 오류의 비영 종료는 실제 Tool 결과이며, Container 시작/시간 초과/정리/저장 실패는 실행 오류입니다. Docker가 없는 환경에서 실제 Container 성공을 검증했다고 주장하지 않습니다. 설정 없는 Build도 fail-closed이며 기본 Agent는 여전히 자동 실행되지 않습니다. 상세 사용법·정의서 점검·검증 한계는 [25번 Build Tool](docs/25-build-tool.md)을 참고하세요.

### Unit Test Tool·불변 보고서 읽기 — 26번

`run_unit_tests`와 `read_test_report`를 연결했습니다. 신뢰된 Host가 Python unittest Scope를 고정하고, Developer의 Snapshot 테스트·QA 작성 테스트·Host 보호 테스트를 분리합니다. 같은 Frozen Source와 읽기 전용 테스트 입력을 Container에서 실행하도록 연결하며, 실제 case/count/종료 코드가 일치하는 보고서만 Input Hash·Manifest·Profile과 함께 불변 저장합니다. 실패한 assertion은 Tool 성공+failed 건수, 실행기 오류·빈 suite·잘못된 보고서는 실행 오류입니다.

Unit Tool은 **Python unittest만 지원**하며 pytest/Jest·독립 비교평가는 포함하지 않습니다. Unit 결과는 QA 최종 Verdict나 A2A Artifact가 아니고 기본 Agent도 아직 자동 실행되지 않습니다. 실제 Docker 검증은 미완료입니다. 사용법·정의서 점검·검증 한계는 [26번 Unit Test Tool](docs/26-unit-test-tool.md)을 참고하세요.

### Browser Test Tool·안전한 단계 Trace — 27번

QA의 `run_browser_tests`를 Frozen Source·Container 전용 Playwright/Chromium Runner와 연결했습니다. Host가 승인한 서비스를 같은 Container의 단일 local origin에서 실행하며, QA/Host 보호 JSON Suite는 이동·입력·클릭·문구/표시/URL 검증의 6개 동작만 허용합니다. 실제 Case/Step 결과·입력 Hash·Host Profile·Manifest를 함께 불변 저장하고 `traceRefs`를 반환합니다. 기존 `read_test_report`는 Unit/Browser 보고서를 모두 조회합니다.

Trace는 원문 DOM·입력값·스크린샷 없는 정제 JSON 단계 기록이며 Playwright Trace Viewer ZIP은 아닙니다. Chromium Sandbox/외부 Network 차단은 완화하지 않습니다. 실제 Docker·Chromium 실행과 팀원 서비스 연결은 미검증/미구현이며 기본 Agent도 자동 실행되지 않습니다. 상세 범위·정의서 점검·실행환경 한계는 [27번 Browser Test Tool](docs/27-browser-test-tool.md)을 참고하세요.

### Security Scan Tool·불변 보안 검사 기록 — 28번

Security의 `run_security_scan`과 `read_security_report`를 Frozen Source·Container 전용 Bandit AST 검사·불변 보고서 Store에 연결했습니다. Host가 정확한 Scanner 버전·Rule·Profile 참조를 선택하고, 동결 Run 설정 및 Source/Step/grant/환경을 검사합니다. 모든 Python 파일을 검사하며 검사 누락·문법/Plugin 오류·0 Python 파일은 정상 결과가 아닙니다.

Scanner 경고는 모두 `SUSPECTED`로 반환하고, 코드 snippet·원본 설명·비밀값을 보고서에 남기지 않습니다. 경고0개로 보안 PASS나 전체 SUCCESS를 만들지 않습니다. Python 정적 분석만 지원하며 실제 Docker/Bandit 실행·제품 보안 검증과 기본 Agent 연결은 미완료입니다. 상세 계약·정의서 점검·한계는 [28번 Security Scan Tool](docs/28-security-scan-tool.md)을 참고하세요.

### Tool 실행 근거·오류 분류·안전 Retry — 29번

선택적 `TrackedMCPExecutor`가 실제 MCP Client 호출 전에 불변 Attempt를 기록하고, 승인된 오류 및 전달 상태에 따라 동일 논리 호출을 최대 2회 재시도합니다. 정상 Build/QA 실패·Scanner 경고는 제품 결과이므로 반복하지 않고, 불명확한 Timeout/Write/Patch는 기본적으로 재전송하지 않습니다.

성공 근거는 현재 claim 이후 저장된 동일 Source/Step/Tool/Profile의 실제 실행 기록과 대조합니다. 과거 결과·다른 검사 설정·중복 실행 기록을 새로운 근거로 연결하지 못하며 입력/원문 코드/SDK 오류를 원장에 저장하지 않습니다. 기본 Agent·제품 Artifact·최종 Verdict·실시간 통합 Trace 연결은 별도입니다. 사용법·정의서 점검·한계는 [29번 실행 근거와 안전 Retry](docs/29-tool-evidence-safe-retry.md)를 참고하세요.

### Planner Agent — 30번

Host가 명시적으로 구성한 `PlannerAgentExecutor`가 기존 LLMEngine으로 작업 계획을 제안받고, 동결된 Requirement UUID·기준을 그대로 보존하여 기존 `requirements.json` Artifact를 반환합니다. 전체 요구사항 coverage·Task 의존성·Schema를 검증하며 설명/인증 대기는 Artifact를 만들지 않고 interrupted 상태로 반환합니다. Planner에는 MCP Tool·Source 수정·제품 최종 판정 권한이 없습니다.

실제 Planner를 `create_app(..., executor=planner)`에 주입한 경우만 Planner Skill과 `executionReady=True`를 광고합니다. 기본 CLI/서버는 여전히 Bootstrap이며 모델 환경변수만으로 자동 실행하지 않습니다. 동결 모델·공유 예산·Host DB 입력이 필요하고 기본 Pipeline 연결 및 전체 Run 예산/Trace 영속화는 후속 작업입니다. 상세 사용법·정의서 점검·검증 한계는 [30번 Planner Agent](docs/30-planner-agent.md)를 참고하세요.

### Developer Agent — 31번

Host가 명시적으로 구성한 `DeveloperAgentExecutor`가 MCP로 할당된 Source를 구현하고, 실제 파일 diff·Git commit object·불변 Snapshot·private Build/Tool 실행 기록을 대조하여 기존 Source/Change/Build Artifact 세 개를 반환합니다. 모델은 제한된 Decision만 제안하며 Hash·변경 목록·Build 근거를 작성하지 않습니다. 정상 실행된 컴파일 실패는 Tool PASS + Build FAIL/PRODUCT로 보고하고 전체 SUCCESS로 바꾸지 않습니다.

31번의 최초 구현에 더해 35번에서 저장된 Issue 기반 수정과 `codeVersion=2~4`를 지원합니다. 기본 서버는 Bootstrap 유지, 실제 Developer를 주입할 때만 실행 Skill을 광고합니다. 이미 Source를 수정한 뒤 중단한 작업은 자동 원복/재실행하지 않습니다. 실제 LLM·Docker·Developer stdio 전체 경로는 미검증입니다. 최초 구현 설정은 [31번 Developer Agent](docs/31-developer-agent.md), 현재 수정 경계는 [35번 수정·재검증](docs/35-fix-revalidation-loop.md)을 참고하세요.

### QA Agent — 32번

Host가 명시적으로 구성한 `QAAgentExecutor`가 같은 불변 Source를 읽고 QA 전용 테스트를 작성하며, 승인된 Unit/Browser 테스트의 실제 private 실행 기록으로 기존 `qa-report.json` Artifact를 반환합니다. 모델은 테스트 케이스와 Requirement의 연결만 제안하고 결과·Manifest·ToolEvidence를 만들지 않습니다. 모든 승인된 QA 테스트와 Host 보호 테스트를 실행하며, 실패한 assertion은 Tool PASS + QA FAIL, 누락/SKIP 케이스는 UNVERIFIED로 구분합니다.

35번에서 최초 `VALIDATING`뿐 아니라 수정 후보의 `REVALIDATING`을 지원합니다. 실제 QA 실행기를 주입할 때만 Skill·`executionReady=True`를 광고하며 기본 CLI/서버는 Bootstrap을 유지합니다. QA는 Source·보호 테스트를 수정하거나 전체 Run을 판정하지 않습니다. 실제 LLM·Docker 실행은 미검증입니다. 초기 Host 설정은 [32번 QA Agent](docs/32-qa-agent.md), 현재 재검증 경계는 [35번 수정·재검증](docs/35-fix-revalidation-loop.md)을 참고하세요.

### Security Agent — 33번

Host가 명시적으로 구성한 `SecurityAgentExecutor`가 같은 불변 Source에서 승인 Scanner Profile 전체를 실행하고, 실제 Scan/Tool 기록과 읽은 코드 근거를 연결하여 기존 `security-report.json` 하나를 반환합니다. 모델은 분석 Draft만 제출하며 Scanner 경고를 누락하거나 Severity·실행 근거·최종 Verdict를 만들 수 없습니다. 경고 0개로 보안 PASS를 만들지 않고, 독립 Host 의미 검증 근거가 없으면 Requirement는 UNVERIFIED, 경고는 SUSPECTED로 남깁니다.

35번에서 최초 후보와 수정 후보 재검증을 지원하며 기본 CLI/서버는 Bootstrap을 유지합니다. 실제 실행기를 주입한 경우에만 Security Skill을 광고합니다. SCN-001의 모든 보안 기준을 입증하는 실제 의미 검증기와 LLM/Docker 실행은 미검증·미완료이며, 공통 Proof 연결 경계가 그 검증을 대신하지 않습니다. 초기 Host 설정은 [33번 Security Agent](docs/33-security-agent.md), 현재 재검증 경계는 [35번 수정·재검증](docs/35-fix-revalidation-loop.md)을 참고하세요.

### Orchestrator + 실제 네 Agent 연결 — 34번

`agents.platform.composition.create_platform`이 승인된 Host 저장소·설정·Provider·서비스 팩토리를 받아 기존 Orchestrator와 네 실제 실행기를 연결합니다. `RunBudgetRegistry`가 Run마다 예산 하나를 발급하고 모든 역할이 동일한 Deadline·누적 모델/Tool/토큰 사용량을 공유합니다. Run 설정 누락은 저장/스케줄 전에 422, 준비 실패는 Agent 호출 전에 `HUMAN_REVIEW`로 멈춥니다.

실행하려면 승인한 Host 모듈의 함수가 `OwnedAgentPlatform`을 반환해야 합니다. 아래 `approved_host:build_platform`은 사용자가 준비할 모듈/함수의 예시이며 저장소에 자동 생성된 기본 설정이 아닙니다.

```bash
PYTHONPATH=src .venv/bin/python -m agents.platform --factory approved_host:build_platform
```

한 프로세스/한 event loop에서 Agent 네 서버를 먼저 준비한 뒤 Orchestrator를 엽니다. 기본 포트는 8000 및 8101~8104입니다. 별도 Agent 프로세스 네 개나 reload/multi-worker로 실행하면 공유 예산이 보장되지 않으므로 이 연결 방식에 사용하지 않습니다. 시작/종료는 listener·공식 SDK Task Store·실행 Worker·Provider의 정리 순서를 지킵니다.

Host 준비 항목·사용법·검증 한계는 [34번 실제 Agent 연결](docs/34-owned-agent-platform.md)을 참고하세요. 34번의 최초 후보 제한은 아래 35번에서 확장했고, 명시적 Owned Host의 기존 Run 예산 영속 복원은 37번에서 추가했습니다. 기본 메모리 Registry는 재시작 Run을 자동 승인하지 않습니다.

### 실제 수정·동일 Snapshot 재검증 — 35번

기존 Orchestrator가 검증 가능한 결함과 `FIX_REQUIRED`를 기록하면 실제 Developer에게 저장된 Issue·이전 결과물을 전달합니다. Developer는 이전 후보를 기준으로 수정하고 새 Git Snapshot·Source/Change/Build 결과물을 생성합니다. QA와 Security는 새 후보의 동일 Manifest를 다시 검사하고 이전 보고서와 Lineage를 연결합니다. 최초 구현을 제외한 수정은 최대 3회이며 전체 예산·동결 요구사항·환경을 다시 발급하거나 변경하지 않습니다.

보안 의미 검증 근거가 부족하면 `UNVERIFIED/HUMAN_REVIEW`이고, 실제 QA FAIL이 함께 있어도 기존 판정상 `HUMAN_REVIEW`에 Issue를 남길 수 있습니다. 네 Task COMPLETED만으로 SUCCESS가 되지 않습니다. 실제 LLM·Docker·회원가입 서비스 시연 성공은 미검증입니다. 상세 계약·검증 결과는 [35번 수정·재검증](docs/35-fix-revalidation-loop.md)을 참고하세요.

### 실제 입력 재개·취소·Human Review 제어 — 36번

기존 `/resume`·`/recover`·`/cancel`을 실제 네 Agent 실행에 연결했습니다. 입력/인증 재개는 같은 Task·Context·Step에서 이어가고 수정 횟수·Source 버전·전체 예산을 초기화하지 않습니다. QA/Security 중 완료된 쪽은 다시 실행하지 않습니다. 새 실행이 필요한 재개는 기존 예산을 확인하며, GET만 필요한 결과 회수는 새 모델 호출과 구분합니다.

처음 시작하거나 재개한 실행을 추적·정리한 뒤 원격 Worker·모델·MCP·Host 작업이 종료되고 `CANCELED`가 확인돼야 Run을 `ABORTED`로 기록합니다. 불확실한 전송·Source 쓰기는 자동 재전송/원복하지 않으며, Human Review로 FAIL/UNVERIFIED를 임의 PASS로 바꾸지 못합니다. 상세 사용법·정의서 점검·검증 한계는 [36번 실제 제어](docs/36-agent-workflow-controls.md)를 참고하세요.

### 실제 Trace·사용량·영속 예산 — 37번

명시적 Owned Host에서 실제 모델 호출과 MCP 물리 시도를 Run/Step/Task/Context에 연결해 저장합니다. 기존 닫힌 Trace Schema는 유지하고 사용량·모델·Tool 상세는 별도 append-only 원장에 기록합니다. `/usage`와 `/telemetry`로 조회할 수 있으며 미연결 기본 서버는 503입니다. 알 수 없는 Token/비용은 `null`이고 Prompt·Source·Tool 원문은 Telemetry에 저장하지 않습니다. SDK 원문 로그도 생략합니다.

Run 예산 예약을 외부 호출 전에 저장하고 재시작 시 기존 호출/토큰·동결 설정·UTC Deadline만 복원합니다. pending·unknown·누락 기록·설정 변경·시간 역행은 실행을 차단하며 새 예산이나 자동 재전송을 만들지 않습니다. 1~37번 번호 로드맵의 마지막 단계이며 실제 외부 LLM/Docker·제품 시연/비교 실험 완료를 의미하지 않습니다. 상세 계약과 검증 한계는 [37번 실행 Telemetry](docs/37-runtime-telemetry-usage.md)를 참고하세요.

### 개발정의서 전체 점검 후 보완

같은 Run의 보호 테스트 내용·연결 기준과 Scanner Version/Rule을 최초 검증 승인 시 불변 Hash로 고정하고 재개·수정·재시작 때 변경을 차단합니다. 동일 후보의 중복 Finding은 반복 결함 카운트를 초기화하지 않으며, 최종 판정은 Trace를 페이지별로 읽어 로그 개수 때문에 정상 성공을 막지 않습니다.

Orchestrator에는 네 LLM 역할과 별개인 Host 전용 MCP 주체를 추가해 `run_build`, `read_test_report`, `read_security_report`만 허용합니다. A2A Task 응답에 프로젝트 metadata가 포함되면 현재 Run/Step과 대조하며 공식 선택적 metadata와 unrelated extension 호환성은 유지합니다. 기존 보류 항목·실제 환경 검증·운영 Host 준비를 완료한 것으로 표시하지 않습니다. 상세 내용은 [개발정의서 보완 내역](docs/contract-compliance-audit-fixes.md)을 참고하세요.

### 실제 실행용 Host 설정 — 38번

기존 1~37번 구현 이후의 실행 준비 단계입니다. `configs/owned-runtime.example.json`에서 네 Agent와 Orchestrator의 주소·서로 다른 DB·공통 모델/한도를 선언하고, API Key/Bearer는 환경변수 이름으로만 참조합니다. 모델 ID·temperature·실행 시간은 미선택 `null`이며 원본 예시는 의도적으로 설정 검증에 실패합니다. 운영자가 값을 정해 `configs/owned-runtime.local.json`에 저장하며 이 파일은 Git에서 제외합니다.

```bash
PYTHONPATH=src .venv/bin/python -m agents.platform.check_configuration --file configs/owned-runtime.local.json
```

`--check-secrets`를 추가하면 process environment에 선언된 비밀값이 있는지만 확인합니다. `.env`는 자동으로 읽지 않으며 기존 `AGENT_*`/`ORCHESTRATOR_*`로 구성이 덮어써지지 않습니다. 이 명령은 DB·서버·LLM·MCP·Docker를 시작하지 않고 `executionReady=false`를 유지합니다. Docker/Profile·Workspace·실제 Host factory는 후속 39~41번입니다. 설정 계약·사용법·개발정의서 점검은 [38번 실행 설정](docs/38-owned-runtime-configuration.md)을 참고하세요.

### 공통 Docker 이미지·실행 도구 준비 — 39번

`configs/owned-tools.example.json`은 같은 Image Digest·Dependency Lock·Hardware와 기존 Build/Unit/Browser/Security 설정을 묶는 Host 전용 예시입니다. 이미지·Version·argv·자원 제한은 운영자 승인 전 `null`이며 원본 그대로는 실패합니다. 로컬 복사본 `configs/owned-tools.local.json`에 승인값을 넣고 검사합니다.

```bash
PYTHONPATH=src .venv/bin/python -m agents.platform.check_tools --file configs/owned-tools.local.json
```

검사는 설정과 실제 Lock Hash까지만 확인하고 `executionReady=false`, `DockerChecked=false`를 반환합니다. Docker/API/제품 Build/Test를 실행하지 않습니다. `docker/sandbox/Dockerfile`은 사전 준비한 승인 Base·Hash 고정 Wheel·Chromium을 위한 별도 Recipe이며 아직 실제 이미지를 만들지 않았습니다. 자세한 준비 항목·역할별 정책·격리 경계는 [39번 이미지·Tool 설정](docs/39-sandbox-image-tool-profiles.md)을 참고하세요.

### 승인된 시작 Source와 Run Workspace 준비 — 40번

`configs/owned-source.example.json`에 시작 Repository·전체 Commit ID·정규 Snapshot Hash·Lock 경로를 명시하는 Host 설정을 추가했습니다. 미승인 값은 `null`이며 원본 그대로는 실패합니다. 로컬 복사본 `configs/owned-source.local.json`에 승인값을 넣고 39번 Tool 설정과 함께 검사합니다.

```bash
PYTHONPATH=src .venv/bin/python -m agents.platform.check_source --file configs/owned-source.local.json --tools configs/owned-tools.local.json
```

이 명령은 실제 Git/Lock을 읽기 전용으로 검증하고 `executionReady=false`, `workspacePrepared=false`를 반환합니다. Host용 `RegisteredSourcePreparer`는 동결한 시작 기준과 일치하는 등록 Run의 빈 Source에만 코드와 최소 Git object를 준비합니다. 기존 파일·불명확한 중간 실패는 덮어쓰거나 자동 재실행하지 않습니다. 실제 Host 연결과 공유 Run deadline 전달은 41번 작업입니다. 사용법·검증·한계는 [40번 시작 Source 준비](docs/40-approved-starting-source.md)를 참고하세요.

### 실행 설정 기반 Host factory — 41번

38번 Runtime·39번 Tool·40번 Source 설정을 `agents.platform.host.create_host`에서 기존 Orchestrator와 네 실제 Agent 실행기에 연결합니다. 먼저 아래 명령으로 검사합니다. 기본 모드는 읽기 전용이며 DB·서버·LLM·MCP·Docker를 시작하지 않습니다.

```bash
PYTHONPATH=src .venv/bin/python -m agents.platform.run_host --runtime configs/owned-runtime.local.json --tools configs/owned-tools.local.json --source configs/owned-source.local.json
```

`--check-secrets`는 선언된 환경변수의 존재만 추가 검사합니다. 승인 설정·시작 코드·이미지·비밀값을 준비한 후 같은 명령에 **`--start`**를 붙여야 다섯 서버가 시작합니다. 시작 자체가 Run 제출이나 제품 성공을 뜻하지 않으며, Run을 제출하면 실제 API 비용과 Docker 실행이 발생할 수 있습니다. 단일 프로세스/이벤트 루프로 실행하고 reload/multi-worker는 사용하지 않습니다.

모델·예산·공통 환경·시작 Snapshot·검사 기준이 승인 설정과 다르면 Run 저장 전에 거절합니다. 같은 Run의 Tool 정책은 불변 Hash로 고정하며, 준비·실행·재개에 같은 시간 예산을 사용합니다. 보호 QA selector가 있으면 `--qa-cases configs/owned-qa-cases.local.json`으로 기존 Requirement ID에 연결한 Host 설정도 필요합니다. Security 의미 증거가 없는 기본 연결은 `UNVERIFIED/HUMAN_REVIEW`를 유지합니다.

API 제출용 승인 `configuration` 확인법, cleanup·재시작 경계와 개발정의서 점검은 [41번 Host factory](docs/41-owned-host-factory.md)를 참고하세요. 팀원 제품·독립 평가 연결, 실제 Docker/외부 LLM 실행 검증은 이번 단계에서 수행하지 않았습니다.

### 승인된 Security 의미 검증기·신뢰 증거 — 42번

`load_approved_assessor()`로 Host가 직접 제공한 독립 함수의 기준·버전·Scenario 계약 Hash·실제 구현 파일/로드된 코드 Hash를 검사합니다. `create_host(proof_verifier=approved)`는 이 승인 정보를 같은 Run의 불변 Host 정책에 포함하고, 현재 Source bytes·실제 Scanner/Tool 기록에 연결한 새 요청 ID/Hash와 별도 불변 의미 검증 Receipt를 확인합니다. raw callback은 이 factory에서 거절하며 JSON/CLI/모델의 임의 import는 없습니다.

`configs/owned-security-approval.example.json`은 미승인 예시이므로 원본은 실패합니다. 실제 승인 기준·검증 함수는 운영자가 별도로 제공해야 하며, 이번 작업은 회원가입 보안 테스트나 제품 PASS 검증을 구현한 것이 아닙니다. 기본 CLI는 검증기 없는 `UNVERIFIED/HUMAN_REVIEW`를 유지합니다. Scanner 경고 0개·모델 설명·QA PASS를 Security PASS로 바꾸지 않습니다. 상세 연결·Receipt 조회·신뢰 경계·개발정의서 점검은 [42번 Security 의미 검증 연결](docs/42-approved-security-semantic-proof.md)을 참고하세요.

### 승인 가격표 기반 LLM 비용 원장 — 43번

실측 토큰·실제 응답 모델/처리 등급과 운영자가 승인한 가격표를 연결합니다. 같은 Run의 가격표/계산 버전을 고정하며, 호출별 불변 영수증을 기존 `/usage`·`/telemetry`에서 조회합니다. 금액은 정확한 USD **소수 문자열**입니다. 캐시/추론 토큰을 중복 계산하지 않고 네 역할·실패/수정/재개 호출을 모두 합산합니다. 가격/Usage/대기 중인 호출이 불명확하면 전체 `costUsd=null`이고 `knownCostUsd`는 확인된 부분 합일 뿐입니다. 비용 확인은 최종 SUCCESS를 의미하지 않습니다.

41번 검사 명령에 `--price-book configs/owned-price-book.local.json`을 선택적으로 추가합니다. 기본 검사는 계속 읽기 전용이며 가격표를 지정하지 않으면 비용은 기존처럼 `null`입니다. `configs/owned-price-book.example.json`은 미승인 형식 예시라 원본은 거절합니다. 운영 모델/가격은 임의로 설정하지 않았고 자동 가격 조회·과거 Run 비용 보충·실제 API/Docker 실행도 하지 않았습니다. 승인 가격표의 global text 토큰 계산과 실제 청구 금액 검증은 구분합니다. 설정 필드·동결/사용량 기록 경계·개발정의서 점검은 [43번 LLM 비용 계산](docs/43-approved-llm-cost-accounting.md)을 참고하세요.

### 자체 실제 실행 준비 점검 — 44번, 실행 미수행

44번 점검 당시 승인된 로컬 Runtime/Tool/Source/가격표와 Host 자격증명이 없었고, 점검한 shell PATH에서 Docker CLI를 찾지 못했습니다. 실제 유료 LLM·Docker·작은 Run 실행은 아직 수행하지 않았습니다. 미승인 예시가 검사에서 거절되는 것을 실행 성공으로 취급하지 않습니다. 필요한 사용자 결정/환경 준비와 실행 순서는 [44번 준비 점검](docs/44-owned-live-smoke-readiness.md)을 참고하세요.

### 역할 1·2 최종 정리 및 인계 — 45번

[45번 플랫폼 인계 안내](docs/45-owned-platform-handoff.md)에 구현 위치·번호별 이력, Bootstrap/Owned Host 차이, 승인 설정·실행 명령, Run 조회·Artifact/Manifest 계약, 팀원 인계 경계와 미검증/보류 항목을 정리했습니다. 사용자 요청에 따라 실제 실행보다 문서를 먼저 정리했으며, **다음은 여전히 44번 실제 실행 준비·확인**입니다. 코드·공식 Schema·의존성·팀원 모듈 변경이나 실제 외부 실행·Git commit/push는 하지 않았습니다.

### Single-Agent 실행 계약 — 47번, 구현 전 설계

사용자 개발정의서 §11-A와 `dev`에 반영된 평가 변경을 대조해 [47번 Single-Agent 실행 계약](docs/47-single-agent-execution-contract.md)을 정리했습니다. 한 Agent가 기획·개발·검증·수정을 이어서 수행하고, Multi와 같은 도구 기능·수정 기회·환경·평가 조건을 적용합니다. 단일 정체성, 보호된 Snapshot/평가 기준, Ver1·Ver2, 기록·평가 인계와 47~56번 구현 순서를 구분했습니다.

47번은 설계 기록입니다. 후속 48~50번 구현은 아래와 같으며, 앞선 44번 실제 LLM·Docker 실행 확인은 별도 보류 항목으로 남습니다.

### Single-Agent 설정·서버·Prompt 기반 — 48~50번

[48번 실행 설정](docs/48-single-agent-run-configuration.md)은 `SINGLE_AGENT`와 기존 `MULTI_AGENT`를 지원하고, Ver2의 순환용 보호 테스트 참조 생략 규칙을 유지합니다. 실제 Single 실행기가 연결되기 전에는 `/api/v1/runs`의 Single 요청을 503 `SINGLE_AGENT_RUNTIME_NOT_CONFIGURED`로 거절해 기존 Planner로 잘못 실행되는 것을 막습니다.

[49번 서버](docs/49-single-agent-a2a-server.md)는 기존 네 역할과 별도의 `SINGLE_AGENT` 서비스 정체성·포트 `8105`·DB `.data/agents/single_agent.sqlite3`를 사용합니다. `PYTHONPATH=src .venv/bin/python -m agents.single`로 통신 서버를 시작할 수 있으며, 현재는 Bootstrap으로 작업을 거절하고 `executionReady=false`를 유지합니다.

[50번 통합 Prompt/기록](docs/50-single-agent-prompts-context.md)은 같은 Agent의 System Prompt와 Context를 유지하면서 단계별 Draft를 검증하고, 재시작 뒤에도 저장된 메시지·산출물을 함께 불러옵니다. 이 기록을 사용하는 최초 실행 경로는 아래 52번에서 연결했습니다. 실제 LLM/Docker 실행과 비교 실험은 아직 수행하지 않았습니다.

### Single-Agent MCP 도구·단계 권한 — 51번

[51번 MCP 권한 연결](docs/51-single-agent-mcp-permissions.md)은 `SINGLE_AGENT` 주체와 업무 단계를 고정해 기존 도구 10개의 합집합을 연결합니다. 모델용 목록·Client·stdio 서버가 각각 단계 권한을 검사하며, 검증 단계는 같은 Frozen Snapshot과 자기 테스트 폴더만 사용합니다. 보호 테스트는 별도 Host 설정에 남깁니다.

`agents.single.mcp.build_single_mcp_configuration()`과 `single_phase_tool_definitions()`가 Host의 단계별 연결 지점입니다. `dev`의 Security 자체 테스트 MCP 구현도 필요한 범위만 반영했습니다. 기본 Single 통신 서버는 계속 Bootstrap이며, 실제 실행은 아래 Host 조합을 명시적으로 구성해야 합니다.

### Single-Agent 최초 구현·검증 실행 — 52번

[52번 실제 실행 연결](docs/52-single-agent-initial-pipeline.md)은 `agents.single.platform.create_single_platform()`으로 Orchestrator와 **하나의 Single Agent 서버·Provider·Context**를 연결합니다. 같은 System Prompt와 저장된 작업 기록을 이어 사용하며 기획 → 코드 작성·Build → QA → Security를 순서대로 수행합니다. 기존 네 실행기를 Single이라고 바꿔 호출하지 않습니다.

기존 Host 서비스의 Snapshot·Sandbox·보호 테스트·실행 증거 검사를 재사용하며, Security 실행 테스트 모드도 처리합니다. 실제 Host 조합으로 만든 서버만 `executionReady=true`를 반환합니다. 기본 Orchestrator의 Single 요청 503 거절과 `python -m agents.single`의 Bootstrap 동작은 유지합니다. 이 조합은 `agents.platform.runner.run_platform()`으로 두 로컬 엔드포인트를 실행할 수 있으며, 기존 Multi Host CLI 설정을 자동 변경하지 않습니다.

최초 후보의 검증에 이어 아래 53번의 수정·재검증, 54번의 재개·취소·영속 예산 복구, 55번의 기록·비용·평가 인계, 56번의 파일 기반 실행·최종 점검을 연결했습니다. 실제 키·Docker 실행과 공정 비교 실험은 별도 확인이 필요합니다.

### Single-Agent 수정·재검증과 판정 — 53번

[53번 수정·재검증](docs/53-single-agent-fix-revalidation.md)은 Build·QA·Security의 확인된 제품 실패를 기존 Issue에 연결하고, **같은 Agent·Provider·Context로 최대 3회 수정**합니다. 수정할 때마다 새 Source·Build 결과를 만들고 같은 후보로 QA → Security를 다시 수행합니다. 이전 후보의 PASS를 복사하지 않으며, 기존 판정기의 SUCCESS·FAIL·HUMAN_REVIEW 정책을 사용합니다.

반복 Issue는 기존 fingerprint 규칙으로 사람 검토에 넘기고, 수정 단계도 최초부터 사용한 Run 예산과 deadline을 이어 사용합니다. 잘못된 Context나 오래된 Task 매핑은 수정 모델·파일 쓰기 전에 거절합니다. 기본 통신 서버의 Bootstrap과 미구성 Single API의 503은 유지하며, 명시적 `create_single_platform()` 구성에서 이 기능을 사용합니다.

### Single-Agent 입력·취소·복구·영속 예산 — 54번

[54번 제어·복구](docs/54-single-agent-controls-durable-budgets.md)는 같은 Step·Task·Context로 입력/인증 뒤 재개하고, 실제 작업 종료를 확인한 뒤 취소합니다. QA가 입력을 기다리는 동안 Security를 보내지 않으며, 완료된 작업은 GET으로 확인해 중복 실행을 막습니다.

Single Host도 기존 영속 예산 원장을 사용합니다. 재시작하면 같은 동결 설정·호출 수·토큰·원래 UTC deadline과 영수증을 확인해 복구합니다. 미완료 모델/도구 예약, 누락·미확인 사용량, 한도 변경·시간 역행·기한 만료는 새 예산을 주지 않고 실행을 차단합니다. 이 감사에 필요한 기본 LLM/MCP 기록은 실제 `SINGLE_AGENT` 주체로 저장합니다.

### Single-Agent 기록·비용·평가 인계 — 55번

[55번 인계 안내](docs/55-single-agent-records-evaluation-handoff.md)는 승인 가격표와 Single 계상을 연결하고, `GET /api/v1/runs/{runId}/metrics`로 Run 전체 시간·사람 대기·호출·토큰·비용을 제공합니다. 미확인 사용량/비용은 null로 남깁니다.

`python -m agents.platform.evaluation_export --database PATH --run-id UUID --store SINGLE_AGENT=PATH --out NEW_DIR`는 모든 후보의 원본 Snapshot/Manifest, 전체 기록·사용량·실제 단일 A2A 저장소 자료를 읽기 전용으로 인계합니다. 실패·취소·Snapshot/기록 누락도 표시합니다. Single A2A는 schemaVersion 2이며, `dev`의 기존 D4 소비자는 네 역할 고정 검사를 이 계약에 맞게 확장해야 합니다. 55번 문서에 역할 4 인계 조건을 명시했습니다.

### Single-Agent 최종 점검·실행 안내 — 56번

[56번 실행·인계 안내](docs/56-single-agent-final-audit-runbook.md)에 47~56번 구현·개발정의서 대조, 설정 준비·요청 생성·조회·내보내기, 역할 3·4 인계 조건을 정리했습니다. `configs/single-runtime.example.json`은 실제 Agent 하나와 `.data/single/` 저장소를 지정하는 미완성 승인 설정 예시입니다.

```sh
PYTHONPATH=src .venv/bin/python -m agents.platform.run_host \
  --architecture SINGLE_AGENT \
  --runtime configs/single-runtime.local.json \
  --tools configs/owned-tools.local.json \
  --source configs/owned-source.local.json
```

기본은 파일 검사이며 서버·모델을 시작하지 않습니다. 승인 설정·환경변수·Docker를 준비한 뒤 같은 명령에 `--start`를 추가하면 Orchestrator와 Single 두 서버를 실행합니다. `--architecture`를 생략하면 기존 Multi 모드입니다. Security 자체 테스트 scope도 양쪽 Host가 같은 정책으로 전달하며, 미확인 근거를 PASS로 처리하지 않습니다. 실제 환경 실행과 독립 비교 실험은 아직 수행하지 않았습니다.

## 실제 OpenAI 통신 검증 — 59번

[59번 실행 안내](docs/59-live-openai-communication.md)는 루트 `.env`의 `OPENAI_API_KEY`를 읽고 명시한 모델로 최대 5회 호출하는 A2A 진단 CLI입니다. [정리된 회원가입 입력의 기존 PDF](docs/reports/agent-communication-openai-2026-11-08/structured-signup-communication-report.pdf)에 송수신 5/5 일치, 내용 기준 4/5 충족, 싱글의 연결 관계 작성 오류, 역할별 시간과 후속 작업을 정리했습니다. 공유용 보고서는 PDF만 유지합니다.

```bash
PYTHONPATH=src .venv/bin/python -m diagnostics.communication \
  --model gpt-4.1-mini-2025-04-14 --out /tmp/a2a-openai-new-run
```

모델과 새 결과 폴더를 명시해야 호출하며 자동 재시도·대체 모델은 사용하지 않습니다. 합성 입력 자료와 고정 참조는 실제 응답 검증에 필요하므로 포함합니다. 목업 전용 대시보드·오류 주입 실행·보고서는 제거했습니다.

### 자연어 요청 3종 검증 — 60번

[60번 실행·평가 안내](docs/60-natural-language-communication.md)는 블로그·문서 서비스·추리게임의 자연어 원문을 입력합니다. 모델에 정리된 요구사항이나 평가 기준을 주지 않고, 요청당 멀티 4회·싱글 1회로 최대 15번 호출합니다. [새 PDF 보고서](docs/reports/agent-communication-natural-2026-11-10/natural-language-3cases-report.pdf)에 실제 송수신 15/15 일치, 미승인 조건의 검사 기준화, 역할 필드 혼입과 시간 차이를 기록했습니다.

입력은 [자연어 요청 폴더](src/diagnostics/communication/fixtures/natural)에, 최신 재실행의 실제 입출력 증거는 Git에서 제외되는 `.data/diagnostics/natural-language-3cases-2026-11-10-redo-01/`에 있습니다. 제품 코드 생성·빌드·실제 QA·보안 스캔은 이번 진단 범위 밖입니다.

### 승인 요구사항 원장·Planner 미승인 가정 분리 — 61·62번

[61번](docs/61-approved-requirement-authority.md)은 Run 생성 때 동결한 요구사항의 출처·ID·해시를 불변 원장으로 식별합니다. [62번](docs/62-planner-unapproved-assumptions.md)은 멀티·싱글 기획 단계에서 `unapprovedAssumptions`를 별도 필드로 받고, 미승인 조건이 표시된 실행 계획을 거절하거나 질문 대기로 남깁니다. 제품 `requirements.json` v1과 A2A·MCP 권한은 유지하고 내부 모델 Draft만 `planner_decision_v2`로 갱신했습니다.

구조적 차단과 실제 모델의 의미 정확성은 구분합니다. 이번 변경으로 작업 설명 속의 모든 숨은 조건을 자동 탐지하거나 실제 API 실험의 개선율을 증명한 것은 아닙니다.

### Developer 요구사항 대조·독립 검증 기준 — 63·64번

[63번](docs/63-developer-requirement-scope.md)은 Developer의 승인 요구사항·Task 연결을 동결 원장과 대조해 내부 모델 입력에 제공합니다. [64번](docs/64-independent-validation-criteria.md)은 QA·Security가 같은 원장의 역할별 기준을 사용하고, 모델 생성 테스트의 `expectedResult`가 해당 Acceptance Criteria 원문과 일치하는지 실행 전 검사합니다. 싱글·멀티와 Host 서비스의 직접 호출 모두 동일 검증을 적용합니다.

기존 제품 Artifact Schema·보호된 평가 테스트 정책·도구 실행 근거는 유지합니다. 기대값 원문 일치는 테스트 코드의 의미 정확성이나 제품 성공을 증명하지 않습니다.

### 역할별 출력·Host 결과 인수 — 65·66번

[65번](docs/65-natural-role-output-contracts.md)은 자연어 진단의 `roleWork`를 역할별 Schema로 제한하고, 응답이 도착했어도 계약을 어기면 후속 Agent로 전달하지 않습니다. Single은 네 관점을 모두 작성하며, 새 기록은 역할별 Schema 해시를 갖는 v2입니다. 기존 실제 API 보고서는 변경하지 않았습니다.

[66번](docs/66-host-artifact-admission.md)은 제품 Host가 역할별 Artifact 이름·개수·중복 ID·Part·project metadata를 먼저 검사하도록 연결했습니다. 추가 결과물을 조용히 무시하지 않고 기존 검토 경로로 돌립니다. 정상 싱글·멀티와 수정 루프를 확인했으며, 인수 통과를 실제 제품 성공으로 사용하지 않습니다.

### 수신 기록·전달 무결성 관측 — 67·68번

[67번](docs/67-a2a-delivery-receipts.md)은 Agent DB의 기존 Message 접수 기록을 읽는 프로젝트 조회 API와 Client를 추가하고, SDK JSON 변환 중 payload·metadata의 실제 값 손실을 송신 전에 거절합니다. 원문 대신 식별자·접수 시각·해시를 반환하며, 접수 확인을 실행 성공으로 사용하지 않습니다.

[68번](docs/68-delivery-integrity-observations.md)은 구조화·자연어 통신 진단에서 송신 요청과 실제 DB 접수 기록을 대조합니다. 기록 미관측과 실제 불일치를 구분하고, 응답·Task ID를 보존하면서 미확인 결과의 후속 전달을 차단합니다. 제품 Host 전체의 자동 차단 정책이나 기존 실제 API 보고서를 변경한 것은 아닙니다.

### 처리 구간·역할별 지연 관측 — 69·70번

[69번](docs/69-communication-phase-timing.md)은 로컬 통신 진단에서 접수·실행 대기, 모델 준비, API 헤더·본문 수신, 로컬 검증, 결과 확인을 분리합니다. 미관측 시간은 null로 유지하고, 수신 확인 조회 때문에 결과 관측 기한이 늘어나지 않도록 했습니다.

[70번](docs/70-communication-performance-summary.md)은 역할별 완료 표본의 구간 p50·p95와 편차, 토큰 수 참고값, 병렬 요청 구간의 합집합을 제공합니다. 결과 JSON과 기존 구조화 HTML 생성기에 연결했으며, 제품 전체시간이나 구조 간 성능 우열로 사용하지 않습니다. 기존 실제 API 보고서는 변경하지 않았습니다.

### 진단 연결·반복 계산 비용 축소 — 71·72번

[71번](docs/71-a2a-session-reuse.md)은 같은 로컬 진단 서버 세션에서 역할별 HTTP Client와 Agent Card를 재사용합니다. 자격증명·콜백을 역할별로 격리하고 요청별 수신 확인·새 식별자·명시적 Context·취소·정리는 유지하며, 재사용 상태를 진단 결과에 기록합니다.

[72번](docs/72-diagnostic-observer-optimization.md)은 구독 없는 CLI의 중간 화면 생성, 최종 행·요약의 중복 계산, 중첩 JSON의 반복 정규화를 줄입니다. 실시간 구독과 모든 최종 검사는 유지합니다. 로컬 비교 함수의 비용 감소를 확인했으며 실제 OpenAI 전체 지연 개선율은 아직 측정하지 않았습니다.

### 보강 후 실제 OpenAI 재검증·보고서 — 73·74번

[73번](docs/73-live-communication-revalidation.md)은 같은 자연어 요청 3개와 같은 모델로 실제 API 15회를 실행하고 선택 실행 소스·환경·Transport·실행 순서를 기록합니다. 송수신·DB 접수·API 실제 입력은 15/15 일치했고 다른 역할 필드 혼입은 0개였습니다.

[74번](docs/74-communication-revalidation-report.md)과 [새 PDF](docs/reports/agent-communication-revalidation-2026-11-13/revalidation-report.pdf)는 자연어 원문, 송수신 15건, 역할별 시간과 남은 의미 해석 위험을 정리합니다. 블로그의 미승인 조건 검사 기준화와 추리게임의 개인정보 범위 확대 위험이 남아 있어 할루시네이션 부재를 선언하지 않습니다. 과거 보고서는 보존하며 실제 제품 Host·MCP·Docker 전체 흐름과 통제된 성능 비교는 별도 확인 대상입니다.

### 원문 조건·검사 연결·의미 검토와 재실험 — 75~80번

[75번 경로 대조](docs/75-diagnostic-product-path-audit.md), [76번 조건 분리](docs/76-natural-condition-classification.md), [77번 기대값 연결](docs/77-natural-expectation-bindings.md), [78번 의미 검토·인계](docs/78-semantic-review-handoff-gate.md)는 자연어 진단에서 원문 인용·미승인 가정·검사 연결·별도 모델 검토·필드별 근거를 확인합니다. 질문·계약 오류·미검증 결과는 다음 역할로 넘기지 않습니다. 제품 승인 원장을 모델 추출 결과로 대체하지 않습니다.

[79번 회귀](docs/79-guarded-single-multi-regression.md)는 관련 173개 검사를 확인했습니다. [80번 실제 재실험](docs/80-guarded-live-revalidation.md)과 [새 PDF](docs/reports/agent-communication-guarded-2026-11-13/guarded-natural-4cases-report.pdf)는 총 API 38회, 역할 송수신 16/16 일치, 초기 질문·거절·미호출, 검토기가 놓친 개인정보 확대와 보완 후 보류, 회사 소개 최종 5/5 인계를 구분해 기록합니다. 의미 판정 정확성과 보수적인 전달 차단은 같은 지표가 아닙니다.

```bash
PYTHONPATH=src .venv/bin/python -m diagnostics.communication.guarded \
  --model gpt-4.1-mini-2025-04-14 --out .data/diagnostics/guarded-new-run
```

현재 guarded는 역할 답변/검토를 각각 최대 20회로 제한하고 자동 Retry·대체 모델을 사용하지 않습니다. 기존 v2 CLI는 과거 진단 재현용입니다. 실제 제품 Host·MCP·Docker 실행은 81~82번으로 남아 있습니다.

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
