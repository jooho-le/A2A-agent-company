# 76번 — 자연어 원문 조건·미승인 제안·질문 분리

## 목적

“동아리 사람들이 쓴다”를 “동아리만 사용할 수 있다”로 바꾸는 추정이 요구사항 필드에 들어가지 않도록 진단 계약을 보강했다. 승인된 제품 원장을 새로 만들지 않고, 자연어 요청의 출처를 보존하는 단계다.

## v3 출력 계약

구현은 `src/diagnostics/communication/guarded_contracts.py`다. 역할별 계약 이름은 `natural_language_handoff_v3_<role>`이며 기존 제품 Artifact Schema와 별개다.

| 필드 | 규칙 |
| --- | --- |
| `requirements[].id` | 중복 없는 간결한 문자열. 제품 Requirement UUID가 아님 |
| `text`·`sourceQuote` | **같은 원문 연속 문자열**. 인용에서 추정·의역을 만들지 않음 |
| `sourceKind` | `EXPLICIT` 또는 `UNDECIDED` |
| `assumptions` | 가정·추가 제안. 확정 요구사항에 합치지 않음 |
| `assumptionStatus` | `UNAPPROVED` 고정 |
| `questions` | 사용자에게 필요한 확인 질문 |
| `decision` | `READY`·`INPUT_REQUIRED`·`REJECTED` |
| `executionState` | `NOT_EXECUTED` 고정 |

요구사항·검사 항목은 각각 최대 64개, 가정·질문은 각각 최대 16개다. 현재 역할 이외의 `roleWork`는 빈 문자열이며 Single은 네 관점을 모두 작성한다. 모든 객체는 닫힌 JSON Schema로 검증한다.

## 판정 규칙

가정·질문·미정 조건이 있는데 `READY`라고 작성하면 `UNAPPROVED_READY_DECISION`으로 거절한다. `INPUT_REQUIRED`에는 실제 질문이 있어야 한다. 이를 빈 가정이나 승인된 조건으로 자동 보정하지 않는다.

정상적인 질문 대기 응답도 통신 결과로 보존한다. 후속 Developer 호출은 중단하고 `UPSTREAM_INPUT_REQUIRED`로 구분한다. 승인 답변을 만들어 넣거나 모델을 다시 호출해 통과시키지 않는다.

## 남는 의미상의 한계

모델이 조건 일부만 인용하거나 미정 여부를 잘못 분류할 가능성은 남는다. 문자 그대로 옮겼다는 사실만으로 올바른 해석이라고 판정하지 않는다. `summary`·`roleWork` 등의 서술은 별도의 [78번](78-semantic-review-handoff-gate.md) 의미 검토를 거친다.

## 검증과 개발정의서 점검

`test_communication_guarded`에서 인용과 다른 요구사항 설명, 가정이 있는 READY, 미정 항목에 기대값 부여, 질문 없는 INPUT_REQUIRED, 다른 역할 출력·허위 실행 선언을 거절하는지 확인한다. 기존 원문·응답을 수정하지 않는지도 확인한다.

싱글·멀티에 같은 계약 생성기·검사기를 적용한다. 제품의 동결 요구사항·승인 Configuration과 공식 A2A 상태는 유지한다. 전체 결과는 [79번](79-guarded-single-multi-regression.md)에 기록한다.

다음은 [77번](77-natural-expectation-bindings.md)의 검사 기대값 연결이다.
