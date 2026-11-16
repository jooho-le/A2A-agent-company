# 순환 중 QA·Security 세부 피드백

순환용 보호 테스트가 같은 실행에서 기록한 세부 관찰값을 실행 보고서 → 불변 실행 기록 → QA·Security 보고서 → 개발 에이전트 수정 요청의 `fixRequest.validationFeedback`으로 전달한다. 추가 모델 호출이나 테스트 재실행 없이 현재 Snapshot의 결과를 사용한다.

각 검사에는 역할, 요구사항 ID, 검사 ID, 상위 판정, 코드 버전, Source·보고서 식별값, 실행 Manifest, 증거 참조가 연결된다. `observations`에는 사례 ID·항목 ID·항목명·준비 조건/제품 검사 구분·실제값·PASS/FAIL/NOT_RUN/UNVERIFIED·사유가 있다. 같은 검사와 보고서 안의 PASS 항목도 함께 전달하므로 이미 통과한 동작을 확인할 수 있다. FAIL이 하나 있어도 독립 항목의 검사는 계속되며 이후 PASS가 FAIL을 덮어쓰지 않는다.

## 기대조건과 원본 기록

개발 에이전트에 전달하는 세부 결과에는 `expected`·`expectedResult`를 넣지 않는다. QA·Security 수정 Issue의 `expectedResult`도 제거하고, 함께 전달하는 QA·Security 보고서 사본에서도 기대조건 필드를 제거한다. 공개 요구사항과 수락 기준은 기존 `requirementScope`에 남는다. 기존 불변 보고서와 Issue 원본은 감사·출처 확인을 위해 수정하지 않는다. 최종 독립 평가 상세 보고서의 기대값 표시는 그대로 유지한다.

## 상태와 관찰 범위

- PASS·FAIL에는 관찰한 불리언·제한된 정수·HTTP 상태 코드 목록을 보존한다. 문자열 실제값·비밀번호·해시·응답·로그·예외 원문은 전달하지 않는다.
- 미실행(`NOT_RUN`)·미검증(`UNVERIFIED`)의 실제값은 null이다. 앞선 값으로 추정하지 않는다.
- `HASH_FORMAT_REQUIRED`·`HASH_VALUES_REQUIRED` 등 의존 조건 사유를 보존한다.
- 세부 계측이 없는 일반 생성 테스트나 과거 실행 기록은 `observationStatus=UNVERIFIED`, `DETAIL_RECORD_MISSING`으로 표시한다. 없는 세부 결과를 만들지 않는다. 상위 검사 결과는 보존한다.
- 세부 기록이 손상되면 미검증 처리하거나 실행 보고서를 거부한다. 세부 결과와 상위 결과가 모순되면 `PARENT_DETAIL_MISMATCH`를 표시하며 상위 판정을 덮어쓰지 않는다.
- `observationStatus=COMPLETE`는 세부 기록과 상위 결과가 일치한다는 뜻이다. 모든 항목이 PASS라는 뜻은 아니다.

## 실행·보고서 계약

Host의 `unit_runner`는 `--kind PROTECTED`일 때 테스트의 `detail_recorder`에서 세부 관찰을 수집한다. `unittest-v1.tests[]`의 선택 필드 `observations` 또는 `observationError`로 반환하며 검사 수·상위 결과·종료 코드 계약은 유지한다. 기존 보고서는 이 선택 필드 없이 읽을 수 있다. 일반 SNAPSHOT·QA_TESTS가 보호 관찰 필드를 주장하면 저장을 거부한다.

세부 관찰은 닫힌 필드·상태·사유 목록, 중복 항목, 값 형식·크기 제한을 검증한 뒤 동일한 실행 기록 해시에 포함된다. QA·Security 서비스는 Host가 검증한 실행 기록을 다시 읽고 현재 Snapshot·정책·입력과 연결한다. 수정 요청은 인증된 보고서 원본에서 재구성한다. 각 Security 검사에는 그 검사에 해당하는 증거를 연결하며 요구사항 대표 증거를 모든 검사에 복제하지 않는다.

현재 SCN-001 순환용 QA 6개·Security 3개 보호 검사가 이 계측을 제공한다. 최종 시험지·입력은 순환 피드백으로 가져오지 않는다.

## 예시

아래는 형식을 설명하는 예시다. 실제 비교 실험 결과가 아니다.

```json
{
  "requirementId": "<공개 요구사항 ID>",
  "testId": "run_unit_tests:protected-qa:QA-SIGNUP-001",
  "codeVersion": 1,
  "outcome": "FAIL",
  "observationStatus": "COMPLETE",
  "reason": null,
  "observations": [
    {"caseId": "valid_email_1", "itemId": "http_status", "label": "응답 상태", "kind": "assertion", "actual": 201, "status": "PASS", "reason": null},
    {"caseId": "valid_email_1", "itemId": "user_delta", "label": "사용자 수 증가량", "kind": "assertion", "actual": 0, "status": "FAIL", "reason": "EXPECTATION_MISMATCH"}
  ]
}
```

원인이 커밋 누락인지 DB 연결 문제인지는 별도 조사 대상이며 이 결과만으로 확정하지 않는다. 수정 범위는 기존 인증된 Issue와 공개 요구사항을 따른다.

## 최종 검증 — 2026-11-15

커밋 전 `tests`·`checks` 전체 **5,780개 통과, 실패·오류·건너뜀 0**으로 재검증했다. 로컬 Docker 평가 통합 검사 9개를 포함하며 실제 비교 실험·유료 모델 호출은 없었다. 세부 FAIL과 상위 SKIP의 모순도 `PARENT_DETAIL_MISMATCH`로 표시한다. 전체 검토 범위와 실행 증거는 [커밋 전 검증 기록](precommit_verification_2026-11-15.md)을 따른다.

## 최초 관련 검증 기록 — 2026-11-15

최초 구현 직후 관련 테스트 **404개 통과, 실패 0, 오류 0, 건너뜀 0**. 이 단계에서는 전체 테스트를 실행하지 않았으며 위 최종 전체 수치에 합산하지 않는다. 실행 전후 소스와 테스트 파일이 동일함을 확인했다. 실제 비교 실험은 실행하지 않았으며 제공자 자격정보를 상속하지 않은 테스트 프로세스에서 검사용 코드·가짜 모델·Docker 전송 대역을 사용했다.

확인한 범위는 보고서 파서·실행기·불변 기록 저장, QA·Security 서비스에서 수정 요청까지의 연결, 실제 순환용 보호 검사, 수정 요청 인증과 재검증, 기존 최종 평가 상세표·입력 재현 호환성이다. 기대조건 필드 제거, 네 가지 세부 상태 보존, PASS 형제 항목 보존, 기록 누락·위조·민감 문자열 차단, 잘못된 Snapshot 연결 거부를 포함한다.

- [실행 요약](/private/tmp/capstone-loop-feedback-ubvy7f2s/completion.json)
- [검사 묶음 1](/private/tmp/capstone-loop-feedback-ubvy7f2s/group-0.xml) / [검사 묶음 2](/private/tmp/capstone-loop-feedback-ubvy7f2s/group-1.xml)
