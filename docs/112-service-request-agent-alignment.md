# 112. 사용자 요청·Agent 구현·품질검사 대상 일치

작성일: 2026-11-26. 담당 범위는 Orchestrator·Agent·MCP와 제품의 접수/오류 표시 연결이다.

## 발견한 실제 문제

Run `c6d09a8c-2cfb-400e-afb9-3adce0248b15`의 요청 원문은 **`할 일 관리웹사이트\n만들어줘.`**였다. 그러나 제품이 회원가입 실험 `SCN-001`을 전송했고, Agent도 그 실험의 회원가입 기준을 수행했다.

- Developer는 웹 화면 없이 API와 JSON 상태 응답을 만들었다.
- QA는 없는 회원가입 입력창을 가정한 브라우저 시험을 만들어 `ACTION_TIMEOUT`이 발생했다. QA 업무 완료와 제품 시험 실패는 서로 다른 상태다.
- Security는 검사 결과 초안을 계약에 맞추지 못해 `LLM_VALIDATION_DRAFT_EXHAUSTED`로 중단됐다. 당시 정확히 어떤 필드가 틀렸는지는 기록만으로 확정할 수 없었다.
- 수정 Developer는 실제 파일 변경 없이 READY를 제출해 `LLM_DEVELOPMENT_NO_PROGRESS`로 중단됐다.
- 기존 로컬 설정의 출력 교정 한도는 0이었고, 화면에는 구체적인 실패 코드가 전달되지 않았다.

## 변경한 흐름

```text
사용자 원문
 → SERVICE-WEB 접수
 → Host가 원문·해시·Requirement UUID를 Run에 고정
 → Planner가 같은 요구사항을 구현 작업으로 분해
 → Developer가 실제 화면과 기능 구현
 → 같은 불변 Source로 QA·Security 검사
 → 실패 동작·원인·근거를 Developer 수정 입력으로 전달
```

### 1. 서비스 요청과 회원가입 비교 실험 분리

| 용도 | 시나리오 | 처리 |
| --- | --- | --- |
| 제품에서 새 웹사이트 요청 | `SERVICE-WEB` · `03d94bc5-1a31-473b-8527-c4acf8ea58a1` | 원문을 기준으로 Run별 요구사항 고정 |
| 회원가입 성능 비교 | 기존 `SCN-001` UUID 유지 | 기존 8개 요구사항·API·Single/Multi·Ver1/Ver2 기준 유지 |

제품의 새 접수와 승인 조회는 SERVICE-WEB을 사용한다. 과거 SCN-001 접수/Run은 계속 조회·복구할 수 있다. 새 접수 응답이 다른 시나리오를 반환하면 연결하지 않는다. 프로젝트의 기존 시나리오 일치 검사는 유지한다.

Host는 원문을 최대 20,000자로 제한하고, UTF-8 문맥 한도에 맞는 조각과 원문 전체 해시를 보관한다. 여기에 실제 HTML 화면, 입력/상태 처리, 보안, 실행 이력 기준을 추가한다. 모델이 승인 규격이나 Requirement ID를 새로 확정하는 방식은 아니다. Planner는 고정된 범위 안에서 구현 계획을 작성한다.

모든 역할의 실행 문맥에서 원문과 고정 계약의 일치를 검사한다. 추가 답변으로 `serviceContract`나 `sourceInterface`를 덮어쓸 수 없다. 기존 범위의 확인 답변과 새로운 기능 범위 변경은 구분하며, 새 범위는 명시적으로 새 Run을 접수한다.

### 2. 네 역할과 Single 지침 정리

- **Planner:** 원문을 화면·사용자 동작·API·상태 변경·검증 작업으로 나눈다. 중요한 모호함은 INPUT_REQUIRED로 확인한다.
- **Developer:** 요청한 앱의 HTML/CSS/JavaScript 화면과 실제 동작을 만든다. 요청에 없는 회원가입을 대신 만들지 않는다.
- **QA:** 실제 Source의 라우트와 UI를 읽고 시험을 작성한다. 회원가입 전용 `AppTestCase`, `self.User`가 일반 앱의 필수 조건이 되지 않는다.
- **Security:** 실제 앱의 입력·데이터 접근·비밀정보 노출 경계를 확인한다. 계정이 없는 앱에 회원가입 정책을 강요하지 않는다.
- **Single:** 동일한 서비스 기준을 사용하되 하나의 정체성·Context·예산을 유지한다. 의미 검증에서도 같은 정적 시스템 지침을 검증한다.

임의의 시스템 프롬프트는 계속 차단한다. 도구 권한, 인증, Sandbox, Source 불변성, 의미 검증, 최종 판정 권한은 유지한다.

### 3. 실제 UI와 검사 대상 연결

QA에 제공하는 `sourceInterface`는 인증된 Source 아카이브에서 추출한 파일·정적 라우트·HTML id/name 선택자와 Source ID/해시다. 생성 코드를 Host에서 실행하지 않으며 파일 수·바이트·AST 크기를 제한한다.

이 정보는 **실제 브라우저 DOM이나 통과 판정이 아니다.** 동적 JS로 만들어지는 요소는 별도 확인이 필요하다. 목록에 없다는 이유로 기능 누락을 확정하지 않는다. 필수 브라우저 시험을 없애거나 통과로 바꾸지 않는다.

Developer가 명백히 `GET /`에서 JSON만 반환하고 HTML 화면도 없는 코드를 완료로 제출하면 `READY_SERVICE_INTERFACE_REVIEW`를 보낸다. 현재 소스를 읽고 실제 변경한 뒤 다시 제출하는 기존 제한 교정 경로를 사용한다. 정적 경고가 없다는 사실만으로 제품 성공을 판정하지 않는다.

### 4. 실패 원인과 수정 입력 보존

- 브라우저 실패는 `MEASURED_FAIL`만 남기지 않고 `BROWSER_ACTION_TIMEOUT` 등 제한된 원인을 기록한다.
- 실패한 동작 번호·동작 종류·실행 근거·시험 버전을 보고서와 수정 피드백에 보존한다.
- 출력 규격/시험 ID/코드 참조 오류는 제한된 `draftIssue` 코드로 복구 기록에 남긴다. 원문 응답이나 민감한 값을 저장하는 우회 경로는 추가하지 않았다.
- Step 조회에 선택 필드 `failureCode`를 추가했다. 화면은 알려진 코드만 쉬운 설명으로 표시하고, 알 수 없는 원문은 표시하지 않는다.

로컬 Multi/Single 설정과 실행 예시에 `max_output_corrections: 2`를 적용했다. 교정도 기존 시간·호출·토큰 예산을 소비하며, 같은 실수를 무한 반복하지 않는다. 비교 실험 설정은 별도로 유지한다.

로컬 설정 수정 전 백업: `/private/tmp/a2a-service-request-config-before-r1gbj0_4`.

## 검증과 실행 범위

외부 API와 Docker를 실행하지 않는 회귀 검사로 아래를 확인한다.

- 할 일 앱·블로그·예약 요청의 원문 고정과 역할별 동일 기준 전달.
- 긴 한글/이모지 요청, 잘못된 문맥 결합, 미등록/실험 설정 오용 차단.
- 실제 LLMEngine과 프로세스 내부 A2A에 합성 응답을 주입한 Planner 및 Single의 서비스 지침 수용.
- API만 있는 완료 초안의 정적 차단과 기존 Source 교정 경로 연결.
- 브라우저 실패 영수증 → QA Artifact → Developer 피드백의 실패 좌표 보존.
- JWT·소유권·승인 설정·멱등 접수/복구의 제품 백엔드 회귀 검사.
- 업무 입력·중복 클릭·과거 접수 복구·오류 표시의 격리 브라우저 검사와 프론트 빌드/lint.

주요 검사 명령:

```bash
.venv/bin/python -m pytest tests/test_service_request_alignment.py tests/test_service_agent_execution.py tests/test_qa_services.py -q
PYTHONPATH=backend:backend/tests:src backend/.venv/bin/python -m unittest test_project_scenarios test_office_service test_host_reads -q
cd frontend
node --test tests/officeRunPopups.test.mjs tests/productOfficeSubmission.test.mjs tests/officeRequestUi.test.mjs
npm run build
npm run lint
```

`check-host`는 `OWNED_HOST_CONFIGURATION_VALID`였으며, 이 점검은 Docker 실행이나 제품 성공을 검증한 것이 아니다.

실행한 검증 결과는 다음과 같다. 묶음 사이에 같은 검사가 포함되어 있으므로 개수를 합산하지 않는다.

| 검증 묶음 | 결과 |
| --- | --- |
| 요청 정합성·Agent 실행·역할 계약·LLMEngine | 101개 통과 |
| QA 영수증·교정·실패 복구를 포함한 회귀 | 176개 통과 |
| Host·승인·범위·Single 실행/수정 흐름 | 162개 통과 |
| Single 대화·의미 검증·생성 시험 파일 복구 | 66개 통과 |
| 제품 백엔드 시나리오·Host 중계·접수 계약 | 27개 통과 |
| 프론트 접수·복구·중복 클릭·오류 안내 | 27개 통과 |
| 프론트 빌드 / lint / 변경 공백 검사 | 통과 |

프론트 브라우저 검사는 외부 연결을 차단한 임시 Chrome 프로필과 합성 HTTP 응답을 사용했다. 실제 제품/Host 서버는 실행하지 않았다. 빌드의 기존 대용량 3D 번들 안내는 남아 있으며 빌드 오류는 아니다.

## 현재 한계와 다음 확인

이번 작업은 코드/계약 보완과 오프라인 검증이다. 실제 OpenAI 생성 결과가 요구 기능을 완성하거나 최종 QA·Security를 통과했다고 주장하지 않는다.

서비스가 지원하는 실행 환경은 승인된 **FastAPI·SQLite·로컬 HTML/CSS/JavaScript·설치된 패키지·네트워크 차단** 범위다. 다른 스택이나 외부 서비스가 필요한 요청은 사용자 확인 또는 별도 승인 환경이 필요하다.

서버와 Docker는 사용자 요청에 따라 종료했고 재시작하지 않았다. 다음 실제 확인은 서버 기동을 다시 요청한 뒤 **새 SERVICE-WEB 작업**으로 한다. 과거 실패 Run이나 보고서를 성공으로 바꾸지 않는다. 코드 변경으로 Host 정책 지문이 달라진 과거 작업을 자동 재개하지 않는다.

권장 커밋 메시지: `사용자 요청 기반 웹앱 개발 경로 분리 및 에이전트 검증·실패 피드백 보강`
