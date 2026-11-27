# 113. QA 테스트용 인증정보 작성·교정 보완

작성일: 2026-11-26. 실제 OpenAI 작업, Docker 시험, 서버 재시작은 수행하지 않았다.

## 확인한 실패

Run `98ead276-fc49-42f5-823a-437a007adcde`의 요청은 `회원가입 기능 만들어줘`였다.

QA가 `write_test_file`을 호출할 때 **41번째 줄에서 `CREDENTIAL_ASSIGNMENT`**가 감지됐다. 기존의 작성 전 교정 1회를 사용했지만 다음 제안의 **50번째 줄에서 같은 규칙이 다시 감지**되어 `LLM_TOOL_POLICY_VIOLATION`으로 중단됐다.

저장된 QA 시험 파일과 단위·브라우저 시험 실행 영수증은 모두 0개다. 이 실행의 최종 결과는 제품 불합격이 아니라 `UNVERIFIED`였다. 차단된 원문은 보관되지 않아 실제로 어떤 문자열을 썼는지는 확정할 수 없다.

코드 확인에서는 일반 서비스 경로의 안내 불일치도 발견했다. 도구는 `testTargets.testData`를 안내하지만 일반 서비스 입력은 `testDataFactory`만 제공하고 있었다. 기존 교정은 검출 줄 번호와 공통 안내를 전달했으나, 어떤 작성 위치를 어떤 런타임 생성 방식으로 바꿀지는 구조화하지 않았다.

## 수정 내용

### 같은 생성기 계약을 같은 이름으로 전달

- 일반 서비스에도 `testData`를 제공하고, 기존 `testDataFactory`는 호환용으로 유지한다.
- 두 필드는 같은 Host 생성기 `_host_test_data.new_password`의 API를 설명한다. 생성기의 실행 방식과 인수 범위는 유지한다.
- QA·Security의 실제 작업 지침과 `write_test_file`의 `content` 설명에 같은 작성 규칙을 넣는다. Single의 QA·Security 단계에도 같은 지침을 사용한다.
- Python 단위 시험이 설정된 경우에만 Python 생성기 지침을 작업 입력에 추가한다. 브라우저 JSON에 Python 코드를 넣도록 안내하지 않는다.

### 실패용 입력도 런타임 생성

```python
from _host_test_data import new_password

empty_sample = new_password(length=0)
short_sample = new_password(length=7)
sample = new_password(length=8)
payload = {}
payload["password"] = short_sample
```

위 길이는 작성 예시이며 새로운 합격 기준이 아니다. 실제 요청의 수용 기준에 따라 길이·문자 종류를 선택한다. 잘못된 타입이나 null을 검사하는 경우에는 그 입력 의도를 유지한다.

생성기는 현재 시험 본문에서 사용한다. 기본 인수·보조 함수의 반환값·딕셔너리 값 등에 다른 고정 인증정보를 남기는 방식은 안내하지 않는다. Host 생성기 파일을 프로젝트에서 읽거나 새로 작성할 필요도 없다.

Source를 먼저 읽고 시험 파일 쓰기를 한 응답에 하나씩 요청하도록 안내했다. 그래야 거절된 쓰기의 제한 교정이 다른 도구의 재실행과 섞이지 않는다. 실제 권한 검사와 호출 배치의 안전 규칙은 유지한다.

### 차단 시 값 없는 수정 안내 제공

새 `test_credential_repair.py`는 거절된 Python 문자열을 AST로만 읽고 다음 정보를 만든다.

| 항목 | 내용 |
| --- | --- |
| 위치 | 줄 번호, 일반 대입·딕셔너리·키워드 인수·기본 인수 구분 |
| 종류 | 비밀번호·토큰·해시·비밀 설정의 제한된 분류 |
| 비밀번호 생성 예시 | 기존 문자열의 길이·문자 종류에 대응하는 생성기 인수 |
| 범위 | 직접 식별할 수 있는 작성 위치만 분석. 최대 1MiB·50,000 AST 노드·16개 안내 |

`credentialRepairPlan`과 `testData`를 기존 거절 피드백에 함께 전달한다. 실제 변수명·인증정보 값·원문 코드는 포함하지 않는다. 일반 오류 메시지 이름을 인증정보 값으로 잘못 분류하지 않도록 기존 차단 규칙과 동일한 이름 범위를 사용한다.

이 정보는 **수정 참고 자료**다. 생성 코드를 실행하거나 자동 변환하지 않고, 기대 결과·주장·시험 결과를 대신 만들지 않는다. 알 수 없는 표현은 기존 보안 검사와 후속 검증에 맡긴다.

### 보안·예산·판정 유지

- 인증정보 패턴 차단, 등록 비밀값 검사, 경로 권한, Source 불변성과 승인 조건은 유지한다.
- 기존의 인증정보 작성 교정 1회를 늘리지 않는다. 다시 위반하면 저장 전에 중단한다.
- 단위 시험 생성기 인수나 런타임 동작, 시험의 합격 조건을 변경하지 않는다.
- 공개 도움말 일부가 인증정보 대입문처럼 마스킹되던 표현은 값 없는 사용 설명으로 바꿨다. 마스킹 규칙을 느슨하게 만들지 않았다.
- 새 안내·진단 구현과 기존 인증정보 규칙을 Host 정책 지문에 포함한다. 과거 Run 기록과 판정은 수정하지 않는다.

## 검증

실패 원문을 저장하지 않았으므로 원래 코드와 동일한 재현이라고 주장하지 않는다. **41·50번째 줄에 합성 고정값을 넣은 수작업 fixture**와 FakeProvider/FakeToolExecutor로 아래를 확인했다.

- QA와 Security가 거절된 원문 값을 전달받지 않고 두 위치의 교정 정보를 받는다.
- 안전한 런타임 생성 코드로 교체한 제안만 한 번 쓰기 도구에 전달된다.
- 고정값을 남기면 두 번째 제안도 차단되고 쓰기는 0회다.
- Single QA도 같은 작성 전 교정 경로를 사용한다. Single QA·Security 작업 지침은 같은 생성 규칙을 사용한다.
- 일반 오류 라벨, null·잘못된 타입, 브라우저 형식은 임의의 정상 비밀번호로 변경하지 않는다.
- 기존 Source 인증정보 차단, 공개 설명 마스킹, 도구 경로 권한, 역할 계약을 회귀 검사한다.

```bash
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest \
  tests/test_test_credential_repair.py tests/test_runtime_test_data.py \
  tests/test_file_access_guidance.py tests/test_source_credentials.py \
  tests/test_test_write_precheck.py tests/test_llm_schema_diagnostics.py \
  tests/test_service_request_alignment.py tests/test_agent_role_contracts.py \
  tests/test_single_agent_prompts.py -q
```

검증 결과: **오프라인 검사 150개 통과**, `git diff --check` 통과. 실제 API 호출로 새 제품이 QA를 통과하는지는 이번 작업에서 확인하지 않았다.

## 적용과 다음 확인

사용자 요청에 따라 실행·재시작하지 않았다. 현재 켜져 있는 Host·Agent에는 이전 코드가 로드되어 있으므로, **테스트 전 Host와 Agent를 재시작하고 새 작업으로 확인**해야 한다. 과거 실패 Run을 자동 재개하거나 결과를 성공으로 덮어쓰지 않는다.

권장 커밋 메시지: `QA 런타임 인증정보 생성 계약 통일 및 보안 차단 교정 안내 보강`
