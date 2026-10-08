# 27. Browser Test Tool 및 안전한 단계 Trace

작성 기준: 2026-10-08. 범위: 1번+2번의 MCP 브라우저 테스트 실행·저장·조회. 3번 제품 서비스, 4번 독립 평가/비교 실험 및 해당 연동은 변경하지 않는다.

> QA의 `run_browser_tests`를 실제 Snapshot·Container 실행 구조와 연결했다. 지원 실행기는 동결 이미지에 준비된 Python Playwright + Chromium이며, 현재 환경에 Docker/Playwright가 없어 실제 Container 브라우저 성공은 미검증이다. Host 브라우저 실행이나 가짜 PASS는 제공하지 않는다.

## 1. 개발한 내용

- Host 전용 `BrowserTestConfiguration`/`BrowserTestSuite`: 승인된 서비스 명령·local origin·Playwright 버전·자원과 테스트 선택 고정.
- 표준 라이브러리만 사용하는 선언형 JSON Suite 계약과 안전한 QA 입력 캡처.
- Container 전용 Runner: 서비스 준비 상태 확인, Chromium 실행, 실제 화면 동작·assertion, Context/Browser/서비스 정리.
- `BrowserTestOutputStore`: 보고서·Case별 안전한 단계 Trace·Manifest·Profile·Host 정책·Input Hash를 원자적/불변 저장.
- 실제 MCP Handler와 SDK stdio 설정 연결, 기존 `read_test_report`의 Unit/Browser 분기.

생성자/목록 조회는 파일 캡처·LLM·서비스·브라우저를 실행하지 않는다. 서비스의 생성 코드도 Container 안에서만 실행한다. 자동 Git commit/Source freeze/A2A Task 완료/QA Artifact 발행/최종 Verdict 및 기본 Agent의 `executionReady=False` 경계는 변경하지 않는다.

## 2. 실제 처리 순서

```text
Host-bound QA + workspaceId/snapshotId/testSuite
→ 등록 Suite·현재 QA Step·같은 Run의 Source/grant/환경 확인
→ QA/Host 보호 Suite + 신뢰된 Runner/Contract/Host JSON bytes 동결
→ 모든 입력 Hash 계산 + 선언형 Suite 검증
→ Frozen Source/Input을 Read-only Container에 준비
→ 승인 서비스 시작 + 같은 Container local HTTP 준비 확인
→ 고정 버전 Playwright/Chromium으로 Case별 새 Context 실행
→ 실제 Case/Step/종료 코드·오류 구분 + 리소스 정리
→ 저장 transaction에서 현재 Run/Step/Source/환경 재검사
→ 보고서·단계 Trace·Manifest/Profile/Input Hash 함께 저장
→ 기존 MCP 필드만 반환
```

QA는 `VALIDATING`/`REVALIDATING`의 현재 attempt에서 유일하게 실행 중인 QA Step의 `inputArtifactIds`에 전달된 Source만 사용한다. live Source·다른 Run·미등록 Snapshot·취소된 Run을 대상으로 실행하거나 결과를 게시하지 않는다. 파일 Read용 Host `frozen_source`와 실제 QA Step 입력을 배정하는 전체 역할 Executor는32/34번 후속이다.

## 3. 기존 MCP 계약 유지

입력은 `workspaceId`, `snapshotId`, `testSuite` 세 필드다. `testSuite`는 Host가 등록한 이름이지 임의 파일/URL/명령이 아니다.

```json
{
  "workspaceId": "HOST_ISSUED_WORKSPACE_UUID",
  "snapshotId": "STORED_SOURCE_ARTIFACT_UUID",
  "testSuite": "signup-browser"
}
```

설명용 UUID를 그대로 실행하지 않는다. 모델이 URL·command/argv·browser executable·이미지·역할·Host 경로·timeout을 추가하면 Protocol Error다.

| 출력 | 의미 |
| --- | --- |
| `total` | 실제 Case 수. 요구사항 개수/coverage와 다름 |
| `passed`, `failed` | 실제 PASS/FAIL Case 수. 이번 계약에는 Skip 없음 |
| `traceRefs` | Case별 `artifact://<실행 기록 UUID>/browser-trace-0000.json` 참조 목록 |
| `executionManifestId` | 실제 저장된 Browser 실행 기록 UUIDv4 |

Trace는 `browser-trace-v1` JSON이며 실행 기록 ID·Suite 이름·testId·Case/Step outcome·동작 이름·durationMs·안전한 오류 코드만 포함한다. **Playwright Trace Viewer ZIP, 스크린샷/동영상/HAR/DOM/Console 원문이 아니다.** Selector·입력값·화면 문구·원본 예외도 반환/Trace에 저장하지 않는다.

기존 출력 Schema에 `reportRef`를 새로 추가하지 않는다. Host/후속 QA는 이 사설 Store의 정해진 보고서 URI를 `executionManifestId`에서 구성한다.

```python
report_ref = f"artifact://{browser_result['executionManifestId']}/browser-test-report.json"
# 기존 read_test_report 입력: workspaceId + reportRef
```

같은 `read_test_report` Tool로 Unit은 `unittest-v1`, Browser는 `browser-v1`의 `testResult`를 읽는다. `traceRefs`를 이 Tool의 보고서로 읽을 수는 없다. Trace bytes 조회는 신뢰된 Host의 `browser_output_store.read_trace(qa_binding, trace_ref)`를 사용하며 새 MCP Trace Tool을 만들지 않았다. URI는 실제 Project/A2A QA Artifact나 HTTP 다운로드 링크가 아니고 임의 URL을 fetch하지 않는다.

## 4. 테스트 Suite와 Host 설정

QA Suite는 `outputs/qa/tests/...`에서 캡처한 JSON이고, Protected Suite는 Host 설정에 제공된 불변 bytes만 사용한다. 종류는 `QA_TESTS`/`PROTECTED` 두 개이며 QA만 실행한다. Protected의 `protected_suite_ref`는 frozen RunConfiguration의 기준과 정확히 일치해야 한다. QA scratch·제품 Source로 보호 기준을 교체하거나 기준 URI를 fetch하지 않는다. 4번 담당 보호 테스트 자체를 대신 작성한 것은 아니다.

Suite는 폐쇄형 선언 데이터다. 예시는 실행 가능한 제품 시나리오가 아니라 지원 동작 설명이다.

```json
{
  "format": "browser-suite-v1",
  "tests": [{
    "testId": "signup.normal",
    "steps": [
      {"action": "goto", "path": "/signup"},
      {"action": "fill", "selector": "#email", "value": "fixture@example.test"},
      {"action": "click", "selector": "#submit"},
      {"action": "assert_text", "selector": "#result", "text": "가입 완료"}
    ]
  }]
}
```

추가 동작은 `assert_visible(selector)`, `assert_url(path)`다. Case는 반드시 goto로 시작하고 적어도 하나의 assertion을 포함한다. Arbitrary JavaScript/evaluate/Python script/Shell·외부 URL·업로드/다운로드/자유 대기시간 API는 없다. 경로는 단일 local origin의 canonical path만 가능하며 `..`, `//`, 절대 외부 URL, query/fragment/입력 percent escape는 거부한다. Unicode 경로는 Runner에서 브라우저 URL 표기에 맞게 인코딩한다.

새 Context로 Cookie/Browser Storage는 Case별 분리하지만 같은 서비스/DB 상태는 Suite 동안 공유한다. DB reset·제품 fixture/acceptance 기준·독립 비교평가는 이번 Tool이 자동 생성하지 않는다. Off-origin HTTP 요청 및 page/frame 이동을 검사하고 차단한다. 일반 외부 CDN·별도 port API·복잡한 iframe/다중 origin 서비스 연동까지 지원한다고 주장하지 않는다.

Host 사용 예시(실제 binding/Source/이미지·해당 app.py가 준비되어 있을 때만):

```python
from dataclasses import replace
from mcp_tools.tools.browser_config import BrowserTestConfiguration, BrowserTestSuite

configuration = replace(
    existing_qa_configuration,
    browser_test_configuration=BrowserTestConfiguration(
        suites=(BrowserTestSuite(name="signup-browser", kind="QA_TESTS"),),
        service_argv=("/usr/local/bin/python", "-B", "/snapshot/app.py"),
        playwright_version=approved_playwright_version,
        base_url="http://127.0.0.1:8765",
    ),
)
```

Client 필드는 `MCPChildConfiguration.browser_test_configuration`, CLI flag는 `--browser-test-configuration-json`이다. Build/Unit/Browser 설정을 함께 전달하면 로컬 Docker endpoint가 같아야 한다. 환경변수나 모델 요청으로 서비스 명령·버전·브라우저 옵션을 자동 고르지 않는다.

Host 필수 설정은 Suite·service_argv·Playwright 정확한 `1.x.x` 버전이다. 기본 Python executable은 `/usr/local/bin/python`, local origin은 `http://127.0.0.1:8765`, readiness path `/`, startup 제한10초, action 제한5000ms다. Port는1024~65535, startup은 최대60초, action은 최대30000ms로 제한한다. 이 숫자는 임시 Host 제한이지 실제 제품에 맞춰 팀이 확정한 성능 기준이 아니다.

서비스를 시작할 수 있는 Frozen Source와 앱/정적 파일·모든 의존성이 승인 이미지에 준비되어야 한다. Browser Tool이 React build 산출물을 자동 export하거나 현재 Host의5173/8000 서비스에 연결하지 않는다. 관련 제품 실행 계약/통합은 이번 범위 밖이다.

## 5. Container·개인정보 경계

22번 Runtime의 비특권 UID10001, Root/Source/Input Read-only, Network none, Capabilities drop, no-new-privileges, Seccomp, CPU/Memory/PID/시간/출력 제한 및 Host FS/Socket/Secret Mount 금지를 유지한다. 서비스·브라우저는 같은 Container의127.0.0.1 origin만 사용하며 Host gateway/port publish/외부 network를 추가하지 않는다.

Runner argv는 `python -I -B /inputs/_browser_runner.py`다. 읽기 전용 Host Contract만 `/inputs`에서 import하고 생성 Source를 Runner/Host Python에 import하지 않는다. 서비스 실행은 Container 안의 `subprocess` + `shell=False` + 독립 process group이며 raw stdout/stderr를 버린다. 준비 확인은 proxy·redirect·body 로그 없는 local HTTP다. 종료/오류 시 Browser/Context/Playwright manager를 닫고 서비스 group TERM/KILL+bounded wait를 수행한다. 외부 Host 프로세스에 이 cleanup을 사용하지 않는다.

Chromium은 `headless=True`, `chromium_sandbox=True`를 고정하고 다운로드·Service Worker·원문 Trace 수집을 끈다. 실제 Playwright package 버전을 Host 설정과 대조하며 실제 Browser 버전도 기록한다. SDK/Chromium/공유 라이브러리는 이미 동결 이미지에 설치되어 있어야 하고 자동 download/install/pull 또는 `--no-sandbox` fallback은 없다. API 기준은 [Playwright BrowserType 공식 문서](https://playwright.dev/python/docs/api/class-browsertype)다.

기존 제한된 image ENV 검사를 유지하되 **Browser Tool에만** `PLAYWRIGHT_BROWSERS_PATH=/ms-playwright`라는 고정 비밀값 아닌 경로를 허용한다. 다른 값/Tool에 이 예외를 확대하지 않는다. 실제 UID10001의 파일 접근·Linux user namespace/Chromium Sandbox·memory/PID/IPC 호환성은 승인 환경에서 별도 검증이 필요하다. [Playwright Docker 문서](https://playwright.dev/python/docs/docker)의 namespace/seccomp·IPC 요구가 기존 엄격한 Runtime과 충돌할 수 있으며, 설치만 하면 동작한다고 보장하지 않는다. Root/Host IPC/SYS_ADMIN/unconfined 권한으로 자동 완화하지 않는다.

입력은 기존 QA FD/O_NOFOLLOW/cooperative flock 캡처를 재사용한다. symlink/hardlink/Secret/private staging/특수 파일·파일 상태 변경을 거부한다. Runner·Contract·Host JSON은 테스트 파일과 분리하고 원본 bytes로 모든 Hash를 계산한다. Test 파일 최대64개·파일당1MiB·**전체 입력16MiB**이며 Host 설정 JSON은64KiB/최대32 Suite, Suite는1MiB/최대100 Case/Case당100 Step/전체1000 Step이다. 보고서/Trace는 각각1MiB 이하, receipt metadata512KiB 이하이고 중복 키/ID·추가 필드·bool 정수·비유한 값을 거부한다.

## 6. 결과·오류와 저장

| 상황 | 처리 |
| --- | --- |
| 정상 화면/기능 assertion 완료 | Tool 성공 + 실제 passed/failed. QA 최종 PASS 아님 |
| assertion/locator 동작 실패·action timeout | 해당 Case FAIL, Tool 성공 + failed 건수·안전한 코드 |
| Chromium 시작/SDK 버전·의존성 미준비 | `BROWSER_START_FAILED` |
| 잘못된/빈 Suite·불완전 Case/Step 보고서·서비스/실행기/OOM/정리/저장 오류 | `TEST_RUNNER_ERROR` |
| Sandbox/Tool 전체 시간 초과 | `TIMEOUT` |
| 경로/권한/Protocol 위반 | 기존 PATH/PERMISSION/JSON-RPC 오류 |
| 없는 보고서 | `REPORT_NOT_FOUND` |

Case IDs/순서·동작 prefix를 선택 Suite와 비교한다. PASS는 모든 승인 Step을 실행해야 하고, FAIL은 마지막 실행 Step만 FAIL이다. counts를 outcome에서 다시 계산하며 exit0/1과 대조한다. 임의 stdout 문장에서 숫자를 추측하거나0개 테스트를 PASS로 만들지 않는다. Browser 시작 오류는 exit3의 안정 코드, 실행기 오류는 exit2로 분리해도 보고서는 발행하지 않는다.

`browser_test_execution_records`에는 보고서·정제 stdout/stderr·안전 Trace BLOB 및 각 Hash/크기·Source Manifest·실제 Profile/image/Container·Host 정책/입력 Hash/Suite Case·동작 inventory를 한 transaction으로 저장한다. 원본 Suite·selector/value·DOM은 저장하지 않는다. QA 원본 테스트 장기 Artifact·요구사항별 evidence/coverage 조립은32번 후속이다.

publication에서 현재 Run/Workspace/동결 환경·유일 실행 중 QA Step/attempt/requirement/codeVersion/input Source와 실제 Source BLOB/grants를 재검사한다. SQL update/delete/replace·중복 실행 ID·Source/Build/Unit/Browser ID 충돌을 거부한다. 조회에서도 metadata/BLOB/Hash·Host policy·Source grant/lineage·Trace 유도를 검증한다. 완료 Run의 과거 기록은 조회 가능하지만 현재 코드 PASS를 뜻하지 않는다.

동일 Container의 악성 서비스가 같은 UID의 다른 프로세스를 방해하는 것까지 cryptographic attestation으로 보장하지 않는다. 서버/Browser 실패 원인을 화면 기능 결함과 항상 자동 구분할 수 있는 것도 아니다. 독립 보호 기준/후속 QA 판정이 필요하다.

## 7. 정의서 점검·후속 경계

| 기준 | 이번 결과 |
| --- | --- |
| §2 역할 책임 | QA만 Browser Tool 사용, 최종 Verdict/수정/Task 흐름 자동 변경 없음 |
| §5 동일 Snapshot/환경 | 실제 Source bytes·QA input Source/grant·image/lock·Input Hash와 current Step 재검증 |
| §8 Protocol/Schema | MCP2026-07-28/SDK v2/stdio/JSONSchema2020-12, 기존 이름/입출력 유지 |
| §8-3/6 오류 | 화면 검증 FAIL과 Browser 시작/실행기/Timeout 오류 분리 |
| §8-7~11 안전 경계 | 고정 Container/Origin·읽기 전용 입력·Secret/Trace 제한·QA 권한, Host 생성 코드 실행 없음 |
| §10 성공 판정 | 테스트 결과만으로 QA PASS·SUCCESS·coverage 계산 안 함 |
| §11-A 보호 평가 | Agent 작성 Suite와 Host 보호 Suite 분리, frozen 기준 참조 확인 |
| 담당 범위 | 1번+2번만. 팀원 제품/평가 모듈 및 연결 미변경 |

시간/취소 한계는25/26번과 같다. Sandbox deadline은 `min(Host timeout,max_call_seconds-2×control_timeout_seconds-1초)`(기본39초)이며 준비·실행·종료 확인이 공유한다. 내부 취소는 소유 Container cleanup을 기다리지만 동기 I/O/외부 세션 종료/SDK2초 종료 유예/프로세스 crash의 hard wall-clock·orphan 정리 완료까지 보장하지 않는다.

자동 Retry/Fix·ToolEvidence·누적 Run 예산/Trace 및 기본 Agent 연결은29/30~34/37번 후속이다. 기존 비밀번호 보호 정책 보완/WAL 초기 경쟁도 해결했다고 표시하지 않는다. 이번으로 기업 시연·실제 회원가입 검증·4번 비교 실험이 완료된 것은 아니다.

## 8. 검증

```bash
PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -p 'test_mcp_browser*.py' -q
PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -p 'test_mcp*.py' -q
PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -q
PYTHONPATH=src .venv/bin/python -m compileall -q src/mcp_tools src/orchestrator/sandbox
.venv/bin/pip check
git diff --check
```

검증 결과:

- Browser 전용 신규 테스트 **197개 통과**: Host 설정/Contract37, 입력 캡처27, Runner38, 보고서20, 불변 Store49, Handler/stdio26.
- MCP 전체 회귀 **798개 통과**(114.674초).
- 프로젝트 전체 회귀 **1,516개 통과**(159.588초).
- 전체 회귀 이후 URL 경로의 `^`를 `%5E`로 직렬화하도록 마지막 보완했고, 해당 Runner/Report **58개를 다시 통과**했다.
- Python compile, 설치 의존성 호환성(`pip check`), `git diff --check` 확인.

SDK stdio의 로컬 Unix socket 검증은 실행 권한을 승인받아 수행했다. Git/SQLite/Artifact/파일·SDK stdio는 실제 임시 fixture이며, Docker/Playwright/서비스는 Fake 실행 fixture다. 생성 Source·실제 웹 서비스·브라우저를 Host에서 실행하지 않았다. 현재 환경에는 Docker executable과 Host Playwright package가 없으며 이를 자동 설치하지 않는다.

## 9. 변경 파일·다음 작업

Browser 설정/Contract/Inputs/Runner/Report/Store/Handler와 기존 Test Report router, 전용 테스트6개·README·본 문서를 추가했다. MCP Client/CLI/안전 오류 코드와 Sandbox의 Browser용 정제 JSON decoder/고정 cache 경로만 연결하고 기존 Build/Unit 저장소는 Browser ID 충돌 거부만 보완했다. 새로운 Host 의존성/Lock 변경은 없다. 기존 팀원 소유 `development-log.md`는 수정하지 않는다.

다음 작업: **28번 — Security Scan Tool**.

커밋 메시지: `고정 Snapshot 기반 MCP Browser Test Tool과 안전한 단계 Trace 구현`

Git commit/push는 직접 수행하지 않는다.
