# 77번 — 자연어 진단 검사 기대값의 출처 연결

## 문제

기존 v2는 `checks.sourceQuote`가 원문에 있는지 확인했지만 `checks.expected`가 그 조건을 뜻하는지는 강제하지 않았다. 따라서 “여러 명의 소통”을 인용하면서 “댓글·동시 작성이 필수”라는 기대값을 쓸 수 있었다.

## 변경 규칙

v3의 각 검사에는 `requirementId`를 추가했다. 다음 조건을 모두 만족해야 한다.

1. 현재 목록의 `EXPLICIT` Requirement에 연결한다.
2. `expected`는 연결한 Requirement의 `sourceQuote`와 정확히 같다.
3. 검사 자신의 `sourceQuote`도 같은 문자열이다.
4. 모든 EXPLICIT 항목이 최소 한 번 연결돼야 한다.
5. UNDECIDED 항목은 확정 기대값의 근거가 될 수 없다.

다른 ID, 가정에서 만든 기대값, 인용과 다른 문구, 누락된 연결은 `UNBOUND_OR_CHANGED_EXPECTATION` 또는 `EXPLICIT_REQUIREMENT_NOT_COVERED`로 거절한다. 명확한 구조 오류에는 의미 검토 API를 추가 호출하지 않는다.

## 역할 사이 목록 동결

Developer·QA·Security가 받은 `upstream.answer.requirements`와 현재 응답의 요구사항 목록을 대조한다. ID·순서·원문·미정 분류 중 하나라도 바뀌면 `UPSTREAM_REQUIREMENTS_CHANGED`로 차단한다.

필요한 요구사항 변경은 질문·별도 확인으로 남겨야 한다. 이전 응답의 가정을 새 기준으로 바꾸거나, 원본 목록을 수정해서 통과시키지 않는다. 실제 제품에서는 여전히 Host의 승인 원장이 기준이다.

## 적용 범위와 한계

제품 64번의 “배정된 기준 문자열에 정확히 연결” 원칙을 진단에도 적용한 것이다. 제품의 `validate_generated_case_expectations()`를 동결 Configuration 없이 호출하거나 가짜 승인 객체를 만들지는 않는다.

검사 이름이나 역할 본문에 숨은 추가 조건, 인용 범위를 잘못 자른 의미 오류까지 이 문자열 검사로 해결했다고 선언하지 않는다. 그런 내용은 별도 의미 검토 대상으로 남긴다. 진단의 검사 계획은 실제 테스트 코드나 실행 Receipt가 아니다.

## 확인 항목

새 회귀 검사는 댓글 기대값 추가, 없는 Requirement 참조, 누락된 검사, 미정 항목의 확정 기대값, 후속 역할의 ID 변경을 주입한다. 원래 응답을 그대로 보존한 채 차단하고, 불필요한 검토 API 호출이 발생하지 않는지도 확인한다. 싱글과 멀티는 같은 함수를 사용한다.

전체 검증 결과는 [79번](79-guarded-single-multi-regression.md), 실제 API 관측은 [80번](80-guarded-live-revalidation.md)에 기록한다. 다음은 [78번](78-semantic-review-handoff-gate.md)의 의미 검토·인계 차단이다.
