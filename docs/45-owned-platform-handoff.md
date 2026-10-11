# 45. 역할 1·2 플랫폼 최종 정리 및 인계 안내

작성 기준: 2026-10-11. 대상: 1번 Orchestrator + 2번 Agent/MCP. 현재 코드의 사용법과 연결 계약을 정리한 문서이며, 팀원 제품·독립 평가를 새로 구현하거나 실제 연결한 단계는 아니다.

## 1. 지금 어디까지 된 상태인가

| 구분 | 상태 |
| --- | --- |
| 1~43번 | Orchestrator, 네 Agent 실행기, MCP Tool, 격리 실행, 승인 설정, 의미 검증 연결, 사용량·비용 원장 구현. 실제 운영 검증과는 별개 |
| 44번 | 준비 점검만 완료. 실제 LLM·Docker 최소 Run 실행은 미수행이며 전체 완료 아님 |
| 45번 | 사용자의 요청에 따라 실제 실행보다 먼저 문서 정리. 이 문서를 실제 실행 성공 증거로 사용하지 않음 |
| 46번 | 후속 점검의 Source 비밀정보·Python deadline 호환성·설정 URL Credential·Agent DB WAL 경쟁 보완. 실제 실행 완료와는 별개 |
| 역할 3·4 | 제품 UI/API/DB, 독립 테스트·보안 기준, Single/Multi 비교 실험은 이 문서의 구현 범위 밖 |

따라서 인계할 수 있는 것은 **플랫폼 코드와 연결 계약**이다. 회원가입 완성 시연, 실제 유료 모델·Docker 동작, 독립 보안 검증, 실제 청구 금액까지 확인했다는 뜻은 아니다. 44번 실제 실행 결과는 확인 후 이 문서에 별도로 추가한다.

44번 점검 당시에는 승인 로컬 설정·Host 자격증명이 없었고, 점검한 shell PATH에서 Docker CLI를 찾지 못했다. 이는 [44번 당시 기록](44-owned-live-smoke-readiness.md)이며, 이번 문서 정리에서 환경변수나 Docker 상태를 다시 검사한 결과가 아니다.

## 2. 구현 위치와 실행 흐름

| 산출물 | 위치 | 책임 |
| --- | --- | --- |
| Orchestrator | [src/orchestrator](../src/orchestrator/) | Run 생성, 순서·상태·수정 루프 관리, Artifact/Issue/Trace 저장, 최종 판정 |
| Agent 서버·실행기 | [src/agents](../src/agents/) | A2A Task 실행, 역할 Prompt·출력 검증, LLM 호출, 각 역할의 실제 작업 |
| MCP 서버·Tool | [src/mcp_tools](../src/mcp_tools/) | stdio 통신, 역할 권한, 파일 조작, 격리 Build/Test/Scan, 실행 근거 |
| Owned Host | [src/agents/platform](../src/agents/platform/) | Orchestrator와 네 Agent 조립, 승인 설정·공통 예산·자원 종료 관리 |
| 프로젝트 JSON 계약 | [schemas/project](../schemas/project/) | Artifact, Manifest, Tool Evidence, Trace, RunConfiguration의 고정 규격 |
| 운영 설정 예시 | [configs](../configs/) | 운영자가 승인할 설정 형식. 원본 예시는 실행 가능한 운영 설정이 아님 |

현재 실행 흐름:

```text
사용자 요청 → Run/정책 동결 → 승인 시작 Source 준비 → Planner → Developer
  → 불변 Source Snapshot + Build 근거
  → QA / Security 병렬 검증 (동일 Snapshot·환경)
  → 최종 판정 또는 수정 요청 → 새 Snapshot 생성 → 재검증
```

초기 `codeVersion`은 1이며 수정 최대 3회로 2~4까지 생성한다. 제품 수정 한도와 Tool의 안전한 재시도 최대 2회는 별도다. 전송 여부·결과가 불명확한 작업을 무조건 다시 호출하지 않는다.

Snapshot 저장소 내부 Commit은 검증을 위한 버전 고정이다. **팀 프로젝트 Git commit/push, 제품 배포·서비스 재시작은 자동 수행하지 않는다.**

번호별 문서는 당시의 작업 이력이다. 현재 사용법은 이 문서와 최신 설정 문서를 기준으로 읽는다.

| 번호 | 내용 |
| --- | --- |
| 1~14 | Orchestrator 계약·서버·상태·A2A·저장·수정 및 최종 판정 |
| 15~19 | Agent/MCP 패키지, 공통 A2A 서버·Task 관리, 역할 Prompt, LLM 엔진 |
| 20~29 | Workspace, 불변 Artifact, Sandbox, MCP Tool, 실행 근거·안전 Retry |
| 30~37 | 네 역할 실행기, Owned Host, 수정·재개·취소, 사용량·Telemetry |
| 38~43 | 승인 Runtime·Tool·Source, Host factory, 독립 Security 증거 연결, 비용 계산 |
| 44~45 | 실제 실행 준비 점검 / 현재 구현 정리와 인계 안내 |

## 3. 바꾸면 안 되는 계약

### 통신과 ID

- A2A는 프로젝트 개발정의서 기준 `1.0`, `HTTP+JSON/REST`다. `POST /message:send`의 `returnImmediately=true`로 Task를 받고, 서버 발급 ID를 저장해 `GET /tasks/{id}`로 조회한다. `A2A-Version: 1.0`과 Agent Card의 `protocolVersion`을 맞춘다.
- MCP는 프로젝트 기준 `2026-07-28`, 로컬 `stdio`다. 정확한 의존성 설치는 기존 `uv.lock`을 사용하며, 이 문서에서 버전을 변경하지 않는다.
- 프로젝트 `runId`, `scenarioId`, `workflowStepId`, `requirementId`, `workspaceId`, `artifactId`는 UUIDv4다. `REQ-001`·`SCN-001`은 표시용 Key다.
- A2A Task·Context·Artifact ID는 Agent가 발급한 opaque 문자열이다. 프로젝트 UUID로 바꾸거나 Orchestrator가 대신 발급하지 않는다.

상세 기준: [1번 계약](01-orchestrator-contracts.md), [6번 A2A Client](06-a2a-client.md), [23번 MCP 통신·권한](23-mcp-stdio-role-enforcement.md).

### 상태와 판정

| 구분 | 의미·예시 |
| --- | --- |
| Workflow `status` | 전체 진행 상태. `PLANNING`, `IMPLEMENTING`, `VALIDATING`, `FIXING`, `REVALIDATING`, `HUMAN_REVIEW`, `FINISHED`, `ABORTED` 등 |
| Step `status` | 해당 역할 작업 상태. `PENDING`, `RUNNING`, `SUCCEEDED`, `FAILED`, `WAITING_INPUT`, `CANCELED` |
| A2A Task 상태 | 원격 작업 상태. `TASK_STATE_WORKING`, `TASK_STATE_COMPLETED`, `TASK_STATE_FAILED`, `TASK_STATE_INPUT_REQUIRED` 등 |
| 최종 `verdict` | 제품 판정. `SUCCESS`, `FAIL`, `UNVERIFIED`, `HUMAN_REVIEW` 또는 아직 `null` |

`TASK_STATE_COMPLETED`는 보고 업무 완료이지 제품 PASS가 아니다. `FINISHED + FAIL/UNVERIFIED`도 정상 조합이다. `ABORTED`는 `verdict=null`과 중단 사유를 가진다.

`SUCCESS`는 같은 Snapshot·실행환경의 Build PASS, 모든 필수 Requirement·QA·Security PASS, Blocking Finding 0, 필수 미검증 0, 필요한 실제 실행·Trace 근거를 모두 확인한 경우에만 가능하다. Scanner 경고 0개나 모델의 설명만으로 Security PASS를 만들지 않는다. 상세: [12번 최종 판정](12-validation-results-verdict.md).

### MCP 역할별 권한

| 호출 주체 | 허용 Tool |
| --- | --- |
| Planner | 없음 |
| Developer | `read_project_file`, `write_source_file`, `apply_patch`, `run_build`, `run_unit_tests` |
| QA | `read_project_file`, `write_test_file`, `run_unit_tests`, `run_browser_tests`, `read_test_report` |
| Security | `read_project_file`, `run_security_scan`, `read_security_report` |
| Orchestrator Host | `run_build`, `read_test_report`, `read_security_report` |

Host는 다섯 번째 LLM Agent가 아니라 관리 주체다. QA/Security는 불변 Source를 읽으며 Developer의 현재 Working Tree를 검증 대상으로 바꾸지 않는다. QA의 테스트 작성 Tool은 `outputs/qa/tests`로 제한한다. Tool 직접 호출에도 역할·Schema·Run·Workspace·경로·비밀정보 권한 검사를 적용한다.

Build/Unit/Browser/Scanner는 기존 Docker Sandbox에서 실행한다. 현재 Tool baseline의 네트워크는 `DENY`이며 Host 직접 실행 fallback이나 Container에 대한 Docker socket·API Key 전달은 없다. 단위 테스트는 Python unittest, 브라우저는 Playwright, 정적 Scanner는 Bandit 기반이다. 임의 shell 명령 실행 Tool로 설명하지 않는다.

## 4. 실행 전 준비할 설정

Python 3.10+와 lock 기준 의존성이 필요하다. 프로젝트 루트에서 `uv sync --frozen`으로 설치한다. 기존 `.venv`가 준비돼 있으면 아래 명령을 사용할 수 있다.

| 로컬 파일 | 필요 조건 | 자세한 작성 기준 |
| --- | --- | --- |
| `configs/owned-runtime.local.json` | 필수. 공통 모델·옵션, 주소/DB, 환경변수 참조, 모델/Tool 호출·토큰·시간 한도 | [38번 Runtime](38-owned-runtime-configuration.md) |
| `configs/owned-tools.local.json` | 필수. 고정 이미지·Lock·Hardware, Tool 프로파일·자원/시간 제한 | [39번 Sandbox·Tool](39-sandbox-image-tool-profiles.md) |
| `configs/owned-source.local.json` | 필수. 승인 Reference Repository, 전체 Commit·Snapshot Hash, Lock 경로·준비 한도 | [40번 시작 Source](40-approved-starting-source.md) |
| `configs/owned-qa-cases.local.json` | QA Tool에 `PROTECTED` selector가 있으면 필수. 승인 테스트와 Requirement 연결 | [41번 Host factory](41-owned-host-factory.md) |
| `configs/owned-security-approval.local.json` | 독립 Security 의미 판정을 연결할 때 필요. 신뢰된 Python Host에서 승인 함수와 함께 제공 | [42번 Security 증거](42-approved-security-semantic-proof.md) |
| `configs/owned-price-book.local.json` | 비용 계산을 사용할 때 필요. 미지정 실행은 가능하지만 비용 `null`. 44번 비용 확인 범위에는 승인 가격표 준비 | [43번 비용 원장](43-approved-llm-cost-accounting.md) |

각 `*.example.json`은 형식 예시다. 미승인 `null`/빈 값을 임의 모델·합성 가격·가짜 Hash로 채워 실행 준비 완료로 취급하지 않는다.

Owned Host는 선언한 **process environment**에서만 API Key·역할 Bearer를 읽는다. `.env` 파일을 자동으로 읽거나 `AGENT_*`·`ORCHESTRATOR_*`로 승인 설정을 덮어쓰지 않는다. 예시 Key 변수명은 `A2A_PLATFORM_LLM_API_KEY`이며 다른 변수명으로 자동 fallback하지 않는다. production 모드는 네 역할 Bearer 참조가 필수다.

비밀값은 채팅·Markdown·Git·Agent Workspace·Tool/Container에 넣지 않는다. `.env`, `configs/*.local.json`, `.data/`, 준비된 Sandbox 자산은 기존 Git 제외 대상이다. 승인 파일·검증기 구현은 Workspace/시작 Reference Repository와 분리하고, 다섯 DB도 서로 분리한다.

## 5. 어떤 명령을 실행해야 하는가

### 기본 Orchestrator와 Owned Host의 차이

| 실행 방식 | 실제 하는 일 |
| --- | --- |
| `uv run uvicorn orchestrator.main:app --app-dir src --reload` | 기본 Orchestrator 서버. 실제 네 Agent·승인 Tool·공유 예산을 자동 조립하는 명령이 아님 |
| `AGENT_ROLE=PLANNER PYTHONPATH=src .venv/bin/python -m agents` | 역할을 명시한 기본 Agent 서버. Bootstrap 실행기는 요청을 `AGENT_RUNTIME_NOT_CONFIGURED`로 거절하며 제품을 개발하지 않음 |
| 아래 `agents.platform.run_host` | 기본은 승인 설정 검사. `--start`를 명시해야 기존 Orchestrator와 네 실제 Agent 실행기를 함께 시작 |

Bootstrap에 모델/URL 설정만 넣었다고 Owned Host로 바뀌지 않는다. 실제 사용에는 아래 경로를 사용한다.

### 먼저 읽기 전용 검사

```bash
PYTHONPATH=src .venv/bin/python -m agents.platform.run_host \
  --runtime configs/owned-runtime.local.json \
  --tools configs/owned-tools.local.json \
  --source configs/owned-source.local.json \
  --price-book configs/owned-price-book.local.json
```

가격표를 사용하지 않으면 `--price-book`을 생략한다. 보호 QA 케이스가 필요하면 `--qa-cases configs/owned-qa-cases.local.json`을 추가한다. 자격증명 존재/형식도 확인하려면 `--check-secrets`를 추가한다. 이는 인증 성공이나 모델 접근 권한 검사까지 수행하는 옵션은 아니다.

설정 검사 성공 출력은 다음과 같다. **실행 준비 완료·Docker 확인·Workspace 준비를 의미하지 않는다.**

```json
{"status":"OWNED_HOST_CONFIGURATION_VALID","credentialCheck":"NOT_REQUESTED","executionReady":false,"DockerChecked":false,"workspacePrepared":false}
```

### 승인과 환경 준비 후에만 서버 시작

```bash
PYTHONPATH=src .venv/bin/python -m agents.platform.run_host \
  --runtime configs/owned-runtime.local.json \
  --tools configs/owned-tools.local.json \
  --source configs/owned-source.local.json \
  --price-book configs/owned-price-book.local.json \
  --start
```

`--start`와 `--check-secrets`는 함께 쓰지 않는다. 시작은 한 프로세스·한 event loop·worker 1이며 reload 없이 다섯 앱을 연다. 별도 Bootstrap 서버를 같은 주소/DB에 동시에 띄우지 않는다. 예시 기본 포트는 Orchestrator 8000, Planner 8101, Developer 8102, QA 8103, Security 8104이며 실제 값은 승인 설정에 따른다.

서버 시작만으로 Run을 제출하지는 않는다. **Run 제출 후에는 유료 모델 호출·Docker 실행이 발생할 수 있다.** 이번 문서 정리에서는 서버 시작이나 Run 제출을 하지 않았다.

CLI에는 Security 승인 함수 선택 옵션이 없다. 기본 CLI의 의미 증거는 `UNVERIFIED`다. 실제 독립 판정을 쓰려면 [42번의 신뢰된 Python Host 연결](42-approved-security-semantic-proof.md)에 따라 `load_approved_assessor()`와 `create_host(proof_verifier=approved)`를 사용한다. 승인 JSON만 추가하거나 모델에게 PASS를 요청해서 연결할 수 없다.

### API 제출용 configuration 만들기

Owned Host에는 승인 설정과 일치하는 `configuration`을 명시해야 한다. 아래 코드는 같은 로컬 설정을 **읽기 전용**으로 검사해 공개 요청 JSON을 구성한다. DB·서버·Provider를 만들거나 요청을 보내지 않는다.

```python
import json
from agents.platform.host import inspect_host_configuration
from orchestrator.domain import SCN_001_ID

plan = inspect_host_configuration(
    runtime_file="configs/owned-runtime.local.json",
    tools_file="configs/owned-tools.local.json",
    source_file="configs/owned-source.local.json",
    price_book_file="configs/owned-price-book.local.json",
)
payload = {
    "scenarioId": str(SCN_001_ID),
    "requestText": "이메일과 비밀번호로 회원가입 기능을 만들어줘.",
    "configuration": plan.approved_configuration.model_dump(mode="json", by_alias=True),
}
print(json.dumps(payload, ensure_ascii=False, indent=2))
```

가격표·보호 QA 케이스·승인 Security 검증기를 사용하는 Host라면 검사 인자도 같은 설정으로 맞춘다. 승인 모델·환경·시작 Commit/Snapshot·전체 시간 예산·Suite/Scanner 참조가 다르면 저장/스케줄 전 `422 OWNED_AGENT_CONFIGURATION_INVALID`로 거절한다. API 모델에서 기본 configuration을 허용하는 것과 Owned Host의 실행 승인 조건은 다르다.

## 6. 화면·평가 담당자가 사용할 조회 계약

모든 아래 경로의 prefix는 `/api/v1`다. 실제 서버의 `/docs`와 [Run API 구현](../src/orchestrator/api/routes/runs.py)이 기준이다.

| 목적 | API | 응답에서 볼 것 |
| --- | --- | --- |
| 등록 기준 확인 | `GET /scenarios` | `scenarios` 배열의 UUID·표시 Key·요구사항·제외 기능·정책 |
| 실행 생성 | `POST /runs` | 201, `Location`, `{run, firstStep, dispatchStatus}` |
| 전체 진행 확인 | `GET /runs/{runId}` | `status`, `verdict`, `codeVersion`, `fixAttempt`, `terminationReason` |
| 역할별 진행 확인 | `GET /runs/{runId}/steps` | `steps` 배열의 역할·Step 상태·A2A Task ID/상태·입출력 Artifact ID |
| 과정·결함 확인 | `GET /runs/{runId}/events`, `/issues` | Trace 이벤트 / Issue 기록 |
| 결과·고정 설정 확인 | `GET /runs/{runId}/artifacts`, `/artifacts/{artifactId}`, `/configuration` | 프로젝트 Artifact JSON / 동결된 RunConfiguration Artifact |
| Workspace·실제 도구 확인 | `GET /runs/{runId}/workspace`, `/tool-attempts` | 공개 Workspace 권한 / 실제 Tool 시도 원장 |
| 사용량·전체 비용 | `GET /runs/{runId}/usage` | `{runId, summary, records, total, limit, offset}` |
| 호출·관측 이력 | `GET /runs/{runId}/telemetry` | `{runId, events, total, limit, offset}`. 이 응답에는 `summary`가 없음 |
| 보류·회수·중단 | `POST /runs/{runId}/resume`, `/recover`, `/cancel` | 기존 Task/Context를 보존. 재개/복구 시 미전송 Step·다음 단계는 새 실행 가능. 취소는 원격 결과 확인 후 처리 |

현재 등록된 회원가입 `scenarioId`는 `f7f9e5c3-ffc3-4b3f-918b-21e1b956ce76`이다. 문자열 `SCN-001`, 미등록 UUID, 빈 요청문은 422다. `POST /runs` 요청은 `scenarioId`, `requestText`, `configuration`을 사용한다. 반환된 `run.runId`로 조회하며 `dispatchStatus=SCHEDULED`는 완료 판정이 아니다.

`/events`, `/usage`, `/telemetry`는 `limit` 기본 100/최대 500, `offset` 기본 0인 페이지 조회다. `/usage.summary`는 페이지 부분합이 아닌 해당 Run 전체 요약이다. Step 응답은 내부 `agentContextId`를 공개하지 않는다.

`/artifacts/{artifactId}`는 **등록된 Artifact JSON 조회**다. Source archive bytes 다운로드·임의 파일 업로드·제품 배포 API가 아니다. `artifact://`도 논리 저장소 참조이며 임의 HTTP URL이나 Host 파일 경로로 바꾸지 않는다.

비용은 승인 가격표와 실제 응답 사용량으로 계산한 USD 소수 문자열 또는 `null`이다. `knownCostUsd`는 확인된 부분 합이며 전체 비용이 아니다. `costComplete`는 제품 성공/Run 완료 여부와 다르다. 가격표가 없거나 사용량·과금 조건·미완료 호출이 불명확하면 전체 `costUsd=null`을 유지한다. 실제 청구서·세금·환율·Docker 비용 검증이나 정확한 USD 선차단 장치는 아니다.

## 7. Agent 결과물과 팀원 인계 경계

### 기존 Agent 결과물 규격

| 생성 주체 | A2A Artifact 이름 | 프로젝트 Artifact |
| --- | --- | --- |
| Planner | `requirements.json` | `REQUIREMENT` |
| Developer | `source-snapshot.json`, `change-report.json`, `build-report.json` | `SOURCE`, `CHANGE_REPORT`, `BUILD_REPORT` |
| QA | `qa-report.json` | `QA_REPORT` |
| Security | `security-report.json` | `SECURITY_REPORT` |
| Orchestrator | 자체 고정 설정 저장 | `RUN_CONFIGURATION` |

QA/Security 보고서는 지정 이름의 A2A Artifact를 각각 정확히 하나 반환하고, `application/json` Data Part 하나를 사용한다. A2A Artifact metadata는 `runId`, `workflowStepId`, `projectArtifactId`, `artifactVersion` 네 필드다. 보고서 내부 ID·배정 Requirement·Task/Artifact ID·codeVersion이 현재 작업 및 외부 Artifact와 일치해야 한다.

Build/QA/Security가 보존할 Execution Manifest는 `repositoryId`, `codeVersion`, `projectArtifactId`, `commitHash`, `gitObjectFormat`, `treeHash`, `snapshotSha256`, `containerImageDigest`, `dependencyLockHash`다. 최신 Working Tree나 다른 버전의 PASS를 섞지 않는다.

전체 필드·추가 필드 금지·버전 연결은 [QA Schema](../schemas/project/qa_report.schema.json), [Security Schema](../schemas/project/security_report.schema.json), [Manifest Schema](../schemas/project/execution_manifest.schema.json), [Tool Evidence Schema](../schemas/project/tool_execution_evidence.schema.json)를 그대로 사용한다. 보고서 결과 `FAIL`과 Tool 실행 실패는 별개다. 정상 실행이 제품 결함을 발견한 경우 Tool 실행 근거는 성공이며 제품 검사 결과는 FAIL일 수 있다.

### 역할 3에게 전달할 것

- 시작 코드·Lock·Build 명령·실행환경을 실제 Tool 프로파일과 맞출 수 있도록 현재 Source/환경 승인 계약을 전달한다. 제품 API·DB 규격을 Orchestrator 내부 SQLite 규격에 억지로 맞추지는 않는다.
- 화면은 위 Run/Step/Artifact API에서 진행·결과를 조회할 수 있다. Snapshot JSON 조회와 실행 가능한 서비스 파일·배포는 서로 다른 작업이다.
- 제품 Git 반영·자동 Build·서비스 실행 연결은 별도 합의/연결 작업이다. 이번 문서가 이를 구현 완료한 것으로 설명하지 않는다.

### 역할 4에게 전달할 것

- 고정 Scenario의 Requirement·Acceptance Criteria와 승인 QA selector/case 연결 규격을 전달한다. 역할 4는 독립 테스트·Security 판정 기준을 준비하며 네 Agent 서버를 다시 구현할 필요는 없다.
- Security 독립 판정은 42번 승인 함수/기준과 현재 불변 Source·실제 Scanner 근거를 사용해야 한다. 모델 제안이나 이전 Run의 PASS를 신뢰 증거로 대체하지 않는다.
- 평가 입력은 RunConfiguration, Trace, Issue, Tool 시도, `/usage`·`/telemetry`다. 병렬 Step 시간을 합쳐 전체 완성 시간으로 쓰거나 미확인 비용을 0원으로 쓰지 않는다.
- 현재 공개 `architecture`는 `MULTI_AGENT`다. `experimentId`를 넣는 것만으로 Single-Agent 실행이나 공정한 비교 실험이 구현되는 것은 아니다. 비교 설계·Single-Agent 기준선·결과 그래프는 역할 4 범위다.

이 절은 향후 연결 때 사용할 계약 안내다. 팀원 코드·테스트·평가 기준을 이번 단계에서 변경하거나 실제 통합하지 않았다.

## 8. 남은 작업·주의할 상태

1. **44번 실제 실행으로 돌아가기**: 사용자 승인 모델/한도·자격증명, Docker/고정 이미지·프로파일, 시작 Repository·Lock, 승인 가격표를 준비한다. 먼저 검사하고 승인된 최소 Run 1개에서 실제 근거를 확인한다.
2. **관측 결과만 인계 문서에 추가하기**: Run ID, 고정 설정·Snapshot/환경 식별자, 실제 호출/Tool 기록, 판정과 미검증 사유, 확인 가능한 사용량·비용을 기록한다. 비밀값은 기록하지 않는다.
3. **팀원 연결은 별도 진행하기**: 역할 3 제품 연결, 역할 4 보호 테스트·독립 Security 기준·비교 실험은 현재 문서 정리와 구분한다.

| 보이는 상태 | 확인할 것 |
| --- | --- |
| `422` Run 생성 실패 | UUID·등록 Scenario·요청문·승인 configuration 일치 여부 |
| `NOT_CONFIGURED` / Bootstrap Agent 거절 | 기본 서버인지 실제 Owned Host인지, 실행기 연결 여부 |
| `/usage`·`/telemetry`가 503 | Telemetry 미설정 또는 원장 조회 실패. 사용량 0으로 해석하지 않음 |
| Security `UNVERIFIED` / Run `HUMAN_REVIEW` | 독립 의미 증거 유무와 필요한 실제 Tool 근거. 판정을 강제로 PASS로 바꾸지 않음 |
| 요청 전송/취소 결과 불명확 | 기존 Run/Task 조회·recover/cancel 규칙 확인. 새 Run/재전송으로 중복 실행하지 않음 |

전체 Run의 모델/Tool 호출·Token·시간 예산은 네 역할·수정·재개에서 공유한다. 준비·대기·입력 대기도 같은 deadline에 포함되며 재개한다고 초기화하지 않는다. 현재 PID 기반 잠금과 공유 예산은 단일 호스트 MVP 경계다.

45번 문서 정리 당시 보류됐던 RunConfiguration URL Credential 보호와 Agent DB WAL 초기화 경쟁은 이후 [46번 점검 보완](46-platform-audit-hardening.md)에서 수정했다. Source 비밀정보 반입 검사와 Python 3.10 deadline 대체 경로도 추가했다. 이는 실제 운영/제품 보안 전수 검증 완료가 아니며, 기존 Credential 포함 불변 DB는 자동 변경 없이 새 검사에서 거부될 수 있다. 역할별 DB 분리 원칙은 그대로다.

## 9. 개발정의서 점검·문서 검증

| 기준 | 이번 정리 결과 |
| --- | --- |
| §0·§3·§6·§7 | 기존 A2A/MCP 버전, UUID/opaque ID, JSON Schema와 상태 구분 유지. 새 규격 없음 |
| §2 | 역할 1·2 구현 인계만 정리. 역할 3·4 코드/독립 기준/실제 통합 변경 없음 |
| §4·§5·§9 | Fix 3회·Tool Retry 2회, 불변 Snapshot·동일 환경·재개 보존 규칙 유지 |
| §8 | 비밀값·Host 직접 실행 fallback 없이 기존 설정/권한/격리 경계 안내 |
| §10·§11-A | 실제 근거와 판정·사용량·비용 구분. 문서/준비 검사/모의 검증을 제품 성공이나 비교 결과로 사용하지 않음 |

45번 당시 작업은 문서 정리뿐이었다. 로컬 링크 대상 59개, CLI `--help`, 문서 내 Python 예제의 구문·기존 import/함수 시그니처, 기존 변경과 신규 문서의 whitespace 검사를 통과했다. Python 예제를 실제 승인 파일에 연결해 실행한 것은 아니다. 서버·유료 LLM·Docker·Run 제출·팀원 통합·전체 회귀 테스트는 45번에서 실행하지 않았다. 후속 코드 보완과 회귀 결과는 [46번](46-platform-audit-hardening.md)에 별도로 기록한다.

[43번에 기록된 442개 회귀 통과](43-approved-llm-cost-accounting.md)는 그 단계의 기록이며 이번에 다시 실행한 결과나 실제 운영 검증이 아니다. 이전 단계에 HTTP listener 검증 기록이 있어도 실제 Provider/Docker 검증과는 구분한다.

커밋 메시지: `역할 1·2 플랫폼 최종 정리 및 인계 문서 추가`

다음 작업 번호는 **44번 실제 실행 준비·확인**이다. 필요한 운영값/환경과 유료 호출 범위를 승인받은 뒤 진행하고, 실제 관측 후 45번 문서의 결과를 갱신한다. 프로젝트 Git commit/push는 하지 않는다.
