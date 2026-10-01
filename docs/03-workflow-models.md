# 3. Workflow 도메인 모델과 상태

> 상태: 구현 완료
> 범위: Run/Step 도메인 모델, Workflow/A2A/Verdict 상태 정의
> 다음 작업: 9번 — Agent 연결 설정과 Run dispatch 통합

## 목적

Orchestrator 내부의 Run과 논리적 Workflow Step을 타입으로 표현한다. 전체 Workflow 상태, 개별 A2A Task 상태, 최종 제품 Verdict는 서로 다른 값으로 보관한다.

## 코드 위치

| 경로 | 내용 |
| --- | --- |
| `src/orchestrator/domain/states.py` | Workflow, Step, A2A Task, Verdict, Agent Role Enum |
| `src/orchestrator/domain/models.py` | `WorkflowRun`, `WorkflowStep`, `AgentContext` Pydantic 모델 |
| `src/orchestrator/domain/__init__.py` | 주요 도메인 타입 공개 |
| `src/orchestrator/domain/trace.py` | camelCase Trace Event snapshot 모델 |
| `src/orchestrator/domain/snapshot_handoff.py` | Code Snapshot/Artifact 및 검증 Handoff 도메인 계약 |

상태 전이와 재시도 정책 구현은 [`04-state-machine.md`](04-state-machine.md)에 정의한다.

## 상태 정의

### Workflow 상태

| 상태 | 의미 |
| --- | --- |
| `RECEIVED` | 요청 접수 |
| `PLANNING` | Planner 실행 중 |
| `WAITING_INPUT` | 추가 입력 대기 |
| `IMPLEMENTING` | Developer 최초 구현 중 |
| `SNAPSHOT_READY` | 불변 코드 Snapshot 준비 완료 |
| `VALIDATING` | 최초 Build/QA/Security 검증 중 |
| `FIX_REQUIRED` | 확인된 결함으로 수정 필요 |
| `FIXING` | Developer가 별도 수정 Cycle 수행 중 |
| `REVALIDATING` | 수정 Snapshot 재검증 중 |
| `HUMAN_REVIEW` | 사람의 판단 대기 |
| `FINISHED` | 최종 Verdict 생성 후 정상 종료 |
| `ABORTED` | 취소 또는 운영자 중단 |

### 최종 Verdict

`SUCCESS`, `FAIL`, `UNVERIFIED`, `HUMAN_REVIEW`를 사용한다. `FIX_REQUIRED`는 진행 상태이며 Verdict가 아니다. `ABORTED`는 종료 상태이며 Verdict와 별도로 표현한다.

### A2A Task 상태

`A2ATaskState`는 계약 문서에 정의된 `TASK_STATE_*` 값을 그대로 보관한다. 내부 Enum 멤버 이름에서 `TASK_STATE_` 접두어를 제거했지만, 각 멤버의 직렬화 값은 A2A 상태 문자열과 일치한다. 이 상태를 Workflow 상태나 Step 상태로 대체하지 않는다.

### Step 상태와 Agent 역할

Step 상태는 Orchestrator가 관리하는 `PENDING`, `RUNNING`, `SUCCEEDED`, `FAILED`, `WAITING_INPUT`, `CANCELED`다. Agent 역할은 `PLANNER`, `DEVELOPER`, `QA`, `SECURITY`로 제한한다. Step의 논리적 성공 여부와 Agent가 돌려준 A2A Task 상태는 별도 필드다.

## 모델 필드

| 모델 | 주요 필드 | 규칙 |
| --- | --- | --- |
| `WorkflowRun` | `run_id`, `scenario_id`, `request_text`, `status`, `resume_state`, `verdict`, `code_version`, `fix_attempt`, `termination_reason`, timestamps | 식별자는 UUIDv4. 빈 요청은 허용하지 않는다. `resume_state`는 `WAITING_INPUT`/`HUMAN_REVIEW` 중일 때만 설정된다. `fix_attempt`는 0~3이며 A2A `attempt`와 별도다. `FINISHED`에는 Verdict가 필요하고 `ABORTED`는 Verdict 없이 종료 사유를 갖는다. |
| `WorkflowStep` | `workflow_step_id`, `run_id`, `agent_role`, `status`, `attempt`, `requirement_ids`, A2A 참조, Artifact ID 목록, `code_version`, timestamps | Step과 Run 식별자는 UUIDv4. A2A `attempt`의 최초 값은 0. A2A Task/Context/Artifact ID는 opaque 문자열이며 프로젝트 Artifact ID(UUIDv4)와 분리한다. ID 참조 목록의 중복은 거부한다. |
| `AgentContext` | `run_id`, `agent_id`, `agent_context_id`, `latest_a2a_task_id` | Agent별 Context/최근 Task 매핑. Context와 Task ID는 Agent 서버 범위의 opaque 문자열이다. |

Pydantic 모델은 미정의 필드를 거부하고, 모델 속성 대입 시에도 필드 검증을 적용한다. `FINISHED`와 `ABORTED` 결과 제약도 검증한다. 실제 상태/결과 전이는 [`transition_run()`](../src/orchestrator/domain/state_machine.py)을 통해 허용 간선과 함께 검증한다. `input_artifact_ids`, `output_artifact_ids`, `a2a_artifact_ids` 등 목록은 새 인스턴스마다 독립적으로 생성된다.

## 설계 경계

- Run과 Step은 상태 데이터를 표현하고, 허용 상태 전이는 별도 상태 머신이 검증한다.
- `attempt`는 A2A 호출 시도 번호, `fix_attempt`는 최초 구현 이후 코드 수정 Cycle 수(최대 3회)다. 서로 대체하지 않는다.
- terminal/interrupted 상태까지의 A2A Task 실행 및 Step/AgentContext/Trace 이벤트 snapshot 생성은 [`07-task-lifecycle.md`](07-task-lifecycle.md)에 구현한다. Run/Step/Context/Trace 영속화와 Run API는 [`08-workflow-storage-run-api.md`](08-workflow-storage-run-api.md)에 구현한다. Artifact Registry/Object Store 영속화는 후속 작업이다. A2A HTTP Client 경계는 [`06-a2a-client.md`](06-a2a-client.md)에, Snapshot 도메인과 Manifest Schema는 [`05-code-handoff.md`](05-code-handoff.md)에 정의한다.
- camelCase A2A metadata 직렬화는 A2A Client/Schema 단계에서 처리한다. 내부 Python 도메인 필드는 snake_case다.
- Agent별 Context Mapping은 Task Runner에서 갱신하며 SQLite에 저장한다. 관련 transaction은 [`08-workflow-storage-run-api.md`](08-workflow-storage-run-api.md)를 따른다.
- WorkflowRun은 최신 `code_version`과 Artifact ID를 연결하고, 불변 Source Snapshot metadata는 별도 Artifact 도메인 계약으로 관리한다.

## 사용 예시

```python
from uuid import uuid4

from orchestrator.domain import AgentRole, WorkflowRun, WorkflowStep

run = WorkflowRun(
    scenario_id=uuid4(),
    request_text="회원가입 기능을 구현한다.",
)
step = WorkflowStep(run_id=run.run_id, agent_role=AgentRole.PLANNER)
```

## 다음 작업

8번에서 Run/Step/AgentContext와 Trace 이벤트 저장소 및 Run 조회 API를 구현했다. 9번에서 Agent dispatch와 통합한다.

## 검증

- 실행 명령: `PYTHONPATH=src uv run python -m unittest discover -s tests -p test_workflow_models.py -v`
- Workflow 모델·상태·metadata 계약 단위 테스트: 9개 통과.
- 전체 회귀 테스트에서 FastAPI Liveness 검사도 HTTP 200으로 통과.
