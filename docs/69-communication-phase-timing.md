# 69번 — 전송·접수 대기·모델·응답 확인 시간 분리

## 목적과 범위

[67·68번](68-delivery-integrity-observations.md)의 수신 대조에 이어, **Agent 간 속도 차이가 어느 구간에서 생겼는지 확인할 수 있도록 진단 시간을 세분화**한다. 실제로 더 빨라졌다는 결과를 만드는 작업은 아니다.

대상은 구조화·자연어 통신 진단이다. 제품 Host의 공식 Trace Schema·RunConfiguration을 바꾸거나, 모든 제품 Run에 새 계측을 연결한 것은 아니다. 프론트 화면·MCP 도구·모델 Prompt·출력 Schema도 변경하지 않는다.

## 측정 방식

동일 진단 실행의 요청들은 같은 `perf_counter_ns` 기준점과 `clockId`를 사용한다. 원격 서버의 UTC 시각과 로컬 시각을 빼서 전송 시간을 만들지 않는다. 결과에는 원시 호스트 시각 대신 실행 기준 상대 구간을 기록한다.

`ProbeRecord.timings`는 기존 숫자 필드를 유지하면서 세부 시간을 추가한다. 새 `timing`에는 `schemaVersion: 1`, 시계 종류·식별자, 관측 순서 검사, 요청 구간, 단계별 상태와 시간을 함께 담는다.

| 주요 필드 | 측정하는 범위 |
| --- | --- |
| `orchestratorPrepareMs` | 요청 준비 시작 → Agent Card 조회 시작 |
| `cardLookupMs` | Agent Card 조회 |
| `serializationMs` | Card 조회 완료 → SDK 요청 송신 관측 |
| `requestTransferMs` | 송신 관측 → 같은 프로세스의 로컬 수신 관측 |
| `admissionAndSchedulingMs` | 수신 관측 → 실행기 시작. 접수 검사·DB 저장·스케줄링 포함 |
| `agentProcessingMs` | 실행기 시작 → 결과 준비 |
| `modelPrepareMs` | 모델 처리 준비 시작 → Schema·입력·요청 구성 완료 |
| `providerPrepareMs` | 모델 요청 준비 완료 → HTTP Transport의 실제 전달 시작 |
| `providerHeadersMs` | Transport 전달 시작 → 응답 헤더 관측 |
| `providerExchangeMs` | Transport 전달 시작 → 응답 본문 전체 수신 관측 |
| `providerDecodeMs` | 본문 수신 완료 → 기존 Provider가 응답을 해석해 반환 |
| `modelValidationMs` | Provider 반환 → 출력 계약·입력 해시 확인 종료 |
| `modelCleanupMs` | 모델 호출 처리 종료 → Provider 정리 종료 |
| `modelTotalMs` | 모델 처리 준비부터 정리까지 |
| `executorBeforeModelMs`, `executorAfterModelMs` | 실행기 내부의 모델 호출 전·후 처리 |
| `resultObservationMs` | 결과 준비 → 저장·Polling·HTTP를 거친 결과 관측 |
| `totalMs` | 개별 진단 요청의 준비 시작 → 관측 종료 |

기존 `queueMs`는 `admissionAndSchedulingMs`의 호환 별칭으로 남긴다. 별도 큐에 순수하게 머문 시간이라고 해석하지 않는다. `ackRoundTripMs`와 `receiptObservationMs`도 유지하며, Agent 처리와 겹치는 구간이므로 전체 시간에 단순 합산하지 않는다.

## API 관측의 의미

기존 `apiRoundTripMs`는 Provider 호출과 로컬 출력 확인을 포함하는 호환 지표다. 새 `providerExchangeMs`는 HTTP 전달부터 본문 전체 수신까지를 구분한다. 이 값에도 네트워크·원격 서비스 처리·본문 소비가 함께 들어가므로 **순수 모델 추론 시간이나 순수 네트워크 지연으로 판정하지 않는다**.

응답 스트림을 관측하는 래퍼는 원래 bytes를 그대로 전달한다. 끝까지 소비했을 때만 `apiBodyRead`를 기록한다. 본문 중간 오류·취소가 발생하면 전체 수신 완료 시점을 만들지 않는다. 이미 본문이 버퍼링된 테스트·사용자 Transport는 헤더 관측과 본문 확인이 같은 시점일 수 있다.

이는 HTTP 본문의 관측이며, 기존 OpenAI 요청을 SSE 스트리밍 모드로 바꾸는 기능이 아니다. 모델 ID·온도·출력 상한·입력 내용·도구 목록·호출 수 정책은 유지한다.

## 미관측·잘못된 시간

| 상태 | 값과 의미 |
| --- | --- |
| `MEASURED` | 시작·종료가 유효하고 순서가 맞는 실제 관측값 |
| `UNVERIFIED` | 필요한 시점이 없으므로 `durationMs: null` |
| `INVALID` | 역전·음수·잘못된 타입의 시점이므로 `durationMs: null` |

시간은 ms 단위 소수 셋째 자리로 표시한다. 실제로 관측된 동일 시점이나 매우 짧은 구간의 반올림 결과는 `0`일 수 있다. 없는 시점을 0으로 채우지는 않는다. 전체 관측 순서가 뒤집혔으면 `ordering: FAIL`로 표시한다. 과거 기록이나 단독 모델 호출처럼 실행 기준점이 없으면 상대 요청 구간은 미검증으로 남긴다.

내부 모델 시점 기록은 화면 관측 콜백을 매번 실행하거나 작업 상태를 바꾸지 않는다. 같은 Probe는 한 모델 시도에만 연결하며, 완료된 Probe를 재사용해 새 API 호출과 이전 시점을 섞으려 하면 호출 전에 거절한다. 새 시도에는 새 Probe가 필요하다.

## 제한시간과 정리

ACK의 Task ID를 먼저 보존하고 그 시점에 결과 관측 기한을 고정한다. 수신 확인 조회에 시간이 걸려도 이후 Polling 기한을 새로 늘리지 않는다. 시간 초과 시 기존 원격 취소·상태 확인 절차를 유지한다.

모델 준비 중 오류도 실패 관측으로 남긴다. API에 도달하지 않았다면 API 시간을 만들지 않고, 본문 오류나 취소가 있으면 실제 관측된 모델 처리 시간과 미완성 교환 구간을 구분한다.

## 구현과 검증

공통 계산은 `src/diagnostics/communication/timing.py`, 시점 기록은 `contracts.py`·`live.py`, 동일 실행 기준과 기한 적용은 `lab.py`·`natural.py`에 있다.

`tests/test_communication_timing.py`는 제어된 시계로 준비·헤더·본문·검증·정리를 분리하고, 버퍼 응답·본문 중간 실패·취소·준비 실패·Probe 재사용·조회 기한과 Task ID 보존을 확인한다. 검증 결과와 재현 명령은 [70번](70-communication-performance-summary.md)에 함께 기록한다.

## 개발정의서 점검

[개발정의서](https://www.notion.so/3e9d062130a9801b943ff3bb463af38a)의 시간 측정·미검증 보존 원칙에 맞춰, 측정 실패를 완료나 0ms로 처리하지 않는다. A2A 1.0 타입·공식 상태·metadata·Artifact Schema, 승인 기준, Snapshot, 예산·Retry·인증·비밀정보 정책은 유지한다.

이번 값은 로컬 진단의 시간이다. 정의서의 제품 `RUN_STARTED → VERDICT_CREATED` 전체시간이나 Human Input 대기시간을 측정한 결과로 대체하지 않는다. 기존 실제 API PDF와 과거 실험 자료는 수정하지 않았다.
