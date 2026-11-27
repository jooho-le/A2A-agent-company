# 8. Workflow 저장소와 Run API

> 상태: 로컬 MVP 구현 완료; Developer Artifact metadata 저장 추가
> 범위: SQLite 영속 저장, Run/Step/AgentContext/Trace/Artifact metadata transaction, Run 제출·조회 API
> 현재 계약·지원 API·남은 경계는 [14번 정의서 준수 보완](14-orchestrator-contract-compliance.md)을 따른다. 이 문서의 단계별 미완료 항목·검증 수치는 당시 이력이며, 실제 Agent/MCP 팀 통합은 별도 범위다.

## 목적

7번에서 반환하던 WorkflowStep, AgentContext, TraceEvent snapshot을 저장하고, 외부 서비스가 Run을 제출하고 상태·Step·Trace를 조회할 HTTP 계약을 제공한다. A2A Task 상태, Workflow 상태, Final Verdict는 각각 별도 필드로 유지한다.

## 저장소

구현은 Python 표준 라이브러리 SQLite를 사용하며 ORM 추가 의존성은 없다. 기본 파일은 프로젝트의 `.data/orchestrator.sqlite3`이고 `.data/`는 Git에서 제외한다. `ORCHESTRATOR_DATABASE_PATH`로 경로를 바꿀 수 있다.

| 테이블 | 키/관계 | 저장 내용 |
| --- | --- | --- |
| `workflow_runs` | `run_id` PK | WorkflowRun JSON snapshot 및 상태/시간 인덱스 |
| `workflow_steps` | `workflow_step_id` PK, `run_id` FK | WorkflowStep JSON snapshot 및 상태/시간 인덱스 |
| `agent_contexts` | `(run_id, agent_id)` PK, `run_id` FK | Agent별 최신 opaque Context/Task 매핑 |
| `trace_events` | 내부 증가 순번, `event_id` UNIQUE, `run_id` FK | Append-only camelCase Trace JSON |
| `project_artifacts` | `artifact_id` PK, `run_id` FK | Source/Change/Build Artifact metadata JSON; UPDATE/DELETE 금지 |

도메인 모델은 검증 후 JSON snapshot으로 저장한다. Trace Event는 개발정의서와 동일한 camelCase schema로 저장한다. Foreign Key와 JSON 유효성 제약을 켜며 Trace는 run 내 append 순서를 보존한다. 로컬 동시 읽기를 위해 SQLite WAL mode를 사용한다.

`create_run()`은 Run, 초기 Step, 초기 Trace를 하나의 transaction으로 기록한다. `save_task_update()`는 최신 Run의 `updatedAt` 갱신, Step upsert, AgentContext upsert, Trace insert를 한 transaction으로 처리한다. Run의 현재 상태/Verdict를 observer가 가진 오래된 snapshot으로 덮지 않는다. `task_update_observer(run)`는 A2ATaskRunner의 async observer에 직접 전달할 수 있고, SQLite I/O는 worker thread에서 수행한다. Run 상태 전이와 Trace 변경은 `save_run_update()`에서 원자적으로 저장한다.

SQLite는 단일 노드 개발/MVP 기준이다. 서버 다중 인스턴스 운영, 스키마 migration 체계, 백업/복구, 인증·접근제어는 운영 배포 전에 결정해야 한다.

## Run API

기본 Prefix는 `/api/v1`이며 설정으로 바꿀 수 있다. JSON 필드는 camelCase다.

### Run 제출

```http
POST /api/v1/runs
Content-Type: application/json
```

```json
{
  "scenarioId": "<scenario-uuid-v4>",
  "requestText": "이메일과 비밀번호로 회원가입 기능을 구현해줘."
}
```

성공 시 `201 Created`, `Location: /api/v1/runs/{runId}`를 반환한다. 요청 본문에서 Scenario Registry가 발급한 UUIDv4를 받는다. Run은 `RECEIVED`, 첫 Planner Step은 `PENDING`으로 저장되고, `RUN_STARTED` 및 `WORKFLOW_STEP_CREATED` 이벤트가 같은 transaction에 들어간다.

Planner Agent URL이 설정된 경우 Run 생성은 실제 Planner dispatch를 background로 예약한다. URL이 없는 로컬 환경에서도 저장 API를 사용할 수 있지만 응답에 `dispatchStatus: NOT_CONFIGURED`가 표시되고 Run은 `RECEIVED`에 머문다. Dispatch 동작은 [`09-planner-dispatch.md`](09-planner-dispatch.md)를 따른다.

### 상태 및 실행 기록 조회

| Method / Path | 설명 |
| --- | --- |
| `GET /api/v1/runs/{runId}` | Workflow Run 상태와 Final Verdict |
| `GET /api/v1/runs/{runId}/steps` | Step별 Agent 역할, Workflow/A2A 상태, Artifact ID |
| `GET /api/v1/runs/{runId}/events?limit=100&offset=0` | 생성 순서 Trace 페이지; `1 <= limit <= 500` |

모든 조회 응답은 없는 Run에 `404`를 반환한다. Run 응답의 `status`와 Step의 `a2aTaskState`는 혼합하지 않는다. 요청 원문과 Agent Context ID는 상태 응답에 되돌려주지 않고 저장소 안에서만 관리해 불필요한 정보 노출을 줄인다.

### 취소

`POST /api/v1/runs/{runId}/cancel`은 `{"reason":"USER_CANCELLED"}`를 받는다. 실행 중인 Step이나 진행 중 A2A Task가 없으면 Run을 `ABORTED`로 전이하고 pending/waiting Step을 `CANCELED`로 바꾸며 Trace를 함께 기록한다. 활성 A2A Task가 있으면 원격 Agent 취소 전송이 연결되지 않은 현 단계에서 로컬 상태만 취소된 것처럼 보이지 않도록 `409 Conflict`를 반환하고 아무 상태도 바꾸지 않는다. `WORKFLOW_STEP_CANCELED`는 프로젝트 보조 Trace Event다.

14번부터 `GET /runs/{runId}/artifacts` 및 개별 Artifact 조회를 제공하며, Planner Requirement·Source/Change/Build/QA/Security와 불변 Run Configuration을 조회한다. 조회 대상은 메타데이터/보고서이며 원본 Archive bytes/Object Store·ACL은 별도 구현 경계다. 재개·복구 및 원격 Task 취소 확인 API도 추가했다. 현재 계약은 [14번](14-orchestrator-contract-compliance.md) 참조.

## 검증 및 개발정의서 대조

| 개발정의서 항목 | 결과 |
| --- | --- |
| Run 생성 및 UUIDv4, 첫 Planner 논리 Step 연결 | 반영; 최초 상태 `RECEIVED` / `PENDING` |
| Run, Step, Agent Context와 전체 로그 영속 저장 | 반영; SQLite FK 및 transaction 적용 |
| Task 갱신의 Step/Context/Event 원자성 | 반영; Trace insert 실패 시 전체 rollback 테스트 |
| Run·Step·Trace 조회 및 pagination | 반영; API draft 경로와 camelCase 응답 |
| Workflow/A2A/Verdict 별도 관리 | 반영; 응답 필드와 저장 모델 분리 |
| 사용자 취소 | 부분 반영; 활성 원격 Task가 없을 때만 취소, 활성 Task는 409 |
| Run 생성 후 Planner/Developer/QA/Security 자동 실행 | 부분 반영; 설정된 URL에 따라 Developer Snapshot/Build 검증 후 QA/Security handoff. 결과 Artifact 판정과 재시도는 후속 단계 |
| Artifact 목록 API, DB migration/운영 HA | 미구현; Artifact Registry 및 배포 설계 이후 |

## 테스트

```bash
PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -p 'test_workflow_repository.py' -v
PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -p 'test_runs_api.py' -v
PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -v
```

테스트는 저장소 재개방 후 데이터 유지, A2A snapshot 원자 저장/rollback, Event 순서, Run 생성·조회, Step/Trace pagination, 잘못된 입력, 취소 가능/불가 조건을 확인한다.

## 다음 작업

9번의 [`Planner dispatch`](09-planner-dispatch.md)는 Agent별 A2A 설정, Run 제출, Task Runner, observer 저장을 연결했다. 10번의 [`Planner 결과 검증`](10-planner-output-developer-dispatch.md)은 validated Requirement/Acceptance Criteria를 Developer Step에 연결한다. 11번의 Developer Artifact metadata와 12번의 QA/Security Report를 같은 append-only Registry에 기록한다. 실제 Artifact bytes/Object Store는 아직 연결되지 않았다.
