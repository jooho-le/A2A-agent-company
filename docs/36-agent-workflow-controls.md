# 36. 실제 입력 재개·취소·Human Review 제어

## 1. 이번 작업 범위

34~35번의 실제 네 Agent 연결과 수정·재검증 흐름에 기존 Run 제어 API를 연결하고, 실제 SDK 실행 중에도 안전하게 재개·취소하도록 보완했다. 1번 Orchestrator와 2번 Agent/MCP만 변경하며 제품 UI·평가 시스템·팀원 서비스 연동은 포함하지 않는다.

기본 Agent CLI는 여전히 Bootstrap이다. 실제 실행은 승인한 Host가 `create_platform()`으로 구성한 플랫폼에서만 가능하다. 새로운 Provider·모델·환경·Git 저장소를 자동 선택하지 않는다.

## 2. 입력·인증 재개

```text
기존 Task INPUT_REQUIRED / AUTH_REQUIRED
  → 호출자는 해당 Run의 현재 workflowStepId를 선택
  → 보호 입력·인증 설정·기존 예산 확인
  → 같은 Task ID / Context ID / Step에 후속 Message
  → Step.attempt만 증가
  → 실제 완료 결과로 다음 단계 진행
```

- Planner의 추가 입력은 `WAITING_INPUT`에서 재개한다.
- Developer·QA·Security의 중단은 정의서 상태표에 맞게 `HUMAN_REVIEW`에 이전 진행 단계를 저장한다. 새로운 범용 `WAITING_INPUT` 상태 전이를 추가하지 않았다.
- `attempt` 증가는 입력/인증 후속 Message 횟수이며 `fix_attempt`, `codeVersion`, Snapshot 버전을 초기화하거나 증가시키지 않는다. 실제 제품 결함 수정은 기존 수정 Cycle만 사용한다.
- QA/Security 중 한쪽만 재개하면 완료된 다른 쪽은 GET으로 결과를 회수한다. Task·Context·Step·attempt·Source를 유지하고 Message/모델 실행을 반복하지 않는다. 두 Task가 모두 완료되기 전에는 보고서를 취합해 제품 Verdict를 만들지 않는다.
- 선택한 QA/Security가 이미 완료된 뒤 동일 Step으로 안전한 입력 재개 요청을 반복하면, 알려진 두 Task를 GET으로만 확인한다. 입력을 완료 Task나 대기 중인 다른 Task에 적용하지 않으며, 예산 확인·모델 호출·자동 수정도 시작하지 않는다. 비밀값·보호 필드 검사는 중복 요청에도 적용하며, 미전송 peer가 있으면 이 경로로 새 실행을 허용하지 않는다.
- 둘 다 입력/인증을 기다리면 명시적으로 한 Step을 선택해야 한다. 같은 답이나 인증 후속 Message를 두 역할에 방송하지 않는다.
- Run 설정·Scenario·Requirement·Manifest·Source·수정 정책·모델·예산·실행 역할 등을 `inputData`로 교체할 수 없다. 알려진 비밀값과 보호 필드는 HTTP API에서 422, 잘못된 상태/대상/예산은 409로 거부한다. 원래 Task를 불필요하게 REJECTED로 만들기 전에 검사한다.

입력 재개 예시:

```http
POST /api/v1/runs/{runId}/resume
Content-Type: application/json

{
  "workflowStepId": "현재 입력 대기 Step의 UUID",
  "inputData": {"answer": "동결된 요구사항과 환경을 그대로 유지하고 진행해 주세요."}
}
```

`workflowStepId`에는 실제 Run의 Step UUID를 사용한다. Agent Task/Context ID는 요청자가 새로 정하는 값이 아니다.

인증은 운영자가 Host의 Agent HTTP 헤더·Provider 설정 등 해당 인증 경계에서 처리한다. `AUTH_REQUIRED` 재개는 해당 역할의 out-of-band 인증 설정을 확인한 뒤 `{ "authenticationConfigured": true }`만 전달한다. 기존의 `request` 필드 재전달은 역할 보호 입력과 충돌하므로 제거했다. Token/API Key/Authorization을 `inputData`에 넣지 않는다. 인증 확인 Message는 실제 인증 성공을 증명하지 않으며, 여전히 인증에 실패하면 Agent가 다시 중단한다.

## 3. 불확실한 쓰기·미전송 작업 복구

- Developer가 Source 쓰기 **전** 중단한 작업은 같은 Task에서 재개할 수 있다.
- Source를 이미 수정한 뒤 입력/인증 대기에 들어갔다면 기존 변경을 보존하고 `DEVELOPER_CHECKPOINT_BASELINE_MISMATCH`로 자동 실행을 거부한다. 원본으로 reset/checkout하거나 불확실한 write를 반복하지 않는다. Orchestrator는 이를 제품 PASS가 아닌 `HUMAN_REVIEW`로 다룬다.
- `A2A_MESSAGE_SENT`가 있는데 Task ID가 없으면 전송 결과가 불확실하다. 자동 재전송과 취소 완료 기록을 모두 거부한다.
- 후속 Message의 응답이 불확실하면 먼저 기존 Task를 GET으로 확인한다. 같은 후속 Message를 자동으로 반복하지 않는다.
- 전송 기록 자체가 없는 Step은 기존 계획·Issue·Source를 사용해 최초 전송할 수 있다. 초기 Developer의 Planner 참조에 `a2aArtifactId`를 포함하도록 수정했고, QA/Security 요청은 초기 실행과 복구가 동일한 공통 빌더를 사용한다. 역할 실행기의 원본 payload 일치 검사를 완화하지 않았다.

## 4. 실행 중 취소

```text
모든 대상·전송 기록 사전 확인
  → 처음 시작하거나 재개한 로컬 실행 소유자 중단·정리
  → 해당 Run 잠금 반환 후 취소 제어 잠금 획득
  → 원격 SDK producer/consumer·모델·MCP·Host 작업 정리
  → 원격 Task CANCELED 저장/확인
  → 모든 미종료 Task 확인 후 Run ABORTED
```

- 최초 dispatch뿐 아니라 `/resume`·`/recover`도 추적되는 실행 소유자와 동일한 Run 제어 잠금을 사용한다. 재개 후 WORKING이 된 Task도 취소할 수 있다.
- 취소로 중단된 `/resume` 요청은 409를 반환할 수 있다. 별도 `/cancel` 결과와 GET Run 상태로 실제 종료를 확인한다.
- SDK 기본 취소의 조기 상태 발행을 그대로 사용하지 않는다. 공개 SDK `aclose()`로 Worker를 먼저 정리하고, cleanup hook의 이벤트는 저장하지 않는 버퍼로 받아 정리가 끝난 후 최신 Task에 versioned CANCELED를 저장한다.
- 동기 Context/Services factory와 SQLite Task observer가 실행 중이면 취소해도 해당 thread가 끝날 때까지 소유한다. asyncio 반복 취소 및 AnyIO level cancellation에도 정리를 분리하거나 재실행하지 않는다.
- 준비 중인 RUNNING Step도 전송 기록이 전혀 없고 로컬 소유자를 정리했다면 원격 Task를 꾸며 만들지 않고 내부 Step만 취소한다.
- 한 Agent만 취소된 뒤 다른 Agent 취소에 실패하면 확인된 취소 기록은 보존하지만 전체 Run은 ABORTED로 만들지 않는다. 다음 시도는 확인된 취소를 반복하지 않는다.
- 완료/실패/거절된 원격 Task를 CANCELED로 덮어쓰지 않는다. 응답 Task/Context가 기존 값과 다르거나 원격 취소가 확인되지 않으면 전체 취소 성공으로 기록하지 않는다.

```http
POST /api/v1/runs/{runId}/cancel
Content-Type: application/json

{"reason": "USER_CANCELLED"}
```

확인된 사용자 취소 결과는 `status=ABORTED`, `verdict=null`, `terminationReason=USER_CANCELLED`이다. 취소한 파일·Snapshot·보고서·로그를 삭제하지 않는다.

## 5. Human Review와 예산

Human Review의 재개는 **기존 동결 작업을 계속할 권한**일 뿐, FAIL/UNVERIFIED를 PASS로 바꾸거나 이미 등록된 증거를 무시하는 승인이 아니다. 이미 해당 단계의 결과가 등록됐다면 `/resume`으로 Verdict를 덮어쓸 수 없다. Terminal Task를 새 Task로 바꿔 재실행하지 않는다. 현재 지원하는 사람의 제어는 안전한 재개·관찰·취소이며, 요구사항/정책 변경이 필요하면 별도로 승인한 새 Run을 사용한다.

실행이 필요한 재개는 기존 `RunBudgetRegistry`의 동일 예산 객체와 Deadline·누적 사용량을 확인한다. 비소모 `check_model_call()`은 예산을 재발급하거나 호출 횟수를 선점하지 않으며, 한도 초과·Token 사용량 불명·기한 만료·미승인 예산은 실행 전에 차단한다. 대기 시간도 기존 Runtime Deadline에 포함된다.

`POST /api/v1/runs/{runId}/recover`에 `{}`를 보내면 알려진 interrupted Task는 GET으로만 관찰한다. 새 모델 호출 여력이 없어도 이미 완료된 QA/Security 결과를 회수·검증할 수 있다. 관찰 결과 수정이 필요하면 `FIX_REQUIRED`까지만 기록하고 자동으로 새 수정 Cycle을 시작하지 않는다. 새 실행은 명시적 재개와 기존 예산 확인이 필요하다. 확실히 미전송인 Step을 복구 전송할 때도 기존 예산이 필요하다.

`FIX_REQUIRED`에서 `POST /api/v1/runs/{runId}/resume`에 `{}`를 보내면 기존 Issue와 기존 수정 정책으로 다음 수정 Cycle을 시작한다. 아직 새 수정 Step이 없으므로 `workflowStepId`나 `inputData`로 이전 검증 Task를 재실행하지 않는다. 예산이 없으면 상태·수정 횟수·Source를 바꾸기 전에 409로 거부하며, 수정 후 실제 Build·QA·Security 결과를 다시 검증한다.

프로세스 재시작 후에는 사용량/Deadline을 새로 발급해서 이전 Run 실행을 재개하지 않는다. 이미 알고 있는 Task의 조회와 완료 검증 결과 회수는 새 실행과 구분한다. 예산·실제 LLM/A2A/MCP Trace·사용량의 **영속 복원은 37번** 작업이며 이번 단계에서 완료한 기능이 아니다.

## 6. 개발정의서 준수 점검

| 기준 | 확인 |
| --- | --- |
| §3·§6: 공식 A2A Task/Message/Context와 HTTP+JSON 1.0 | 공식 SDK 유지, opaque ID 변경 없음, returnImmediately+Polling 유지 |
| §4·§7: Task 상태와 Workflow/Verdict 분리 | interrupted 재개, 정의서 상태표 유지, COMPLETED만으로 SUCCESS 금지 |
| §4: 수정 3회·불확실 write 즉시 Retry 금지 | fix_attempt/Source 버전 보존, dirty Source 자동 재실행·원복 금지 |
| §5·§9: 동일 불변 Snapshot·Manifest·Lineage | 완료 peer 재실행 없이 동일 후보 증거 취합, 보호 payload 교체 금지 |
| §8: 역할 권한·Secret·Container 경계 | Source/Test 권한 및 실제 MCP 근거 검증 유지, Host 생성 코드 실행·Network fallback 추가 없음 |
| §10: 최종 판정 책임 | Orchestrator만 판정, Human Review 승인으로 PASS 생성 금지 |
| §11: 상태/제어 기록·Secret 보호 | 기존 append-only Trace·SDK Task history 유지, 비밀 입력 거부, 확인된 취소만 기록 |

## 7. 검증과 한계

전용 회귀는 실제 역할 실행기·공식 SDK HTTP/ASGI·Git·SQLite·private Artifact/Tool Store·실제 MCP Dispatcher를 사용한다. Provider/Docker/의미 보안 Proof는 명시적인 합성 fixture이다. 생성 Source/Test bytes를 Host에서 실행하지 않는다.

실제 로컬 TCP 검증에서는 Orchestrator와 네 Agent 서버를 실행해 Planner 입력 재개 → 동일 Task WORKING → 취소를 확인했고, 모델 정리 후 ABORTED, 동일 Task/Context/Step·예산·Deadline 유지와 다섯 listener 반환을 검증했다.

최종 전체 회귀: **2,563개 PASS / 643.288초**, 오류·실패·Skip 없음.

```bash
PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -q
```

첫 전체 실행은 2,562개 중 기존 완료 검증 Step의 중복 입력 재개 테스트에서 오류 1개가 발생했다. 테스트를 완화하지 않고 명시적 완료 Step 재요청을 GET-only로 보완했다. 기존 선택 재개 13개·입력 보호 11개와 실제 SDK/ASGI 신규 중복 입력 테스트를 통과한 뒤 위 전체 회귀를 다시 실행했다. 완료 Task를 새로 실행하거나 다른 대기 Task에 답을 전달하지 않는 기존 계약을 유지한다.

`compileall`, `pip check`, `git diff --check`도 통과했다. 이 전체 회귀는 unittest 기반 Orchestrator/Agent/MCP 검증이며, 4번 담당의 pytest 평가 전체나 실제 제품 품질 비교 실험을 수행한 결과는 아니다.

새 전용 테스트는 총 59개이다.

| 테스트 파일 | 수 | 핵심 확인 |
| --- | ---: | --- |
| `test_planner_developer_continuation.py` | 11 | Planner·초기/수정 Developer의 동일 Task 재개, dirty Source 보존 |
| `test_validation_interrupted_controls.py` | 10 | QA/Security 인증 재개, 기존 Test/Source 보존, 모델·MCP·Host 정리 |
| `test_control_owner_lifecycle.py` | 8 | AnyIO·반복 취소 drain, 실행 소유자/잠금 반환, 초기 Developer 미전송 복구 |
| `test_workflow_control_guards.py` | 11 | 보호 입력·기존 예산 사전 검사, 불확실 전송 차단, SQLite observer 정리 |
| `test_owned_agent_controls.py` | 18 | 실제 SDK/ASGI 전체 제어, 완료 peer/중복 입력 보존, 한도 소진 후 GET 회수, 명시적 수정 재개 |
| `test_owned_controls_tcp.py` | 1 | 다섯 실제 로컬 서버에서 입력 재개 후 실행 중 취소·listener 반환 |

- 실제 외부 LLM·Docker·회원가입 시연 성공·실제 보안 의미 검증·Single vs Multi 비교 실험은 미검증이다.
- 단일 Host/단일 프로세스/단일 event loop 구성이다. 다중 replica나 분산 취소·예산 복원을 지원한다고 주장하지 않는다.
- 기존 보류인 비밀번호 보호 정책 보완과 Agent Task Store 공유 DB 초기화 WAL 경쟁은 이번 범위에 포함하지 않았다.
- 정리 시간 제한은 작업을 버리고 취소 성공으로 보고할 권한이 아니다. 실제 Worker가 끝날 때까지 정리를 소유한다.

## 8. 커밋 메시지와 다음 작업

커밋 메시지: `실제 Agent 입력 재개와 실행 중 취소 및 Human Review 제어 보완`

다음 작업: **37번 — 실제 LLM/A2A/MCP Trace·사용량·비밀정보 마스킹**. 실제 실행의 Trace/사용량과 기존 예산 복원 경계를 영속 기록에 연결한다. Git commit/push는 직접 수행하지 않았다.
