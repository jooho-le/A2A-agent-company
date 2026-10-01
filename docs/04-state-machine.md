# 4. Workflow 상태 전이와 재시도 정책

> 상태: 구현 완료
> 범위: Run 상태 전이, 대기 후 재개, 취소, 코드 수정 한도, MCP 재시도, 반복 Issue 감지
> 다음 작업: 12번 — QA/Security 결과 검증과 Verdict/수정 루프

## 목적

상태 이름을 정의하는 데서 그치지 않고, Orchestrator가 Run을 어떤 순서로 전환할 수 있는지 검증한다. 제품 결함에 따른 코드 수정과 MCP/환경 오류의 Tool 재시도는 서로 다른 카운터와 정책으로 처리한다.

## 코드 위치

| 경로 | 내용 |
| --- | --- |
| `src/orchestrator/domain/state_machine.py` | 허용 전이표, `transition_run`, `TransitionError` |
| `src/orchestrator/domain/retry_policy.py` | MCP Tool 오류 분류·재시도 결정, Issue fingerprint·반복 감지 |
| `src/orchestrator/domain/constants.py` | 코드 수정, MCP 재시도, 반복 Issue 한도 |
| `src/orchestrator/domain/models.py` | Run의 대기 재개 상태와 결과 불변 조건 |
| `tests/test_state_machine.py` | 정상·수정·대기·취소·종료 상태 전이 검증 |
| `tests/test_retry_policy.py` | 오류별 재시도, 한도, Issue 반복 감지 검증 |

## 상태 전이표

| 현재 상태 | 허용되는 정상 다음 상태 |
| --- | --- |
| `RECEIVED` | `PLANNING`, `ABORTED` |
| `PLANNING` | `IMPLEMENTING`, `WAITING_INPUT`, `HUMAN_REVIEW`, `ABORTED` |
| `WAITING_INPUT` | 진입 직전 상태로 재개, `ABORTED` |
| `IMPLEMENTING` | `SNAPSHOT_READY`, `HUMAN_REVIEW`, `ABORTED` |
| `SNAPSHOT_READY` | `VALIDATING`, `FIX_REQUIRED`, `HUMAN_REVIEW` |
| `VALIDATING` | `FINISHED`, `FIX_REQUIRED`, `HUMAN_REVIEW` |
| `FIX_REQUIRED` | `FIXING`, `HUMAN_REVIEW` |
| `FIXING` | `REVALIDATING`, `HUMAN_REVIEW`, `FINISHED` |
| `REVALIDATING` | `FINISHED`, `FIX_REQUIRED`, `HUMAN_REVIEW` |
| `HUMAN_REVIEW` | 저장한 재개 상태, `FINISHED`, `ABORTED` |
| `FINISHED`, `ABORTED` | 종료 상태이며 전이 불가 |

상태 전이는 `transition_run(run, target, ...)`을 통한다. 입력 Run을 직접 변경하지 않고 불변 조건을 검증한 새 Run을 반환한다. 같은 상태로의 전이, 표에 없는 전이, 종료 상태 변경은 `TransitionError`다.

## 일시 중지와 재개

`WAITING_INPUT` 또는 `HUMAN_REVIEW`에 진입하면 직전 진행 상태를 `resume_state`에 보관한다. 재개할 때는 이 상태로만 복귀할 수 있으며, 복귀와 동시에 `resume_state`를 지운다. 대기 중 취소·종료되는 경우에도 재개 상태를 지운다.

`HUMAN_REVIEW`는 사람의 판단이 필요한 일시 중지 상태다. 검토 후 자동 진행을 재개하거나, 사람이 최종 판단을 내린 경우 `FINISHED`와 적절한 Verdict로 종료할 수 있다. `HUMAN_REVIEW` Verdict는 종료 전 검토 필요 표시로만 허용되며 재개 시 제거된다.

## 취소와 명세 예외

개발정의서의 일반 전이표에는 일부 상태에만 `ABORTED` 간선이 있지만, API 계약에는 전역 `POST /runs/{runId}/cancel`이 정의되어 있다. 이 충돌은 **명시적 사용자/운영자 취소에 한해 모든 비종료 상태에서 `ABORTED`를 허용**하는 예외로 해석했다. 그 외 자동 흐름은 표의 간선만 허용한다. Run API는 [`08-workflow-storage-run-api.md`](08-workflow-storage-run-api.md)에 구현했고, 활성 원격 A2A Task가 존재하면 실제 원격 취소가 연결되기 전까지 409로 보류한다.

취소 전이는 비어 있지 않은 `termination_reason`을 필수로 받고 Verdict는 저장하지 않는다. 상태 머신은 Run만 갱신한다. 실제 활성 A2A Task 취소 요청과 취소 결과 기록은 API/A2A 실행 계층의 책임이며 이번 작업 범위가 아니다.

## 코드 수정 및 최종 Verdict

- 최초 구현은 `fix_attempt=0`에서 시작한다.
- `FIX_REQUIRED → FIXING` 전이 때만 수정 횟수를 1 증가시킨다. 최대 3회다.
- 세 번째 수정 뒤 재검증에서 결함이 남으면 다시 `FIX_REQUIRED`로 보내지 않는다. `FINISHED` + `FAIL`로 종료하거나 판단이 필요한 경우 `HUMAN_REVIEW`로 보낸다.
- `FINISHED`에는 `SUCCESS`, `FAIL`, `UNVERIFIED`, `HUMAN_REVIEW` 중 하나의 Verdict가 필요하다.
- `ABORTED`는 Verdict가 없어야 하고 종료 사유가 있어야 한다.
- `fix_attempt`는 코드 수정 Cycle 카운터다. MCP Tool 재시도나 A2A `attempt`와 공유하지 않는다.

`SUCCESS`는 Build·필수 요구사항·QA·Security가 동일한 불변 Snapshot 및 환경에서 모두 통과한 경우에만 사용한다. 제품 결함과 검증 인프라 실패를 혼동하지 않는다. 수정 한도 후 해결되지 않은 제품 결함은 `FAIL`, 재시도 한도 후에도 Tool/환경 장애로 검증할 수 없으면 `UNVERIFIED`다.

## MCP Tool 재시도 정책

최초 Tool 호출은 `retries_used=0`이다. 최초 호출 외 최대 2회, 즉 전체 최대 3회 호출을 허용한다.

| 오류 종류 | 정책 |
| --- | --- |
| 프로세스 시작 실패, 리소스 사용 중 | 재시도 한도 내 재시도 |
| Timeout | 부작용이 안전하게 제한된 호출만 재시도. 그 외에는 상태 확인 |
| MCP 전송 중단 | 호출이 적용되지 않았음이 확인된 경우에만 재시도. 결과 불명은 상태 확인 |
| Write 결과 불명 | 파일/대상 현재 상태 또는 hash 확인 후 판단. 즉시 재호출 금지 |
| 입력 Schema 오류, 권한 거부, Path traversal, 미지원 Tool | 재시도하지 않음 |
| Build 코드 실패, QA assertion 실패, Security finding | Tool/전송 오류가 아닌 제품 결과. 자동 Tool 재시도하지 않음 |

`decide_tool_retry()`는 `RETRY`, `DO_NOT_RETRY`, `INSPECT_STATE` 중 하나를 반환한다. 이 결정은 그 자체로 Workflow Verdict를 만들지 않는다. 상위 Orchestrator가 오류 원인 및 검증 결과를 종합해 상태와 Verdict를 정한다.

## 반복 Issue 감지

Issue fingerprint는 개발정의서 순서대로 `requirement_id + test_id + issue_category + normalized_location`을 UTF-8 문자열로 이어 SHA-256 계산한다. Location 정규화와 이전 Issue 이력 저장은 호출자/Issue Registry 책임이다.

수정 후 동일 fingerprint가 재검증에서 연속 재발하면 반복 횟수를 증가시키고, 다른 fingerprint가 관측되면 연속 반복 횟수를 0으로 초기화한다. 동일 Issue가 **두 번 연속 수정 Cycle 후에도 재발**하면 `HUMAN_REVIEW` 후보로 올린다. 본 단계의 helper는 계산만 수행하고 Issue Registry 및 영속 저장은 후속 작업이다.

## 설계 경계

- Workflow 상태 머신은 로컬 도메인 규칙이며 A2A Task 상태를 대신하지 않는다.
- DB transaction과 Event/Trace 영속화, 활성 원격 Task가 없는 Run의 취소 API는 [`08-workflow-storage-run-api.md`](08-workflow-storage-run-api.md)에 구현했다. 활성 A2A Task의 원격 취소는 Agent Client/Registry 연결 이후 작업이다. Agent Card/Task 송수신 Client 경계는 [`06-a2a-client.md`](06-a2a-client.md), Task 실행·상태 반영 경계는 [`07-task-lifecycle.md`](07-task-lifecycle.md)에 정의한다.
- 코드 Snapshot 동일성·무결성 확인은 [`05-code-handoff.md`](05-code-handoff.md)에서 정의하며 실제 Artifact 권한 강제는 Storage 계층에 남아 있다.
- 상태 변경 결과를 DB에 기록할 때에는 실패한 전이가 일부만 저장되지 않도록 Run/Event 저장을 같은 트랜잭션 경계로 다룬다.

## 검증

실행 명령:

```bash
PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -v
```

해당 단계 완료 당시 전체 22개 단위 테스트 통과. 상태 전이 정상/오류 경로, 종료 상태 불변성, 대기 후 재개, 3회 수정 한도, 사용자 취소 예외, MCP 재시도 분기, 반복 Issue fingerprint를 확인했다.

## 다음 작업

5번에서 만든 Snapshot/Artifact Handoff는 [`05-code-handoff.md`](05-code-handoff.md)를 따른다. A2A 전송 경계는 [`06-a2a-client.md`](06-a2a-client.md), Task 폴링과 Step/Context 갱신은 [`07-task-lifecycle.md`](07-task-lifecycle.md), 저장소·Run API는 [`08-workflow-storage-run-api.md`](08-workflow-storage-run-api.md), Planner dispatch는 [`09-planner-dispatch.md`](09-planner-dispatch.md), Planner 출력 검증과 Developer dispatch는 [`10-planner-output-developer-dispatch.md`](10-planner-output-developer-dispatch.md)에 구현했다. 11번에서 Developer 결과 검증과 QA/Security handoff까지 연결했으며, 12번에서 결과 해석 및 Verdict를 진행한다.
