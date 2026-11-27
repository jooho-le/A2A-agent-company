# 71번 — 진단 세션 안에서 Agent 연결과 Card 재사용

## 목적과 적용 범위

[69·70번](70-communication-performance-summary.md)의 시간 구분에 이어, **같은 Agent를 반복 호출할 때 HTTP 클라이언트와 Agent Card를 매번 다시 만드는 비용**을 줄인다. 기존에는 요청마다 클라이언트를 열고 Card를 조회한 뒤 닫았다.

대상은 로컬 통신 진단이다. 제품 Host 전체의 연결 정책이나 OpenAI 클라이언트 수명주기를 바꾼 것은 아니다. Agent Prompt·모델·출력 상한·입력·검증·도구·예산은 유지한다.

## 실행 흐름

1. `running_lab`이 서버·DB와 함께 한 진단 세션을 소유한다.
2. 역할을 처음 호출하면 그 역할의 HTTP 클라이언트와 A2A Client를 생성한다.
3. 첫 Card 조회는 기존 SDK의 프로토콜·인터페이스·동일 Origin 검사로 확인한다.
4. 같은 세션의 후속 요청은 동일 역할의 Client와 확인한 Card를 재사용한다.
5. Message·metadata·Task·수신 확인은 요청마다 새로 보내고 조회한다.
6. 세션 종료 시 클라이언트를 닫고 기존 서버·DB·소켓 정리도 수행한다.

새 구현은 `src/diagnostics/communication/peer_sessions.py`에 두고, `lab.py`의 요청·정리 경로에 연결했다. 세션 밖에서 직접 호출하는 단독 Probe는 기존 요청별 연결 소유 방식을 유지한다.

## 재사용 경계

| 항목 | 정책 |
| --- | --- |
| 역할 | Planner·Developer·QA·Security·Single의 Client를 각각 분리 |
| Endpoint·Bearer | 첫 역할 바인딩에 고정. 바뀌면 기존 Client를 다른 대상으로 재사용하지 않고 거절 |
| Agent Card | 직접 띄운 동일 서버 세션에서만 캐시. 새 세션은 다시 조회 |
| 요청 식별 | Message·Run·Step·Scenario·attempt는 현재 요청에서 설정 |
| Task·결과·수신 확인 | 캐시하지 않고 원래 A2A API·DB 기록으로 확인 |
| Context | 명시적으로 전달한 Context만 이어 사용. 별도 Run은 자동 상속하지 않음 |
| 역할별 동시 대여 | 같은 역할을 동시에 공유하려는 시도는 거절. QA/Security는 서로 다른 Client로 병렬 가능 |
| 세션 종료 | 닫힌 세션을 자동 재개하지 않음. 새 수명주기가 필요 |

서버·자격증명이 바뀌면 새 진단 세션을 시작해야 한다. 외부 Agent에 대한 장기 Card 캐시나 임의 TTL 정책을 추가하지 않는다.

## 요청별 관측과 인증 격리

재사용 Client에 이전 요청의 콜백을 계속 붙여두지 않는다. 현재 대여한 요청의 hook을 `ContextVar`로 연결하고, 역할도 함께 검사한다. 대여 종료·취소 시 바인딩을 해제한다.

종료된 바인딩을 상속한 자식 Task가 나중에 Client를 사용하려 하면 HTTP 전달 전에 거절한다. 콜백은 현재 요청의 Message ID·본문·응답 상태에만 연결되며, 인증 헤더는 각 역할 Client의 기존 Bearer를 유지한다. 관측 데이터나 오류에 토큰·Endpoint 값을 추가하지 않는다.

개별 요청의 취소·오류가 나더라도 알려진 Task ID와 기존 원격 취소 절차를 보존한다. 취소된 대여가 끝나면 같은 역할은 새 Probe에서 다시 사용할 수 있다. 재사용이 Message 재전송이나 모델 재호출을 자동으로 발생시키지는 않는다.

## 기록되는 상태

새 행의 `transport`에서 다음을 확인한다.

```json
{
  "scope": "DIAGNOSTIC_SESSION",
  "sessionId": "진단 세션 UUID",
  "clientReused": true,
  "agentCardCached": true
}
```

처음 대여한 역할은 두 Boolean이 `false`다. 단독 Probe의 scope는 `PER_PROBE`, sessionId는 `null`이다. **Client 재사용은 실제 TCP 연결이나 TLS 세션이 재사용됐다는 증명이 아니다.** HTTP 계층이 연결을 다시 열 수 있으므로 소켓 재사용 횟수를 추정하지 않는다.

이 정보는 진단 JSON에만 남기며, 모델 입력이나 기존 workflow metadata에 넣지 않는다. 캐시·준비 상태가 다른 실행을 비교할 때 구분할 수 있도록 기록한다.

## 검증

`tests/test_communication_peer_sessions.py`에서 실제 SDK·ASGI·SQLite로 Card 1회 조회, 새 Message/Task/Context, 역할별 인증·병렬 콜백 격리, 바인딩 변경·중복 대여·종료 후 사용 거절, 취소·정리를 확인한다.

`tests/test_communication_overhead.py`의 실제 로컬 TCP 검사에서는 Multi 2회와 Single 2회의 **요청 10개에 Card 조회 5회·Client 소유자 5개**를 확인했다. 추가 Single 반복·실시간 구독 실행에서도 Card 조회가 늘지 않았다. 모델 호출 수는 원래 요청 수를 유지한다.

Single 한 Run의 2회 반복에서는 Context를 유지하면서 Task ID가 달라짐을 확인했고, 별도 Run들의 Context는 서로 달랐다. 모델 대기 중 취소 시 원격 `TASK_STATE_CANCELED` 확인과 이후 새 요청의 수신 검증 통과도 확인했다.

## 개발정의서 점검

공식 A2A 1.0 타입·상태·Agent Card 검사·metadata·Artifact Schema, 승인 요구사항·Snapshot·예산·Retry·인증 정책을 유지한다. 캐시로 원격 결과를 대신하거나 수신·내용 검증을 생략하지 않는다. Task 완료를 제품 성공으로 바꾸지 않는다.

실제 OpenAI·Docker 재실험과 전체 지연 개선율은 이번 결과에 포함하지 않는다. 캐시 상태가 추가됐으므로 향후 비교에서는 동일한 관측 모드와 재사용 상태도 함께 확인한다. 상세 회귀 결과는 [72번](72-diagnostic-observer-optimization.md)에 기록한다.
