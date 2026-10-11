# 39. 공통 Docker 이미지와 Build/Test/Scanner 설정 준비

작성 기준: 2026-10-11. 범위: 1번 Orchestrator + 2번 Agent/MCP의 실행 준비. 3번 제품 서비스, 4번 독립 평가·비교 실험은 변경하지 않는다.

## 1. 구현 내용과 현재 상태

- 기존 Build/Unit/Browser/Security 설정을 하나의 Host 전용 JSON으로 묶었다.
- 네 Tool에 같은 Image Digest·Docker Unix endpoint를 전달하고, 기존 `ExecutionBaseline`에 같은 Dependency Lock Hash·Hardware Profile과 `DENY`를 설정한다.
- Tool마다 CPU·메모리·PID·Timeout 등 제한 8개를 모두 명시해야 한다. 기존 개발 기본값으로 빈 설정을 채우지 않는다.
- Unit 정책은 Developer의 Source 테스트와 QA의 생성/보호 테스트로 분리한다.
- 오프라인 검사 CLI는 설정과 실제 Lock 파일의 Hash만 검사한다.
- 승인된 Linux Base·Hash 고정 Wheel·Chromium을 사용하는 별도 이미지 준비 Recipe를 추가했다.
- Browser에만 `/ms-playwright` 경로를 고정 ENV로 주입하도록 기존 Sandbox를 보완했다.

**설정/준비 기반을 구현한 단계다. 실제 승인 이미지가 생성·설치되었거나 Build/Test/Scan이 성공한 단계는 아니다.** Base Image, Package Version, Scanner Rule, 실행 argv, 자원 제한은 운영자가 선택해야 한다. 이미지 다운로드·Docker build/run·LLM 호출·Git commit/push는 수행하지 않았다.

## 2. 추가·변경 파일

| 파일 | 역할 |
| --- | --- |
| `src/agents/platform/tool_configuration.py` | 닫힌 Host Bundle, 기존 설정 변환, 역할별 Unit 정책, 파일/Lock 검사 |
| `src/agents/platform/check_tools.py` | Docker를 실행하지 않는 설정 검사 CLI |
| `configs/owned-tools.example.json` | 미승인 값을 `null`로 둔 설정 예시 |
| `docker/sandbox/Dockerfile` | 승인된 의존성만 별도로 설치하는 이미지 준비 Recipe |
| `docker/sandbox/.dockerignore` | 준비된 Lock/Wheel/Browser 외 Build Context 제외 |
| `src/orchestrator/sandbox/runtime.py` | Browser-only 고정 ENV와 동일 inspect 검증 |
| `tests/test_tool_configuration.py` | Bundle·Lock·CLI·불변성/ACL 회귀 |
| `tests/test_sandbox_browser_environment.py` | Fake Docker 기반 Browser ENV 경계 회귀 |

`README.md`, `.gitignore`도 갱신했다. A2A/Artifact/Trace Schema, MCP Tool Schema·개수·Protocol Version, 기존 Runner, 팀원 서비스·평가 코드는 변경하지 않았다.

## 3. Host 설정 계약

`configs/owned-tools.example.json`을 편집기로 `configs/owned-tools.local.json`에 복사하고 운영자 승인값을 입력한다. 원본의 Image/Hash/Hardware/한도/argv/Version/Rule `null`은 의도적으로 검증에 실패한다. 예시의 Selector 이름·Python 경로·localhost 주소도 승인이나 실제 존재를 의미하지 않는다.

| 필드 | 규칙 |
| --- | --- |
| `schemaVersion` | 정확한 정수 `1` |
| `imageReference` | 기존 Sandbox가 지원하는 `sha256:<64 hex>` 또는 `repository@sha256:<64 hex>`. `latest`/단독 tag 금지 |
| `dependencyLockFile` | 신뢰된 Host 파일 경로. 상대경로는 JSON 파일 폴더 기준. Agent Tool 인자가 아님 |
| `dependencyLockHash` | 실제 파일 bytes의 `sha256:<64 hex>` |
| `hardwareProfile` | 운영자가 명시한 공통 Hardware 식별자. 실물 장비를 측정/증명하지 않음 |
| `dockerEndpoint` | 기존 Codec의 정규 Unix socket URI. TCP/원격 Docker 금지 |
| `maxCallSeconds` | 유한한 실제 숫자, `0 < 값 <= 600`. 각 Tool의 실행 시간 + 제어시간 2회 + 1초 이상 |
| `build` | 기존 `BuildConfiguration` JSON. `profile.name/argv/limits` 명시 필수 |
| `unit` | 기존 Unit JSON. `scopes/python_executable/limits` 필수 |
| `browser` | 기존 Browser JSON. Suites/Service argv/Version/Python/Local URL/Ready Path/Startup·Action Timeout/Limits 모두 필수 |
| `security` | 기존 Security JSON. Profiles/Python/Limits 필수 |

하위 Tool JSON의 `image_reference`/`docker_endpoint`, Build profile의 `image_reference`는 **금지**한다. 같아 보이는 값을 중복 선언해도 거절하며 공통값만 전달한다. `networkPolicy`, `allowedHosts`, arbitrary ENV/Mount/Shell 등 새 입력면은 없다. 기존 Codec이 하위 unknown field, Shell argv, 비밀 인자, 잘못된 Profile/Version/Reference를 계속 검사한다.

모든 Tool의 `limits`는 아래 8개를 **빠짐없이** 포함한다.

```text
cpus, memory_bytes, pids, timeout_seconds, tmpfs_bytes,
max_stdout_bytes, max_stderr_bytes, control_timeout_seconds
```

숫자 범위는 기존 `SandboxLimits`를 사용한다. 문자열·bool·비유한 값은 허용하지 않는다. 이 단계는 정확한 CPU·Memory·Timeout을 임의로 확정하지 않는다. 승인 시간보다 MCP 호출 시간이 짧으면 자동 축소하지 않고 Bundle을 거절한다. 이후 Host factory에서는 38번 LLM Tool Timeout과도 일치시켜야 한다.

### 역할별 정책과 불변성

- Unit에는 `SNAPSHOT`과 `QA_TESTS`가 각각 하나 이상 필요하다.
- `unit_configuration_for(DEVELOPER)`는 `SNAPSHOT`만 반환한다.
- `unit_configuration_for(QA)`는 `QA_TESTS`와 선택적 `PROTECTED`만 반환한다. 다른 역할은 거절한다.
- Browser에도 `QA_TESTS` Suite가 하나 이상 필요하며 선택적 보호 Suite의 bytes/reference는 기존 Codec으로 검증한다.
- Unit/Browser/Security는 같은 Python 실행 경로를 사용한다. Security Profiles의 Bandit Version은 하나로 통일한다.
- 보호 기준의 Requirement 연결, 동일 Run의 불변 Hash 검증은 기존 QA 서비스·EvaluationPolicyStore·Run 계약에 맡긴다. Bundle이 독립 평가 기준이나 보안 의미 검증 근거를 생성하지 않는다.
- Bundle은 frozen이고 중첩 정책은 canonical JSON 문자열로 저장한다. 반환할 때 기존 설정 타입을 새로 복원하므로 원본 dict 변조가 정책을 바꾸지 않는다.
- 직접 생성과 `dataclasses.replace()`도 필수 필드/8개 제한을 검사한다. 기존 Codec 기본값으로 승인 정책을 복원하는 우회를 차단한다.

## 4. 오프라인 검사

```bash
PYTHONPATH=src .venv/bin/python -m agents.platform.check_tools --file configs/owned-tools.local.json
```

정상 결과:

```json
{"status":"TOOL_CONFIGURATION_VALID","executionReady":false,"DockerChecked":false}
```

이 명령은 다음만 확인한다.

- 최대 256 KiB의 UTF-8 일반 JSON 파일, 닫힌 필드, 중복 Key·NaN/Infinity 거절.
- 기존 네 Tool Codec, 필수 한도·공통 이미지/endpoint·시간/역할 교차 조건.
- 최대 1 MiB의 비어 있지 않은 일반 Lock 파일과 선언된 SHA-256 일치.
- 파일 자체의 Symlink·FIFO·Directory·초과 크기 거절, 읽는 동안 관찰 가능한 파일 변경 검사.

Lock 경로의 `../`는 신뢰된 Host 입력으로 허용하지만 Shell 변수·`~`·URI는 확장하지 않는다. 파일 부모의 모든 Symlink를 금지하거나 검사 뒤의 Host 파일 변경을 영구 차단하는 기능은 아니다. `.env`/프로세스 환경변수/Key를 읽거나 정책을 덮어쓰지 않는다.

다음은 **확인하지 않는다**: Docker 설치/Daemon 상태, 실제 Image Digest·ENV·OS·Architecture, 해당 이미지의 Lock 설치 여부, Chromium/OS Library 호환성, Python/Playwright/Bandit 실버전, argv/서비스 존재, Hardware 실물, Port 사용 여부, 실제 Build/Test/Scan 성공. 이 검증들은 기존 실행 시 검사와 41·44번에 남는다. 검사 통과를 `SUCCESS`/보안 PASS로 바꾸지 않는다.

오류는 exit code 1과 다음 코드만 출력한다. 제출된 경로·JSON·Credential 원문은 출력하지 않는다.

| 코드 | 의미 |
| --- | --- |
| `TOOL_CONFIGURATION_INVALID` | JSON 파싱/Schema, 필수값, 기존 Codec, 시간/공통값/역할 조건 오류 |
| `TOOL_CONFIGURATION_FILE_INVALID` | 파일 접근, 파일 종류·크기·UTF-8 또는 읽기 일관성 오류 |
| `TOOL_CONFIGURATION_LOCK_MISMATCH` | 실제 Lock bytes와 선언한 Hash 불일치 |

Host 코드에서는 `load_tool_configuration(path)`으로 읽고 `.baseline`, `.build_configuration`, `.unit_configuration_for(role)`, `.browser_configuration`, `.security_configuration`, `.max_call_seconds`를 기존 조립 코드에 전달한다. 이 객체만으로 DB·MCP child·Docker·Run을 생성하지 않는다.

## 5. 이미지 준비 방식 — 아직 실행하지 않음

이미지 준비는 **신뢰된 의존성을 사전에 설치하는 별도 단계**이며, 생성된 제품의 `run_build`가 아니다. Recipe는 [Docker 공식 Digest 고정 지침](https://docs.docker.com/build/building/best-practices/)과 [pip 공식 Hash 고정 설치 방식](https://pip.pypa.io/en/stable/topics/secure-installs/)을 사용한다.

운영자가 먼저 결정·준비할 항목:

1. Linux/CPU Architecture, `/usr/local/bin/python`·pip 및 Chromium OS Library를 포함한 승인 Base의 실제 repository Digest. 기존 runtime 허용 ENV만 있고 Browser ENV·VOLUME·ONBUILD·Secret이 없는 Base여야 한다. ONBUILD는 `FROM` 때 첫 guard 이전에 실행될 수 있으므로 사전 검토한다. 기본 Base나 자동 `latest`는 없다.
2. Playwright·Bandit·제품 런타임의 직접/전이 의존성을 모두 exact version + 승인 Wheel Hash로 고정한 `requirements.lock`. Host 프로젝트 `uv.lock`을 그대로 pip requirements로 사용하지 않는다.
3. 해당 Linux/Python Architecture에 맞는 사전 검토한 `wheelhouse/*.whl`. 생성 Source·sdist/Host 빌드 의존성은 넣지 않는다.
4. **동일한 Playwright Version/Architecture**로 사전 준비한 `ms-playwright/` Chromium assets와 non-root 읽기/실행 권한. Browser 설치 경로는 [Playwright 공식 Browser 문서](https://playwright.dev/python/docs/browsers)의 `PLAYWRIGHT_BROWSERS_PATH` 방식에 따른다.

준비 파일 위치:

```text
docker/sandbox/prepared/
  requirements.lock
  wheelhouse/          # 승인된 .whl만
  ms-playwright/       # 선택한 Version과 일치하는 Linux Chromium assets
```

이 폴더 전체와 `configs/*.local.json`은 Git ignore 대상이다. 현재 준비 파일·실제 로컬 설정을 만들지 않았다. 의존성 다운로드·Base Image 확보는 운영자 승인 후 별도로 수행해야 한다.

Recipe는 사전 준비된 Wheel만 `--no-index --only-binary=:all: --require-hashes`로 설치하고 모든 `RUN`의 Network를 `none`으로 제한한다. 실제 BuildKit과 외부 패키지는 실행하지 않아 Dockerfile 실행 성공을 검증한 것은 아니다. Root 설치는 이미지 준비 내부에서만 수행하고 최종 USER는 `10001:10001`이다. Host에 제품 코드를 설치/실행하지 않는다.

주의:

- Build Context는 **`docker/sandbox`만** 사용한다. 프로젝트 전체·`.env`·Git·API Key·보호 테스트·생성 Source를 전달하지 않는다. `.dockerignore`는 준비 Lock/Wheel/Browser만 허용하고 마지막 규칙으로 `.env`/`.ssh`/`.aws`/`.git`/`.codex`·일부 Credential 파일을 다시 제외한다. 임의 이름의 Secret이나 opaque Wheel/Browser 안의 내용을 모두 판별하는 DLP 기능은 아니다. 운영자가 준비 assets를 사전 검토한다.
- `BASE_IMAGE`는 실행 전에 운영자가 repository Digest로 검토한다. Dockerfile guard 이전에 `FROM` 해석이 일어날 수 있으므로 guard가 네트워크 승인/다운로드를 대신하지 않는다. 실제 build 명령은 이번에 실행하지 않았다.
- Base의 숨은 ENV/Volume/OnBuild는 기존 Sandbox의 실제 Image 검사를 통과해야 한다. Recipe가 이를 자동 삭제하지 않는다.
- 실제 이미지의 `Id` 또는 승인 Registry Digest를 Bundle의 `imageReference`에 반영한다. 임의 Hash를 예시에 넣어 검증을 통과시켜도 이미지 준비 완료는 아니다.
- 같은 Lock bytes를 40번 시작 Source에도 사용해야 기존 Snapshot의 Lock Hash 검사와 일치한다. Bundle Hash만 바꿔 기존 Run 환경을 변경할 수 없다.
- Chromium sandbox를 유지한다. Host 커널/default seccomp와 호환되지 않으면 실행을 실패로 남기고 확인한다. `chromium_sandbox=False`, privileged/cap 추가, Network 개방으로 성공을 꾸미지 않는다.

### Browser-only ENV 보완 이유

기존 Browser runner는 Playwright의 기본 경로를 사용했지만 Container HOME `/work`는 임시 쓰기 영역이다. 공통 이미지에 `ENV PLAYWRIGHT_BROWSERS_PATH=/ms-playwright`를 넣으면 다른 세 Tool의 기존 Image ENV 허용 목록과 충돌한다.

따라서 이미지 ENV 목록은 확대하지 않고 `run_browser_tests`에서만 신뢰된 고정값을 주입한다. 생성 argv와 `container inspect` 기대값이 같은 helper를 사용한다. 사용자/모델이 경로나 ENV를 선택할 수 없고 Host의 Key/Proxy/Browser 경로도 전달하지 않는다.

## 6. 개발정의서 준수 점검

| 기준 | 확인 |
| --- | --- |
| §0·§3·§6·§7 A2A/공식 상태 | Protocol/HTTP+JSON/Task·Context/폴링 변경 없음 |
| §2 책임 분리 | Host 구성·Tool 준비만. 제품 UI/API/DB와 독립 실험 개발 제외 |
| §4 수정/Retry | Fix 3회·MCP Retry 2회 유지. `maxCallSeconds`를 Retry 승인으로 사용하지 않음 |
| §5·§9 동일 Source/Manifest | 공통 이미지·Lock 기준을 기존 Baseline으로 전달. 실제 Snapshot/Manifest 증거 검사는 유지 |
| §8-7·8-8 경로/Secret | Tool Host 경로·Secret 접근면 확대 없음. CLI 출력은 안정된 코드만 |
| §8-9 Dependency Preparation | 이미지 준비와 생성 Source 실행을 분리. Tool 실행 중 설치/외부 Network 없음 |
| §8-10 Sandbox | non-root/read-only/Network none/cap-drop/제한/소켓 금지 유지. 정확한 숫자를 자동 승인하지 않음 |
| §8-11 역할 권한 | 기존 Server ACL 유지. Unit 설정을 Developer/QA 역할별로 분리 |
| §10 최종 판정 | 설정/Lock/Scanner 설치 확인을 제품 SUCCESS나 Security PASS로 인정하지 않음 |
| §11·11-A 재현성 | 공통 Digest/Lock/Hardware, exact Playwright/Bandit 설정. 실제 실행 버전·불변 정책 검증 유지 |

사용자가 보류한 URL Credential 보호와 공유 Agent DB WAL 초기화 경쟁은 수정하지 않았다. 실제 보안 의미 검증 근거 연결(42번), 비용 산출(43번), 외부 환경 실행 검증(44번)은 완료로 표시하지 않는다.

## 7. 검증

신규 Bundle/환경 회귀와 기존 Sandbox·Codec·Runner·38번 설정·Tool/서비스 회귀를 실행했다. **총 568개 모두 통과**했다.

- 신규 Bundle 51개 + Browser ENV 6개: **57개 / 0.351초 PASS**. 직접 constructor/replace의 기본값 복원 우회도 거절했다.
- 기존 Sandbox Runtime/Docker CLI·네 설정 Codec·38번 설정: **252개 / 13.171초 PASS**.
- 기존 Unit/Browser/Security Runner: **105개 / 0.091초 PASS**.
- 기존 네 실제 Tool·QA/Security 서비스: **154개 / 54.114초 PASS**.
- `compileall`, `pip check`, `git diff --check`, 새 파일 trailing whitespace 검사 통과. pip cache 접근 경고는 있었으나 의존성 오류는 없었다.
- `git check-ignore`로 로컬 JSON과 준비된 Lock/Wheel/Browser 폴더의 제외를 확인했다. 실제 준비 assets/로컬 설정은 생성하지 않았다.
- 원본 미승인 예시를 CLI로 검사하면 exit 1 + `TOOL_CONFIGURATION_INVALID`를 반환한다. 의도된 결과다.
- 독립 리뷰에서 발견한 직접 constructor 기본값 복원과 Browser assets 아래 Secret 경로 재포함 문제를 보완했다. Docker Context 필터는 정적 검토이며 실제 Docker build를 수행하지 않았다.

```bash
PYTHONPATH=src:tests .venv/bin/python -m unittest tests.test_tool_configuration tests.test_sandbox_browser_environment -q
PYTHONPATH=src:tests .venv/bin/python -m unittest tests.test_sandbox_runtime tests.test_sandbox_docker_cli tests.test_mcp_build_config tests.test_mcp_unit_config tests.test_mcp_browser_config tests.test_mcp_security_config tests.test_runtime_configuration -q
PYTHONPATH=src:tests .venv/bin/python -m unittest tests.test_mcp_unit_runner tests.test_mcp_browser_runner tests.test_mcp_security_runner -q
PYTHONPATH=src:tests .venv/bin/python -m unittest tests.test_mcp_build tests.test_mcp_unit tests.test_mcp_browser tests.test_mcp_security tests.test_qa_services tests.test_security_agent_services -q
PYTHONPATH=src .venv/bin/python -m compileall -q src tests
.venv/bin/pip check
git diff --check
```

Fake Docker·오프라인 파일/SQLite/임시 Git·신뢰된 테스트 fixture를 사용했다. 실제 Docker Image/Chromium/외부 LLM·제품 성공을 주장하지 않는다. 전체 unittest 및 팀원 pytest 평가 전체를 실행한 것은 아니다.

## 8. 커밋 메시지와 다음 작업

커밋 메시지: `공통 Docker 이미지 준비와 역할별 실행 도구 설정 추가`

다음 작업: **40번 — Run Workspace와 승인된 시작 코드 준비**. 역할 1·2의 최소 실행 기반이며 팀원 회원가입 제품을 대신 구현하는 단계가 아니다.

Git commit/push는 수행하지 않는다.
