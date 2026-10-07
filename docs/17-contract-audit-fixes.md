# 17번까지 점검 후 계약·재개·검증 이력 보완

작성일: 2026-10-07

범위는 1번 Orchestrator와 2번 Agent 공통 실행 기반이다. 17번까지 점검에서 발견한 7개 항목 중 사용자가 제외한 **2번 비밀번호 보호 정책 보완**을 제외하고 6개를 수정했다. 새 18번 기능 구현이나 3번 웹 서비스·4번 평가·팀 통합 작업은 아니다.

## 1. 수정한 6개 항목

번호는 직전 점검 결과의 번호를 유지한다.

| 점검 번호 | 문제 | 수정 결과 |
| --- | --- | --- |
| 1 | Orchestrator는 재개 시 attempt를 증가시키지만 Agent는 이전 값만 허용 | INPUT_REQUIRED/AUTH_REQUIRED의 명시적 새 Message에서 현재 attempt + 1만 허용. 같은 Task/Context와 나머지 Workflow identity 유지 |
| 3 | QA·Security가 동시에 입력 대기하면 하나만 선택해 재개할 수 없음 | 선택한 Step만 후속 입력·인증 Message 전송. 다른 알려진 Task는 GET으로만 관찰하고 양쪽 완료 전 Report·최종 결과 확정하지 않음 |
| 4 | 실제 검증 근거가 없거나 Finding이 UNVERIFIED인데 Issue 재검증 이력은 PASS | 적절한 Tool, 같은 ExecutionManifest, 실행 완료 근거 확인. 근거가 없거나 Finding이 SUSPECTED/UNVERIFIED이면 Issue 이력도 UNVERIFIED |
| 5 | Artifact 버전 필드가 true 또는 문자열 "1"을 정수로 변환 | artifactVersion/codeVersion/schemaVersion의 bool·문자열·소수·비유한 수를 거부. ProtoJSON의 정수 값 1.0은 정상 허용 |
| 6 | opaque A2A ID의 앞뒤 공백을 제거하여 원문 참조가 달라짐 | 공백만 있는 ID는 거부하되 유효한 Task/Artifact ID는 원문 그대로 보존. 전체 Pipeline 및 DB 등록에서도 동일성 확인 |
| 7 | 잘못된 요청의 422 응답에 제출한 비밀번호/API Key 등이 포함 | 공통 RequestValidationError 처리기를 등록. input/body/ctx/원래 예외 메시지를 반환하지 않고 type·loc·일반 안내만 제공 |

### 재개·저장 규칙

- attempt만 정확히 1 증가한다. Run/Step/Scenario/Requirement/Code Version/입력 Artifact 참조/Task/Context/소유자는 바꿀 수 없다.
- 후속 Message 접수 시 receipt, 최신 Task metadata, DB metadata binding, revision/history를 같은 트랜잭션으로 갱신한다.
- SDK 상태 이벤트로 attempt를 바꾸거나 terminal Task를 다시 실행할 수 없다.
- 같은 messageId의 재전송은 현재 저장된 Task 조회만 수행한다. 이전 attempt 요청을 다시 보내도 과거 상태로 돌아가지 않는다.
- stale 취소는 최신 binding 또는 과거 실제 접수 receipt로 승인된 metadata만 허용하며, 최신 attempt/history/Artifact를 보존한다.

### QA·Security 개별 재개

두 Task 모두 입력 또는 인증을 기다리면 `workflowStepId`를 지정한다. 인증은 기존대로 요청 본문이 아니라 운영자 HTTP 인증 설정으로 해결한다.

```json
{
  "workflowStepId": "선택할-현재-Step-UUID",
  "inputData": {"scope": "기존 요구사항 유지"}
}
```

선택하지 않은 Task에는 후속 Message를 보내지 않는다. 먼저 완료된 Task의 진행 상황은 저장하고, 남은 Task를 나중에 선택해 재개할 수 있다. 수정 후 REVALIDATING에서도 같은 규칙을 사용하고 fixAttempt/codeVersion을 임의 증가시키지 않는다. 입력·인증 후속 전송 결과가 불명확한 경우에는 기존 GET-only 복구 정책을 유지한다.

### Issue 재검증 결과

Tool 실행 완료 PASS와 제품 검증 PASS는 다르다. Tool PASS는 검사가 실행됐다는 근거이며 실제 테스트·Requirement 결과가 FAIL이면 Issue 재검증도 FAIL이다. 근거 없는 PASS나 FAIL은 확인된 해결·재발로 기록하지 않고 UNVERIFIED로 남긴다. 기존 append-only 이력은 수정하지 않고 새 재검증 이벤트를 추가한다.

## 2. 변경 파일

| 파일 | 내용 |
| --- | --- |
| `src/agents/api/sqlite_task_store.py` | 승인된 재개 attempt, 원자 metadata 갱신, 최신 결과를 보존하는 stale 취소 |
| `src/orchestrator/application/workflow_controls.py` | 선택한 Validator만 재개, 다른 Task GET-only, 완료 전 결과 확정 방지 |
| `src/orchestrator/infrastructure/sqlite_workflows.py` | Issue 재검증의 실제 Tool/Manifest 근거 확인 |
| `src/orchestrator/domain/contract_validation.py` | JSON 정수 검증 공통 함수 |
| `src/orchestrator/domain/{snapshot_handoff,developer_artifacts,validation_artifacts,planning_artifacts}.py` | 버전 숫자 계약, opaque ID 원값 보존 |
| `src/orchestrator/application/planner_output.py` | Planner schemaVersion 숫자 계약 |
| `src/orchestrator/api/errors.py`, `src/orchestrator/main.py` | 안전한 422 처리 및 등록 |
| `tests/test_agent_runner_contracts.py` | 실제 SDK Client/Runner와 ASGI Agent의 INPUT/AUTH 재개·재시작·replay |
| `tests/test_selected_validation_resume.py` | QA↔Security 순차 재개·재검증·복구·lease·불확실 전송 |
| `tests/test_issue_revalidation_evidence.py` | 근거 없는 PASS/FAIL 및 Finding UNVERIFIED 차단 |
| `tests/test_artifact_contract_boundaries.py` | 숫자 형식·ProtoJSON·opaque ID 파싱/전체 저장 |
| `tests/test_api_validation_errors.py` | 잘못된 입력·추가 필드·JSON·경로/쿼리·예외의 422 응답 |
| `tests/test_agent_task_store.py`, `tests/test_agent_lifecycle.py` | 재개 계약 fixture 수정, identity·CAS·rollback 회귀 |

## 3. 개발정의서 준수 점검

| 기준 | 확인 결과 |
| --- | --- |
| §3 공식 객체와 내부 Workflow/Project Artifact 분리 | SDK Task/Message/Artifact와 내부 UUID 구조 유지, opaque ID 원문 보존 |
| §4 실패·수정·재시도 정책 | 수정 최대 3회·Tool retry 최대 2회 유지. 같은 Task 재개를 코드 수정 횟수와 혼용하지 않음 |
| §6·§7 A2A/Context·공식 상태 | 기존 HTTP+JSON 1.0 및 SDK 버전 유지. INPUT/AUTH만 승인된 새 Message로 재개, terminal 불변 |
| §9 결과 판정 | A2A COMPLETED를 제품 SUCCESS로 취급하지 않음. 같은 Snapshot/Manifest의 실제 Tool 근거가 필요 |
| §11 Trace/Issue 이력 | 접수·상태·이력 원자 보존, Issue 재검증 이벤트 추가 전용 유지 |
| 민감정보 출력 | 422 응답의 제출 값·예외 원문 제거. 아래 제외된 설정 URL 문제까지 해결됐다고 주장하지 않음 |
| 담당 경계 | backend/frontend/evaluation, 제품 DB, 실제 LLM/MCP 실행 및 팀 통합 변경 없음 |

## 4. 제외한 항목과 남은 제한

### 사용자 요청으로 제외: 점검 2번 비밀번호 보호 정책 보완

RunConfiguration의 `protectedTestSuiteRef`/`scannerProfileRef` URL에 Credential이 포함됐을 때의 저장·조회 보호는 **이번에 수정하지 않았다**. 해당 모델과 Reference 마스킹 정책은 변경하지 않았다. 이 참조에는 실제 비밀번호·Token을 넣지 않아야 하며, 모든 입력 경로의 비밀정보 보호가 완료됐다는 판정은 하지 않는다.

### 별도로 확인한 기존 동시 초기화 제한

서로 다른 역할이 같은 새 Agent DB를 동시에 초기화하는 기존 테스트를 100회 반복했을 때 1회 `PRAGMA journal_mode=WAL`에서 `database is locked`가 발생했다. DB 역할 혼합이 아니라 한 시작자의 오류 종류가 예상 RoleError 대신 SQLite OperationalError가 되는 문제다. 이번 6개 수정 범위 밖이라 코드는 변경하지 않았다. 역할별 DB를 분리하는 운영 원칙은 그대로다.

실제 역할 Prompt·LLM·MCP·Sandbox·Artifact bytes/ACL 구현은 후속 번호의 작업이다. 이 보완이 1번+2번 전체 구현 완료나 회원가입 제품 검증 완료를 의미하지 않는다.

## 5. 검증·커밋·다음 작업

```bash
PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -q
git diff --check
```

- 전체 unittest **342개 통과**: 기존 291개 + 회귀 테스트 51개 추가.
- 실제 SDK Client/TaskRunner ↔ ASGI Agent에서 INPUT/AUTH 각각 attempt 0→1→2, 같은 Task/Context 유지, 재시작 후 명시적 재개·old-message replay 확인.
- QA→Security 및 Security→QA 선택 재개, REVALIDATING, 상대 GET 실패·terminal 상태에서 진행 보존 확인.
- JSON 계약의 정수 형식·ProtoJSON 호환·opaque ID 전체 저장, Issue Tool 근거, 안전한 422 경계 확인.
- Project JSON Schema 16개 JSON 구문 확인, `git diff --check` 통과.
- 테스트는 임시 DB·ASGI·Fixture Executor/Client 기반이다. 외부 LLM/MCP 호출·제품 QA/Security 실행·비교 실험을 수행하지 않았다.
- SDK 종료 큐 경고와 위 간헐적 DB 초기화 제한은 숨기지 않는다. 테스트 통과를 모든 동시성/운영 조건 보장으로 해석하지 않는다.

커밋 메시지 제안: `재개 계약과 검증 이력 및 입력 오류 처리 보완`

**다음 작업: 18번 — 역할별 Prompt와 출력 계약.** 기존 번호는 바꾸지 않는다. Git commit/push는 수행하지 않았다.
