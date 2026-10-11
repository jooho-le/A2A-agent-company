# 38. 실제 실행용 Host 설정과 안전한 설정 검증

작성 기준: 2026-10-11. 범위: 1번 Orchestrator + 2번 Agent/MCP의 실행 준비. 기존 1~37번 구현 이후 추가한 38~45번 마무리 계획의 첫 단계다.

## 1. 구현 내용

- 네 Agent와 Orchestrator를 함께 설정하는 `OwnedRuntimeConfiguration`을 추가했다.
- 하나의 공통 모델·LLM 한도를 네 역할의 기존 `AgentSettings`로 변환한다. 역할별 모델·예산 덮어쓰기는 허용하지 않는다.
- 서버 주소는 기존 loopback 세 가지로 제한하고 Agent URL은 주소와 역할별 포트에서 생성한다.
- 서로 다른 포트와 DB, Agent에 노출되지 않는 설정/DB 위치를 검사한다.
- JSON에는 API Key/Bearer의 **환경변수 이름만** 선언하고 실제 비밀값은 명시적인 resolver에서 `SecretStr`로 읽는다.
- 설정만 확인하는 별도 CLI를 추가했다. DB 생성·서버 시작·Provider 생성·LLM 호출·MCP/Docker 실행은 하지 않는다.

이 설정은 **신뢰된 운영자용 Host 입력**이다. A2A/MCP 요청, 모델 Tool 인자, Artifact, 최종 Verdict의 새 계약이 아니다. 기본 `python -m agents`는 Bootstrap을 유지하고, 실제 실행은 기존처럼 별도 Host factory를 명시해야 한다.

## 2. 추가 파일

| 파일 | 역할 |
| --- | --- |
| `src/agents/platform/configuration.py` | 닫힌 설정 모델, 파일 로더, 경로 검사, 비밀값 해석, 기존 설정으로 변환 |
| `src/agents/platform/check_configuration.py` | 실행하지 않는 설정 확인 CLI |
| `configs/owned-runtime.example.json` | 실제 모델·시간을 임의 선택하지 않은 설정 예시 |
| `tests/test_runtime_configuration.py` | 오프라인 설정 회귀 검증 |

`README.md`, `.env.example`, `.gitignore`도 갱신했다. `configs/*.local.json`은 Git에서 제외한다. 팀원의 서비스·평가 코드, canonical Schema, SDK/의존성 버전은 변경하지 않았다.

## 3. 설정 계약

| 항목 | 정책 |
| --- | --- |
| `schemaVersion` | 정수 `1`. 프로토콜 버전이 아니라 Host 설정 자체의 버전 |
| `host` | `127.0.0.1`, `localhost`, `::1` 중 하나. 기본은 기존 local 주소 |
| `orchestrator`, `agents` | Orchestrator + PLANNER/DEVELOPER/QA/SECURITY. 모든 역할의 포트·DB 명시 필수 |
| `workspaceRoot` | 신뢰된 Host 기준 경로. Agent Tool 인자로 전달하지 않음 |
| `model` | 공통 Provider/modelId/temperature 필수. 지원되는 경우에만 revision 사용 |
| Provider | 현재 기본 factory에 구현된 `openai`만 지원. 모델명은 기본값 없음 |
| `seed` | 현재 OpenAI adapter는 지원하지 않으므로 생략 또는 `null`. 조용히 무시하지 않음 |
| `apiKeyEnv`, `bearerTokenEnv` | 대문자 환경변수 이름. Key/Token 원문 필드 없음 |
| Bearer | local에서 생략/`null`이면 미설정. production 표시에서는 네 역할 모두 명시 필수 |
| `llmLimits` | 기존 `LLMLimits`의 일곱 항목 모두 명시. 개발용 기본값을 승인값으로 자동 채우지 않음 |
| `runtimeBudgetMs` | 명시적인 양의 정수. 기존 Run의 전체 실행 예산에 사용할 준비 값 |

예시의 모델 ID·temperature·runtimeBudgetMs는 `null`이며 의도적으로 검증을 통과하지 않는다. 운영자가 값을 결정하기 전에는 실행 가능하다고 표시하지 않는다. 예시에 적힌 LLM 한도는 기존 개발 기본값을 보여주는 것으로, 실제 과금 한도를 승인하거나 보장하지 않는다.

### JSON/경로 검증

- 닫힌 모델을 사용하며 알 수 없는 항목·역할별 모델 override·직접 비밀값 입력을 거절한다. 공개 설정과 중첩 역할 모델은 frozen이다.
- 정수 필드에 문자열·bool·소수는 허용하지 않는다. temperature/timeout은 유한한 실제 숫자만 허용한다.
- 파일은 최대 64 KiB의 일반 UTF-8 JSON이다. 중복 Key, NaN/Infinity, 비정규 파일·FIFO·파일 symlink를 거절한다.
- 상대경로는 **설정 파일이 있는 폴더** 기준이다. `../.data/...`는 신뢰된 Host 경로로 허용하지만 `~`, 환경변수·Shell 문자열 확장, URI는 지원하지 않는다.
- 다섯 DB 및 예약된 `-wal`/`-shm`/`-journal` 경로의 동일 경로·파일/디렉터리 조상 충돌, 기존 symlink alias와 hardlink 동일성을 검사한다.
- 아직 존재하지 않는 DB도 대소문자·Unicode 정규화 차이로 같은 파일이 되지 않도록 NFC+casefold 경로를 보수적으로 비교한다. case-sensitive 파일시스템에서도 이런 이름 쌍은 거절한다. 모든 OS의 경로 alias를 완전히 증명한다고 주장하지 않는다.
- DB/보조 파일과 설정 폴더가 `workspaceRoot` 안에 들어가는 구성을 거절한다. DB·Workspace 부모가 실제 디렉터리인지와 기존 경로의 파일 종류도 확인한다.
- 경로 검사에서는 파일 metadata를 읽을 수 있지만 DB를 열거나 폴더·파일을 생성하지 않는다. 이 검사가 이후 Host 파일 변조를 막는 OS 격리 기능은 아니다.

## 4. 비밀값과 기존 환경변수의 분리

JSON에는 `A2A_PLATFORM_LLM_API_KEY` 같은 이름만 들어간다. 실제 값은 운영자가 process environment에 제공한다. API Key를 서버 측 환경변수로 받는 방식은 [OpenAI 공식 인증 안내](https://developers.openai.com/api/reference/overview)의 지침을 따른다. Key 발급·API 인증·원격 모델 접근·유료 호출은 수행하지 않았다.

이 단계의 checker/resolver는 `.env`를 자동으로 읽지 않는다. `.env.example`의 주석은 필요한 이름을 안내할 뿐이며, 파일에 적었다는 것만으로 `--check-secrets`가 통과하지 않는다. 운영자가 사용하는 환경 관리 도구로 해당 값을 process environment에 제공해야 한다. Key/Token은 채팅·커밋·Source Workspace·Container에 넣지 않는다.

존재하지 않거나 공백뿐인 값은 `SECRET_MISSING`, 비문자 값·앞뒤 공백·제어문자·과도한 길이는 `SECRET_INVALID`로 거절한다. 오류와 CLI 출력에는 실제 값, JSON 원문, 파일 경로를 포함하지 않는다.

기존 `AGENT_*`, `ORCHESTRATOR_*`, `.env`가 명시적인 Host 구성을 덮어쓰면 안 된다. source를 끈 내부 설정 타입에서 검증한 뒤 **기존 정확한 `Settings`/`AgentSettings` 타입**으로 반환한다. 제외된 Secret 필드도 보존한다. 이는 기존 기본 서버의 환경변수 로딩 방식을 변경하지 않는다.

## 5. 사용 방법

1. 예시를 편집기로 복사하여 `configs/owned-runtime.local.json`으로 저장한다. 이 파일은 Git ignore 대상이다.
2. 사용할 모델 ID, 지원되는 모델 revision, temperature, 실행 시간과 LLM 한도를 직접 결정한다. JSON에 실제 Key/Token을 적지 않는다.
3. 설정 형식·경로 충돌만 확인한다.

```bash
PYTHONPATH=src .venv/bin/python -m agents.platform.check_configuration --file configs/owned-runtime.local.json
```

정상 결과:

```json
{"status":"RUNTIME_CONFIGURATION_VALID","credentialCheck":"NOT_REQUESTED","executionReady":false}
```

4. 선택한 이름의 비밀값을 process environment에 제공한 후, 필요한 경우 존재 여부도 확인한다.

```bash
PYTHONPATH=src .venv/bin/python -m agents.platform.check_configuration --file configs/owned-runtime.local.json --check-secrets
```

정상 결과:

```json
{"status":"RUNTIME_CONFIGURATION_VALID","credentialCheck":"PRESENT","executionReady":false}
```

둘 다 실제 API 인증·모델 옵션 호환성·Docker 존재·포트 사용 여부를 확인하는 명령이 아니다. `executionReady`를 계속 `false`로 반환하며 네 Agent를 시작하지 않는다. 원본 예시를 그대로 검사하면 `RUNTIME_CONFIGURATION_INVALID`와 exit code 1이 나오는 것이 정상이다.

### Host 코드에서 사용

```python
from pathlib import Path
from agents.platform.configuration import load_runtime_configuration, resolve_runtime_configuration

path = Path("configs/owned-runtime.local.json").absolute()
configuration = load_runtime_configuration(path)
prepared = resolve_runtime_configuration(configuration, base_directory=path.parent)

# prepared.orchestrator_settings / prepared.agent_settings
# prepared.model / prepared.limits / prepared.runtime_budget_ms
# 이 객체만으로 create_platform 또는 LLM 호출을 자동 실행하지 않는다.
```

실제 조립은 41번의 Host factory에서 진행한다. Commit·Image Digest·Dependency Lock·QA 보호 기준·Scanner Profile은 39~42번에서 별도로 준비하고 기존 Run Configuration/Manifest 검증을 계속 사용한다. `runtime_budget_ms`도 준비 값일 뿐, 이 단계가 새 Run을 만들거나 예산을 발급하지 않는다.

## 6. 오류 코드

| 코드 | 의미 |
| --- | --- |
| `RUNTIME_CONFIGURATION_INVALID` | 역할/타입/필수 항목/옵션/포트 또는 CLI 계약 오류 |
| `RUNTIME_CONFIGURATION_FILE_INVALID` | 파일을 읽을 수 없거나 비정규·크기·UTF-8·JSON 파싱 오류 |
| `RUNTIME_CONFIGURATION_STORAGE_CONFLICT` | DB/Workspace/설정 위치 또는 경로 종류 충돌 |
| `RUNTIME_CONFIGURATION_SECRET_MISSING` | 선언된 비밀 환경변수 미설정 또는 공백 |
| `RUNTIME_CONFIGURATION_SECRET_INVALID` | 선언된 비밀값 형식 오류 |

## 7. 개발정의서 준수 점검

| 개발정의서 기준 | 이번 단계의 확인 |
| --- | --- |
| §0·§6·§7 공식 A2A/상태 | 1.0·HTTP+JSON·Task/Context ID·폴링·공식 상태 그대로 유지 |
| §2 책임 분리 | Host 설정만 추가. Orchestrator가 제품 코드를 작성하거나 결과를 조작하지 않음 |
| §4 수정/재시도 | Fix 최대 3회·MCP Retry 최대 2회 변경 없음. LLM 한도와 혼동하지 않음 |
| §5·§9 동일 Snapshot/환경 | 현재 Registry·Manifest·Source ACL 변경 없음. Host 설정만으로 검증 완료를 주장하지 않음 |
| §8 Tool/격리/비밀 보호 | stdio·역할 Tool 목록·Container Network DENY 유지. Key/Token 원문은 설정 파일과 Tool에 전달하지 않음 |
| §10 최종 Verdict | 설정 통과/Task COMPLETED를 제품 PASS 또는 SUCCESS로 바꾸지 않음 |
| §11·§11-A 관측/재현성 | 공통 모델·공통 한도 사용. 기존 Run Configuration·Trace Schema 변경/값 재발급 없음 |
| 담당 범위 | 1·2번 실행 준비만. 3번 제품 UI/API/DB, 4번 독립 평가·비교 실험 제외 |

사용자가 보류한 설정 URL Credential 보호와 기존 공유 Agent DB 초기화 WAL 경쟁은 수정하지 않았다. 역할별 다른 DB를 준비하는 것으로 기존 경쟁 문제 자체를 해결했다고 주장하지 않는다.

## 8. 검증과 한계

새 오프라인 설정 회귀와 기존 구성·예산·Bootstrap·LLM 설정·Run 계약 회귀를 수행했다. 실제 Key가 아닌 명시적인 fake environment를 사용했다.

- 신규 설정 회귀 **64개 / 1.003초 모두 통과**. 닫힌 Schema·숫자·불변성·비밀 참조·환경 오염·DB/SQLite 보조 파일/Workspace 경로·대소문자/Unicode alias·안전 CLI를 확인했다. 마지막 제어문자/경로 오류 보호까지 반영한 코드 기준이다.
- 기존 관련 회귀 **130개 / 13.988초 모두 통과**. `test_owned_agent_composition`, `test_agent_platform_budgets`, `test_agent_platform_runner`, `test_llm_configuration`, `test_agent_bootstrap`, `test_agent_mcp_settings`, `test_run_configuration`, `test_owned_agent_pipeline` 묶음이다. 실제 외부 Provider/Docker 대신 기존 명시적 fixture를 사용한다.
- `compileall`, `pip check`, `git diff --check`와 새 파일의 trailing whitespace 검사를 통과했다. pip cache의 기존 접근 경고는 있었으나 의존성 오류는 없었다.
- `git check-ignore configs/owned-runtime.local.json`으로 로컬 설정 파일의 제외 여부를 확인했다. 실제 로컬 설정·Key·Token 파일은 생성하지 않았다.
- 독립 코드 리뷰에서 찾은 Workspace 부모 파일·SQLite 보조 파일 충돌·새 DB 이름 alias·Model JSON 이름 상속 문제를 수정하고 재현 거절을 확인했다.
- 전체 unittest/팀원 pytest 평가를 재실행했다고 주장하지 않는다. 이번 검증은 신규 64개와 기존 관련 130개다.

```bash
PYTHONPATH=src:tests .venv/bin/python -m unittest tests.test_runtime_configuration -q
PYTHONPATH=src:tests .venv/bin/python -m unittest tests.test_owned_agent_composition tests.test_agent_platform_budgets tests.test_agent_platform_runner tests.test_llm_configuration tests.test_agent_bootstrap tests.test_agent_mcp_settings tests.test_run_configuration tests.test_owned_agent_pipeline -q
PYTHONPATH=src .venv/bin/python -m compileall -q src tests
.venv/bin/pip check
git diff --check
```

실제 외부 LLM·Container·회원가입 시연·보안 의미 검증·비용 집계가 완료된 단계는 아니다. API Key나 모델을 선택하지 않아도 설정 프레임워크 자체는 구현·검증할 수 있다.

## 9. 커밋 메시지와 다음 작업

커밋 메시지: `실제 Agent 실행용 Host 설정과 안전한 검증 명령어 추가`

다음 작업: **39번 — 승인된 Docker 실행 이미지와 Build/Test/Scanner 프로파일 준비**.

Git commit/push는 수행하지 않는다.
