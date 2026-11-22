# 104. 시험 보조 기능 계약과 시험 오류 수정 경로 보완

작성일: 2026-11-23. 103번에서 확인한 시험 작성 오류를 보완했다. **실제 OpenAI 호출·새 성능 실험·Docker 실행은 0회**이며, 아래 검사는 직접 작성한 안전한 fixture와 가짜 Provider/Docker를 사용한다.

## 무엇을 고쳤는가

| 구간 | 변경 | 목적 |
| --- | --- | --- |
| Agent 입력 | `testTargets.appFixture.methods`에 실제 인자와 반환 구조 제공 | 없는 기능을 있다고 가정하거나 반환값을 잘못 읽는 실수 예방 |
| 시험 작성 안내 | 로컬 이메일 생성 예제, 모듈 함수 `new_password`, 딕셔너리 키 접근 안내 | `self.fresh_email`, `self.new_password`, 잘못된 튜플 분해 방지 |
| READY 전 검사 | Python AST로 확인 가능한 오용을 검사 | 제품을 다시 수정하기 전에 시험 작성 Agent가 고치도록 피드백 |
| 실제 시험 실행 | 실패 프레임·Host 메서드·실제 지역값을 대조해 제한적으로 오류 분류 | 제품 결함과 시험 보조 기능 사용 오류 분리 |
| 같은 단계의 시험 수정 | 새 사유 코드를 기존 `TestFixRequired` 경로에 연결 | 같은 제품을 대상으로 QA·Security가 자기 시험만 한 번 수정 |

공개 API의 모듈·클래스·메서드 계약은 stdlib runner의 `app_fixture_contract()`에서 가져온다. 앱이나 생성 시험을 Host에서 import·실행해서 계약을 추측하지 않는다. 두 역할과 Single·Multi가 같은 계약 및 수정 경로를 사용한다.

## Agent에 제공하는 사용법

- `fresh_email`은 직접 만드는 로컬 변수다. Host 인스턴스의 기능이 아니다.
- 비밀번호는 `_host_test_data.new_password(...)` 모듈 함수로 실행 중 생성한다.
- `signup_email_state(...)`는 딕셔너리다. `state['totalRows']`, `state['exactEmailRows']`, `state['hasDirectEmailUniqueKey']`로 읽는다.
- `concurrent_signup_statuses(...)`는 정수 HTTP 상태 코드 두 개다. 개별 정수를 `assertIn`의 목록 자리에 넣으면 안 된다.
- 응답 코드는 공개 규격의 성공·거부 범위를 따른다. 공통 규격이 2xx·4xx일 때 임의로 200·400만 허용하지 않는다.
- 동시 생성 시험에는 아직 가입하지 않은 이메일을 사용한다. 해시 측정 함수 호출 자체를 정책 통과로 간주하지 않는다.

보호시험의 입력·정답·최종 판정 기준은 추가로 공개하지 않았다.

## 사전 검사와 실행 결과의 차이

사전 검사는 생성 파일을 **파싱만** 한다. 실제 Host 클래스를 직접 상속하거나 확인 가능한 로컬 상속을 사용할 때 다음을 확인한다.

| 코드 | 확인 범위 |
| --- | --- |
| `TEST_HOST_MEMBER_MISSING` | 선언되지 않은 `self.fresh_email`, `self.new_password` 사용 |
| `TEST_HOST_RETURN_MISUSED` | Host 딕셔너리 분해 직후 숫자로 비교하거나, 동시 요청의 개별 정수를 목록처럼 검사 |

직접 정의한 helper·속성은 유지한다. 알 수 없는 외부 상속, 동적 속성 설정, 데코레이터, 이름 재바인딩 등 확신할 수 없는 구문은 추측으로 차단하지 않는다. 이 검사는 기존 출력 교정 예산을 사용하며 실제 PASS 판정을 만들지 않는다.

실행 시에는 단순한 예외 문구나 `[origin=TEST]` 표식만으로 분류하지 않는다.

| 실행 사유 | 추가 확인 |
| --- | --- |
| `NOT_RUN_FIXTURE_MEMBER_MISSING` | 실제 실패가 시험 메서드에서 발생했고, AttributeError의 대상이 현재 시험 인스턴스이며 해당 Host 기능이 없음 |
| `NOT_RUN_FIXTURE_RETURN_MISUSED` | 실제 Host 메서드가 유지되고, 바로 다음 숫자 assertion에서 딕셔너리의 키 문자열 세 개를 값으로 사용한 것이 확인됨 |

이 두 분류는 CLI의 **생성 시험 `QA_TESTS`에서만 활성화**한다. 보호시험과 제품 Snapshot 자체 시험의 기존 실패를 이 경로로 바꾸지 않는다. 사용자 정의 반환형, 실제 DB 값 불일치, HTTP 500, 불확실한 오류는 원래 FAIL로 남는다. 시험이 스스로 새 사유로 `skipTest()`를 호출해도 Host가 검증한 오류로 인정하지 않는다.

## 수정 횟수와 근거 보존

검증된 사용법 오류는 PASS가 아니라 `SKIP → UNVERIFIED`로 남기고, 해당 생성 시험의 사유 코드·시험 ID·기존 보고서 참조만 작성 Agent에게 전달한다. 원본 예외 값이나 보호시험 내용은 전달하지 않는다.

기존 같은 단계의 수정 기회 **한 번**과 공유 Run 예산을 그대로 쓴다. 시험 ID·요구사항 ID·제목·선언된 기대 결과는 `same_cases()`로 유지하며, QA·Security의 제품 Source 수정 권한도 추가하지 않았다. 수정 후 다시 실행해도 문제가 남으면 미확인 결과로 남고 무한 재시도나 자동 성공 처리를 하지 않는다. 실제 HTTP·DB 결함 및 이미 기록된 다른 실패를 지우는 처리도 없다.

## 점검

최종 코드 고정 후 **관련 검사 170개 + Host 경로 검사 2개 통과**를 확인했다.

```bash
PYTHONPATH=src .venv/bin/python -m pytest \
  tests/test_host_fixture_usage.py tests/test_generated_test_repair_feedback.py \
  tests/test_runtime_test_data.py tests/test_signup_output_evidence.py \
  tests/test_mcp_unit_runner.py tests/test_mcp_unit_report.py \
  tests/test_owned_host.py tests/test_test_data_pipeline.py -q -x

PYTHONPATH=src .venv/bin/python -m pytest \
  tests/test_qa_agent.py::QAAgentTests::test_verified_fixture_misuse_gets_one_repair_then_stays_unverified \
  tests/test_structural_recovery.py::BaselinePipelineTests::test_conflict_reread_rejection_then_minimal_fix_revalidates_and_saves -q -x
```

확인한 내용은 실제 fixture 시그니처와 안내의 일치, 선언된 사용자 helper 보존, 오용 사전 검사, 실행 프레임에 근거한 분류, 진짜 제품 실패 보존, 보호·Snapshot 시험 제외, 위조 skip 사유 차단이다. Fake Provider/Docker를 사용하는 Host 검사에서는 수정 기회가 한 번으로 제한되고, 오류가 계속되면 동일 시험·Source 근거를 가진 UNVERIFIED 결과가 남는 것도 확인했다. Single·Multi의 기존 공통 데이터·수정 경로 검사도 통과했다.

검사 이력도 구분한다. 초기 확장 검사에서는 265개 통과·2개 실패가 있었다. 한 건은 하위 Python 프로세스에 `PYTHONPATH=src`가 전달되지 않은 실행 환경 문제였다. 다른 한 건은 파이프라인이 첫 실행에서 HUMAN_REVIEW로 종료된 경우이며, 세부 원인은 확정하지 않았다. 코드 수정을 멈춘 뒤 해당 검사를 두 차례 독립 실행해 모두 통과했다. 초기 전체 묶음까지 한 번에 전부 통과했다고 기록하지 않는다.

기존 실험의 생성 시험 파일 4개는 AST로만 읽었다. 네 파일의 알려진 없는 helper 사용과 QA 두 파일의 숫자 반환값 오독이 사전 검사에 잡혔다. 발견한 Source 위치 수를 새 실행 실패 건수나 제품 성능 수치로 집계하지 않았다.

개발정의서 대조: Orchestrator의 판정 책임, QA·Security의 고정 Snapshot 검증, MCP 경로 권한, 같은 시험의 제한된 재수정, 불변 실행 근거, Single·Multi 공통 조건을 유지했다. 모델·예산·승인·인증·보호시험의 판정 기준을 완화하지 않았다.

## 다음 작업

Host fixture 정책과 Draft 사전 검사 정책은 각각 V6로 갱신된다. 파일 해시 및 정책 지문도 달라지므로 101·102번 결과는 이전 판본의 기록으로 보존한다.

**105번: 새 판본의 실행 조건을 동결하고 4조건을 각 1회 실제 실행해 표를 작성한다.** 이번 104번에서는 실행하지 않았다. 추가 반복 횟수는 그 결과와 토큰 예산을 보고 정한다. 과거 판본의 Ver1과 새 판본의 Ver2를 동일 조건으로 섞어 집계하지 않는다.

커밋 메시지 제안: `시험 보조 기능 계약 명시 및 생성 시험 오류 복구 보완`
