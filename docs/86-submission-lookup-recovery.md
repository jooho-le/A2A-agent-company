# 86번 — 접수 조회와 응답 유실·재시작 복구

## 목적과 기준

[85번](85-idempotent-run-submission.md)의 영속 접수 기록을 조회하는 서버 전용 API를 추가했다. POST 응답이 유실돼도 같은 내부 키로 기존 Run을 찾고, 기존 복구 제어를 이용하도록 연결한다. 기준은 [Host·프론트엔드 연동 실행 기준 §4·9·10](https://app.notion.com/p/414d062130a981cb9c52c76b89b4369e)이다.

```text
GET /api/v1/submissions/{internalKey}
Authorization: Bearer <제품 서버 전용 Host 토큰>
```

인증된 실제 Host에서만 제공한다. 인증이 없는 개발용 Bootstrap에 같은 DB를 연결하더라도 이 조회는 `503 HOST_API_NOT_CONFIGURED`로 차단한다.

## 응답 계약

| 필드 | 의미 |
| --- | --- |
| `internalKey` | 제품 서버가 보관한 Host 내부 UUIDv4 |
| `status` | `CREATED`: Run과 접수 기록이 함께 저장됨. 작업 완료가 아님 |
| `requestSha256` | 85번의 원본 Host 요청 JSON 식별값 |
| `runId`, `createdAt` | 확인된 Run ID와 최초 접수 시각 |
| `originalResponse` | 최초 POST 응답의 Run·초기 Step·dispatchStatus |
| `run` | 조회 시점의 현재 Run 공개 상태 |
| `recovery` | 아래의 보수적인 후속 처리 안내 |

`originalResponse`는 역사적 영수증이고 `run`이 현재 상태다. 따라서 최초 `RECEIVED`와 현재 `WAITING_INPUT`이 함께 나오는 것은 정상이다. `SCHEDULED`는 최초 프로세스의 예약 의도이며 실제 전송·실행 완료 증명이 아니다.

### 복구 안내

| `recovery.action` | 조건 | 처리 |
| --- | --- | --- |
| `RECOVER` | RECEIVED + 초기 Planner PENDING + Task/전송 흔적 없음 | 운영자 확인 후 기존 Run의 `/recover`를 명시적으로 호출 가능 |
| `OBSERVE` | 그 밖의 진행 상태 | 같은 Run·Step·Trace를 조회. 새 업무를 생성하지 않음 |
| `REVIEW` | 입력 대기 또는 사람 검토 | 필요한 입력·검토·역할 인증 조건을 확인 |
| `RECONCILE` | 전송 흔적은 있으나 해당 Step의 Task ID가 없음 | 원격 실행 확인 필요. 자동 재전송 금지 |
| `NONE` | FINISHED 또는 ABORTED | 종료 상태 유지 |

`reasonCode`는 안내 근거이고 `automaticRetryAllowed`는 항상 `false`다. 안내는 실행 권한이나 복구 성공 보장이 아니다. 활성 작업·제어 잠금·기존 예산·승인 설정은 실제 제어 API에서 다시 검사한다.

여기서 자동 재시도 금지는 생성 POST·Agent 재전송을 뜻한다. 상태 GET의 정기 조회·제한된 연결 복구는 기존 조회 계약대로 처리한다.

## 오류와 불확실성

- `401`: Host 인증 실패.
- `404 SUBMISSION_NOT_FOUND`: 조회 시점에 커밋된 기록 없음. 진행 중 POST가 아직 커밋되지 않았을 수 있으므로 **POST 실패 확정이 아님**.
- `422`: 내부 키가 UUIDv4가 아님.
- `503 SUBMISSION_LOOKUP_UNAVAILABLE`: 저장소 장애 또는 영수증·Run·Step 연결 불일치.
- 조회 성공·오류에 `Cache-Control: no-store`를 적용한다.

조회는 하나의 SQLite 읽기 트랜잭션으로 접수·현재 Run·Step·전송 흔적을 확인한다. DB 변경, Agent 호출, Workspace 생성, 예산 재할당은 하지 않는다. 원문 요청·토큰·Host 경로도 반환하지 않는다.

## 기존 복구 경로 재사용

`POST /api/v1/runs/{runId}/recover`를 재사용한다. 저장 직후 예약 전에 종료된 Run은 기존 일회성 Planner claim과 제어 잠금으로 같은 Run을 시작한다. 이미 Task ID가 있으면 그 Task를 조회하고, 전송 결과가 불분명하면 409로 거절한다. 입력 전달은 기존 `/resume`·`workflowStepId`·`inputData` 계약을 따른다.

복구용 새 Agent·Run·전송 재시도 횟수·모델 예산을 추가하지 않았다. 조회 실패·404만 보고 새 키를 자동 발급하거나 POST를 자동 재전송하지 않는다. 제품 서버의 클라이언트 키 조회·소유권·접수 상태 저장은 3번 담당이다.

## 검증과 정의서 점검

실제 임시 SQLite·ASGI와 Fake A2A Client로 다음을 확인했다.

- 예약 전 중단된 접수를 재시작 후 조회하며 Run·Step·Trace·작업 수가 증가하지 않음.
- 명시적 복구 시 같은 Run/Step으로 최초 전송 1회. 다시 복구해도 기존 Task 조회만 수행.
- Task ID 없는 불확실한 전송은 조회에서 경고하고 복구 시 전송 0회.
- 최초 접수 상태와 현재 실행 상태 분리, 종료 Run 유지, 손상된 영수증·미인증 접근 차단.

개발정의서의 A2A 상태·ID 생성 주체·수정 최대 3회·Tool Retry 최대 2회·불변 Configuration·최종 판정은 유지했다. 전체 관련 검사 명령과 실행 범위는 [88번](88-host-product-api-handoff.md)에 기록한다.
