# 1. Orchestrator 연동 계약

> 상태: 개발정의서 대조 반영 v0.2  
> 범위: Orchestrator가 로컬 Mock Agent와 협업하기 위한 개발 전 계약  
> 13번에서 Scenario 기준 및 수정·재검증 루프 구현 완료. 다음 Orchestrator 작업은 14번 재개/복구 정책이다. 실제 Agent/MCP 팀 통합은 별도 범위다.

## 1. 목적과 범위

이 문서는 Orchestrator와 Agent 사이의 실행 경계, 식별자, A2A 호출, Workflow 상태 및 결과 전달 규칙을 정의한다. 실제 팀원 Agent가 준비되기 전에도 같은 계약을 구현한 Mock Agent로 Orchestrator 전체 흐름을 개발할 수 있도록 한다.

이번 단계의 범위는 다음과 같다.

- Orchestrator는 사용자 요청과 전체 Workflow를 관리한다.
- Planner, Developer, QA, Security는 각각 독립적인 A2A Server다.
- Orchestrator는 A2A Client로 Agent를 호출하고 Task 결과를 조회한다.
- Agent는 내부적으로 MCP Tool을 사용한다. Orchestrator는 MCP Tool을 직접 호출하지 않는다.
- 모든 Agent 업무 결과는 A2A Task와 Artifact로 추적한다.
- 현재 단계에서는 실제 팀원 Agent나 Web UI와 연결하지 않고, 이 계약을 만족하는 로컬 Mock Agent를 사용한다.

## 1.1 첫 MVP 시나리오

첫 시나리오는 `SCN-001` 회원가입 기능 자동 개발·검증이다. 내부 `scenario_id`는 UUIDv4이며 `SCN-001`은 표시용 Key다. 로그인, OAuth, 이메일 인증, 비밀번호 찾기, 프로필 관리는 첫 MVP에서 제외한다.

| Requirement Key | 검증 기준 요약 | 주 검증 담당 |
| --- | --- | --- |
| `REQ-001` | 유효한 이메일/비밀번호로 사용자 1명이 생성되고 성공 응답 | QA |
| `REQ-002` | 잘못된 이메일은 저장되지 않고 오류 응답 | QA |
| `REQ-003` | 애플리케이션 사전검사와 별도로 DB canonical email UNIQUE 제약을 적용해 동시 중복 가입도 방지 | QA + Security |
| `REQ-004` | 7자 이하 비밀번호 거부, 8자 이상 허용 | QA |
| `REQ-005` | 승인된 비밀번호 해시 정책으로 저장 | Security |
| `REQ-006` | 평문 비밀번호와 저장 Hash가 응답·A2A·로그·Trace에 노출되지 않음 | Security |
| `REQ-007` | 실제 가입 결과와 사용자 응답이 일치 | QA |
| `REQ-008` | 요구사항부터 재검증까지 Trace로 연결 가능 | Orchestrator |

MVP의 비밀번호 정책은 Argon2id, `m=19456 KiB`, `t=2`, `p=1`이며 각 비밀번호마다 고유 Salt를 사용한다. 비교용 이메일은 앞뒤 공백 제거 후 전체 소문자화하며, Gmail dot/plus 등 Provider별 변환은 하지 않는다. 상세 수용 기준은 개발정의서를 기준으로 한다.

## 2. 프로토콜 및 실행 방식

| 항목 | 계약 |
| --- | --- |
| A2A 프로토콜 | 1.0 |
| A2A Python SDK | `a2a-sdk>=1.0.0`, 정확한 해석 버전은 `uv.lock`으로 고정 |
| A2A Binding | HTTP+JSON/REST |
| A2A 요청 버전 | `A2A-Version: 1.0` |
| A2A Content-Type | `application/a2a+json` |
| Agent Card | Agent별 `/.well-known/agent-card.json` 제공 |
| Task 시작 | `POST /message:send`, `returnImmediately: true` |
| Task 상태 조회 | `GET /tasks/{id}` 폴링 |
| Streaming / Push | MVP에서 사용하지 않음 |
| MCP Specification | `2026-07-28` |
| MCP 연결 | Agent 프로세스가 로컬 stdio MCP Server에 연결 |
| MCP SDK | MCP 담당 Agent/MCP 서버에서 공식 Python SDK v2 사용. Orchestrator는 MCP Tool을 직접 호출하지 않음 |

Protocol Version `1.0`과 SDK 배포 버전은 서로 다른 값이다. 비스트리밍은 SSE를 사용하지 않는다는 뜻이다. Agent 작업은 비동기로 실행하며, Orchestrator는 응답으로 받은 Task를 폴링한다.

## 3. 역할과 책임 경계

| 구성요소 | 책임 | 하지 않는 일 |
| --- | --- | --- |
| Orchestrator | Run·Workflow Step 생성, Agent 순서와 의존성 관리, A2A 호출·폴링, 결과 수집, 재작업 결정, 최종 판정, Trace 기록 | 제품 코드 직접 수정, MCP Tool 직접 호출, QA/Security 결과 임의 변경 |
| Planner Agent | 자연어 요청을 요구사항과 Acceptance Criteria로 구조화하고 작업 계획 생성 | 제품 코드 수정, 테스트 결과 조작 |
| Developer Agent | 승인된 요구사항 구현·수정, 변경 결과와 코드 Artifact 반환, MCP로 Build 수행 | 요구사항이나 검증 기준 임의 변경 |
| QA Agent | 고정된 코드 Snapshot에 대한 독립 기능 테스트 및 보고서 생성 | Source Snapshot 수정, 보안 최종 판정 |
| Security Agent | 고정된 코드 Snapshot의 보안 분석·재현 검사 및 보고서 생성 | Source Snapshot 수정, QA 판정 변경 |
| MCP Server | Agent가 요청한 허용 Tool을 입력 검증·권한 확인 후 실행 | 제품의 전체 성공/실패 판정 |

역할과 경로 권한은 Prompt만으로 제한하지 않는다. MCP Server 및 Snapshot 저장소가 실제 권한을 강제한다.

## 4. 식별자 계약

기계 식별자는 UUIDv4를 사용한다. 사람이 보는 `REQ-001`, `SCN-001`은 표시용 Key이며 내부 식별자와 분리한다.

| 식별자 | 생성 주체 | 범위 및 용도 |
| --- | --- | --- |
| `run_id` | Orchestrator | 사용자 요청 1회의 전체 실행. 모든 Agent와 이벤트를 연결한다. |
| `workflow_step_id` | Orchestrator | Planner 실행, 개발, QA 등 Orchestrator 내부 논리 작업 |
| `a2a_task_id` | 해당 Agent Server | A2A Task의 공식 ID. 새 Task ID를 Orchestrator가 임의 지정하지 않는다. |
| `agent_context_id` | 해당 Agent Server | 해당 Agent Server의 A2A Context. opaque 값으로 취급한다. |
| `messageId` | Message 생성자 | A2A Message 식별자 |
| `artifact_id` | Artifact Registry | 프로젝트 전체에서 유일한 Artifact 레코드 ID(UUIDv4) |
| `a2a_artifact_id` | 해당 Agent Server | A2A Task 내 공식 Artifact ID |
| `requirement_id` | Requirement Registry | 요구사항 내부 UUIDv4 |
| `requirement_key` | Requirement Registry | 사람이 보는 표시용 Key, 예: `REQ-001` |
| `scenario_id` | Scenario Registry | 시나리오 내부 UUIDv4 |
| `scenario_key` | Scenario Registry | 사람이 보는 표시용 Key, 예: `SCN-001` |
| `issue_id` | Issue Registry | 결함 UUIDv4 |
| `event_id` | Trace Store | 단일 Trace 이벤트 UUIDv4 |

`run_id`는 여러 Agent를 관통하는 프로젝트 ID다. 각 Agent가 반환한 `agent_context_id`는 그 Agent에만 저장·전달하며 다른 Agent 호출에 재사용하지 않는다. Agent 간 연결은 `run_id`, `workflow_step_id`, Artifact 참조로 수행한다.

## 5. A2A 호출 계약

### 5.1 Agent Card

Orchestrator는 Agent를 호출하기 전에 설정된 주소에서 Agent Card를 조회한다. `supportedInterfaces`에 `protocolBinding: HTTP+JSON`, `protocolVersion: 1.0`이 선언되어야 한다. Agent의 `version`은 Agent 애플리케이션 버전이며 Protocol Version과 별개다.

### 5.2 새 Task 요청

Orchestrator는 새 업무에 대해 Message를 만들고 Agent에 보낸다. 새 Task 요청에서 `taskId`는 생략하며, 해당 Agent의 첫 호출에는 `contextId`도 생략한다. Agent Server가 Task ID와 필요 시 Context ID를 생성해 응답한다. 같은 Agent의 후속 요청은 그 Agent가 발급한 `agent_context_id`를 재사용할 수 있고, `INPUT_REQUIRED` Task를 이어갈 때는 같은 Agent가 발급한 Task/Context ID를 사용한다. 다른 Agent의 Context ID는 재사용하지 않는다.

```http
POST /message:send
Content-Type: application/a2a+json
A2A-Version: 1.0
```

```json
{
  "message": {
    "messageId": "<uuid>",
    "role": "ROLE_USER",
    "parts": [
      {
        "data": {
          "request": "이메일과 비밀번호로 회원가입 기능을 만들어줘."
        },
        "mediaType": "application/json"
      }
    ]
  },
  "configuration": {
    "acceptedOutputModes": ["application/json"],
    "returnImmediately": true
  },
  "metadata": {
    "runId": "<uuid>",
    "workflowStepId": "<uuid>",
    "scenarioId": "<uuid>",
    "attempt": 0,
    "requirementIds": []
  }
}
```

`metadata`는 `message` 내부가 아니라 SendMessage 요청 최상위에 둔다. JSON의 A2A Role 값은 `ROLE_USER` 또는 `ROLE_AGENT`를 사용한다. 프로젝트 metadata의 JSON 필드는 camelCase를 사용한다. UUID 내부 ID와 `REQ-001` 같은 표시용 Key는 혼용하지 않는다.

프로젝트 metadata 검증 Schema는 [`schemas/project/workflow_metadata.schema.json`](../schemas/project/workflow_metadata.schema.json)이다. 이 Schema는 프로젝트 확장만 검증한다. A2A 공식 객체의 규범적 기준은 A2A 1.0 Proto와 SDK이며, 공식 객체를 자체 Schema로 복제하지 않는다.

### 5.3 응답과 Task 조회

실제 Workflow Step은 추적 가능한 Task 응답을 요구한다. 응답의 `task.id`를 `a2a_task_id`로 저장하고, 응답에 포함된 `contextId`는 해당 Agent 전용으로 저장한다. Task ID는 opaque 문자열로 취급한다.

```json
{
  "task": {
    "id": "agent-server-generated-task-id",
    "contextId": "planner-server-context-id",
    "status": {
      "state": "TASK_STATE_SUBMITTED",
      "timestamp": "2026-09-28T10:00:00Z"
    },
    "metadata": {
      "runId": "<uuid>",
      "workflowStepId": "<uuid>",
      "scenarioId": "<uuid>",
      "attempt": 0
    }
  }
}
```

```http
GET /tasks/{a2a_task_id}
A2A-Version: 1.0
```

Orchestrator는 조회한 A2A Task가 terminal 또는 interrupted 상태가 될 때까지 폴링한다. `INPUT_REQUIRED`와 `AUTH_REQUIRED`에서는 폴링을 멈추고 각각 사용자 입력 대기 또는 Human Review로 넘긴다. 필수 Step에서 Task 대신 즉시 Message만 반환하면 A2A 자체 오류는 아닐 수 있지만, 프로젝트 계약 위반으로 처리한다.

### 5.4 프로젝트 metadata

프로젝트 필드는 A2A 객체를 확장하거나 대체하지 않고 `metadata`로 전달한다.

| 필드 | 타입 | 필수 | 규칙 |
| --- | --- | --- | --- |
| `runId` | UUID 문자열 | 예 | 전체 실행 ID |
| `workflowStepId` | UUID 문자열 | 예 | 현재 내부 Step ID |
| `scenarioId` | UUID 문자열 | 예 | 시나리오 내부 ID |
| `attempt` | 정수 | 예 | Agent 호출 시도 번호. 최초 호출은 0 |
| `requirementIds` | UUID 문자열 배열 | 아니오 | 관련 내부 요구사항 ID |
| `codeVersion` | 양의 정수 | 조건부 | 코드 Snapshot을 대상으로 하는 Step에서 필수 |
| `projectArtifactIds` | UUID 문자열 배열 | 아니오 | Artifact Registry에서 조회할 프로젝트 Artifact ID |

`runId`, `workflowStepId`, `scenarioId`, `attempt`를 모든 업무 호출에 포함한다. 비밀번호, API Key, Token 등 비밀정보는 Message, metadata, Artifact, Trace에 넣지 않는다.

### 5.5 A2A 결과와 오류 해석

| Agent 응답 | Orchestrator 처리 |
| --- | --- |
| `TASK_STATE_SUBMITTED` / `TASK_STATE_WORKING` | Task를 저장하고 상태 조회 계속 |
| `TASK_STATE_COMPLETED` + 결과 Artifact PASS | Agent Step 완료. 전체 Verdict는 아직 계산하지 않음 |
| `TASK_STATE_COMPLETED` + QA/Security Artifact FAIL | Agent 업무는 정상 완료, 제품 결함이므로 `FIX_REQUIRED` |
| `TASK_STATE_FAILED` 또는 Polling Timeout | Agent 실행 실패. 제품 결함으로 단정하지 않고 재시도 후 검증 불가로 처리 |
| `TASK_STATE_INPUT_REQUIRED` | `WAITING_INPUT`으로 전환 |
| `TASK_STATE_AUTH_REQUIRED` | 자동으로 자격증명을 만들거나 전달하지 않고 `HUMAN_REVIEW` |
| `TASK_STATE_REJECTED` | 역할·Capability·정책 위반 원인을 기록하고 `HUMAN_REVIEW` 후보로 처리 |
| A2A 요청 Schema/인증 오류 | 입력 또는 설정 오류로 기록. 같은 요청을 무조건 재시도하지 않음 |

전송 오류, Agent 실행 오류, 정상 실행 후 생성된 QA/Security FAIL 보고서는 서로 다른 오류 유형으로 기록한다.

## 6. Artifact와 코드 Snapshot 계약

Agent는 결과물을 A2A Task의 Artifact로 반환한다. Orchestrator는 A2A의 `a2a_artifact_id`와 별도로 전역 `artifact_id`를 Artifact Registry에 등록한다.

| 결과 | Artifact 예시 | 필수 연결 정보 |
| --- | --- | --- |
| Planner | Requirements, Acceptance Criteria, Task Plan | `run_id`, `workflow_step_id`, 요구사항 ID |
| Developer | Source Snapshot, Build Report, Change Report | `codeVersion`, `commitHash`, `treeHash`, `snapshotSha256` |
| QA | Test Case, QA Report, Issue | 검사한 `codeVersion`, `snapshotSha256` |
| Security | Security Report, Finding, Issue | 검사한 `codeVersion`, `snapshotSha256` |

Task Artifact 예시에서 공식 `artifactId`는 해당 Task 범위의 opaque ID다. 전역 UUID `projectArtifactId`는 Artifact Registry의 별도 ID이며 둘을 같은 필드로 취급하지 않는다.

```json
{
  "artifactId": "planner-artifact-1",
  "name": "requirements.json",
  "description": "Structured requirements for the requested feature",
  "parts": [
    {
      "data": {
        "schemaVersion": 1,
        "requirements": [
          {
            "requirementId": "<uuid-v4>",
            "key": "REQ-001",
            "description": "유효한 이메일과 비밀번호로 계정을 만든다.",
            "acceptanceCriteria": ["유효한 입력이면 사용자 1건이 생성된다."]
          }
        ],
        "implementationPlan": [
          {
            "taskId": "TASK-001",
            "title": "회원가입 API 구현",
            "description": "Requirement와 Acceptance Criteria를 구현한다.",
            "requirementIds": ["<위 requirementId>"]
          }
        ]
      },
      "mediaType": "application/json"
    }
  ],
  "metadata": {
    "runId": "<uuid>",
    "workflowStepId": "<uuid>",
    "projectArtifactId": "<uuid>",
    "artifactVersion": 1
  }
}
```

Planner `requirements.json` payload의 전체 필수 조건은 [`10-planner-output-developer-dispatch.md`](10-planner-output-developer-dispatch.md)와 [Planner Output Schema](../schemas/project/planner_output.schema.json)를 따른다.

Build, QA, Security 결과는 반드시 동일한 불변 코드 Snapshot과 동일한 실행환경을 가리켜야 한다. 코드가 수정되면 기존 Artifact를 덮어쓰지 않고 새 `codeVersion` 및 Artifact를 만든다. Agent 간에는 로컬 파일 경로만 전달하지 않고, 수신 Agent가 권한을 확인해 가져올 수 있는 Artifact Registry 참조를 전달한다.

## 7. Workflow 실행 계약

### 7.1 정상 흐름

```text
요청 접수
→ Planner
→ Developer 구현
→ 코드 Snapshot 고정
→ Build
→ QA와 Security 검증
→ Orchestrator 판정
```

QA와 Security는 동일 Snapshot이 고정되고 Build가 통과한 뒤 병렬 실행할 수 있다. 둘 다 완료되어야 전체 판정이 가능하다.

### 7.2 수정 흐름

```text
QA 또는 Security에서 확인된 결함
→ Issue Artifact 등록
→ `FIX_REQUIRED`
→ `FIXING`에서 새 Developer 수정 Step/Task
→ 새 Snapshot 생성
→ Build 재실행
→ QA와 Security 모두 재검증
```

터미널 상태의 A2A Task를 재시작하지 않는다. 수정은 같은 Agent Context를 필요에 따라 참조할 수 있지만, 반드시 새 Workflow Step과 새 A2A Task로 생성한다.

### 7.3 초기 반복 한도

| 항목 | 계약 |
| --- | --- |
| 코드 수정 | 최초 구현 이후 최대 3회 |
| MCP Tool 오류 재시도 | 최초 호출 외 최대 2회 |
| 수정 후 같은 Issue가 2회 연속 재발 | 자동 진행 중단 후 `HUMAN_REVIEW` |
| 입력 Schema 또는 권한 오류 | 자동 재시도하지 않음 |
| 검증 환경 오류 | 정해진 Tool 재시도 후에도 해결되지 않으면 `UNVERIFIED` |

수정 횟수와 Tool 재시도 횟수는 별도로 센다. 오류별 재시도 결정과 전체 Workflow 전이는 [`04-state-machine.md`](04-state-machine.md)에서 관리한다.

## 8. 상태와 판정 계약

### 8.1 A2A Task 상태

Agent 한 개의 Task 상태는 A2A 공식 enum을 그대로 저장한다.

```text
TASK_STATE_UNSPECIFIED
TASK_STATE_SUBMITTED
TASK_STATE_WORKING
TASK_STATE_COMPLETED
TASK_STATE_FAILED
TASK_STATE_CANCELED
TASK_STATE_INPUT_REQUIRED
TASK_STATE_REJECTED
TASK_STATE_AUTH_REQUIRED
```

`TASK_STATE_COMPLETED`는 Agent 업무가 완료됐다는 뜻이다. QA Report의 내용이 PASS인지, 전체 제품이 성공했는지는 별도 판정한다.

### 8.2 Orchestrator Workflow 상태

| 상태 | 뜻 |
| --- | --- |
| `RECEIVED` | 요청 접수 |
| `PLANNING` | Planner 실행 중 |
| `WAITING_INPUT` | 사용자 또는 사람의 추가 입력 대기 |
| `IMPLEMENTING` | Developer 최초 구현 중 |
| `SNAPSHOT_READY` | 불변 Snapshot 준비 완료 |
| `VALIDATING` | 최초 Build/QA/Security 검증 중 |
| `FIX_REQUIRED` | 확인된 제품 결함으로 수정 필요 |
| `FIXING` | Developer가 결함을 수정 중 |
| `REVALIDATING` | 수정 Snapshot 재검증 중 |
| `HUMAN_REVIEW` | 자동 처리를 멈추고 사람 판단 대기 |
| `FINISHED` | 최종 Verdict 생성 후 정상 종료 |
| `ABORTED` | 취소 또는 운영자 중단 |

허용 전이, pause/resume, 3회 수정 한도, 전역 취소 API와 전이표의 예외 해석은 [`04-state-machine.md`](04-state-machine.md)를 따른다.

### 8.3 최종 Verdict

| Verdict | 조건 |
| --- | --- |
| `SUCCESS` | 동일 Snapshot/환경의 Build, 필수 Requirement, QA, Security가 모두 PASS이며 필수 미검증 항목이 없음 |
| `FAIL` | 확인된 제품 결함이 수정 한도 후에도 남거나 복구 불가능한 필수 Requirement 실패가 확정됨 |
| `UNVERIFIED` | 제품 결함 판정이 아니라 검증 Tool/환경 문제로 필수 검증을 완료하지 못함 |
| `HUMAN_REVIEW` | 요구사항 충돌, 반복 결함, 정책 선택 등 사람 판단이 필요함 |

`FIX_REQUIRED`는 진행 상태이고 Verdict가 아니다. `ABORTED`도 Workflow 종료 상태이며 Final Verdict와 분리한다. Orchestrator는 LLM 자연어 응답이 아니라 구조화된 Build/Test/Security 결과로 판정한다.

## 9. Orchestrator API 초안

Web 담당자와 연결하기 전까지 계약 초안으로 사용한다.

| API | 용도 |
| --- | --- |
| `POST /runs` | 사용자 요청으로 Run 생성 |
| `GET /runs/{runId}` | Workflow 상태 및 Final Verdict 조회 |
| `GET /runs/{runId}/steps` | Step별 Agent·A2A Task 상태 조회 |
| `GET /runs/{runId}/events` | Trace 이벤트 조회 |
| `GET /runs/{runId}/artifacts` | 결과 Artifact 목록 조회 |
| `POST /runs/{runId}/cancel` | 실행 취소 요청. 활성 A2A Task가 있으면 취소를 요청하고 결과를 저장 |

응답은 프로젝트 내부 UUID, Workflow 상태, A2A Task 상태, 관련 Artifact ID를 혼동 없이 별도 필드로 제공한다.
로컬 API prefix, request/response JSON, pagination, 저장 및 미연결 Agent dispatch 범위는 [`08-workflow-storage-run-api.md`](08-workflow-storage-run-api.md)를 따른다.

## 10. Mock Agent 수용 기준

로컬 Mock Agent는 실제 Agent와 같은 Agent Card 및 A2A 계약을 제공한다. 다음 시나리오를 지원해야 한다.

1. 정상 흐름: Planner → Developer → Build PASS → QA PASS 및 Security PASS → `SUCCESS`
2. 수정 흐름: QA 또는 Security FAIL → Developer 새 Task와 새 Snapshot → 전체 재검증 PASS
3. 실행 오류: Agent Task `FAILED` 또는 Tool/환경 오류 → 제품 `FAIL`과 구분하여 `UNVERIFIED` 등 정책에 맞게 처리
4. 한도 초과: 수정 3회 또는 Tool Retry 한도 소진 후 정의된 Verdict로 종료

Mock은 Orchestrator 계약을 개발하기 위한 대체 구현이며 실제 팀원 Agent 연동은 다음 작업 범위다.

## 11. 개발 완료 기준

- 식별자 생성 주체와 용도가 구분되어 있다.
- 새 A2A Task 요청에서 Orchestrator가 Task ID를 임의 발급하지 않는다.
- Agent별 A2A Context를 분리해 저장한다.
- Orchestrator는 A2A로 Agent를 호출하고, Agent가 MCP를 사용한다.
- Task 상태와 Workflow 상태, Final Verdict가 별도 값으로 관리된다.
- 모든 코드 검증 결과는 같은 불변 Snapshot/환경에 연결된다.
- Mock Agent만으로 정상·수정·미검증 종료 흐름을 표현할 수 있다.

## 12. 후속 명세와의 관계

이 문서는 첫 개발 단계의 계약 요약이다. 상세 항목은 후속 개발 문서에서 구체화한다.

| 후속 작업 | 상세 문서 |
| --- | --- |
| 서비스 실행·설정 | `02-orchestrator-bootstrap.md` |
| 도메인 모델·상태 상수 | 이후 Workflow Model 명세 |
| 실제 A2A 객체 Schema | `03-a2a-contract.md` 및 공식 Proto 정의 |
| 전체 상태 전이·재시도 | [`04-state-machine.md`](04-state-machine.md) |
| Snapshot/Artifact 접근 | [`05-code-handoff.md`](05-code-handoff.md), `09-version-policy.md` |
| 최종 Verdict | `10-verdict-policy.md` |
| 이벤트 저장 형식 | `11-trace-schema.md` |
