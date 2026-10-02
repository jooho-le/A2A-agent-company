# 9. Planner Agent 설정과 Run Dispatch

> 상태: Planner A2A Dispatch 구현 완료
> 범위: Agent URL Registry, Run 제출 background dispatch, Planner Task polling과 Workflow 상태 연결
> 다음 작업: 13번 — 권위 있는 Requirement 기준 및 수정·재검증 루프

## 목적

8번 Run API에서 생성한 pending Planner Step을 설정된 Planner Agent에 실제 위임한다. A2A 1.0 Agent Card 검증, Task 실행/polling은 기존 `A2AAgentClient`와 `A2ATaskRunner`를 재사용하며, 저장소 observer로 실행 중 snapshot을 보존한다.

## Agent endpoint 설정

| Environment | Agent Role |
| --- | --- |
| `ORCHESTRATOR_PLANNER_AGENT_URL` | `PLANNER` |
| `ORCHESTRATOR_DEVELOPER_AGENT_URL` | `DEVELOPER` |
| `ORCHESTRATOR_QA_AGENT_URL` | `QA` |
| `ORCHESTRATOR_SECURITY_AGENT_URL` | `SECURITY` |

URL은 Agent Card discovery에 사용할 Base URL이다. interface endpoint는 Agent Card에서 가져오며 HTTP+JSON/A2A 1.0, JSON 입출력, 동일 origin 조건을 [`06-a2a-client.md`](06-a2a-client.md)에 따라 검증한다. 현재 실행 Flow는 Planner URL만 소비하고 다른 role URL은 후속 단계에서 사용한다.

## 호출 순서

```text
POST /api/v1/runs
  → Run + PENDING Planner Step + 접수 Trace transaction
  → BackgroundTasks에 dispatch 예약 (Planner URL이 설정된 경우)
  → DB에서 RECEIVED Run / PENDING Planner를 원자적으로 claim
  → Run PLANNING + Step RUNNING + dispatch Trace 저장
  → GET Agent Card → A2A 1.0 계약 검증
  → A2ATaskRunner.submit_and_wait({"request": requestText})
  → 각 Step/AgentContext/Trace snapshot을 observer transaction으로 저장
  → disposition을 Workflow 상태로 해석
```

Planner `agent_id`는 Context mapping용 고정 key `planner`다. 첫 호출은 Agent `contextId`를 보내지 않는다. `runId`, `workflowStepId`, `scenarioId`, `attempt=0`은 Task Runner가 프로젝트 metadata에 넣는다. Request 원문은 API 응답·Trace·일반 로그에 복제하지 않는다.

저장소의 `claim_planner_dispatch()`가 Run과 Step을 함께 바꾸므로 동일 Run의 중복 BackgroundTask는 두 번째 A2A 전송을 하지 않는다. claim 전에 취소됐거나 이미 처리 중/완료된 Run도 중복 dispatch하지 않는다. `WORKFLOW_STATE_CHANGED`, `WORKFLOW_STEP_DISPATCH_STARTED`, `A2A_DISPATCH_REQUIRES_REVIEW`는 공식 A2A event가 아니라 프로젝트 Trace Schema의 자유 문자열 `eventType`을 사용하는 프로젝트 보조 이벤트다.

## Run 상태 매핑

| Planner A2A disposition | Workflow Run | 정책 |
| --- | --- | --- |
| `COMPLETED` | 10번 후속 처리 전에는 `PLANNING`; 유효한 Plan은 `IMPLEMENTING`, 무효 Plan은 `HUMAN_REVIEW` | Agent Task만 완료. 전체 Run/Verdict를 완료 처리하지 않음 |
| `WAITING_INPUT` | `WAITING_INPUT` | `resume_state=PLANNING`; 사용자 입력을 기다림 |
| `HUMAN_REVIEW` | `HUMAN_REVIEW` | 인증/거부 등 자동 진행 중단 |
| `AGENT_FAILED`, `CANCELED`, `PROTOCOL_ERROR`, `POLLING_TIMEOUT` | `HUMAN_REVIEW` | A2A/Agent 실패를 제품 결함으로 보지 않음. 자동 재전송 없이 기록·검토 |
| A2A 호출 예외 | `HUMAN_REVIEW` | 오류 종류만 로그에 남기고 예외 본문/request를 로그에 복사하지 않음 |

Planner 결과의 Artifact ID와 A2A state는 Step/Trace에서 조회할 수 있다. 10번에서 Requirements/Acceptance Criteria/Task Plan payload를 검증한 뒤에만 Developer Step으로 넘긴다. Planner Task 완료 자체는 Workflow나 제품의 성공을 의미하지 않는다.

## 미설정·장애·내구성 경계

- Planner URL이 없으면 Run 저장은 허용하고 제출 응답의 `dispatchStatus`를 `NOT_CONFIGURED`로 반환한다. 해당 Run은 `RECEIVED`와 `PENDING` 상태로 남아 자동 실행되지 않는다.
- URL이 있으면 응답의 `dispatchStatus`는 `SCHEDULED`다. FastAPI `BackgroundTasks`는 현재 프로세스 내부 MVP 실행기이며 durable queue가 아니다.
- 프로세스가 A2A SendMessage 도중 종료되거나 전송 응답이 불명확하면 자동 재전송하지 않는다. `RUNNING` Step/Run을 복구 확인 대상으로 남긴다. 메시지 적용 여부를 알 수 없을 때 중복 실행하지 않는다는 개발정의서 원칙을 따른다.
- 개발정의서는 `TASK_STATE_FAILED`/Polling Timeout을 제품 결함과 구분하고 재시도 후 검증 불가로 처리하도록 하지만, A2A 재시도 횟수·단위는 MCP Tool Retry와 별도로 고정하지 않았다. 이 구현은 횟수를 임의로 만들지 않아 Agent 실패/timeout을 `HUMAN_REVIEW`에 남긴다. 확정된 A2A retry policy와 `UNVERIFIED` 전이는 후속 보완이 필요하다.
- Planner Task `COMPLETED`는 Report/Plan PASS 또는 제품 성공과 동의어가 아니다.

운영에 durable queue, process restart 후 자동 재개, 활성 A2A Task 취소가 필요하면 별도의 lease/outbox/recovery 정책을 합의해야 한다. 현재 claim은 중복 dispatch 억제 경계이지 worker crash recovery를 제공하지 않는다.

## 검증 및 정의서 대조

| 개발정의서 항목 | 결과 |
| --- | --- |
| 역할별 Agent Base URL 분리 | 반영; Planner/Developer/QA/Security 설정 독립 |
| Agent Card로 A2A Protocol 1.0 확인 | 기존 Client resolution과 계약 검사 사용 |
| Run에서 Planner Task 제출 및 polling | 반영; 기존 A2ATaskRunner 연결 |
| Run/Step/Context/Trace 일관성 | 반영; claim 및 observer transaction |
| A2A 결과를 제품 Verdict로 오판하지 않음 | 반영; Planner Completed 후에도 Run은 PLANNING, Verdict 없음 |
| uncertain A2A side effect 재전송 | 자동 재시도하지 않고 HUMAN_REVIEW; 결과 불명 요청 중복 방지 |
| Agent FAILED/Timeout 후 retry 소진 시 UNVERIFIED | 부분 반영; 별도 A2A retry count가 미정이라 현재는 HUMAN_REVIEW |
| 전체 Planner→Developer→QA→Security workflow | 부분 구현; 10번에서 Developer dispatch까지 연결, Build/QA/Security와 최종 Verdict는 후속 단계 |
| Background worker crash recovery / durable queue | 미구현; 프로세스 내부 BackgroundTasks 한계 명시 |

테스트는 Registry role 분리, 중복 dispatch claim, Planner Task COMPLETED/INPUT_REQUIRED/failure 해석, A2A payload 및 Context 저장, API background scheduling을 검증한다.

## 다음 작업

10번에서 Planner가 돌려준 Artifact Schema·Requirement ID·Acceptance Criteria를 검증하고 Developer WorkflowStep을 생성했다. 11번에서 Developer Artifact 검증과 Snapshot/Build/QA/Security handoff, 12번에서 QA/Security 결과 검증 및 Verdict 상태 기록을 연결했다. 권위 Requirement 기준과 자동 수정·재검증은 후속 작업이다.
