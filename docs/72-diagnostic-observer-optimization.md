# 72번 — 관측 화면 생성과 JSON 비교의 반복 비용 축소

## 목적

통신 검증이 실행 자체를 과하게 늦추지 않도록 **필요 없는 중간 화면 계산과 반복 정규화**를 줄인다. 보내는 내용이나 검사 조건을 줄여 빠르게 만드는 방식은 사용하지 않는다.

대상은 `src/diagnostics/communication/`이다. 제품 프론트, 4번 담당의 독립 평가·보호된 테스트, 기존 실제 API PDF는 수정하지 않는다. [71번](71-a2a-session-reuse.md)의 연결 재사용과 함께 적용한다.

## 바뀐 처리

| 기존 비용 | 변경 |
| --- | --- |
| 실시간 구독자가 없어도 매 상태에서 `view()`를 만들었음 | `emit=None`이면 중간 화면 없이 최종 관측 결과만 생성 |
| 최종 요약과 행 목록이 각각 전체 `view()`를 다시 만듦 | 최종 행을 한 번 만들고 요약에 같은 관측값 사용 |
| 한 `view()` 안에서 응답·metadata 비교 3개를 두 번 계산 | 같은 화면 계산 안에서 검사 결과를 재사용 |
| 중첩 JSON의 하위 비교마다 해당 트리를 다시 정규화 | 각 입력을 한 번 정규화한 뒤 내부 재귀 비교 |
| 자연어 후속 단계 여러 개를 건너뛸 때 같은 차단 사유를 반복 계산 | 차단 사유를 한 번 계산하고 미실행 행들에 적용 |

`differences()`의 경로·종류·정렬·배열 순서·Boolean/숫자 구분은 유지한다. `false`와 `0`은 계속 다르게 보고, `100`과 `100.0`은 기존 JSON 규칙대로 비교한다. 추가·누락·길이·타입·값 변경을 생략하지 않는다.

결과를 여러 화면이나 요청 사이에서 캐시하지 않는다. 다음 관측 전에 입력이 변하면 새 해시와 차이를 다시 계산한다. 역할별 내용·수신·동일 식별자 검증도 유지한다.

## 실시간 모드와 최종 결과 모드

```python
report = await lab.execute(options)                 # FINAL_ONLY
report = await lab.execute(options, events.append)  # LIVE
```

구조화 CLI는 원래 이벤트를 소비하지 않았으므로 `FINAL_ONLY`를 사용하도록 연결했다. 실시간 콜백을 전달한 호출은 기존 start·row·done 이벤트를 유지한다. 콜백을 무시하는 함수라도 명시적으로 전달하면 LIVE이며, 코드를 추측해 모드를 바꾸지 않는다.

`measurement.observerMode`에 `FINAL_ONLY` 또는 `LIVE`를 기록한다. `peerClientScope`와 행별 `transport`도 함께 남긴다. 기존 `observerCallbackMs`는 실제 콜백 실행 비용이며, 최종 JSON 생성 비용이나 모든 관측 비용을 뜻하지 않는다.

중간 화면을 생략해도 원본 송신·수신·접수·실행기 입력·응답·수신 확인·모델 관측은 계속 기록한다. 최종 검증과 오류·미검증 행도 그대로 출력한다. 자연어 진단은 후속 Agent로 전달하기 전의 필수 계약 검사를 계속 수행한다.

## 실행 소유권

같은 Lab에서 두 실행을 동시에 시작해 `records`를 덮어쓰지 못하도록 했다. 실행 중 추가 Run은 `DIAGNOSTIC_RUN_BUSY`로 거절하고, 두 번째 예산 admission이나 기록 초기화를 하지 않는다. 기존 QA/Security의 서로 다른 역할 병렬 호출은 유지한다.

첫 실행이 끝나거나 취소되면 다음 실행이 가능하다. 잘못된 observer도 admission 전에 거절한다. 이는 진단 객체의 공유 상태를 보호하는 규칙이며, 제품 서비스의 동시 사용자 처리 정책을 새로 정의한 것은 아니다.

## 확인된 반복 비용 감소

| 검사 | 결과 |
| --- | --- |
| 구독 없는 구조화 진단 | Probe별 최종 `view()` 1회, 화면 콜백 비용 0 |
| 실시간 구독 진단 | start·중간 row·done 이벤트 유지 |
| 한 화면의 기본 전송 비교 | 11회에서 8회로 축소. 내용 검사 내부 비교는 별도 유지 |
| `differences()`의 중첩 데이터 정규화 | 각 입력 노드를 한 번 방문하는 정규화 횟수 확인 |
| 다음 관측 전 본문 조작 | 이전 PASS 해시를 재사용하지 않고 새 FAIL 검출 |

원래 Git HEAD의 비교 함수를 별도 메모리 namespace로 읽어, 변경 함수와 동일한 차이를 내는지 확인한 뒤 보조 측정을 했다. 저장소 파일이나 Git 기록을 되돌리지 않았다.

| 로컬 JSON 비교 보조 측정 | 값 |
| --- | --- |
| 합성 입력 | 약 7.6KB, 요구사항 30개와 중첩 16단계 |
| 차이 | 타입 변경·값 변경 2개, 변경 전후 결과 동일 |
| 방식 | 준비 실행 3회, 측정 30회, 앞뒤 실행 순서 교대 |
| 변경 전 중앙값 | 2.353ms |
| 변경 후 중앙값 | 0.269ms |

이 값은 **JSON 차이 계산 함수만의 로컬 측정**이다. 원격 모델·네트워크·DB·전체 Agent 실행을 측정한 값이 아니므로 서비스가 같은 비율로 빨라졌다고 해석하지 않는다. 실제 API 전체 지연은 별도 재실험이 필요하다.

## 검증 명령과 결과

```bash
PYTHONPATH=src:tests .venv/bin/python -m unittest \
  test_communication_peer_sessions test_communication_overhead \
  test_communication_timing test_communication_performance \
  test_communication_live test_communication_natural \
  test_communication_delivery test_a2a_delivery -q -f
```

총 **79개 관련 검사 모두 통과, 11.399초**다. 실제 로컬 TCP·ASGI·SQLite를 사용하고 모델 응답은 합성 Transport로 제어했다. 동일 항목의 반복 실행은 합계에서 제외했다. 이 시간은 테스트 소요시간이며 실제 API나 서비스 지연 수치가 아니다. 변경 Python의 AST·문서 링크·`git diff --check`도 통과했다.

검증에는 역할별 인증·콜백 격리, Card 재사용, 새로운 Message/Task/Context, 동일 Single Context, 취소·정리·후속 재사용, 미관측·변조·내용 차단·예산·실시간 이벤트 보존을 포함한다.

## 개발정의서 점검과 다음 작업

기존 승인 기준·Snapshot·공식 A2A 상태·metadata·Artifact Schema·모델·Prompt·출력 상한·도구·예산·Retry는 유지한다. 실패·미검증 결과를 지우지 않고, 캐시·관측 모드가 다른 실험을 같은 조건이라고 숨기지 않는다. 정의서의 공정한 비교·원본 보존·실제 근거에 따른 판정을 적용한다.

다음 묶음은 **73·74번**이다. 실제 호출 재검증과 결과 정리 단계에서 이번 관측 모드·재사용 상태도 함께 확인한다.
