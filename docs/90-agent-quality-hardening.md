# 90. 전체 에이전트 작업 품질 및 검증 복구 보강

작성일: 2026-11-22. 범위는 Planner·Developer·QA·Security와 동일 역할을 순차 수행하는 Single 실행기다. 프론트엔드와 제품 기능, 독립 최종 시험지는 변경하지 않았다.

## 왜 보강했나

[89번 실제 실행](89-local-multi-ver1-smoke.md)에서 두 문제가 확인됐다.

- 생성 제품의 기본 요청 검증 오류 응답에 비밀번호가 노출됐다. 정상 회원가입 응답만 확인해서는 비노출 기준을 충족할 수 없었다.
- QA가 필수 브라우저 시험 파일을 만들지 않아 최종 판정·제품 수정 순환 전에 중단됐다. 파일 사전 검사는 89번에서 보완했고, 이번에는 역할별 작업 절차와 시험 수정 피드백까지 보강했다.

이번 작업은 모델을 새로 학습하거나 다른 모델로 바꾸는 작업이 아니다. 기존 에이전트의 지침, 입력 안내, 완료 전 검사, 실패 피드백을 함께 보완했다.

## 역할별 변경

| 대상 | 반영 내용 | 목적 |
| --- | --- | --- |
| Planner | 요구사항마다 정상·거부 흐름, DB 불변조건, 보안 출력 경로와 담당 검증을 계획하도록 공통 절차 추가 | 기획에서 예외 처리와 검증 작업이 빠지는 것을 줄인다 |
| Developer | DB commit·rollback·중복 경쟁, 프레임워크 검증 오류, DB 예외·로그 점검을 명시하고 제한된 Source 사전 검사 연결 | 정상 기능만 만든 뒤 완료하는 것을 줄인다 |
| QA | 단위·브라우저 파일과 선언 ID 준비, 응답과 실제 DB 변화 확인, 시험 준비 오류와 제품 실패의 구분을 명시 | 시험 미실행을 제품 성공이나 결함으로 오판하지 않게 한다 |
| Security | 정상·거부·잘못된 요청·DB 실패의 응답과 로그를 검사하도록 절차 추가 | 성공 응답만 가리거나 스캐너 경고가 없다는 이유로 통과하지 않게 한다 |
| Single | 네 역할의 절차와 같은 Developer 사전 검사·시험 수정 피드백을 공유 | Single과 Multi에 서로 다른 보완 기준을 적용하지 않는다 |
| Host | 새 검사·시험 복구 코드와 공유 작업 지침을 실행 정책 해시에 포함 | 보강 전후 실행을 같은 구현 조건으로 혼합하지 않는다 |

`roles/work_rules.py`에서 절차를 공유한다. 역할 계약 버전과 Single 프롬프트 버전은 5로 올렸으며, Artifact JSON Schema 버전과 요구사항은 바꾸지 않았다. Single은 기존 `SINGLE_AGENT` 정체성과 Context를 유지한다.

## 공개 요구사항 안내

`signup_observation_plan()`을 SCN-001의 공개 REQ-001~008 전체로 확장했다. 승인된 Acceptance Criteria 원문을 보존하며, Planner·Developer에는 구현 안내를, QA·Security에는 각자 할당된 요구사항의 관찰 항목만 제공한다.

이 안내는 새 요구사항이나 시험 결과가 아니다. 보호시험 입력, 독립 최종 시험 코드, 정답 구현은 포함하지 않는다. 다른 시나리오를 회원가입 기준으로 바꾸지도 않는다.

## Developer 완료 전 보안 점검

`output_safety_precheck.py`는 실제 실패에서 확인한 단순한 FastAPI 구조를 읽기 전용 AST로 검사한다. 동결 계약의 가입 경로에서 일반 문자열 비밀번호를 받는 Pydantic 본문 모델을 사용하고, 별도 오류 처리 흔적이 없으면 `DEFAULT_VALIDATION_MAY_ECHO_CREDENTIALS`를 전달한다.

- Source를 import하거나 Host에서 실행하지 않는다. 파일 크기와 AST 노드 수를 제한한다.
- 사용자 정의 처리기·미들웨어·앱 팩토리 등 해석이 불확실한 구현은 기존 Build·Security 검사에 맡긴다. 경고가 없다는 사실은 PASS가 아니다.
- 출력 교정이 활성화된 실행에만 연결하며, Single·Multi가 같은 교정 횟수·모델 호출·Tool 예산을 사용한다. 단계 내 같은 위험에 대한 교정 기회는 한 번이며, 문구만 바꿔 다시 받지 못한다.
- 안내 후에도 위험 구조가 남거나 수정 예산이 없으면 READY를 그대로 승인하지 않는다. 중단·복구 기록에는 새 코드가 저장된다. 정적 경고만으로 제품의 최종 FAIL을 만들지 않고, 기존 증거·검토 규칙을 유지한다.
- 실제 보안 판정은 이후의 격리된 실행과 측정 근거가 담당한다.

## QA·Security 시험 수정 피드백

기존 한 번의 시험 수정 경로에 다음 사실을 추가했다.

| 항목 | 전달 범위 |
| --- | --- |
| 위치 | 실제 Tool·시험 범위·테스트 ID |
| 상태 | PASS·FAIL·UNVERIFIED와 정해진 미실행 사유 코드 |
| 출처 | 이미 저장된 시험 보고서 참조 |
| 관찰 | 허용된 출력 경로 상태, DB UNIQUE·해시 정책의 불리언·숫자 측정값 |

최대 32개 사례만 전달한다. 보호시험 범위, 예외 전문, 응답 본문, 비밀번호·저장 해시는 이 경로로 전달하지 않는다. 개별 시험 PASS와 요구사항 PASS가 같지 않다는 점도 명시했다.

시험 수정 중에는 제품 Snapshot·요구사항·선언된 사례를 고정한다. ID·제목·기대 결과를 바꾸거나 케이스를 삭제하면 기존 `same_cases()` 검사가 거부한다. 제품 결함은 기존 Developer 수정·재검증 경로에서 처리한다.

## 개발정의서와 비교 조건 점검

- Orchestrator가 호출 순서·Snapshot·Issue·최종 판정을 관리하는 책임 경계를 유지했다.
- 인증, MCP 역할 권한, 승인 설정, Docker 격리, Source 불변성과 수정 이력을 완화하지 않았다.
- Ver1의 공통 보호시험 제공과 Ver2의 미제공 구분을 유지했다. 공통 보완은 양쪽에 같은 방식으로 적용한다.
- 확인되지 않은 실행을 성공으로 바꾸지 않는다. 기존 성공 판정·보호시험·수정 횟수 한도를 낮추지 않았다.
- 프론트엔드와 기존 사용자 작업은 수정하지 않았고 Git 커밋·푸시는 하지 않았다.

## 검증

새 사전 검사와 Single·Multi 교정 경로, 테스트 피드백 필터, 요구사항 범위, Host 정책 해시를 오프라인 검사했다. 실제 API나 모델 비용을 발생시키는 호출은 하지 않았다.

검사 중 로컬 가상환경에 SQLAlchemy·Argon2 개발 의존성이 없어 보호시험 관찰값 검사 3개가 미실행되는 문제도 확인했다. 프로젝트 개발 의존성 범위 안에서 설치한 뒤 관련 묶음을 다시 검사했다. 실험용 Docker 이미지·Lock·기존 측정 결과는 변경하지 않았다.

| 검사 묶음 | 결과 |
| --- | --- |
| 새 Source 사전 검사·Single/Multi 교정, 작업 지침, 시험 피드백 필터, Host 설정·정책 해시 | 120개 통과 |
| 보호시험 관찰값, 출력 교정, 라이브러리 오류 교정, 복합 Draft 사전 검사 | 의존성 보완 후 71개 통과 |
| Multi·Single 제품 수정 순환, 보호시험 충돌 처리, 생성 시험 재검증, 측정 근거 기반 중단 | 47개 통과 |
| 요구사항 범위, Single 프롬프트·실행 순서, Developer 실행기 | 80개 통과 |
| 변경 파일 공백·패치 형식 | `git diff --check` 통과 |

주요 재현 명령은 다음과 같다. `-B`는 바이트코드 생성을 막으며, 테스트는 Fake Provider와 격리된 fixture를 사용한다.

```bash
PYTHONPATH=src:tests .venv/bin/python -B -m pytest -q \
  tests/test_agent_work_rules.py tests/test_agent_output_safety_precheck.py \
  tests/test_output_safety_correction.py tests/test_generated_test_repair_feedback.py \
  tests/test_owned_host.py

PYTHONPATH=src:tests .venv/bin/python -B -m pytest -q \
  tests/test_loop_validation_feedback.py tests/test_llm_output_corrections.py \
  tests/test_ready_library_correction.py tests/test_combined_draft_checks.py

PYTHONPATH=src:tests .venv/bin/python -B -m pytest -q \
  tests/test_owned_agent_fix_pipeline.py tests/test_single_agent_fix_pipeline.py \
  tests/test_protected_supersession.py tests/test_generated_recheck.py \
  tests/test_measured_repair_stop.py
```

검사에는 단일 프로세스 fixture의 event stream 미설정 경고가 있었다. 다중 replica 스트리밍 검증이나 실제 Docker·OpenAI 재실행을 완료했다는 의미는 아니다. 저장소 전체 검사를 수행했다고 주장하지 않는다.

## 남은 확인과 다음 작업

이번 보강 뒤의 실제 API 성공률·시간·비용은 아직 측정하지 않았다. 89번 실제 결과인 6/7 요구사항 충족, 독립 시험 8/9 통과, QA 단계 중단은 그대로 보존한다. 오프라인 회귀 검사 통과를 완제품 성공률 개선으로 표시하지 않는다.

**다음 91번:** 보강된 Multi Ver1을 실제 API로 한 번 실행하여 QA 보고서 → 제품 수정 → 재검증 → 최종 판정 흐름을 확인한다. 이후 같은 보강 버전·모델·입력·시작 코드·예산으로 네 조건의 반복 비교를 진행한다.

커밋 메시지 제안: `전체 에이전트 작업 기준 및 보안 사전 검사·검증 복구 보강`
