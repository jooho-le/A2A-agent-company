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

Build/Test/Scan의 실제 MCP 호출은 Agent/MCP 담당 범위입니다. Orchestrator는 전달된 실행 근거를 검증·저장합니다. 20~22번에는 Workspace·Artifact·Container 실행 기반, 23번에는 MCP stdio 통신·Schema·역할 권한, 24번에는 실제 파일 Tool, 25번에는 Container Build, 26번에는 Python unittest, 27번에는 Playwright 브라우저 실행, 28번에는 Bandit 정적 보안 검사, 29번에는 실제 호출 원장·오류 분류·안전 Retry 어댑터를 추가했습니다. 기본 Agent/Dispatch 연결은 후속 작업입니다. 실제 Docker·회원가입 코드의 성공·비교 실험 완료를 뜻하지 않습니다.

자동 QA/Security dispatch를 시험하려면 `.env`에 `ORCHESTRATOR_PLANNER_AGENT_URL`, `ORCHESTRATOR_DEVELOPER_AGENT_URL`, `ORCHESTRATOR_QA_AGENT_URL`, `ORCHESTRATOR_SECURITY_AGENT_URL`을 모두 설정합니다. QA/Security 중 하나라도 설정되지 않으면 Snapshot과 pending Step은 보존하고 Run을 `HUMAN_REVIEW`로 전환합니다.

기본 DB 경로는 `.data/orchestrator.sqlite3`이며 `ORCHESTRATOR_DATABASE_PATH` 환경변수로 바꿀 수 있습니다. `.data/`는 Git에서 제외됩니다.

## Agent/MCP 개발 기반 — 15~17번

담당 범위를 1번 Orchestrator와 2번 Agent/MCP로 확장했습니다. `src/agents/`에는 공통 A2A 서버·설정이, `src/mcp_tools/`에는 stdio 서버·Client·역할별 실제 Tool이 있습니다. 공통 LLM 엔진과 Workspace/Artifact/Sandbox를 바탕으로 Planner·Developer·QA의 명시적 실행기를 추가했습니다. Security 실행기와 기본 Pipeline 자동 연결은 후속 작업이며 기본 Agent CLI는 Bootstrap을 유지합니다. 3번 웹 서비스와 4번 평가 모듈은 변경하지 않습니다.

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

현재는 최초 `codeVersion=1` 구현만 지원하며 Issue 기반 수정 루프는 35번입니다. 기본 서버는 Bootstrap 유지, 실제 Developer를 주입할 때만 실행 Skill을 광고합니다. 이미 Source를 수정한 뒤 중단한 작업은 자동 원복/재실행하지 않습니다. 실제 LLM·Docker·Developer stdio 전체 경로는 미검증이며 자동 Pipeline 연결은 후속입니다. 상세 사용법·정의서 점검·검증 한계는 [31번 Developer Agent](docs/31-developer-agent.md)를 참고하세요.

### QA Agent — 32번

Host가 명시적으로 구성한 `QAAgentExecutor`가 같은 불변 Source를 읽고 QA 전용 테스트를 작성하며, 승인된 Unit/Browser 테스트의 실제 private 실행 기록으로 기존 `qa-report.json` Artifact를 반환합니다. 모델은 테스트 케이스와 Requirement의 연결만 제안하고 결과·Manifest·ToolEvidence를 만들지 않습니다. 모든 승인된 QA 테스트와 Host 보호 테스트를 실행하며, 실패한 assertion은 Tool PASS + QA FAIL, 누락/SKIP 케이스는 UNVERIFIED로 구분합니다.

현재는 최초 `VALIDATING`·`fix_attempt=0`·`codeVersion=1`만 지원합니다. 실제 QA 실행기를 주입할 때만 `measured-initial-qa` Skill·`executionReady=True`를 광고하며 기본 CLI/서버는 Bootstrap을 유지합니다. QA는 Source·보호 테스트를 수정하거나 전체 Run을 판정하지 않습니다. 실제 LLM·Docker 실행은 미검증이고, 자동 Pipeline 연결(34번)과 수정 후 재검증(35번)은 후속입니다. 상세 범위·Host 설정·정의서 점검·검증 한계는 [32번 QA Agent](docs/32-qa-agent.md)를 참고하세요.

### Security Agent — 33번

Host가 명시적으로 구성한 `SecurityAgentExecutor`가 같은 불변 Source에서 승인 Scanner Profile 전체를 실행하고, 실제 Scan/Tool 기록과 읽은 코드 근거를 연결하여 기존 `security-report.json` 하나를 반환합니다. 모델은 분석 Draft만 제출하며 Scanner 경고를 누락하거나 Severity·실행 근거·최종 Verdict를 만들 수 없습니다. 경고 0개로 보안 PASS를 만들지 않고, 독립 Host 의미 검증 근거가 없으면 Requirement는 UNVERIFIED, 경고는 SUSPECTED로 남깁니다.

최초 후보만 지원하며 기본 CLI/서버는 Bootstrap을 유지합니다. 실제 실행기를 주입한 경우에만 Security Skill을 광고합니다. SCN-001의 모든 보안 기준을 입증하는 실제 의미 검증기와 LLM/Docker 실행은 미검증·미완료이며, 공통 Proof 연결 경계가 그 검증을 대신하지 않습니다. 상세 사용법·정의서 점검·한계는 [33번 Security Agent](docs/33-security-agent.md)를 참고하세요. 다음은 **34번 기존 Orchestrator와 실제 Agent 연결**입니다.

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
