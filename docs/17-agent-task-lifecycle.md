# 17. Agent Task 생명주기·영속화·Context·중복 방지

> 범위: 1번+2번 담당의 공통 Agent 실행 기반.
> 기준: 사용자 개발정의서 3·4·6·7·11번 항목, 기존 Project Metadata 및 A2A SDK 계약.
> 실제 역할 Prompt·LLM·MCP 실행, 3번 웹 서비스·4번 평가 및 해당 연동은 이번 작업에 포함하지 않는다.

2026-10-07 재개 기록: 브랜치 정리 때 보관한 17번 작업을 15·16번이 반영된 `jooho`에 복원하고, 아래 자동 테스트 291개와 변경 범위·개발정의서 준수 여부를 다시 확인했다. 기존 stash는 복구용으로 유지했으며 브랜치·커밋·원격 저장소는 변경하지 않았다. 현재 작업이 복원되어 있으므로 동일 stash를 다시 적용할 필요는 없다.

이후 17번까지 점검한 6개 계약 문제와 회귀 테스트 51개 추가는 [점검 후 보완 문서](17-contract-audit-fixes.md)에 기록했다. 아래 291개는 최초 17번 검증 수치이며 보완 후 전체 검증은 342개다. 사용자가 제외한 설정 URL Credential 보호 문제와 별도로 발견한 간헐적 동시 DB 초기화 제한은 해당 문서의 남은 이슈를 참고한다.

## 1. 이번에 개발한 내용

- 16번의 메모리 Task 저장을 역할별 SQLite 영속 저장으로 바꿨다.
- 공식 SDK `VersionedTaskStore`를 구현하여 동시 갱신의 버전을 비교한다. 공식 Task/Message/Artifact 객체와 ProtoJSON을 그대로 사용한다.
- 요청을 실행하기 전에 `messageId`, 요청 fingerprint, 서버 발급 Task/Context, 마스킹된 입력을 하나의 트랜잭션으로 저장한다.
- Context의 Agent/Run/Scenario 소유 관계와 Task의 Workflow identity를 재시작 후에도 보존한다. 호출 `attempt`는 승인된 후속 Message에서만 정확히 1 증가한다.
- 동일 요청은 기존 Task만 반환한다. 완료된 Task나 응답이 유실된 요청도 다시 실행하지 않는다.
- INPUT_REQUIRED/AUTH_REQUIRED는 보존하고, 명시적 새 Message로만 같은 Task를 재개한다.
- 종료/재시작으로 중단된 진행 Task는 실행 실패와 중단 사유를 기록한다. 성공이나 제품 결함으로 꾸미지 않는다.
- Task 갱신 이력과 입력 history를 추가 전용으로 저장하고, 같은 DB를 여러 Agent 프로세스가 실행하지 못하게 제한한다.

HTTP API, A2A 버전 `1.0`, `returnImmediately: true`, Task polling 계약은 16번과 같다. SDK 패키지 버전은 기존 `1.2.1`이며 신규 의존성은 추가하지 않았다.

## 2. 변경 파일

| 파일 | 역할 |
| --- | --- |
| `src/agents/api/sqlite_task_store.py` | 공식 SDK 버전 저장 계약, SQLite Task/Context/receipt/이력, 실행 소유권, 중단 복구 |
| `src/agents/api/handler.py` | 요청 선검증·중복 조회·예약 ID 연결·후속 입력·취소·종료 접수 경계 |
| `src/agents/runtime/lifecycle.py` | 실제 실행기보다 먼저 SUBMITTED 게시, 예외 원문 유출 방어 |
| `src/agents/main.py` | lifespan에서 DB 시작, SDK worker 종료 후 DB 정리 |
| `src/agents/core/config.py` | 운영자 DB 경로 설정 |
| `.env.example`, `README.md` | 영속 저장 설정과 운영 제한 안내 |
| `tests/test_agent_task_store.py` | DB·CAS·소유권·중단 복구·추가 전용 이력 검증 |
| `tests/test_agent_lifecycle.py` | API 중복 요청·재시작·후속 입력·상태/Artifact 보존 검증 |
| `tests/test_agent_lifecycle_safety.py` | 예외·취소·첫 이벤트 대기·종료 안전성 검증 |
| 기존 Agent 서버 테스트 | 실제 `.data`가 아닌 각 테스트 전용 임시 DB 사용 |

16번의 메모리 Store 클래스는 과거 어댑터로 남아 있지만, 현재 앱의 기본 저장소는 SQLite다. Orchestrator 저장소·제품 DB·평가 모듈은 변경하지 않았다.

## 3. 저장과 실행 소유권

프로젝트 루트에서 실행할 때 아래 기본 경로를 사용한다. 상대 경로는 실행 디렉터리(CWD) 기준이므로 재시작할 때도 같은 위치에서 실행한다.

| 역할 | 기본 DB |
| --- | --- |
| Planner | `.data/agents/planner.sqlite3` |
| Developer | `.data/agents/developer.sqlite3` |
| QA | `.data/agents/qa.sqlite3` |
| Security | `.data/agents/security.sqlite3` |

운영자만 `AGENT_DATABASE_PATH`로 경로를 바꿀 수 있다. 모델·HTTP 요청이 DB 경로를 선택하지 않는다. 설정 객체·앱 생성만으로 DB를 만들지 않고, 서버 lifespan 시작 시 생성한다. `.data/`는 기존 Git 제외 정책을 유지한다.

DB에는 역할, 프로세스 lease, Context binding, 최신 Task, Task revision, Message history, 요청 receipt를 저장한다. 최신 Task의 A2A 상태는 제품의 Workflow/검증 Verdict와 별개다.

- DB 역할을 동결하여 다른 역할이 같은 파일을 사용하면 시작을 거부한다. 제품/Orchestrator 등 다른 앱의 기존 DB도 거부한다.
- 한 DB는 한 호스트의 **단일 Agent 서비스 프로세스**만 소유한다. 다른 프로세스의 PID가 살아 있거나 생존 여부를 확인할 수 없으면 시작을 거부한다.
- 이전 PID가 확실히 종료된 경우에만 소유권을 회수한다. 시간이 지났다는 이유로 실행 중인 소유권을 빼앗지 않는다.
- 쓰기마다 lease token/PID를 확인하고, Task 변경·revision·history를 한 트랜잭션으로 커밋한다.
- revision/history/receipt/Context binding은 SQL Trigger로 UPDATE/DELETE를 차단한다. 관리자가 DB 파일 자체를 편집하는 공격까지 막는 변조 방지 저장소는 아니다.
- Task 목록·삭제용 HTTP API는 추가하지 않았다. 삭제로 중복 방지 근거를 없애거나 terminal Task를 다시 실행하지 않는다.

## 4. 요청 중복 방지

```text
검증한 Message
  → DB에 receipt + Task/Context + 입력 history 원자 저장
  → 예약한 서버 ID를 공식 SDK RequestContext에 연결
  → SUBMITTED 이벤트 게시
  → 실제 실행기 호출
  → 상태/Artifact를 SDK 이벤트로 갱신
```

`returnImmediately` 요청은 실행기가 LLM/Tool의 첫 결과를 기다리기 전에 접수 상태를 받을 수 있다. 그 이후 상태는 GET으로 확인한다.

| 요청 | 동작 |
| --- | --- |
| 처음 보는 messageId | Agent가 Task/Context를 발급하고 한 번 실행 접수 |
| 같은 messageId + 같은 검증·마스킹된 요청 | 저장된 최신 Task 반환, 실행하지 않음 |
| 같은 messageId + 다른 입력/metadata/Task/Context/설정 | 충돌 오류, 실행하지 않음 |
| 다른 messageId + taskId 없음 | 새 Task. 같은 내용이라는 이유로 이전 작업과 합치지 않음 |
| 재시작 후 동일 messageId | 기존 Task 반환. 중단/완료 여부에 관계없이 자동 재실행하지 않음 |

fingerprint는 Key 순서를 정규화한 검증·마스킹 후 요청의 SHA-256이다. 마스킹으로 제거한 Credential 값은 실행 입력도 아니고 중복 판정용 원본 비밀값도 저장하지 않는다. 이 동작은 [A2A 공식 명세의 선택적 messageId 기반 멱등 처리](https://a2a-protocol.org/v1.0.1/specification/#331-idempotency)에 맞춘 프로젝트 정책이다.

receipt는 실행 전에 저장되므로 응답 유실이나 접수 후 crash에도 서버 ID가 남는다. 다만 **LLM/MCP 외부 부작용까지 전역 exactly-once를 보장한다는 뜻은 아니다.** 실행 여부가 불확실한 작업은 실패/중단을 보존하고 자동으로 다시 실행하지 않는 정책이다.

## 5. Context·재개·취소 규칙

- 새 Context는 해당 Agent 서버가 발급한다. 다른 Agent의 Context, 알 수 없는 Context, 다른 Run/Scenario의 Context는 받지 않는다.
- 같은 Agent/Run/Scenario에서 새 Step은 기존 Context에 새 Task를 만들 수 있다.
- 기존 Task를 이어가려면 원래 Task ID·Context ID·소유자와 Workflow identity를 유지해야 한다. Run/Step/Scenario/Requirement/Code Version/Artifact 참조 등은 바꿀 수 없다.
- SUBMITTED/WORKING Task에 새로운 후속 Message를 넣어 별도 실행을 겹치게 하지 않는다. 동일 접수 Message를 다시 보낸 경우는 상태 조회만 한다.
- INPUT_REQUIRED/AUTH_REQUIRED 후속 입력은 **새 messageId**와 **현재 `attempt + 1`**로 보낸다. `attempt`는 호출 시도 번호이며, 동일 Task 재개에서도 증가시키는 기존 7번 Orchestrator 계약을 따른다. 같은 값 재사용·역행·건너뛰기는 거부한다. 같은 Task/Context에서 SUBMITTED로 다시 접수하고, receipt·최신 metadata binding·revision·이전 질문과 새 입력 history를 원자적으로 저장한다. SDK 상태 이벤트만으로 `attempt`를 바꿀 수 없다.
- 원래 Message를 그대로 재전송하면 대기 상태 Task만 조회된다. 재개 승인이 되지 않는다.
- 인증 Header는 HTTP 인증 경계에서만 사용한다. AUTH_REQUIRED 해소를 위해 Credential을 Message/Artifact/history에 넣지 않는다. 실제 인증 작업 및 Human Review 연동은 후속 단계다.
- COMPLETED/FAILED/CANCELED/REJECTED는 terminal이다. 새 입력으로 재실행하거나 저장 내용을 덮어쓰지 않는다.
- 이미 CANCELED이면 반복 취소는 같은 Task를 반환한다. 다른 terminal Task는 취소 불가 오류다.
- SDK 취소 CAS 특례에 따라 현재 nonterminal Task에 대한 취소는 오래된 버전이어도 적용할 수 있다. 최신 binding 또는 과거 실제 접수 receipt의 승인된 metadata만 허용하고, **최신 attempt/Artifact/history를 보존하고 취소 상태만 반영**한다. 미승인 attempt나 다른 Task/Context identity는 거부한다. 이미 terminal이 된 Task에는 취소를 덮지 않는다.

후속 실제 Agent 실행기는 INPUT_REQUIRED/AUTH_REQUIRED를 게시한 뒤 해당 `execute()`를 반환해야 한다. 이전 호출이 후속 입력 접수 뒤에도 결과를 계속 게시하면 안전한 순차 실행 계약을 깨뜨릴 수 있다.

## 6. 종료와 재시작

정상 종료는 신규 실행 접수 차단 → 공식 SDK `handler.aclose()` → 진행 Task 중단 기록 → lease 해제 순서다. worker의 종료가 확인되기 전에는 DB를 다른 실행 주체에게 넘기지 않는다.

재시작 때는 단일 소유권을 확보한 다음 복구한다.

| 저장 상태 | 복구 정책 |
| --- | --- |
| SUBMITTED / WORKING | FAILED + `AGENT_EXECUTION_INTERRUPTED`, 자동 실행 없음 |
| INPUT_REQUIRED / AUTH_REQUIRED | 그대로 보존, 명시적 후속 Message 대기 |
| COMPLETED / FAILED / CANCELED / REJECTED | 결과·Artifact·history 그대로 보존 |

접수 후 실제 실행 전에 종료된 Task도 실행이 안전하게 이루어졌다고 가정하지 않고 중단으로 기록한다. 실패한 terminal Task를 강제로 재개하지 않는다. 다시 시도할지는 Orchestrator/운영자가 새 논리 작업으로 판단할 문제다.

`TASK_STATE_FAILED`는 **Agent 실행 실패**다. 제품 기능이 FAIL이라고 판단하지 않는다. 이 단계는 제품 Workflow나 Final Verdict를 임의 변경하지 않는다.

## 7. 눈으로 확인하기

```bash
AGENT_ROLE=PLANNER PYTHONPATH=src .venv/bin/python -m agents
```

1. `http://127.0.0.1:8101/docs`에서 SendMessage 예시를 실행한다. Header는 `A2A-Version: 1.0`.
2. 반환된 `task.id`와 `contextId`, 보낸 `messageId` 및 전체 요청을 기록한다.
3. `GET /tasks/{id}`로 조회한다. 기본 런타임은 여전히 `REJECTED / AGENT_RUNTIME_NOT_CONFIGURED`다.
4. **messageId를 포함해 같은 요청을 그대로** 다시 보내면 같은 Task가 반환된다.
5. 서버를 종료하고 같은 역할·DB 경로로 다시 시작한다. 앞의 Task ID를 조회하면 기록이 남아 있다.

입력 대기/Artifact/장시간 실행·취소 예시는 테스트용 Executor로 검증한다. 현재 기본 서버가 실제 Planner 작업을 수행하거나 제품을 만드는 시연은 아니다.

## 8. 개발정의서 준수 점검

| 정의서 항목 | 이번 단계 점검 |
| --- | --- |
| §3 공식 A2A 객체·ID와 내부 Workflow 분리 | SDK Proto 객체·UUIDGenerator, 기존 Project Metadata 재사용 |
| §3 Agent별 Context, Run/Step 추적 | 역할 DB 및 identity binding 영속 저장. 승인된 후속 Message의 attempt +1만 허용 |
| §4 Retry·수정·결과 불확실성 | 요청 replay는 조회만 수행. 수정 최대 3회·MCP retry 최대 2회 정책 변경 없음 |
| §6 HTTP+JSON 1.0·returnImmediately·Task poll | 기존 API 유지, 실제 실행 전 접수 이벤트 게시 |
| §7 공식 상태·제품 Verdict 분리 | 공식 TaskState 그대로, 중단 FAILED를 제품 FAIL/SUCCESS로 매핑하지 않음 |
| §11 추적·원본 보호 | 마스킹된 요청/history·Task revision을 원자/추가 전용으로 기록 |
| Secret·권한 경계 | Credential Header만 사용, 입력/저장/응답/SDK 로그 정제, 예외 원문 차단 |
| 담당 범위 | 1+2번 기반만 구현, 3번/4번 파일·연동 변경 없음 |

정의서 전체 완료 판정이 아니다. 실제 불변 Snapshot/ACL/Sandbox/MCP·LLM 사용량 Trace 등은 해당 후속 번호에서 구현·점검한다. Task 저장 이력은 프로젝트 Trace Schema 전체 구현을 대체하지 않는다.

## 9. 검증·커밋·다음 작업

```bash
PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -q
git diff --check
```

검증 결과:

- 신규 저장소 검증 26개, API 생명주기 검증 16개, 실행/종료 안전성 검증 10개: **52개 통과**.
- 전체 unittest **291개 통과**: 이전 239개 + 신규 52개.
- 중복·동시 요청의 단일 접수, 재시작 후 Task/Context/Artifact 보존, INPUT/AUTH 재개 및 terminal 불변성 확인.
- 일반 stale CAS 차단, stale 취소의 최신 결과 보존, 추가 전용 이력·소유권·역할 DB Guard 확인.
- 첫 이벤트를 내지 않고 대기하는 실행기에서도 접수 응답·취소·서버 종료가 진행됨을 확인.
- 비밀값의 DB 비저장, 실행기 예외 원문 차단, 기존 SDK A2A Client 호환 검증 통과.
- `git diff --check` 통과. backend/frontend/evaluation 및 Orchestrator 구현·의존성/Lock 변경 없음.

테스트는 임시 SQLite와 ASGI/테스트용 Executor로만 수행한다. 실제 LLM·MCP·제품 테스트·비교 실험 완료를 주장하지 않는다. SDK의 비스트리밍/종료 큐 경고는 여전히 관찰되며, 경고를 숨기거나 모든 큐 이벤트가 종료 중 유실 없이 저장된다고 보장하지 않는다. 대신 접수·이력·마지막 저장 상태를 보존하고 종료 시 남은 진행 Task를 중단으로 기록한다.

운영 제한: 단일 호스트·역할 DB당 한 서비스 프로세스, 비스트리밍 MVP다. SQLite 잠금/SDK 종료 경고는 숨기지 않으며 여러 worker·분산 실행 지원을 선언하지 않는다. 여러 Uvicorn worker로 같은 DB를 실행하면 시작이 거부된다.

커밋 메시지 제안: `Agent Task 영속 저장과 중복 요청 방지 및 재시작 복구 구현`

**다음 작업: 18번 — 역할별 Prompt와 출력 계약.** Planner/Developer/QA/Security의 책임·금지·입출력·근거 요구를 기존 Artifact Schema에 맞춰 정의한다. 실제 LLM Provider 연결은 19번이다. Git commit/push는 수행하지 않는다.

18번 구현과 최신 검증 결과는 [역할별 Prompt와 출력 계약](18-role-prompts-output-contracts.md)에 기록했다. 이 문서의 17번 검증·다음 작업 표기는 당시 이력으로 유지한다.
