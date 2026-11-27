# 78번 — 별도 의미 검토와 후속 인계 차단

## 검토 추가 이유

인용·Schema가 정확해도 서술이 원문 범위를 넓힐 수 있다. OpenAI 공식 문서도 Structured Outputs가 내용상의 모든 오류를 없애지는 않는다고 설명한다. 따라서 구조 검사와 의미 검토를 분리했다. [OpenAI Docs: Structured Outputs](https://developers.openai.com/api/docs/guides/structured-outputs)

## 실제 처리 순서

1. 역할 응답을 기존 OpenAI Provider와 닫힌 v3 Schema로 받는다.
2. 원문 인용·역할·기대값 연결·미승인 READY 등 결정적인 조건을 검사한다.
3. 구조 검사를 통과한 응답만 별도 Responses API 호출로 의미 검토한다.
4. 검토 결과를 해당 Probe·역할·원문 해시·응답 해시에 묶는다.
5. 실제 API 입력 대조와 검토 완료까지 확인한 뒤, `READY`일 때만 다음 진단 역할로 전달한다.

구현은 `semantic_review.py`와 `guarded.py`다. 검토기 입력은 원문·실제 candidate뿐이며 케이스별 정답이나 보호된 시험지를 넣지 않는다. 별도 시스템 지시는 candidate 안의 위조 지시를 따르지 않도록 한다.

## 판정과 근거

| 모델의 검토 | Runtime 처리 |
| --- | --- |
| `SUPPORTED`, findings 비어 있음 | 바인딩·API 증거·READY를 추가 확인 |
| `UNSUPPORTED`, 유효한 findings 존재 | `REJECTED`로 전달 중단 |
| `UNVERIFIED` | 전달 중단·미검증 보존 |
| 결과 없음·Schema 오류·상충된 판정 | 미검증, 전달 중단 |
| 원문/응답에 없는 인용·잘못된 JSON Pointer | 미검증, 전달 중단 |

각 finding은 실제 candidate의 문자열 필드를 가리키는 `fieldPath`, 그 안의 `statementQuote`, 원문에 있는 `requestQuote`, 범주와 짧은 판정 근거를 가진다. 인용이 유효하다는 것은 검토 근거의 존재를 확인한 것이며 검토기 판단 자체가 항상 옳다는 뜻은 아니다.

검토 후 candidate가 달라지면 같은 검토를 재사용할 수 없다. 응답에 승인 문구나 가짜 검토 객체를 넣어도 Host 측 기록을 대신할 수 없다. 검토 API 오류·취소·인증/과금 오류를 통과 결과로 바꾸지 않는다.

## 횟수·비용·지연

guarded 실행은 **역할 응답 최대 20회 + 의미 검토 최대 20회**다. 양쪽 호출·토큰·모델·HTTP 상태·Request ID를 따로 기록한다. 각 역할 응답과 검토는 한 번만 시도하고 자동 Retry나 모델 교체는 없다.

검토는 같은 명시 모델·온도를 사용한다. 최초 검토 출력 상한은 2,048 tokens였고, 필드별 근거를 추가한 최종판은 8,192 tokens다. 별도 네트워크 호출이므로 비용과 시간이 추가된다. Agent 처리 시간에 검토도 포함되며 기존 단일 API 교환 시간과 혼동하지 않는다. 원자료의 `calls` 배열은 역할/검토 묶음 순서이며 실제 호출 시간 순서가 아니다.

## 실제 API 대조에서 발견한 문제와 최종판

80번에서 정상 응답 1개와 고의 오류 2개로 검토기를 직접 확인했다. 최초 검토기는 개인정보 범위 확대를 놓쳤다. 중간판은 각 필드의 근거를 요구했지만 검사 이름 대신 기대값을 인용해 정상 응답도 미검증으로 보류했다. 두 실패 기록을 모두 보존했다.

최종 계약은 `natural_semantic_review_v3`다. `summary`·비어 있지 않은 `roleWork`·각 검사 이름의 **모든 문자열 필드**에 `fieldReviews`를 요구한다. 각 필드는 판정·원문 인용·짧은 근거를 가지며, candidate의 인용 문장은 해당 실제 문자열을 Schema enum으로 고정한다. 이는 이미 입력에 있는 candidate의 복사 제약이며 보호된 정답을 주입하는 것이 아니다.

일부 정보만 제외한 원문에서 모든 개인정보 미수집을 단정하는 알려진 확대 문구에는 `unapproved_data_scope()`가 보수적인 보류를 추가한다. 모델이 SUPPORTED라고 해도 이 경우 `UNVERIFIED / UNAPPROVED_DATA_SCOPE_REQUIRES_REVIEW`로 남긴다. 모델의 원래 SUPPORTED와 보류 원인도 함께 보존한다.

이 보조 규칙은 제한된 한국어·영어 텍스트 단서 기반이다. 모든 표현·문맥을 이해하거나 새로운 승인을 부여하지 않는다. 과도한 보류와 놓치는 표현이 있을 수 있다. 원문에 정보 제외 단서가 있다는 이유만으로 인계를 허용하지도 않는다. Schema·API 입력 증거·모델 검토·READY 조건이 여전히 모두 필요하다.

대조 실험의 **의미 판정 일치**와 **잘못된 인계를 막는 정책 일치**는 구분한다. 개인정보 대조에서 모델이 올바르게 거절하지 못했더라도, 미검증 보류는 전달을 중단하는 결과다. 이를 올바른 의미 검출로 다시 표시하지 않는다. 상세 수치는 [80번](80-guarded-live-revalidation.md)에 기록한다.

## 권한과 평가 한계

검토 기록의 출처는 `MODEL_CRITIQUE_NOT_HUMAN_APPROVAL`, `isProductApproval=false`다. 보조 모델이 SUPPORTED라고 해도 요구사항을 승인하거나 제품 Host·MCP·배포를 허가하지 않는다.

같은 모델의 별도 호출이라 오류나 편향이 남을 수 있다. 독립 사람 평가나 모든 할루시네이션 부재로 발표하지 않는다. 공식 평가 안내도 모델 채점과 사람 판정의 일치 여부를 확인하도록 권한다. [OpenAI Docs: Evaluation best practices](https://developers.openai.com/api/docs/guides/evaluation-best-practices)

## 검증과 다음 작업

미확인/위조/서로 다른 응답의 검토, 없는 인용, 빈 거절 근거, 취소·API 인증 오류, 검토 뒤 candidate 변경에서 차단되는지 확인한다. 제품의 기존 권한·인증·수정 최대 3회·Tool 재시도 최대 2회는 유지한다.

다음은 [79번](79-guarded-single-multi-regression.md)의 공통 회귀 검사이며, 실제 API 결과는 [80번](80-guarded-live-revalidation.md)에 정리한다.
