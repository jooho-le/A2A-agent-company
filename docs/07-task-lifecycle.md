# 7. A2A Task Lifecycle과 Workflow Step 연결

> 상태: 실행·상태 반영 경계 구현 완료
> 범위: A2A Task 제출/이어가기/폴링, WorkflowStep·AgentContext 갱신, Trace Event 생성
> 현재 계약·지원 API·남은 경계는 [14번 정의서 준수 보완](14-orchestrator-contract-compliance.md)을 따른다. 이 문서의 단계별 미완료 항목·검증 수치는 당시 이력이며, 실제 Agent/MCP 팀 통합은 별도 범위다.

## 목적

A2A Task, Workflow Step, 전체 Workflow Run 상태는 서로 다른 생명주기다. 이 계층은 Agent-issued Task ID와 Context ID를 각 Workflow Step/AgentContext에 연결하고, Task를 terminal 또는 interrupted 상태까지 조회한다. `TASK_STATE_COMPLETED`는 해당 Agent 업무의 완료일 뿐, QA/Security 결과나 전체 제품 Verdict를 결정하지 않는다.

## 구현 위치

| 경로 | 내용 |
| --- | --- |
| `src/orchestrator/application/a2a_tasks.py` | Task submit/continue/poll, Step·Context snapshot 갱신, 결과 disposition |
| `src/orchestrator/application/__init__.py` | Task Runner 공개 API |
| `src/orchestrator/a2a/client.py` | A2A 새 Task, interrupted Task continuation, Task 조회 Client |
| `src/orchestrator/domain/trace.py` | 프로젝트 Trace Event 모델과 camelCase JSON 직렬화 |
| `schemas/project/trace_event.schema.json` | 개발정의서의 Trace Event JSON Schema |
| `tests/test_a2a_tasks.py` | 상태 매핑, 이어가기, polling timeout/resume, Snapshot·Trace 회귀 테스트 |

## 호출 경로

```text
WorkflowRun + PENDING WorkflowStep
  → A2A_MESSAGE_SENT Trace
  → POST /message:send
  → A2A_TASK_RECEIVED Trace + AgentContext/WorkflowStep 갱신
  → GET /tasks/{agent-issued-id}
  → A2A_TASK_STATE_CHANGED Trace + 상태 갱신
  → terminal / interrupted 결과 반환
```

`A2ATaskRunner.submit_and_wait()`는 일반 JSON payload를 보내고, `submit_snapshot_and_wait()`는 QA/Security 역할·Run·Code Version·입력 Artifact가 Handoff와 일치하는지 검사한 뒤 동일한 polling 경로를 사용한다. 기존 `AgentContext`가 주어지면 같은 `agent_id`의 Context ID만 다음 요청에 실어 보낸다. 다른 Agent의 Context는 허용하지 않는다.

새 Task는 서버 응답에서 받은 ID를 그대로 Step에 저장한다. Polling URL에 넣을 때만 SDK Client의 경로 segment를 위해 opaque ID를 인코딩하며, 저장 ID 자체를 파싱·변형하지 않는다. A2A Artifact ID는 `WorkflowStep.a2a_artifact_ids`에 저장하고, 프로젝트 전역 Artifact UUID인 `input_artifact_ids`/`output_artifact_ids`와 섞지 않는다.

## interrupted Task 이어가기

`INPUT_REQUIRED`에서는 기존 Step을 `WAITING_INPUT`으로 반환한다. 사용자 입력을 받은 뒤 `continue_after_input()`을 호출하면 같은 Agent-issued Task ID와 해당 Agent Context ID로 `continue_task()`를 전송하고 A2A 호출 `attempt`를 증가시킨다. 이 응답은 기존 Task ID를 유지해야 한다. `AUTH_REQUIRED`와 `REJECTED`는 `HUMAN_REVIEW` disposition이므로 자동으로 인증정보를 만들거나 보내지 않고 이 메서드로 이어가지 않는다.

## A2A 상태와 Orchestrator 동작

| A2A Task 상태 | WorkflowStep 상태 | Runner disposition | 동작 |
| --- | --- | --- | --- |
| `SUBMITTED`, `WORKING` | `RUNNING` | `IN_PROGRESS` | bounded polling 계속 |
| `COMPLETED` | `SUCCEEDED` | `COMPLETED` | Agent 업무 완료. 결과 Artifact의 PASS/FAIL은 별도 판정 |
| `FAILED` | `FAILED` | `AGENT_FAILED` | Agent 실행 오류. 제품 결함으로 간주하지 않음 |
| `CANCELED` | `CANCELED` | `CANCELED` | Step 취소 |
| `INPUT_REQUIRED` | `WAITING_INPUT` | `WAITING_INPUT` | 사용자 입력 대기 후 같은 Task 이어가기 가능 |
| `AUTH_REQUIRED` | `WAITING_INPUT` | `HUMAN_REVIEW` | 인증 자동 생성/전송 금지, 사람 판단 |
| `REJECTED` | `FAILED` | `HUMAN_REVIEW` | 역할/정책 원인을 검토 |
| `UNSPECIFIED` | `FAILED` | `PROTOCOL_ERROR` | 비정상 상태로 종료하고 상위 Workflow에서 분류 |

Runner는 `WorkflowRun.status`나 `verdict`를 바꾸지 않는다. 호출자는 disposition을 보고 [`transition_run()`](../src/orchestrator/domain/state_machine.py) 등 상위 정책으로 Run을 전이한다. 특히 `COMPLETED != SUCCESS`, `AGENT_FAILED != 제품 FAIL`이다.

## Polling 한도와 재개

`TaskPollingPolicy`는 기본적으로 1초 간격, 300초 제한을 사용한다. 이는 실행 가능한 MVP 운영 기본값이며 A2A 공식 규칙이나 개발정의서의 고정 값은 아니다. 호출자는 정책을 주입해 환경에 맞게 조정할 수 있다. 시간은 monotonic clock을 기준으로 계산한다.

제한 시간 내 terminal/interrupted 상태가 관찰되지 않으면 disposition은 `POLLING_TIMEOUT`이다. 마지막 공식 Task 상태와 Step 상태는 `RUNNING`/`SUBMITTED` 또는 `WORKING` 그대로 두며, timeout을 `TASK_STATE_FAILED`로 위조하지 않는다. `A2A_POLL_TIMED_OUT` 이벤트를 남기고 결과를 반환한다. `resume_polling()`은 저장된 Agent-issued Task/Context를 검증해 같은 Task 관찰을 이어간다. Agent 실행 재시도 및 최종 `UNVERIFIED` 결정은 상위 Workflow 정책이 담당하며 Runner는 자동 재전송하지 않는다.

## Step/Context/Trace 기록 경계

Runner는 입력 모델을 직접 바꾸지 않고 새 `WorkflowStep`, `AgentContext`, `TraceEvent` snapshot을 반환한다. 선택적 async `observer(step, agent_context, event)`는 다음 업데이트마다 호출된다. [`SQLiteWorkflowRepository.task_update_observer(run)`](../src/orchestrator/infrastructure/sqlite_workflows.py)는 이 callback을 Step/Context/Event 한 transaction 저장에 연결한다.

기록 이벤트는 `A2A_MESSAGE_SENT`, `A2A_TASK_RECEIVED`, 상태 변경 시 `A2A_TASK_STATE_CHANGED`다. 상태가 그대로여도 Artifact/Context snapshot이 변하면 프로젝트 보조 이벤트 `A2A_TASK_UPDATED`를 만든다. Timeout에는 `A2A_POLL_TIMED_OUT`을 기록하고 실제 `a2aTaskState`는 그대로 둔다. Trace Event는 개발정의서의 camelCase field/schema를 따르며 payload 본문이나 비밀정보는 기록하지 않는다.

## 검증 및 정의서 대조

```bash
PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -p 'test_a2a_tasks.py' -v
PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -v
```

검증 항목: Task ID/Context ID 유지, agent 간 Context 분리, 정상 terminal polling, terminal Agent 오류와 제품 Verdict 분리, input/auth/reject 해석, 동일 Task input continuation, timeout 후 재개, Snapshot Handoff 일치성, Trace Schema field shape.

| 개발정의서 항목 | 결과 |
| --- | --- |
| Agent-issued Task/Context ID를 opaque로 관리 | 반영, Context 교차 재사용 차단 |
| `/tasks/{id}`를 terminal/interrupted 상태까지 polling | 반영, 입력/인증 대기에서는 안전하게 멈춤 |
| Task/Workflow/Verdict 상태를 분리 | 반영, Runner는 Run Verdict를 수정하지 않음 |
| `INPUT_REQUIRED` 후 같은 Task에 후속 Message | 반영, 같은 Agent Task/Context ID 유지 및 attempt 증가 |
| Agent 실패/timeout을 제품 결함으로 오판하지 않음 | 반영, 별도 disposition, timeout은 Task state를 덮지 않음 |
| Trace `A2A_MESSAGE_SENT`, `A2A_TASK_RECEIVED`, `A2A_TASK_STATE_CHANGED` 기록 | 반영, observer로 저장할 수 있는 event 모델 제공 |
| DB에 Task/Context/Event transaction 영속화 | 8번에서 SQLite transaction으로 구현 |
| 자동 Agent 재시도 및 전체 Run 전이/최종 Verdict | 상위 Workflow 통합 범위, 미구현 |

## 다음 작업

8번에서 저장소/API, 9번에서 Planner Run Dispatch와 Runner 연결, 10번에서 검증된 Planner 결과로 Developer Step 생성 및 A2A 전달을 구현했다. 11번에서 Developer 결과 Artifact 검증과 QA/Security handoff를, 12번에서 QA/Security 결과 Artifact 해석과 상태/Verdict 기록을 연결했다. 고정 기준과 재개 가능한 수정 루프는 13번 작업이다.
