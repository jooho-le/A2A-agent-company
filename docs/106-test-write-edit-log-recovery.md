# 106. 생성 시험·편집 복구·DB 로그 비노출 보완

작성일: 2026-11-23. 105번의 실제 실패 기록을 바탕으로 플랫폼을 수정했다. **이번 작업의 실제 OpenAI 호출·새 Run·Docker 실행은 모두 0회**다. 기존 실험의 Source나 성능 수치는 바꾸지 않았다.

## 변경 요약

| 확인된 문제 | 보완한 동작 |
| --- | --- |
| Security 시험 파일에 실제 NUL 바이트 저장 | Python 시험 내용을 쓰기 전에 파싱해 거절하고, 기존 교정 한도 안에서 재작성 요청 |
| 출력 검사 함수에 문자열 대신 목록 전달 | 공개 helper의 인자형·인자명·keyword-only 규격을 안내하고 명확한 오용을 사전 검사 |
| 해시 측정 결과에 없는 키 사용 | 실제 반환 키를 안내하고 확인 가능한 잘못된 키 접근을 검사 |
| 존재하지 않는 교체 문구를 반복해서 편집 | 새 파일 읽기 → 같은 파일·현재 해시에 묶인 전체 파일 수정으로 복구 절차 고정 |
| 중복 외 DB 오류가 빠져 로그에 해시 노출 | 중복 예외만 잡는 commit 경로를 완료 전 검토 대상으로 추가하고 안전한 오류 처리 안내 |

## 1. 시험 파일을 쓰기 전에 검사

`test_write_precheck.inspect_test_write()`는 제안된 Python 파일을 **파싱만** 한다. 생성 앱이나 시험을 import·실행하지 않으며, Source를 자동 변환하지 않는다. 브라우저 JSON 등 다른 형식은 해당 형식의 기존 검증을 유지한다.

| 사유 코드 | 검사하는 내용 |
| --- | --- |
| `GENERATED_TEST_SYNTAX_INVALID` | NUL 등 실제 Python 문법 오류 |
| `TEST_HOST_ARGUMENT_INVALID` | 알려진 Host helper의 잘못된 인자명·필수 인자·keyword-only 사용과 명확한 문자열 인자형 오류 |
| `TEST_HOST_RESULT_KEY_UNKNOWN` | 직접 측정한 Host 결과에서 존재하지 않는 키를 바로 읽는 구문 |
| 기존 member·return 코드 | 없는 helper 사용, 딕셔너리 반환값의 숫자 오독 등 104번 검사 |

여러 호출을 한 응답에 보냈더라도 **첫 도구를 실행하기 전에 배치 전체를 확인**한다. 뒤쪽 시험 쓰기가 잘못됐다면 앞쪽 읽기·쓰기도 실행하지 않는다. 이전 응답에서 이미 성공한 쓰기는 반복하지 않는다.

교정은 기존 `max_output_corrections`와 Run 모델·토큰 예산을 사용한다. 같은 오류의 무한 재작성 기회를 추가하지 않았다. 소진하면 정책 사유 `TEST_SOURCE_INVALID`를 가진 `LLM_VALIDATION_DRAFT_EXHAUSTED`로 멈춘다. 피드백에는 닫힌 사유·파일 위치·행 번호를 보내며 원본 시험 내용이나 비밀번호를 복사하지 않는다.

직접 정의한 helper, 외부 상속, 동적 인자·불확실한 변경은 추측으로 차단하지 않는다. 명확하지 않은 경우에는 실제 실행과 기존 판정을 유지한다. ACL·인자 스키마·인증정보 검사가 이 검사보다 먼저 적용된다.

## 2. 실제 helper 인자 오류도 같은 시험의 수정으로 연결

공개 fixture 계약에 실제 인자형과 비밀번호 생성 수명·제약을 추가했다. 비밀번호 문자열은 목록으로 감싸지 않고 그대로 전달해야 한다. 짧은 비밀번호·필드 누락 같은 음성 시험은 제거하지 않고, 해당 helper가 지원하지 않는 요청은 `self.client.post`와 기존 응답·DB assertion으로 유지하도록 안내한다.

실행 중에는 **실제 Host 함수의 코드 프레임이 제품 요청 전에 인자를 거절한 경우**에만 `NOT_RUN_FIXTURE_ARGUMENT_INVALID`로 분류한다. 동일 문구의 일반 ValueError나 실제 제품 실패를 이 사유로 바꾸지 않는다. 확인 가능한 반환 키 오용도 기존 제한된 반환형 오류 경로에 연결했다.

이 분류는 생성 `QA_TESTS`에서만 켠다. 보호시험·제품 Snapshot 시험에는 적용하지 않는다. 모델이 스스로 같은 사유로 skip해도 Host 검증으로 인정하지 않는다.

기존 시험 ID·요구사항·제목·선언된 기대 결과·Source를 유지하며, QA·Security가 시험을 고치는 기회는 기존처럼 한 번이다. 계속 잘못되면 UNVERIFIED로 남는다. 실제 제품 FAIL을 PASS로 바꾸는 처리는 없다.

## 3. 편집 대상 불일치 복구

도구가 **아무것도 쓰지 않았다고 확인된** `EDIT_TARGET_MISSING`·`EDIT_TARGET_AMBIGUOUS`에 다음 절차를 적용한다.

1. `read_project_file`만 제공해 해당 파일의 실제 내용과 새 SHA-256을 읽는다.
2. 다음 교정 요청에는 `write_source_file`만 제공한다. 경로와 `expectedSha256`을 방금 읽은 값으로 스키마에 고정한다.
3. 모델은 현재 파일에 수정을 반영한 전체 내용을 제출한다. 기존 무관한 코드를 보존하라는 안내와 기존 Source 보존 검사는 유지한다.
4. 순변경이 없으면 완료로 인정하지 않는다. 동시 변경은 기존 CAS 검증이 거절하며, 결과 불명 쓰기는 재시도하지 않는다.

같은 실패 뒤 다른 `oldText`를 계속 추측하는 부분 편집은 제공하지 않는다. 제공하지 않은 부분 편집을 모델이 다시 요청해도 실행 전에 차단된다. 읽기·쓰기 권한, 경로 검사, 해시 검사나 예산을 완화하지 않았다. 처음부터 허용된 읽기·전체 쓰기 도구가 없는 단계에 새 권한을 추가하지도 않는다.

## 4. DB 오류 로그의 해시 노출 경로 검토

Single Ver2에서 확인된 패턴은 commit을 `IntegrityError`로만 감싸서 **다른 SQLAlchemy 오류가 프레임워크 오류 기록으로 빠져나가는 경로**였다. `DB_ERROR_PATH_NOT_HANDLED`로 이 명확한 패턴을 Source 검토에 포함했다. 실제 보호시험 실패로 단정하는 정적 판정은 아니다.

교정 안내는 기존 중복 거부를 유지하면서 다른 DB 오류에 rollback·값 없는 오류 응답을 적용하고, 예외 문자열·SQL 바인드 값·traceback을 출력하지 않도록 한다. `create_engine(..., hide_parameters=True)`도 추가 방어로 안내한다. 파라미터 숨김만으로 오류 처리·rollback이 구현됐다고 인정하지 않는다.

또한 `addFilter()` 등록 사실만으로 exception 로그 검토를 생략하던 처리를 제거했다. 필터가 있다는 사실은 `exc_info`, 캐시된 `exc_text`, SQL 파라미터가 모두 제거됐다는 증거가 아니다. 일반 로그를 모두 끄는 방식이나 평가 기준 변경은 요구하지 않는다.

직접 작성한 SQLAlchemy·FastAPI fixture에서는 같은 종류의 DB 오류로 해시가 오류 기록에 들어가는 것을 재현하고, 안전한 로컬 처리 또는 실제 engine의 `hide_parameters` 설정이 해당 노출을 막는지 구분해 검사했다. 불안전한 fixture 로그는 테스트 전용 메모리에만 보관하며 stdout·pytest 루트 로그로 전달하지 않는다.

## 5. 정책·근거 관리

- 새 쓰기 전 검사 모듈도 Host의 `recoveryImplementationSha256`에 포함했다. 코드가 바뀌면 실행 정책 해시가 달라지는 것을 검사한다.
- Host fixture 정책 V7, Draft 사전 검사 정책 V7, 도구 오류 복구 정책 V4, 가입 Source 검토 정책 V5다.
- 기존 A2A 상태·Artifact 구조·MCP 권한·Single Context·보호시험·독립 최종 시험·가격표·예산은 유지했다.
- 105번의 실패 파일은 AST로만 읽었다. Multi Ver1 QA의 인자형 오류, 두 Security 파일의 문법 오류, Single Ver2 제품의 DB 처리 누락을 새 검사에서 확인했다.
- 기존 Run·Source·`.env`·사용자 설정·105번 보고서는 수정하지 않았다. 프로젝트 Git 커밋·푸시는 하지 않았다.

## 검증

| 검사 | 결과 |
| --- | --- |
| Engine·fixture·편집·Source 검토·MCP·기존 비노출 검사 | **337 PASS** |
| QA·Security·Single·Multi·Host·보고서·setup 복구 경로 | **149 PASS** |
| 마지막 인자 경계·짧은 비밀번호·인증 우선순위·engine 파라미터 숨김 확인 | **61 PASS** |

위 수치는 별도 실행 묶음이며 일부 기존 fixture가 겹치므로 하나의 고유 검사 수로 합산하지 않는다. 고의적인 미-await 시험과 SDK의 복제본 스트리밍 비활성화 등 기존 경고는 남았으며 실패는 없었다.

```bash
PYTHONPATH=src .venv/bin/python -m pytest \
  tests/test_test_write_precheck.py tests/test_host_fixture_usage.py \
  tests/test_generated_test_repair_feedback.py tests/test_runtime_test_data.py \
  tests/test_source_conflict_recovery.py tests/test_signup_quality_precheck.py \
  tests/test_signup_database_log_recovery.py tests/test_source_quality_repair_flow.py \
  tests/test_mcp_unit_runner.py tests/test_mcp_unit_report.py tests/test_mcp_files.py \
  tests/test_llm_runtime.py tests/test_signup_output_evidence.py -q -x

PYTHONPATH=src .venv/bin/python -m pytest \
  tests/test_qa_agent.py tests/test_security_agent.py tests/test_test_data_pipeline.py \
  tests/test_single_agent_pipeline.py tests/test_validation_artifacts.py \
  tests/test_loop_validation_feedback.py tests/test_per_test_setup_recovery.py \
  tests/test_owned_host.py -q -x

PYTHONPATH=src .venv/bin/python -m pytest \
  tests/test_test_write_precheck.py tests/test_host_fixture_usage.py \
  tests/test_signup_database_log_recovery.py tests/test_runtime_test_data.py -q -x
```

337개 묶음 이후 안내 문구와 수기 DB fixture의 engine 옵션 확인을 마무리했고, 마지막 묶음은 두 경계 시험도 추가해 61개가 통과했다. 첫 명령을 다시 실행하면 추가된 두 시험 때문에 수집 개수는 달라질 수 있다.

검증한 핵심 경계는 다음과 같다.

- Multi QA·Security 및 **실제 Single identity의 QA·Security 단계**에서 모두 잘못된 배치를 첫 부작용 전에 차단하고, 유효한 재작성만 실행한다.
- 교정 예산이 없거나 같은 오류를 반복하면 쓰기 없이 종료한다. 실패한 원본 코드·로그·비밀번호가 재작성 피드백에 복사되지 않는다.
- workspace 인증 오류는 시험 문법 오류보다 먼저 거절된다. 기존 인증정보 탐지와 권한·CAS 검사를 유지한다.
- 잘못된 부분 편집 재요청은 실행되지 않는다. 새 읽기 후 현재 경로·SHA에 묶인 전체 쓰기만 복구로 허용한다.
- 실제 DB 값·HTTP 응답 실패와 위조 helper 오류 문구는 FAIL로 남는다. 실제 helper 인자 오류만 같은 시험의 제한된 재수정으로 전달한다.
- 새 검사의 파일 해시를 바꾸면 Host 정책 해시도 달라진다. 독립 시험 기준이나 기존 성능 결과를 수정해 통과를 만들지 않았다.

## 다음 작업과 한계

수기 fixture와 Fake Provider·Docker를 사용하는 오프라인 검증이다. **실제 모델이 새 안내와 복구 절차를 따라 4조건을 완주하는지는 아직 재검증하지 않았다.** 105번의 8/9를 9/9로 바꾸거나 보류된 Run을 성공으로 고치지 않는다.

**다음 107번:** 보완한 같은 판본으로 4조건을 각 한 번 실제 실행하고 결과표를 다시 작성한다. 이번 작업에서 유료 재실행은 하지 않았다.

커밋 메시지 제안: `생성 시험 사전 검증과 편집 복구 및 DB 로그 비노출 보완`
