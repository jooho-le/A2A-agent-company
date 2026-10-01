# 3. Workflow 도메인 모델과 상태

> 상태: 구현 완료
> 범위: Run/Step 도메인 모델, Workflow/A2A/Verdict 상태 정의
> 다음 작업: 4번 — 상태 전이와 실패·수정·재시도 정책

## 목적

Orchestrator 내부의 Run과 논리적 Workflow Step을 타입으로 표현한다. 전체 Workflow 상태, 개별 A2A Task 상태, 최종 제품 Verdict는 서로 다른 값으로 보관한다.

## 코드 위치

| 경로 | 내용 |
| --- | --- |
| `src/orchestrator/domain/states.py` | Workflow, Step, A2A Task, Verdict, Agent Role Enum |
| `src/orchestrator/domain/models.py` | `WorkflowRun`, `WorkflowStep` Pydantic 모델 |
| `src/orchestrator/domain/__init__.py` | 주요 도메인 타입 공개 |

## 상태 정의

### Workflow 상태

| 상태 | 의미 |
| --- | --- |
| `RECEIVED` | 요청 접수 |
| `PLANNING` | Planner 실행 중 |
| `WAITING_INPUT` | 추가 입력 대기 |
| `IMPLEMENTING` | 구현 또는 수정 중 |
| `SNAPSHOT_READY` | 불변 코드 Snapshot 준비 완료 |
| `VALIDATING` | 최초 Build/QA/Security 검증 중 |
| `FIX_REQUIRED` | 확인된 결함으로 수정 필요 |
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
| `WorkflowRun` | `run_id`, `scenario_id`, `request_text`, `status`, `verdict`, `code_version`, timestamps | 식별자는 UUIDv4. 빈 요청은 허용하지 않는다. Verdict는 최종 판정용 별도 필드다. |
| `WorkflowStep` | `workflow_step_id`, `run_id`, `agent_role`, `status`, `attempt`, A2A 참조, Artifact ID 목록, `code_version`, timestamps | Step과 Run 식별자는 UUIDv4. 최초 `attempt`는 0. A2A Task/Context ID는 Agent가 발급하는 opaque 문자열이다. |

Pydantic 모델은 미정의 필드를 거부하고, 모델 속성 대입 시에도 필드 검증을 적용한다. `input_artifact_ids`와 `output_artifact_ids`는 새 인스턴스마다 독립된 목록으로 생성된다.

## 설계 경계

- 이 단계는 데이터 모양과 상태 이름만 정의하며 상태 전이 허용 여부를 결정하지 않는다.
- DB 저장, API 입출력 Schema, A2A HTTP Client, Artifact 본문은 후속 작업이다.
- camelCase A2A metadata 직렬화는 A2A Client/Schema 단계에서 처리한다. 내부 Python 도메인 필드는 snake_case다.
- 실제 코드 Snapshot 내용 대신 `code_version`과 Artifact ID 참조만 보관한다.

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

4번에서 허용 상태 전이, 실패·수정 흐름, 재시도 한도와 검증 규칙을 별도 상태 머신으로 정의한다.
