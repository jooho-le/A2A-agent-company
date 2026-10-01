# 6. A2A 1.0 Agent Client와 Snapshot 인계

> 상태: Client 경계 구현 완료
> 범위: Agent Card 검증, A2A 1.0 HTTP+JSON 요청, 프로젝트 metadata 및 Snapshot payload 전달, Task 조회
> 다음 작업: 8번 — Workflow 저장소와 Run API 연결

## 목적

팀원 Agent 서버가 준비되기 전에도 공식 A2A Python SDK를 사용해 실제 HTTP 경계와 동일한 방식으로 Agent를 호출할 수 있도록 한다. A2A 공식 객체는 SDK/Proto 타입을 사용하고, 프로젝트별 실행 정보만 최상위 `metadata`에 추가한다.

## 구현 위치

| 경로 | 내용 |
| --- | --- |
| `src/orchestrator/a2a/client.py` | Agent Card 조회/검증, SDK Client 생성, 새 Task 전송, 단건 Task 조회 |
| `src/orchestrator/a2a/requests.py` | 프로젝트 metadata 모델, SDK `SendMessageRequest` 생성, Snapshot 인계 payload |
| `src/orchestrator/a2a/__init__.py` | A2A Client와 요청 계약 공개 API |
| `tests/test_a2a_client.py` | SDK/Mock HTTP 전송, Agent Card, metadata, Snapshot handoff 계약 회귀 테스트 |

## Agent Card 계약

`A2AAgentClient`는 설정된 Agent base URL의 공식 Agent Card 경로를 SDK의 `A2ACardResolver`로 조회한다. 다음을 만족하지 않으면 Client를 만들지 않는다.

- `supportedInterfaces`에 `protocolBinding: HTTP+JSON`, `protocolVersion: 1.0`이 있어야 한다.
- `version`은 Agent 애플리케이션 버전으로 보존하며 A2A Protocol 버전과 혼동하지 않는다.
- 프로젝트 계약에 따라 기본 입력/출력 모드 모두 `application/json`을 지원해야 한다.
- Agent Card가 반환한 인터페이스는 설정된 Agent와 동일한 scheme/host/port를 사용해야 한다. 다른 origin으로 요청을 유도하는 Card는 거부한다.
- 여러 Protocol 버전이 나열되어 있으면 선택한 A2A 1.0 인터페이스만 SDK에 전달한다. SDK가 더 높은 버전을 선택해 프로젝트 계약을 우회하지 않도록 하기 위함이다.
- A2A 1.0이 없으면 SDK의 `VersionNotSupportedError`, 나머지 계약 위반은 `AgentCardContractError`로 실패한다.

## SendMessage 요청

`build_send_message_request()`는 SDK의 공식 `SendMessageRequest` protobuf를 만든다. JSON payload는 `ROLE_USER` Message의 `application/json` DataPart로 전달하고, 프로젝트 metadata는 Message 내부가 아닌 요청 최상위에 둔다.

```json
{
  "message": {
    "messageId": "<uuid-v4>",
    "role": "ROLE_USER",
    "parts": [{ "data": { "request": "QA 검증을 수행한다." }, "mediaType": "application/json" }]
  },
  "configuration": {
    "acceptedOutputModes": ["application/json"],
    "returnImmediately": true
  },
  "metadata": {
    "runId": "<uuid>",
    "workflowStepId": "<uuid>",
    "scenarioId": "<uuid>",
    "attempt": 0
  }
}
```

새 Task 생성 시 `taskId`를 보내지 않으며, Agent별 첫 호출은 `contextId`도 생략한다. 같은 Agent의 후속 요청은 저장한 Context를 보낼 수 있고, interrupted Task를 이어가는 전용 `continue_task()`는 기존 Agent-issued Task/Context ID를 그대로 보낸다. 요청은 `POST /message:send`이며 전송 Header는 `Content-Type: application/a2a+json`, `A2A-Version: 1.0`이다. SDK Client 설정은 non-streaming, polling 모드로 고정한다. 이 단계의 `polling=True`는 `returnImmediately=true` 요청을 설정하는 의미다. 반복 조회와 Step/Context 상태 기록은 [`07-task-lifecycle.md`](07-task-lifecycle.md)에 구현한다.

Metadata 모델은 [`workflow_metadata.schema.json`](../schemas/project/workflow_metadata.schema.json)의 프로젝트 확장 필드와 일치한다. camelCase alias, 추가 필드 거부, UUIDv4, 음수가 아닌 `attempt`, 중복 없는 ID 배열을 검증한다. 공식 A2A 객체를 별도의 프로젝트 JSON Schema로 재정의하지 않는다.

## Snapshot 인계

`build_snapshot_handoff_request()`와 `send_snapshot_handoff()`는 5번에서 고정한 QA 또는 Security read-only Handoff를 전달한다. Payload에는 `projectArtifactId`, Artifact Registry URI, `READ_ONLY` 권한, 공통 `ExecutionManifest`가 담긴다. 최상위 프로젝트 metadata에는 다음 값을 함께 전달한다.

- `runId`, 호출 Step/Scenario ID, `attempt`
- Manifest의 `codeVersion`
- `projectArtifactIds`에 프로젝트 Artifact UUID
- 호출자가 제공한 `requirementIds`

프로젝트 `projectArtifactId`와 Agent가 Task 안에서 발급하는 A2A `artifactId`는 별개다. Handoff에 해당 QA/Security recipient Grant가 없으면 요청 생성 단계에서 거부한다. 실제 Object Store ACL 강제는 Artifact Registry/저장소 계층 책임이며 이 요청 생성 코드만으로 강제된다고 간주하지 않는다.

## 응답과 오류 경계

- 새 업무 요청은 서버가 발급한 A2A `Task` 응답을 요구한다. 즉시 `Message` 응답만 반환되면 프로젝트 실행 계약 위반으로 처리한다.
- 반환된 Task ID는 SDK 응답에서 그대로 돌려준다. Orchestrator가 Task ID를 새로 만들거나 UUID라고 가정하지 않는다.
- `get_task(task_id)`는 SDK `GetTaskRequest`로 `GET /tasks/{id}`를 한 번 조회한다. 경로 주입을 막기 위해 URL path segment로 인코딩하지만, ID를 UUID로 해석하거나 저장값을 정규화하지 않는다.
- Workflow 성공/실패 판정, terminal Task 상태까지의 retry/poll 주기, Agent별 Context ID 영속화, Run/Step/Event DB 저장은 여기서 수행하지 않는다.
- 네트워크 오류를 임의 재시도하지 않는다. 멱등성이나 서버 반영 여부를 모르는 전송 오류는 후속 Workflow 실행 계층에서 상태 확인 후 처리해야 한다.

## 검증

```bash
PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -p 'test_a2a_client.py' -v
```

Mock HTTP Transport로 Agent Card 조회, 다중 버전 중 1.0 선택, A2A Header/경로, SendMessage payload와 top-level metadata, Agent 발급 Task ID 보존, 단건 Task 조회, Message-only 응답 거부, 다른 origin 거부, Snapshot Manifest 및 권한 Grant 전달을 확인한다.

## 정의서 점검

| 요구사항 | 결과 |
| --- | --- |
| A2A 1.0 HTTP+JSON 및 지정 Content-Type/Version Header | 반영, HTTP Mock 테스트 통과 |
| Agent Card 앱 버전과 Protocol 버전 분리 | 반영 및 테스트 |
| 새 Task ID를 Orchestrator가 임의 생성하지 않음 | 반영, 응답 ID 그대로 사용 |
| 프로젝트 metadata를 SendMessage 최상위에 camelCase로 전달 | 반영, Schema 필드 테스트 |
| Developer Snapshot을 QA/Security에 같은 Manifest로 전달 | 요청 builder에 반영, Manifest/Grant 테스트 |
| terminal/interrupted 상태 polling과 Step/Context/Trace snapshot | [`07-task-lifecycle.md`](07-task-lifecycle.md)에 반영 |
| DB 영속 저장 및 실제 ACL 강제 | 후속 저장소 작업, 미구현 |
| 실제 저장소 read-only ACL | Snapshot/Registry 구현 범위, 미구현 |

## 다음 작업

8번에서는 7번의 update observer를 저장소 transaction에 연결하고, Run/Step 상태 조회 API를 구현한다.
