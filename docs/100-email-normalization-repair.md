# 100. 이메일 정규화의 실제 함수·검사 순서 연결

작성일: 2026-11-23. 99번에서 남은 공백 이메일 실패를 대상으로 코드와 회귀 검사를 보완했다. **OpenAI 호출·신규 Run 제출은 0회**다.

## 문제와 원인

99번 최종 제품은 8/9 시험을 통과했다. 남은 실패는 `SEC-SIGNUP-003`이었다. 정상 이메일의 앞뒤 공백을 제거하기 전에 `email_validator.validate_email(...)`를 호출해 422로 거부했다. 첫 제품 수정에서는 DB 예외 분기만 바뀌고, 실제 정규화 함수는 그대로였다.

기존 Source 검사는 직접 import한 `validate_email(payload.email)` 형태를 확인했지만, 실제 코드의 **모듈 이름을 통한 호출 + 보조 함수의 인자 전달**을 연결하지 못했다. 실패 피드백에도 이 경로의 함수 좌표를 우선 읽을 정보로 제공하지 못했다. 같은 파일을 고쳤다는 사실만으로 정규화 경로가 바뀌었다고 판단하면 안 된다.

## 변경 내용

| 영역 | 변경 |
| --- | --- |
| Source 입력 흐름 | 모듈을 import하지 않고 AST로 라이브러리의 직접·모듈 호출, import 별칭, 위치·키워드 인자, 보조 함수 인자 전달을 추적한다. |
| 검사 순서 | 파서에 전달되는 같은 이메일 값이 호출 전에 `strip()`됐는지 확인한다. 검사 후 공백 제거, 다른 변수의 공백 제거로 경고를 지우지 않는다. |
| 구체적인 수정 대상 | Source 교정의 `sourceRepairTargets`에 실제 `sourceRef`, Tool 파일 경로, 줄 번호, 공개 파서 이름, 승인된 처리 순서를 전달한다. |
| 실제 실패 피드백 | 선택된 현재 Source의 canonical 계정 생성이 4xx로 거부된 관찰을 `emailNormalizationRepair`로 묶는다. 정적으로 찾은 파서 함수와 바이트 해시를 붙이고 첫 파일 읽기 목록에 연결한다. |
| 잘못된 진전 방지 | 이메일 경고의 `reviewRevision`을 해당 함수 AST로 계산한다. 다른 함수의 DB 예외 처리나 주석만 바꾸면 그 경고의 진전으로 취급하지 않는다. 기존 무변경 수정 차단은 유지한다. |
| Single·Multi | 공통 Source 검사·입력 투영·교정 메시지를 사용한다. Single IMPLEMENTING·FIXING과 Multi Developer에 같은 규칙이 적용된다. |

예를 들어 99번 Source에서 전달하는 수정 정보는 다음과 같다.

```yaml
toolPath: source/main.py
sourceRef: source/main.py::normalize_email
parser: email_validator.validate_email
requiredOrder:
  - TRIM_WHITESPACE
  - CASE_NORMALIZATION
  - FORMAT_VALIDATION
  - LOOKUP_AND_INSERT
```

이는 공개 `emailPolicy`와 Source 관찰에 따른 안내다. 숨겨진 시험 입력이나 전체 정답 서비스를 모델에 주는 방식은 아니다. 대소문자·dot/plus 처리와 DB 유일성 정책을 임의로 변경하지 않는다.

## 실제 실패와 정적 후보를 구분하는 규칙

- `emailNormalizationRepair`는 Host가 선택한 실패만 사용한다. Run·Source Artifact·codeVersion·Snapshot·실행 Manifest와 승인된 시험 버전을 대조한다.
- Ver1에서는 승인된 practice 보호시험만, Ver2에서는 해당 Source의 관찰된 생성 시험을 사용할 수 있다. `final:` 시험이나 다른 보호시험 판본의 관찰은 수정 입력으로 채택하지 않는다.
- 다른 요구사항·제외된 Issue·다른 Source·미측정 보고서·이미 통과한 canonical 관찰로 새 수정 대상을 만들지 않는다.
- Source 좌표는 `STATIC_CANDIDATE_ON_SELECTED_SNAPSHOT`이다. 실행된 예외 프레임이나 확정된 인과 관계라고 표시하지 않는다.
- 입력값·이메일 원문·비밀번호·Source 본문·원시 예외를 새 교정 정보에 복사하지 않는다. 파일 경로·식별자·줄 번호·관찰된 HTTP 상태·참조 해시만 제한적으로 사용한다.
- 기존의 다른 긴급 실패에 대한 파일 읽기 순서는 보존한다. 정규화 위치를 추가하되 다른 실패 피드백을 삭제하지 않는다.

## 과잉 차단 방지와 범위

알려진 전처리가 파서 호출 전에 있으면 경고하지 않는다. 호출자가 정리한 이메일을 보조 함수에 전달하는 형태도 구분한다. 다른 이름으로 가려진 라이브러리 호출을 공식 API라고 단정하지 않는다.

이메일을 처리할 수 있는 Pydantic validator·공백 정리 설정이나 동적·분기 전처리는 불확실한 경로로 남겨 실제 실행 검증에 맡긴다. 비밀번호 전용 validator나 `extra="forbid"` 같은 무관한 설정으로 이메일 검사를 생략하지 않는다.

기존 Source 크기·파일·AST 한도를 유지하고 함수 256개·인자 전달 분석 8회·출력 좌표 64개로 범위를 제한했다. 모든 Python 구현을 증명하는 정적 분석기는 아니다. 경고가 없다는 사실을 제품 PASS로 바꾸지 않으며, Build·QA·Security와 독립 최종 시험이 판정 책임을 유지한다.

## 검증 결과

| 검사 | 결과 |
| --- | --- |
| 구현 전 새 회귀 검사 | 7개 실패 / 9개 통과로 누락 재현 |
| 최종 Source·피드백·교정·Single/Multi Prompt·97·98번 관련 검사 묶음 | **219개 통과** |
| Host·Developer 수정·Single/Multi 빌드 실패 후 흐름 검사 | **61개 통과** |
| Multi·Single IMPLEMENTING·Single FIXING의 재현 응답 검사 | 실제 파일 읽기·SHA 확인·정규화 함수 수정·재검토 통과 |

재현 응답 검사는 각 모델 4회·Tool 2회·출력 교정 1회의 고정 한도 안에서 끝났다. 정해진 모델 응답을 사용했으며 실제 OpenAI의 자율 수정 성공률이 아니다. 검사 묶음은 겹치므로 합산하지 않는다. Host 묶음의 SDK `event_stream` 미설정 경고 79개는 다중 replica 스트리밍 검증을 했다는 뜻이 아니다.

주요 명령:

```bash
PYTHONPATH=src:tests .venv/bin/python -B -m pytest -q \
  tests/test_email_normalization_repair.py tests/test_email_normalization_feedback.py \
  tests/test_source_quality_repair_flow.py tests/test_validation_error_repair_guidance.py \
  tests/test_signup_quality_precheck.py tests/test_developer_input_projection.py \
  tests/test_invalid_input_repair_feedback.py
```

## 99번 실제 실패 Source의 별도 Docker 진단

99번 v2 불변 Snapshot을 별도 폴더로 복사해 새 안내가 지목한 `normalize_email()`의 형식 검사 앞에 공백 제거·소문자화를 추가했다. 다른 구현·시험 기준은 유지하고, 앱 실행과 시험은 기존 격리 Docker 이미지에서만 수행했다.

| 항목 | 진단 복구 전 | 진단 복구 후 |
| --- | --- | --- |
| 실제 앱·DB 시작 검사 | PASS | PASS |
| 동일 최종 시험 | **8 PASS / 1 FAIL / 0 SKIP** | **9 PASS / 0 FAIL / 0 SKIP** |
| SEC-SIGNUP-003 | FAIL | PASS |
| 유료 API 호출 | 0 | 0 |

원본 Snapshot의 SHA-256은 보존됐다. 99번 Run의 FAIL·측정값·등록 Source는 그대로다. 스크립트·안내·복사본·결과는 Git 제외 경로 `.data/verification/100-email-normalization-20261123/`에 남겼다.

**이 9/9는 사람이 수정한 진단 복사본의 결과다.** 신규 Run이나 에이전트가 혼자 고친 성공 표본으로 집계하지 않는다. 이 진단으로 확인한 것은 실제 실패 위치와 필요한 처리 순서이며, 보완된 안내를 실제 모델이 따르는지는 다음 API 실행에서 확인해야 한다.

## 개발정의서 점검

- Orchestrator의 판정·실행 제어와 Agent의 구현 책임을 유지했다. Host가 실제 제품을 자동으로 고쳐 주지 않는다.
- A2A·Artifact JSON Schema, 승인 요구사항·보호시험·독립 최종 시험, 인증·권한·Sandbox·해시 기반 쓰기를 완화하지 않았다.
- 모델·Tool·토큰·시간·출력 교정·제품 수정 한도를 늘리지 않았다.
- 역할 계약·Single Prompt는 9, Source 검토는 V4, 교정 정책은 V5, Developer 입력 투영은 V20이다. 정규화 정책 V1·구현 해시도 Host 실행 정책에 포함해 이전 판본과 구분한다.
- 사용자 설정·`.env`·기존 실험·99번 원본·비밀 파일은 변경하지 않았다. 프로젝트 Git 커밋·푸시는 하지 않았다.

**다음 101번:** 보완된 코드로 Multi Ver1을 실제 API로 단회 실행해 정규화 경로가 실제로 수정되고 최종 9개를 통과하는지 확인한다. 이번 100번에는 포함하지 않았다. 성공이 확인되면 Single 단회 확인과 원래의 조건별 성능 비교로 진행한다.

커밋 메시지 제안: `이메일 정규화 함수 추적과 실제 실패 기반 수정 안내 보완`
