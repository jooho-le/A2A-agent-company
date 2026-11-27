# 97. 입력 검증 오류의 정확한 수정 대상 전달

작성일: 2026-11-23. 96번 실제 실행에서 Developer가 일반 HTTP 오류 처리기를 반복 수정하던 원인을 보완했다. 이번에는 OpenAI API·신규 Run을 실행하지 않았다.

## 수정 근거

96번 종료 Source에는 `HTTPException` 처리기가 있고 `RequestValidationError` 처리기는 없었다. 설치된 FastAPI 소스를 확인하면 두 예외의 상속 경로가 다르며 기본 요청 검증 오류 처리기는 `exc.errors()`를 JSON 응답으로 보낸다. 입력값이 포함될 수 있는 경로를 일반 HTTP 처리기로 보호했다고 판단하면 안 된다.

새 회귀 검사를 먼저 기존 코드에서 실행해 수정 대상 정보 누락과 다른 앱의 처리기를 잘못 인정하는 실패 3개를 재현한 뒤 구현을 변경했다.

## 변경 내용

| 영역 | 변경 |
| --- | --- |
| Source 검토 | 승인된 앱의 이름, 일반 HTTP 처리기 발견 여부·줄 번호, 요청 검증 처리기 발견 여부를 제한된 관찰 정보로 만든다. Source 원문·입력값·예외 본문은 전달하지 않는다. |
| 수정 메시지 | `fastapi.exceptions.RequestValidationError`, import 예시, 정확한 앱의 등록 예시, 수정 파일과 기존 HTTP 처리기 보존 여부를 `sourceRepairTargets`로 전달한다. |
| 잘못된 보호 판단 | 다른 앱의 처리기·미들웨어·생성자 설정으로 현재 가입 앱의 경고를 지우지 않는다. 실제 앱에 HTTPException 전용 생성자 매핑만 있는 경우도 요청 검증 보호로 인정하지 않는다. |
| 역할 지침 | Developer와 Security에 HTTP 오류와 요청 검증 오류의 차이를 명시한다. Single은 동일한 공통 역할 지침과 수정 메시지를 사용한다. |
| 버전·비교 조건 | 역할 계약·Single Prompt는 7, Source 검토는 V3, 교정 정책은 V4다. Host의 구현 지문에 반영되며 예전 실행과 같은 구현으로 섞지 않는다. |

예를 들어 96번 Source에서 관찰한 수정 대상은 다음과 같다. 이 값들은 공개 프레임워크 API와 승인된 앱·Source 좌표이며 정답 구현이나 숨겨진 시험 입력이 아니다.

```yaml
toolPath: source/main.py
applicationAttribute: app
exceptionType: fastapi.exceptions.RequestValidationError
importExample: from fastapi.exceptions import RequestValidationError
registrationExample: '@app.exception_handler(RequestValidationError)'
preserveExistingHttpExceptionHandler: true
```

기존 읽기 → 실제 SHA-256에 맞춘 쓰기·편집 → Source 재검토 규칙을 그대로 사용한다. 프레임워크 지시는 요청 검증 이전의 오류 경로를 구분하고, 기존 HTTP 거부 동작·상태·응답 규격을 보존하도록 한다. 원시 오류 입력·본문·예외 문자열을 응답이나 로그로 내보내지 않게 안내한다.

다른 이름의 앱과 예외 import 별칭도 검사한다. 사용자 정의 매핑·동적 설치처럼 정적으로 판단하기 어려운 구성은 실제 실행 검증에 맡기며, 정적 경고 없음은 PASS로 취급하지 않는다.

## 검증

| 검사 | 결과 |
| --- | --- |
| 구현 전 새 회귀 검사 | 3개 실패·3개 통과로 누락 재현 |
| 초기 대상 전달·기존 Source·역할 지침 검사 | 46개 통과 |
| Single/Multi 수정 경로·역할 계약·Prompt·보안 교정 | 초기 131개, 추가 범위 보완 후 최종 136개 통과 |
| Host 정책·시작 검사까지 포함한 관련 묶음 | 170개 통과 |
| 최종 Docker 재현 검사 | 3개 통과 |

검사 묶음은 겹치므로 합산하지 않는다. SDK fixture의 event_stream 미설정 경고 55개가 있었으며 다중 replica 스트리밍 검증을 했다는 뜻은 아니다.

새 엔진 회귀 검사는 Multi Developer, Single IMPLEMENTING, Single FIXING에서 모델에 전달된 `sourceRepairTargets`를 읽어 올바른 예외 종류·등록 대상·보존 대상을 확인한다. 정해진 모델 응답으로 실제 파일을 읽고 해시를 확인해 수정하며, 모델 호출 4회·Tool 2회·교정 1회의 고정 한도 안에서 재검토를 통과한다.

Docker 검사도 일반 HTTP 처리기만 있는 합성 서비스에서 비노출 시험 하나가 실제 FAIL임을 확인한다. 이후 전달된 수정 정보를 사용해 기존 HTTP 처리기 바이트를 보존하고, 요청 검증 처리기를 추가한 파일로 동일 최종 9개 시험이 모두 PASS임을 Single·Multi 양쪽에서 확인했다. 모델 응답은 재현용이다. 실제 OpenAI가 자율적으로 수정했다는 성능 결과가 아니다.

## 96번 실제 Source 복사본의 별도 진단

실제 실패 Source를 `.data/verification/97-validation-error-20261123/`에 복사해 같은 수정 정보로 요청 검증 처리기만 추가하고 Docker에서 검사했다. 원본 Source의 바이트 해시는 보존됐다. 이 진단은 신규 Run·등록 Snapshot·공식 성능 표본이 아니다.

| 관찰 | 복사 전 상태 | 처리기만 추가한 복사본 |
| --- | --- | --- |
| 입력 검증 기본 오류의 정적 경고 | 있음 | 없음 |
| 최종 시험 진단 PASS / FAIL / 판정 불가(SKIP) | 0 / 5 / 4 | 0 / 4 / 5 |
| 앱·DB 시작 규격 | DB 준비 누락이 원본에 존재 | `SCN001_USER_TABLE_REQUIRED`로 실패 |

원본 `main.py`·`database.py`·`models.py`에는 테이블 생성 호출이나 앱 startup/lifespan 초기화가 없었다. 실제 시작 probe도 사용자 테이블이 없다고 확인했다. 가입 성공을 전제로 하는 다른 검사도 판정할 수 없었다.

따라서 실제 Source에서 비노출 시험이 PASS로 확정됐다거나 완제품이 통과했다고 기록하지 않는다. DB 초기화 누락은 96번 원본에도 있었고 이번 진단에서 추가 확인됐다. 이번 변경만으로 전체 문제가 해결됐다고 주장하지 않는다.

## 개발정의서 점검

- Orchestrator의 판정 책임과 Agent의 구현 책임을 유지한다. Host가 생성 제품을 자동 정답 코드로 바꾸는 기능은 추가하지 않았다.
- A2A·Artifact 규격, 승인 요구사항, 해시·격리·역할 권한, 보호시험·최종 시험 기준은 변경하지 않았다.
- Single과 Multi에 같은 지침·교정 정보를 적용하고 Ver1·Ver2의 보호시험 제공 차이를 유지했다.
- 호출·토큰·시간·Tool·출력 교정·제품 수정 한도를 늘리지 않았다. 비밀값·원시 예외·보호시험 입력은 모델의 새 수정 정보에 포함하지 않는다.
- `.env`, 사용자 로컬 설정, 96번 Run·Source·측정값과 기존 성능 표본은 변경하지 않았다. Git 커밋·푸시는 하지 않았다.

주요 재현 명령:

```bash
PYTHONPATH=src:tests .venv/bin/python -B -m pytest -q \
  tests/test_validation_error_repair_guidance.py tests/test_signup_quality_precheck.py \
  tests/test_source_quality_repair_flow.py tests/test_agent_work_rules.py \
  tests/test_agent_role_contracts.py tests/test_single_agent_prompts.py \
  tests/test_output_safety_correction.py

A2A_DOCKER_REPAIR_TEST=1 PYTHONPATH=src:tests .venv/bin/python -B -m pytest -q \
  tests/test_source_quality_docker.py
```

**다음 98번:** Source 재작성 때 앱·DB 초기화가 누락되는 경로를 보완하고, 등록 후보가 실제 시작 규격을 통과한 뒤 QA·Security로 진행하도록 재현 검사한다. 실제 API 재실행은 이번 작업에 포함하지 않는다.

커밋 메시지 제안: `입력 검증 오류의 정확한 수정 대상과 예외 처리 지침 보완`
