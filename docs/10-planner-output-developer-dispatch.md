# 10. Planner 결과 검증과 Developer Dispatch

> 상태: MVP 구현 완료
> 범위: Planner Artifact 검증, Requirement/Acceptance Criteria 연결, Developer WorkflowStep 생성 및 A2A 전달
> 13번에서 권위 Scenario 기준 및 수정·재검증 루프 구현 완료. 다음 Orchestrator 작업은 14번 재개/복구 정책이다. 실제 Agent/MCP 팀 통합은 별도 범위다.

## 목적

Planner Task가 `TASK_STATE_COMPLETED`라고 해서 계획이 유효하다고 간주하지 않는다. Planner가 반환한 프로젝트 Artifact의 출처와 JSON 계약을 확인한 다음에만 Developer Step을 만들고, Planner Context를 공유하지 않은 별도 Developer A2A Task로 계획을 전달한다.

## Planner Artifact 계약

개발정의서는 Planner가 Requirement ID(UUIDv4), Acceptance Criteria, Task Plan을 만들도록 요구하지만 내부 JSON 필드 전체를 고정하지 않았다. 담당 Agent 간 구현을 맞추기 위해 이번 MVP에서는 아래 프로젝트 계약을 선택했다. 공식 A2A Artifact 객체는 SDK 모델 그대로 사용하며, 아래 Schema는 Artifact `parts[].data`의 프로젝트 payload만 검증한다.

- Artifact `name`: `requirements.json`
- Artifact 안에는 `application/json` Data Part가 정확히 하나 있어야 한다.
- Metadata의 `runId`, `workflowStepId`가 현재 Planner Run/Step과 일치해야 한다.
- Metadata의 `projectArtifactId`는 UUIDv4, `artifactVersion`은 양의 정수여야 한다.
- A2A `artifactId`는 해당 완료 Planner Task의 `artifacts`에 실제 포함되어야 한다.
- Payload는 [`planner_output.schema.json`](../schemas/project/planner_output.schema.json)을 따른다.
- Requirement ID/Key, Task ID는 각각 중복될 수 없다. UUID는 v4여야 한다.
- 각 Requirement는 하나 이상의 비어 있지 않은 Acceptance Criteria를 가져야 한다.
- 구현 Task의 Requirement 참조는 모두 존재해야 하며 모든 Requirement가 적어도 한 Task에 포함돼야 한다.
- Task dependency는 존재하는 Task만 참조하고 순환할 수 없다.

```json
{
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
      "description": "요구사항과 수용 기준에 맞춰 가입 API를 구현한다.",
      "requirementIds": ["<위 requirementId>"]
    }
  ]
}
```

정의서의 예시 Artifact 이름과 `requirements` 구조를 유지하면서 Requirement UUID, 수용 기준, 구현 계획을 필수화했다. 이 구조는 팀 Agent 구현과 공유할 연동 계약이며, Planner 담당자가 같은 Schema를 출력하도록 맞춰야 한다.

## 처리 흐름

```text
Planner Task COMPLETED
  → requirements.json 및 출처 metadata 확인
  → Planner 출력 Schema를 구현한 Pydantic 검증 및 참조 무결성 확인
  ├─ 실패: PLANNING → HUMAN_REVIEW, Developer Step 미생성
  └─ 통과: Planner Step outputArtifactIds 연결
            + Developer Step 생성(inputArtifactIds, requirementIds)
            + PLANNING → IMPLEMENTING
            → Developer Agent Card 조회 및 독립 A2A Task 전송/polling
```

Developer 요청 payload에는 검증된 `plan`, Planner Artifact의 `a2aArtifactId`/`projectArtifactId`/`artifactVersion`, 그리고 11번에서 정의한 `outputContract`(필수 Artifact 이름·JSON Schema·Build Tool 근거·변경 금지 경계)를 포함한다. 프로젝트 metadata에는 Developer Step UUID, Requirement UUID 목록, 예상 `codeVersion`, `projectArtifactIds`를 넣는다. Planner `contextId`는 Developer 호출에 전달하지 않으며 Agent Context를 각각 별도로 저장한다. 원래 사용자 요청 전문은 재전송하지 않는다.

유효한 Planner Plan을 DB transaction 하나에서 Planner Step의 output Artifact ID 연결, Developer Step 생성, Run 전이, Trace 추가로 기록한다. Developer URL이 있으면 Step을 `RUNNING`으로 선점해 중복 전송을 막는다. URL이 없으면 Plan/Step 참조를 남기고 Run을 `HUMAN_REVIEW`(`resumeState=IMPLEMENTING`), Developer Step을 `PENDING`으로 둔다.

## 상태 처리

| 결과 | Run | Step/후속 처리 |
| --- | --- | --- |
| Planner Task 완료 + Plan 유효 | `IMPLEMENTING` | Developer Step을 만들고 설정된 Agent에 전달 |
| Planner Task 완료 + Artifact/Plan 불일치 | `HUMAN_REVIEW` (`resumeState=PLANNING`) | `PLANNER_OUTPUT_REJECTED`, Developer Step 없음 |
| Developer endpoint 미설정 | `HUMAN_REVIEW` (`resumeState=IMPLEMENTING`) | Developer Step `PENDING`, 자동 전송 없음 |
| Developer Task `INPUT_REQUIRED` | `WAITING_INPUT` (`resumeState=IMPLEMENTING`) | 같은 Developer Step을 입력 대기 상태로 저장 |
| Developer Task 완료 | `IMPLEMENTING` 유지 | 개발 Task 완료일 뿐 Snapshot/Build/QA/Security 성공이 아님 |
| Developer 전송/Task 실패·timeout 등 | `HUMAN_REVIEW` | 불명확한 전송을 자동 재실행하지 않음 |

추가 Trace 이벤트 `PLANNER_OUTPUT_VALIDATED`, `PLANNER_OUTPUT_REJECTED`, `DEVELOPER_DISPATCH_NOT_CONFIGURED`는 프로젝트 이벤트다. A2A 공식 상태나 Event로 가장하지 않는다.

## Artifact/내구성 경계

현재 저장소에는 Artifact Registry/Object Store가 아직 없다. `projectArtifactId`는 Planner가 반환한 UUIDv4를 참조로 보존하고, 검증된 Plan 데이터는 바로 Developer A2A 요청에 inline 전달한다. 따라서 이 단계는 실제 Artifact URI 조회, 전역 Registry 등록 여부 검증, ACL/불변성 보장을 구현했다고 주장하지 않는다. 이 기능은 Agent 간 Registry 연동 단계에서 보완해야 한다. 프로세스 재시작 후 PENDING Developer Step 자동 재개도 아직 제공하지 않는다.

Developer Task 완료 후 결과 처리는 [`11-developer-snapshot-build.md`](11-developer-snapshot-build.md)에서 이어진다. Task 완료만으로 성공 처리하지 않으며 검증된 Source/Build Artifact와 동일 Snapshot QA/Security handoff를 요구한다.

## 개발정의서 대조

| 항목 | 결과 |
| --- | --- |
| Planner Requirement ID는 UUIDv4, 표시용 Key와 분리 | 반영; ID/Key 형식·중복 검증 |
| Acceptance Criteria와 Task Plan 결과 전달 | 반영; 버전 있는 프로젝트 JSON Schema 및 A2A payload |
| Planner 완료를 Workflow/Product 성공으로 오판하지 않음 | 반영; Developer 완료 후에도 `IMPLEMENTING`, Verdict 없음 |
| Agent별 Context 분리 및 Artifact 참조 handoff | 부분 반영; 별도 Context/ID 전달은 반영, Artifact Registry의 내용·ACL 확인은 미구현 |
| Artifact Registry에서 실제 전역 ID·내용·ACL 검증 | 미구현; Registry/Object Store 없음, 제한점 명시 |
| 정확한 Planner JSON shape가 개발정의서에 규정됨 | 미정; 본 문서와 Schema에서 팀 통합용 MVP 계약으로 명시 |
| Acceptance Criteria가 원래 사용자 의도를 의미상 보존하는지 | 미검증; 구조·참조는 검증하지만 의미 일치는 평가자/사람 확인 필요 |
| `SCN-001` 기준 필수 REQ-001~REQ-008 전체의 누락·기준 약화 여부 | 미검증; Scenario/Requirement Registry와 고정 Acceptance Criteria 주입 경로가 아직 없음 |
| 전체 Planner→Developer→Build→QA→Security 완료 | 부분 구현; QA/Security 결과 Artifact 해석과 최종 Verdict는 후속 작업 필요 |

## 검증

```bash
PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -p 'test_planner_output.py' -v
PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -p 'test_dispatch.py' -v
PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -v
```

유효/무효 Planner Artifact, UUID·참조 무결성, 의존성 순환, 출처 Run/Step 일치, Planner→Developer A2A handoff, endpoint 미설정을 확인한다.

## 다음 작업

11번에서 Developer Source/Change/Build Artifact 검증 및 같은 Snapshot QA/Security dispatch, 12번에서 QA/Security Report 검증과 Verdict/상태 기록, 13번에서 고정 Scenario/Requirement Registry와 수정·재검증 dispatch를 구현했다. 실제 Agent/MCP 실행은 팀 통합 후 검증해야 한다.
