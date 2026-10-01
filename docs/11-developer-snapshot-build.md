# 11. Developer 결과 Artifact와 Snapshot/Build Handoff

> 상태: 로컬 Orchestrator MVP 구현 완료; 실제 Artifact Registry/Object Store 연동은 미완료
> 범위: Developer 결과 검증, Artifact metadata 영속화, Build PASS 후 동일 Snapshot QA/Security dispatch
> 다음 작업: 12번 — QA/Security 결과 검증, Verdict, 수정·재검증 흐름

## 목적

Developer A2A Task가 `TASK_STATE_COMPLETED`라는 사실만으로 Source나 Build가 유효하다고 판단하지 않는다. 완료 Task에 들어 있는 Source Snapshot, Change Report, Build Report의 계약과 상호 참조를 검증하고, Build PASS인 경우 QA와 Security에 동일한 Source Artifact/Execution Manifest를 전달한다.

Orchestrator는 Planner/Developer/QA/Security를 A2A로 호출한다. Build는 Developer Agent가 개발정의서의 MCP Tool을 이용해 실행하며 Orchestrator가 MCP Server를 직접 호출하거나 소스 코드를 수정하지 않는다.

## 구현 위치

| 경로 | 내용 |
| --- | --- |
| `src/orchestrator/application/developer_output.py` | Developer 완료 Task와 세 Artifact/metadata/상호 참조 검증 |
| `src/orchestrator/domain/developer_artifacts.py` | immutable Change Report·Build Report·파일 변경 모델 |
| `src/orchestrator/domain/snapshot_handoff.py` | Source Snapshot·Execution Manifest·QA/Security read-only 요청 모델 |
| `src/orchestrator/application/dispatch.py` | Developer 결과 등록, Build 분기, QA/Security 병렬 A2A dispatch |
| `src/orchestrator/infrastructure/sqlite_workflows.py` | Artifact metadata, Run/Step/Trace를 원자적으로 등록 |
| `schemas/project/developer_artifact_metadata.schema.json` | A2A Artifact `metadata` project extension |
| `schemas/project/developer_source_snapshot.schema.json` | Source Snapshot Data Part |
| `schemas/project/developer_change_report.schema.json` | Change Report Data Part |
| `schemas/project/developer_build_report.schema.json` | Build Report Data Part |

## Developer A2A 결과 계약

완료된 Developer Task는 아래 이름의 Artifact를 각각 정확히 하나 반환해야 한다. 각 Artifact는 `application/json` Data Part 하나를 가지며 별도 프로젝트 `metadata`를 붙인다. 이는 A2A Protocol 객체를 바꾸는 규칙이 아니라 팀 Agent 사이의 프로젝트 payload 계약이다.

| A2A Artifact `name` | Data Part Schema | 용도 |
| --- | --- | --- |
| `source-snapshot.json` | [`developer_source_snapshot.schema.json`](../schemas/project/developer_source_snapshot.schema.json) | 불변 Source Artifact Registry metadata 및 Execution Manifest 원본 |
| `change-report.json` | [`developer_change_report.schema.json`](../schemas/project/developer_change_report.schema.json) | 변경 요약과 상대 경로별 추가/수정/삭제 내역 |
| `build-report.json` | [`developer_build_report.schema.json`](../schemas/project/developer_build_report.schema.json) | Build Tool `exitCode`, `durationMs`, `executionManifestId`와 동일 Manifest |

세 Artifact의 A2A `artifactId`는 Task-scoped opaque string이고, Data Part의 UUIDv4 `artifactId`/metadata `projectArtifactId`는 프로젝트 Artifact ID다. 서로 대체하거나 같은 형식이라고 가정하지 않는다.

각 A2A Artifact의 metadata는 [`developer_artifact_metadata.schema.json`](../schemas/project/developer_artifact_metadata.schema.json)을 따른다.

```json
{
  "runId": "<현재 Run UUIDv4>",
  "workflowStepId": "<Developer Step UUIDv4>",
  "projectArtifactId": "<Data Part artifactId와 동일한 UUIDv4>",
  "artifactVersion": 1
}
```

실제 Agent 연동 시 Developer는 Data Part 안의 `runId`, `workflowStepId`, `a2aTaskId`, `a2aArtifactId`, `requirementIds`, `codeVersion`과 위 metadata를 실제 Task/Step 및 A2A Artifact에 맞춰 출력해야 한다. 현재 팀별 Agent의 payload가 다른 경우 이 계약을 통합 기준으로 맞춘다.

Orchestrator는 Developer 호출 payload의 `outputContract`에도 필수 Artifact 이름, Schema 파일 경로, `run_build` Tool 결과 필드, 변경 금지 경계를 전달한다. Developer Agent 구현 담당자는 해당 파일을 동일 프로젝트 계약으로 사용하고, 실제 Build 실행과 Tool 권한은 Developer/MCP 쪽에서 보장한다.

## 검증 규칙

Orchestrator는 Artifact 등록 전에 아래 조건을 모두 검사한다.

- Task가 `TASK_STATE_COMPLETED`이고 A2A Task ID가 저장된 Developer Step과 동일하다.
- 요구된 세 Artifact가 각각 정확히 하나 존재하고 각 A2A `artifactId`가 비어 있지 않다.
- Data Part는 정확히 하나이며 media type은 `application/json`이다.
- Artifact metadata 필드는 정의된 네 필드만 가지며 현재 `runId`, Developer `workflowStepId`, 프로젝트 ID, Artifact Version과 일치한다.
- Data Part 필드는 각 Schema에 맞고, 내부 A2A Task/Artifact 참조도 실제 응답과 일치한다.
- Source/Change/Build의 Requirement UUID 목록은 Developer Step의 목록과 동일하고, `codeVersion`은 현재 `fix_attempt`에서 기대하는 버전이다.
- Change Report 경로는 정규화된 상대 경로여야 하며 절대 경로, `..`, Windows drive 경로, 중복 파일 경로를 거부한다.
- Build Report는 등록하려는 Source Artifact ID와 같은 ID를 가리키며 `executionManifest`의 모든 필드가 Source에서 만든 Manifest와 일치한다.
- `exitCode`와 `durationMs`는 정수이며 `durationMs >= 0`, Manifest ID는 UUIDv4다.
- 이후 Artifact Version은 같은 Run/종류의 직전 버전 Artifact를 참조해야 한다.

검증 실패 시 불완전한 Artifact 일부를 Registry에 남기지 않는다. Run은 `IMPLEMENTING → HUMAN_REVIEW`(`resumeState=IMPLEMENTING`)로 이동하며 Developer Output 거부/등록 실패 Trace를 기록한다.

## 저장과 상태 전이

검증된 Source/Change/Build metadata 3건, Developer Step의 `outputArtifactIds`, Run `codeVersion`, 상태 전이, Trace 및 QA/Security Step 준비는 SQLite transaction 하나로 처리한다. `project_artifacts` 테이블에는 UPDATE/DELETE 거부 Trigger와 Artifact ID PK를 둬 로컬 metadata 기록을 append-only로 유지한다.

```text
Developer Task COMPLETED
  → Source/Change/Build Artifact 계약·상호 참조 검증
  → metadata 3건 + Developer Step outputArtifactIds 원자 저장
  → IMPLEMENTING → SNAPSHOT_READY
  ├─ Build exitCode != 0 → FIX_REQUIRED (수정 전까지 QA/Security 호출 없음)
  ├─ Build exitCode == 0 + QA/Security URL 둘 다 설정
  │    → VALIDATING + QA/Security Step을 RUNNING으로 선점
  │    → 서로 독립 Context로 병렬 A2A 호출
  │    → 둘 다 같은 Source Artifact/Execution Manifest를 받음
  └─ Build PASS + 둘 중 하나라도 endpoint 없음
       → VALIDATING → HUMAN_REVIEW (resumeState=VALIDATING)
       → QA/Security Step은 PENDING, 부분 호출하지 않음
```

Build 실패는 제품 전체 Verdict `FAIL`을 뜻하지 않는다. 유효한 Build Report와 Source는 보존하고 Run을 `FIX_REQUIRED`로 둔다. 수정 횟수를 소진한 상태에서 Build가 실패하면 현재 자동 수정 가능한 상태가 아니므로 `HUMAN_REVIEW`로 멈춘다.

QA/Security Task는 둘 다 완료돼도 Run을 `VALIDATING`에 둔다. A2A Task `COMPLETED`는 해당 Agent 업무의 완료이지 Report 내용의 PASS가 아니며, 이 단계에서는 최종 Verdict를 만들지 않는다. 두 호출 중 실패·거절·입력 대기·timeout·전송 불확실성이 있으면 두 호출의 결과를 기다린 다음 `HUMAN_REVIEW`로 전환한다.

## Artifact/권한 경계와 미완료 항목

현재 SQLite의 `project_artifacts`는 검증된 metadata와 ID 참조만 저장한다. 실제 Artifact Registry API, Object Store, archive 다운로드, 다운로드 bytes의 `snapshotSha256` 검증, `artifactUri` ACL은 없다. 따라서 이 단계가 보장하는 것은 Build·QA·Security 요청에 같은 Manifest/Artifact ID를 넣는 것까지이며, 외부 저장소 파일이 변경 불가능하거나 `READ_ONLY`가 실제로 강제된다고 보장하지 않는다. 그 권한은 Artifact Registry/스토리지/MCP 담당 구현과 연결돼야 한다.

Build MCP 실행 자체도 이 단계에서 수행하지 않는다. Developer가 반환한 Build Report의 정수 exit code와 Manifest 연결을 검증한다. Infrastructure가 컨테이너를 시작하지 못한 경우를 제품 Build 실패와 구분해 최종 `UNVERIFIED`로 확정하는 Report/오류 분류 연결은 아직 없으며, 현재는 누락되거나 유효하지 않은 완료 결과를 `HUMAN_REVIEW`로 보낸다.

프로세스 재시작 뒤 preclaimed QA/Security Step 자동 dispatch 재개, 누락 endpoint를 채운 후 PENDING Step을 재개하는 기능도 아직 없다. 현재 dispatch 중 외부 효과가 불확실하면 중복 호출을 피하고 사람 검토를 요구한다.

## 개발정의서 대조

| 요구사항 | 결과 |
| --- | --- |
| Orchestrator가 Agent를 A2A로 호출하고 MCP를 직접 수행하지 않음 | 반영; Build는 Developer 결과 Artifact로 확인 |
| Developer의 Source/Change/Build 결과 연결과 UUID/A2A ID 분리 | 반영; project Artifact metadata를 append-only SQLite에 저장 |
| Build Tool exit code/duration/Execution Manifest 추적 | 반영; 필드·정수성·Source 동일성 검증, tool 실행 자체는 Developer 책임 |
| Build/QA/Security 동일 Snapshot 및 실행환경 | 부분 반영; Source Manifest와 Build Report를 대조하고 같은 Handoff를 양쪽에 전달, 실제 bytes/ACL 강제는 미구현 |
| Build 실패에서 QA/Security PASS를 섞지 않음 | 반영; Build nonzero면 `FIX_REQUIRED`, Validator 호출 없음 |
| QA/Security가 Source를 변경할 수 없음 | 부분 반영; A2A 요청은 `READ_ONLY`, 외부 Artifact/MCP 권한 강제는 미구현 |
| Scenario Registry의 고정 필수 Requirement/Acceptance Criteria 보호 | 미구현; 10번과 동일하게 Orchestrator가 기준 원본을 조회하는 Scenario Registry가 없어 Planner가 반환한 기준을 전달하는 데 그침 |
| 모든 QA/Security 결과 확인 후 제품 Verdict 계산 | 다음 12번; 현재 Agent Task 완료를 PASS로 취급하지 않음 |
| Tool/Infrastructure 실패를 제품 FAIL과 구분 | 부분 반영; 불확실한/잘못된 완료 결과는 `HUMAN_REVIEW`, 최종 `UNVERIFIED` 확정 연결 미구현 |

## 검증

```bash
PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -p 'test_developer_artifacts.py' -v
PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -p 'test_dispatch.py' -v
PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -v
```

Artifact 필드/Lineage/경로와 Manifest, 누락 또는 불일치 Developer 결과, Build FAIL, Build PASS 후 동일 Manifest QA/Security handoff, 누락 Validator endpoint, SQLite metadata 저장 및 전체 회귀 흐름을 검사한다.

## 다음 작업

12번에서 QA Report와 Security Report의 출처/Schema/Requirement별 결과/Manifest를 검증하고, `SUCCESS`/`FAIL`/`UNVERIFIED`/`HUMAN_REVIEW`를 개발정의서 조건대로 결정한다. 필요하면 Issue/수정 요청으로 Developer 새 Code Version을 만들고 Build·QA·Security를 새 Snapshot에서 재검증한다.
