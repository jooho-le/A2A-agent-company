# 67번 — A2A 수신 기록 조회와 송신 JSON 보존

## 목적

“Agent가 받았다고 답했다”는 말에만 의존하지 않고, **Agent가 실제 DB에 접수한 Message의 식별자와 내용 해시를 조회**한다. 새 저장소를 만들지 않고 기존 불변 `agent_message_receipts`를 읽는다. 저장 구조·기존 중복 전송 방지 해시는 변경하지 않는다.

이번 범위는 1번 담당의 통신·상태 관측과 2번 담당의 Agent 수신 인터페이스다. 프론트 화면이나 제품 기능은 수정하지 않는다.

## 동작 순서

1. 기존 `POST /message:send`로 요청을 보낸다.
2. Agent가 접수한 Message, 요청 해시, 발급한 Task/Context, 접수 시각을 기존 DB에 저장한다.
3. 송신자는 원격 Task ID를 보존한 뒤 Message ID로 수신 기록을 조회한다.
4. 서버는 저장된 요청과 기존 해시·metadata·Task 연결을 확인하고, 원문 대신 식별자와 해시만 반환한다.
5. 같은 Message 재조회는 같은 접수 기록을 가리킨다. 조회 때문에 Agent를 실행하거나 Task 상태를 바꾸지 않는다.

Task의 History에는 서버가 발급한 ID가 추가될 수 있으므로, **최초 접수 요청을 저장한 `request_json`을 해시의 기준으로 사용**한다. 재개 이후에도 이전 Message의 attempt·접수 시각은 유지된다.

## 조회 API

```http
GET /project/delivery-receipts/{messageId}
A2A-Version: 1.0
Authorization: Bearer <설정된 Agent 토큰>
```

이 경로는 **프로젝트 확장 API**다. A2A 공식 Task/Message/Artifact의 필드나 상태에 수신 확인 필드를 추가하지 않는다. 기존 인증 설정과 SDK 소유자 범위를 적용하며, Bearer 토큰이 설정된 서버에서는 동일한 인증이 필요하다. 사용자 JWT 통합을 새로 구현한 것은 아니다.

| 항목 | 의미 |
| --- | --- |
| `schemaVersion` | 프로젝트 수신 확인 계약 버전 `1` |
| `messageId`, `receiverRole` | 접수한 Message UUIDv4와 실제 저장소의 Agent 역할 |
| `taskId`, `contextId` | 해당 Message가 연결된 원격 ID. UUID로 강제하지 않는 opaque 문자열 |
| `runId`, `workflowStepId`, `scenarioId` | 기존 workflow metadata의 UUIDv4 |
| `attempt`, `codeVersion` | 당시 요청의 attempt와 선택 코드 버전 |
| `requestSha256` | 기존 저장소 알고리즘으로 계산한 전체 접수 요청 해시 |
| `payloadSha256` | 순서를 보존한 Message Parts의 해시. JSON 데이터와 mediaType 등을 포함 |
| `metadataSha256` | 요청 metadata 전체의 해시 |
| `receivedAt` | 해당 Message가 DB에 최초 접수된 시각 |

정상 응답은 `application/a2a+json`, `A2A-Version: 1.0`, `Cache-Control: no-store`를 사용한다. 원문 요청·인증 토큰·Task Artifact·실행 성공 판정은 반환하지 않는다.

알 수 없거나 현재 소유자에게 속하지 않는 Message는 `404`, 잘못된 Message ID·추가 query parameter는 `400`이다. DB 무결성·서비스 소유권 확인 실패는 수신 확인을 발급하지 않는 오류로 처리한다. 경로·SQL·예외 원문을 응답에 덧붙이지 않는다.

기존 Client에 추가한 `await client.get_delivery_receipt(message_id)`로 조회할 수 있다. Agent Card에서 확인한 동일 서비스 인터페이스를 사용하며, 정상 응답의 버전·미디어 타입·DTO도 검사한다. `404`는 `None`, 다른 조회 실패는 `DeliveryReceiptError`다.

## 송신 내용이 변하는 경우

SDK의 JSON 표현은 숫자를 double로 보관한다. 전송 전에 입력 payload와 metadata를 SDK로 변환하고 다시 읽어 **실제 값 손실이 없는지** 확인한다.

| 입력 예 | 처리 |
| --- | --- |
| `100` → `100.0` | 같은 JSON 숫자로 비교하여 허용 |
| `false` → `0` | 다른 타입이므로 불일치 |
| `9007199254740993` → `9007199254740992` | 실제 정밀도 손실이므로 송신 전에 거절 |
| `"9007199254740993"` | 문자열 값이 보존되므로 허용 |
| 배열 순서·문자열 변경 | 다른 내용으로 비교 |

정확히 표현되는 큰 정수까지 일괄 차단하지는 않는다. 공통 해시는 객체 키 순서와 정수형 실수만 정규화하고, 문자열·배열 순서·Boolean은 보존한다. 비교 대상의 깊이는 64, 정규화 JSON은 1MiB로 제한한다. RFC 표준 서명용 canonicalization은 아니다.

`requestSha256`는 과거 접수 기록과 호환되도록 기존 SDK JSON 직렬화 알고리즘을 유지한다. 나머지 두 해시는 공통 JSON 비교 규칙을 사용하므로 서로 다른 종류의 해시끼리 비교하지 않는다.

## 구현 파일

| 위치 | 역할 |
| --- | --- |
| `src/orchestrator/domain/json_fingerprint.py` | 제한된 JSON 정규화와 SHA-256 |
| `src/orchestrator/a2a/delivery.py` | 수신 확인 DTO·생성·송수신 대조 |
| `src/orchestrator/a2a/requests.py` | SDK 변환 시 payload·metadata 손실 거절 |
| `src/agents/api/sqlite_task_store.py` | 일관된 읽기 트랜잭션, 소유자·기존 해시·Task 연결 검사 |
| `src/agents/api/handler.py`, `routes.py` | 기존 수명주기·인증·버전 검사를 거치는 조회 경로 |
| `src/orchestrator/a2a/client.py` | 명시적인 수신 확인 조회 |

## 검증과 정의서 점검

`tests/test_a2a_delivery.py`에서 숫자 정밀도·타입·순서, 식별자와 세 종류 해시의 변조, 잘못된 DTO, 중복 전송·재개·재시작 시 기록 보존, 소유자 격리, 읽기 전후 Task 버전, 인증·버전·조회 오류를 검사한다. 상세 통합 검증 결과는 [68번](68-delivery-integrity-observations.md)에 함께 기록한다.

- A2A 1.0 타입·공식 상태·기존 workflow metadata 계약을 유지한다.
- 프로젝트 UUIDv4와 원격 opaque Task/Context ID를 구분한다.
- 읽기 API는 재전송·모델 호출·자동 Retry·상태 변경을 수행하지 않는다.
- 기존 마스킹 이후 SDK가 해석해 저장한 JSON을 확인한다. HTTP 원본 bytes나 마스킹 전 비밀값 보존의 증명은 아니다.
- DB 접수 확인은 LLM 입력·도구 실행·내용 정확성·제품 성공을 증명하지 않는다. 서명된 외부 감사 증명도 아니다.

제품 Client에는 명시적 조회 기능을 추가했다. 모든 제품 Run에서 자동으로 조회·차단하는 정책을 적용한 것은 아니며, 자동 대조는 이어지는 68번의 통신 진단 경로에 연결한다.
