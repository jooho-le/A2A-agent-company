# 70번 — 역할별 지연 편차와 오래 걸린 구간 요약

## 목적

[69번](69-communication-phase-timing.md)의 세부 시간을 사용해 **어느 역할의 어떤 구간이 오래 걸렸는지, 관측값이 충분한지** 확인한다. 속도를 같게 맞추거나 Multi가 더 빠르다는 결론을 자동으로 만드는 작업은 아니다.

계산은 `src/diagnostics/communication/performance.py`에 분리하고 기존 구조화·자연어 진단 결과의 `performance`에 연결했다. 구조화 CLI의 기존 HTML 생성기에는 단계별 p50 표를 추가했다. 제품 프론트와 4번 담당의 독립 비교평가·보호된 테스트는 변경하지 않는다.

## 결과 위치

| 진단 | 저장되는 위치 |
| --- | --- |
| 구조화 요청 | 새 실행의 `report.json → runs[] → performance` |
| 자연어 요청 | 새 실행의 `run.json → runs[] → performance` |
| 구조화 HTML 요약 | 기존 `report.html`의 ‘단계별 지연 관측’ 표 |

이번 작업에서 실제 API를 다시 호출하거나 새 PDF·실험 결과 파일을 만들지는 않았다. 위 위치는 이후 기존 CLI를 실행했을 때의 결과 구조다.

## 역할별 통계

`performance.roles.<역할>`에서 다음을 확인한다.

| 필드 | 의미 |
| --- | --- |
| `completedSamples` | 진단과 모델이 모두 완료된 호출 수 |
| `incompleteSamples` | 실행했지만 위 완료 근거를 충족하지 않은 호출 수 |
| `notExecutedSamples` | 계획 또는 건너뜀 상태여서 실행하지 않은 요청 수 |
| `deliveryVerifiedSamples` | 완료 호출 중 수신 확인까지 통과한 수 |
| `contentContractPassedSamples` | 완료 호출 중 선언된 내용 계약도 통과한 수 |
| `phases.<구간>` | 관측 표본 수, 미검증·잘못된 값 수, 최소·최대·p50·p95 |
| `dominantPhase` | 기본 처리 구간 중 p50이 가장 큰 항목 |
| `dominantModelPhase` | 모델 처리 내부 구간 중 p50이 가장 큰 항목 |
| `providerMsPerOutputToken` | API 교환 시간을 보고된 출력 토큰 수로 나눈 참고 분포 |

일반 지연 통계에는 완료 호출만 넣는다. 실패·취소가 빨리 끝났다는 이유로 정상 지연이 개선된 것처럼 계산하지 않는다. 미완료 원문·오류·측정값은 기존 행과 모델 호출 기록에 남긴다.

완료는 내용 정확성이나 제품 성공을 뜻하지 않는다. 수신 확인·내용 계약 건수를 따로 표시하므로, 완료했지만 전달 자격이 거절된 결과도 숨기지 않는다. 보호된 평가 테스트를 대신하는 성공률은 만들지 않는다.

## 역할 간 차이

`roleSpread`는 역할별 `agentProcessingMs` p50의 가장 작은 값과 큰 값을 비교한다. `fastestRole`, `slowestRole`, `deltaMs`, `ratio`를 제공한다. 비교할 역할이 두 개 미만이면 미검증이며, 기준값이 0이면 배율은 `null`이다.

이는 **현재 관측의 차이**다. 역할마다 입력·출력 크기와 업무가 다르므로 배율을 Agent 자체 성능이나 구조 우열로 해석하지 않는다. 표본 수를 함께 읽어야 하며, 한 번 측정한 p95도 서비스의 장기 p95를 입증하지 않는다. 백분위는 실제 완료 표본의 nearest-rank 방식으로 계산한다.

출력 토큰 수도 함께 보도록 했다. [OpenAI 지연 최적화 안내](https://developers.openai.com/api/docs/guides/latency-optimization)는 생성량을 지연의 한 요인으로 설명한다. 다만 여기의 토큰당 시간에는 네트워크·원격 처리 등이 포함되므로 순수 생성 속도나 추론 시간을 뜻하지 않는다. 토큰 수가 없거나 0이면 값을 만들지 않는다.

## 병렬 시간 계산

정의서의 “병렬 실행시간을 단순 합산하지 않는다”는 원칙을 적용한다. 같은 실행 기준을 가진 개별 요청의 준비~관측 종료 구간을 모아 겹침을 계산한다.

| `timeline` 필드 | 의미 |
| --- | --- |
| `probeWorkSumMs` | 개별 진단 요청 구간 길이의 합. 전체시간으로 사용하지 않음 |
| `observedUnionMs` | 하나 이상의 요청이 관측 중이었던 구간의 합집합 길이 |
| `observedEnvelopeMs` | 가장 이른 요청 시작부터 가장 늦은 요청 종료까지 |
| `concurrentMs` | 두 개 이상 요청의 관측 구간이 겹친 시간 |
| `maxConcurrency` | 요청 관측 구간의 최대 동시 개수 |
| `diagnosticElapsedMs` | 별도 상위 필드. 진단 실행 자체의 시작~종료 관측값 |

QA가 0~100ms, Security가 25~125ms였다면 합계는 200ms지만 합집합은 125ms, 겹친 시간은 75ms다. 이 예시는 계산 규칙 설명용이며 실제 모델 성능 수치가 아니다. 이 구간은 HTTP·대기·결과 확인을 포함한 요청 범위로, Agent CPU의 동시 실행 시간과 같지 않다.

기록 일부가 없으면 `PARTIAL`로 표시하고 관측된 부분만 계산한다. 서로 다른 `clockId`가 섞이거나 구간이 뒤집혔으면 `INVALID`이며 합집합 값을 발급하지 않는다. 미실행 요청에는 시간을 부여하지 않는다.

접수·대기·Agent 처리·결과 확인의 기본 구간과 그 안의 모델 세부 구간을 별도로 비교한다. ACK·수신 확인·모델 내부 시간을 중복 합산해 전체시간을 만들지 않는다.

## 검증 결과

```bash
PYTHONPATH=src:tests .venv/bin/python -m unittest \
  test_communication_timing test_communication_performance \
  test_communication_live test_communication_natural \
  test_communication_delivery test_a2a_delivery -q -f
```

총 **64개 관련 검사 모두 통과, 8.339초**다. 시간 계산·본문 처리·기한·재사용, 병렬 합집합·서로 다른 시계·미관측·실패 표본 제외·토큰 누락·HTML 요약, 실제 로컬 A2A의 싱글/멀티와 자연어 기록 보존을 포함한다. 같은 항목의 반복 실행은 합계에서 제외했다. 이 실행시간은 테스트 소요시간이며 서비스 성능 수치가 아니다. 변경 Python 파일의 AST·문서 링크·`git diff --check`도 통과했다.

기존 HTML 요약에서도 수신 확인·metadata·모델 입력 대조가 미통과한 행을 송수신 PASS로 집계하지 않도록 맞췄다. 예전 PDF를 소급 수정하거나 미검증 결과를 삭제하지 않는다.

## 개발정의서 점검과 다음 작업

모델·요구사항·Prompt·Schema·도구·한도·실행 순서를 자동 변경하지 않는다. 제품 Run의 동결 설정과 실제 최종 판정 조건도 유지한다. 이번 검증은 합성 모델 응답과 로컬 A2A·임시 DB 기반이며, 실제 OpenAI 성능·Docker·비교 실험의 개선율을 입증한 결과는 아니다.

다음 묶음은 **71·72번**이다. 관측한 구간을 바탕으로 후속 성능 보강을 진행한다.
