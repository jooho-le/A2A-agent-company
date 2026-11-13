# 68번 — 송수신 대조와 미관측 단계 구분

## 목적

[67번](67-a2a-delivery-receipts.md)의 수신 기록을 통신 진단에 연결해 **어디까지 확인됐고, 어떤 이유로 다음 Agent에 전달할 수 없는지** 구분한다. 수신 확인이 없다는 이유만으로 데이터가 사라졌다고 판정하거나, HTTP 응답이 왔다는 이유만으로 정상 전달로 처리하지 않는다.

대상은 기존 구조화 요청·자연어 요청 진단이다. 제품 Host 전체의 Run/Trace 정책이나 프론트 애니메이션을 일괄 변경하지 않는다. 제품 Agent에도 수신 조회 API가 제공되지만, 제품 Pipeline의 모든 호출에 자동 검증을 강제한 것은 아니다.

## 실제 대조 순서

1. `POST /message:send`의 송신 hook에서 SDK가 보낸 전체 요청 JSON을 관측한다.
2. ACK의 Task/Context ID를 먼저 보존한다.
3. 같은 인증 연결로 해당 Message의 DB 접수 기록을 한 번 조회한다.
4. 송신 요청에서 계산한 값과 수신 기록의 Message·역할·Run·Step·Scenario·attempt·코드 버전·Task/Context·세 가지 해시를 대조한다.
5. 기존 수신 hook·실행기 입력·모델 입력 관측·응답 대조도 계속 수행한다.
6. 수신 대조가 통과한 응답만 후속 진단 Agent의 입력으로 사용할 수 있다. 자연어 진단은 기존 내용·역할 계약도 함께 통과해야 한다.

전체 송신 요청은 내부 대조에 사용한다. 새 공개 `delivery` 항목에는 수신 확인 DTO와 비교 결과를 담으며, 별도의 전체 요청 복제본을 추가하지 않는다. 기존 진단의 관측 원문 필드는 유지한다.

## 결과를 읽는 방법

```json
{
  "delivery": {
    "stage": "RESPONSE_VERIFIED",
    "receipt": { "schemaVersion": 1, "...": "실제 접수 기록의 식별자·해시" },
    "check": { "status": "PASS", "differences": [] }
  }
}
```

위 JSON은 구조를 설명하는 축약 예시다. `receipt` 전체 필드는 67번 계약을 따른다. `delivery.check`는 `PASS`·`FAIL`·`UNVERIFIED`이며, 대조할 기록이 없으면 성공으로 바꾸지 않는다.

| `delivery.stage` | 의미 |
| --- | --- |
| `NOT_DISPATCHED` | 아직 계획 상태이거나 앞 단계 차단으로 건너뜀 |
| `SEND_NOT_OBSERVED` | 진단이 진행됐지만 송신 관측이 없음. 전송 여부를 단정하지 않음 |
| `DELIVERY_UNVERIFIED` | 송신은 관측했으나 유효한 수신 기록을 확인하지 못함 |
| `INTEGRITY_MISMATCH` | 수신 기록·식별자·요청/응답 관측 중 실제 불일치가 확인됨 |
| `PERSISTED_ONLY` | DB 접수 대조는 통과했지만 실행기 입력은 미관측 |
| `EXECUTOR_OBSERVED_RESULT_UNVERIFIED` | 실행기 입력은 관측했지만 결과 응답을 확인하지 못함 |
| `RESPONSE_OBSERVED_UNVERIFIED` | 결과는 보이지만 전체 송수신 대조가 아직 완성되지 않음 |
| `RESPONSE_VERIFIED` | 수신 기록·식별자·기존 요청/응답 대조 통과 |

이 단계는 **통신 관측 상태**다. `RESPONSE_VERIFIED`여도 생성한 내용의 근거 검사는 실패할 수 있다. 내용·할루시네이션 여부는 기존 `grounding`·`sourceGrounding`과 자연어 검토 결과를 별도로 읽는다. 모델 입력 관측도 별도 `model` 필드에 남기며, DB 접수만으로 모델 입력 일치를 주장하지 않는다.

## 불일치·조회 실패 때의 처리

| 상황 | 처리 |
| --- | --- |
| 정상 수신 확인 | `PASS`; 다른 기존 계약도 충족하면 후속 전달 |
| 조회 `404` | `UNVERIFIED / RECEIPT_NOT_OBSERVED`; 후속 전달 차단 |
| 조회 통신·인증·응답 계약 오류 | `UNVERIFIED / RECEIPT_QUERY_UNAVAILABLE`; 후속 전달 차단 |
| 유효한 DTO의 해시·연결 정보 불일치 | `FAIL`과 불일치 필드명 기록; 후속 전달 차단 |
| Agent 작업은 완료했지만 수신 확인 미통과 | 원래 `COMPLETED`·Task ID·응답을 보존하고 전달 자격만 거절 |

수신 확인을 얻으려고 Message를 재전송하거나 LLM을 다시 호출하지 않는다. 이미 ACK를 받았다면 그 Task를 계속 관측하며, 기존 제한시간·취소·정리 절차를 유지한다.

자연어 진단은 `DELIVERY_RECEIPT_REJECTED` 또는 `DELIVERY_RECEIPT_UNVERIFIED`를 전달 거절 사유로 기록한다. 그 때문에 실행하지 않은 다음 역할은 `SKIPPED / UPSTREAM_DELIVERY_NOT_VERIFIED`로 남긴다. 응답 내용을 지워 정상 결과처럼 고치지 않는다.

요약에는 단계별 `deliveryStages` 건수와 미검증 수신 확인 문제도 포함한다. `verifiedRequests`는 기존 대조에 더해 수신 확인도 통과해야 증가한다.

## 시간 관측

기존 전송·대기·Agent 처리·응답 관측·전체 시간을 유지하고 `receiptObservationMs`를 추가했다. ACK 관측부터 수신 확인 조회·대조가 끝날 때까지의 시간을 같은 프로세스의 `perf_counter_ns`로 측정한다.

이 시간은 조회 왕복과 검증 오버헤드를 포함한다. Agent가 생각한 시간이나 순수 네트워크 편도 시간으로 해석하지 않는다. `receivedAt`의 서버 시각과 클라이언트 시각을 빼서 속도를 만들지도 않는다. 상세 처리 속도 개선은 후속 작업 범위다.

## 검증

`tests/test_communication_delivery.py`는 합성 모델 응답과 실제 로컬 HTTP A2A·임시 SQLite를 사용한다. 실제 OpenAI 과금 호출은 없다.

- 수신 기록 없음·해시 불일치·조회 예외를 각각 주입한다.
- 각 경우 자연어 3개 케이스에서 멀티 Planner와 Single의 응답은 보존하고 멀티 후속 역할만 건너뛰는지 확인한다.
- 전송·응답 대조는 통과해도 수신 확인 실패 때문에 후속 전달이 차단되는지 확인한다.
- 각 실험의 모델 호출이 Planner/Single 6회에 그치고, 재전송·자동 재호출이 없는지 확인한다.
- 미관측·접수만 확인·실행기 입력 확인·응답 확인·실제 불일치의 단계 분류와 요약을 검사한다.

67번 신규 계약·API·저장소와 68번 관측·오류 주입 검사는 **19개 통과**했다. 공통 A2A 경로·서버 안전성·예산·기존 싱글/멀티·수정 루프·Host 결과 인수를 포함해, 분할 실행한 **서로 다른 검증 290개가 모두 통과**했다. 최종 수신 Client 변경을 확인하려고 반복한 검사는 합계에서 제외했다. Python AST와 문서 링크, `git diff --check`도 확인했다.

```bash
PYTHONPATH=src:tests .venv/bin/python -m unittest test_a2a_delivery test_communication_delivery test_communication_live test_communication_natural -q
```

위 명령은 이번 수신 확인과 관련 통신 회귀를 재현한다. 일부 검사는 로컬 루프백 소켓을 사용한다.

넓은 회귀 검사 중 기존 `test_owned_agent_fix_pipeline.py`의 합성 QA 입력이 승인 기준과 다른 임의의 기대값을 사용해 차단되는 문제를 발견했다. 변경 전 요청 생성 코드로도 `REQUIREMENT_SCOPE_INVALID`를 재현했으며, 해당 입력이 원래 Scenario의 `acceptanceCriteria`를 참조하도록 수정했다. 제품의 판정 조건을 완화하지 않았고, 이후 수정 루프 6개가 통과했다.

## 개발정의서 준수와 한계

공식 A2A 상태, 기존 metadata·Artifact Schema, 승인·요구사항·Snapshot 기준, 인증·MCP 권한·Retry 정책을 바꾸지 않는다. 수신 확인으로 제품 성공이나 할루시네이션 부재를 판정하지 않는다. 외부 Agent에 조회 기능이 없으면 미검증으로 남긴다.

이번 검증은 전송 관측과 차단 동작의 회귀 확인이다. 기존 실제 OpenAI PDF·실험 데이터는 수정하지 않았으며, 새로운 실제 API 실험이나 Docker 검증을 수행한 결과가 아니다.

## 다음 작업

다음 묶음은 **69·70번**이다. 기존 보강 로드맵의 후속 작업을 두 개씩 이어서 진행한다.
