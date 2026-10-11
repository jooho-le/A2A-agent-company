# 41. 실행 설정 기반 Host factory 조립

작성 기준: 2026-10-11. 범위: 1번 Orchestrator + 2번 Agent/MCP. 팀원 제품 UI/API/DB와 독립 평가·비교 실험을 구현하거나 연결하지 않는다.

## 1. 이번에 연결한 것

38번 실행 설정, 39번 Tool 설정, 40번 승인 시작 Source를 기존 `create_platform`에 연결하는 명시적 Host factory를 추가했다.

- Orchestrator와 Planner·Developer·QA·Security의 기존 HTTP 앱·실제 실행기를 하나의 플랫폼으로 조립한다.
- 공통 모델/전체 예산, 서로 다른 다섯 DB·주소, 역할별 Bearer, 환경변수 API Key를 기존 설정에 연결한다.
- 등록된 최초 Run의 Planner claim 이후, A2A 전송 전에 승인 Source를 준비한다. 같은 Run deadline을 넘긴다.
- Developer에는 Build/Unit, QA에는 Unit/Browser와 불변 Snapshot, Security에는 Scanner와 같은 불변 Snapshot을 전달한다.
- Host의 실제 Tool 설정·보호 케이스를 불변 정책 Hash로 고정하고 같은 Run의 재개/재시작 때 변경을 거절한다.
- 실패·취소·종료 때 할당한 Provider를 끝까지 회수하고 한 번만 종료한다.

**구성 연결을 구현한 것이지 실제 제품 Build/Test/Scan·LLM 호출·시연 성공을 확인한 단계는 아니다.** 실제 승인 모델·이미지·시작 Repository·로컬 Credential을 임의로 선택하거나 만들지 않았다. 기본 Bootstrap과 기존 동기 `--factory` CLI도 변경하지 않았다.

## 2. 파일

| 파일 | 역할 |
| --- | --- |
| `src/agents/platform/host.py` | 읽기 전용 교차 검사, async factory, 승인 RunConfiguration 템플릿 |
| `src/agents/platform/host_services.py` | 기존 역할별 RuntimeServices와 실제 MCP 설정 조립, 공유 deadline |
| `src/agents/platform/host_policy_store.py` | 최초 claim 때 Host 정책 고정, 읽기 전용 재확인 |
| `src/agents/platform/run_host.py` | 기본 검사 / 명시적 서버 시작 CLI |
| `configs/owned-qa-cases.example.json` | 선택적 보호 QA 케이스 연결 예시. 실제 테스트나 판정 기준 아님 |
| `tests/test_owned_host.py` / `test_owned_host_cli.py` | 실제 임시 Git 기반 구성·Source 연결·입력·cleanup·CLI 회귀 |
| `tests/test_host_services.py` / `test_host_policy_store.py` | 역할/소유권/예산과 SQLite 정책 불변성 회귀 |

`README.md`를 갱신했다. 공식 A2A/Artifact/Trace Schema·Task 상태·MCP Tool 목록·ACL·SDK/의존성·팀원 코드는 변경하지 않았다.

## 3. 기본은 읽기 전용 검사

기존 로컬 Runtime·Tool·Source 설정을 준비한 뒤 프로젝트 루트에서 실행한다. 원본 예시는 미승인 `null` 값이 있으므로 그대로는 실패한다. `.local.json`은 기존 Git ignore 대상이다.

```bash
PYTHONPATH=src .venv/bin/python -m agents.platform.run_host --runtime configs/owned-runtime.local.json --tools configs/owned-tools.local.json --source configs/owned-source.local.json
```

성공 출력:

```json
{"status":"OWNED_HOST_CONFIGURATION_VALID","credentialCheck":"NOT_REQUESTED","executionReady":false,"DockerChecked":false,"workspacePrepared":false}
```

`--check-secrets`를 붙이면 선언된 process environment 비밀값의 존재/형식만 추가 검사하고 `credentialCheck=PRESENT`로 표시한다. `.env`를 자동으로 읽지 않는다. 값·이름·Host 경로·Source 원문을 출력하지 않는다.

검사는 설정 형식, 주소/DB 분리, 실제 시작 Git·Archive·Lock, 공통 환경, Tool 시간 한도, 보호 케이스 연결을 대조한다. DB·Workspace·Provider·서버·MCP·Docker를 만들거나 실행하지 않는다. Credential 존재는 API 인증 성공을, 설정 검사 통과는 이미지 존재나 제품 성공을 뜻하지 않는다.

Host 설정은 Workspace와 시작 Reference Repository 밖에 두어야 한다. DB와 SQLite sidecar도 Reference·설정·Lock 경로와 중첩/별칭이 될 수 없다. 기존 DB/sidecar hardlink는 보수적으로 거절한다. 실제 Lock 파일만 Reference 안에 둘 수 있다. 같은 OS 권한을 가진 Host의 모든 파일 교체 경쟁을 격리하는 Sandbox는 아니다.

## 4. 명시적 실행

승인 설정·이미지·시작 Repository와 환경변수 비밀값을 준비한 운영자만 실행한다.

```bash
PYTHONPATH=src .venv/bin/python -m agents.platform.run_host --runtime configs/owned-runtime.local.json --tools configs/owned-tools.local.json --source configs/owned-source.local.json --start
```

`--start`와 `--check-secrets`는 함께 사용하지 않는다. 시작 경로 자체가 필수 Credential을 확인한다. 시작/종료 한도는 기존 Runner 기준이며 `--startup-timeout`은 0.01~300초, `--shutdown-timeout`은 0.01~600초이다. 무한 대기값·NaN·Infinity는 거절한다. 이 한도는 Run runtimeBudgetMs를 대체하거나 cleanup 중인 Worker를 강제 유기할 권한이 아니다.

async factory는 승인 검사·Credential 확인 후 공통 Workflow DB와 네 Provider를 할당하고 기존 플랫폼을 반환한다. 이 함수만으로는 Agent DB·Workspace·Listener·MCP·Container·모델 호출을 시작하지 않는다. CLI의 기존 Runner가 다섯 포트를 사전 확보하고 Agent 네 서버를 준비한 뒤 Orchestrator를 연다.

한 프로세스/한 event loop로 실행한다. 별도 Agent 프로세스·reload·multi-worker는 공유 예산 연결 방식에 사용하지 않는다. 서버 시작은 Run을 자동 제출하지 않는다. **Run을 제출하면 실제 외부 API 비용과 Docker 실행이 발생할 수 있다.** 이번 작업에서는 `--start`와 유료 호출을 실행하지 않았다.

기존 앱의 `/docs`와 `/api/v1/scenarios`로 API/등록 시나리오를 확인한다. `/api/v1/runs`의 `configuration`은 승인 설정을 명시해야 한다. 서버가 누락값을 나중에 채워 동결 설정을 바꾸지 않는다.

신뢰된 Python의 async Host 함수 내부에서 템플릿을 얻는 방법:

```python
from agents.platform.host import approved_run_configuration, create_host

platform = await create_host(
    runtime_file="configs/owned-runtime.local.json",
    tools_file="configs/owned-tools.local.json",
    source_file="configs/owned-source.local.json",
)
try:
    configuration = approved_run_configuration(platform).model_dump(mode="json", by_alias=True)
    # 기존 POST 요청의 configuration 필드에 넣을 공개 템플릿.
    # 여기서는 Run 제출·서버 시작을 하지 않는다. Key/Host 경로는 포함하지 않는다.
finally:
    await platform.aclose()
```

모델·Fix/Retry/시간 예산·Image/Lock/Hardware/Network·시작 Commit/Snapshot·보호 Suite·Scanner 참조가 승인값과 다르면 Run 저장/스케줄 전 422 `OWNED_AGENT_CONFIGURATION_INVALID`이다. 기존 선택적 실험 metadata는 canonical 계약대로 허용하되, 이것만으로 비교 실험 조건을 승인하지 않는다.

## 5. 보호 QA 케이스 연결

39번 QA Unit/Browser에 `PROTECTED` selector가 있으면 대응하는 Host 케이스 설정이 필수다.

```bash
PYTHONPATH=src .venv/bin/python -m agents.platform.run_host --runtime configs/owned-runtime.local.json --tools configs/owned-tools.local.json --source configs/owned-source.local.json --qa-cases configs/owned-qa-cases.local.json
```

형식은 `schemaVersion: 1`과 `cases` 배열이며, 각 케이스는 `toolName`, `selector`, `testId`, `requirementId`, `title`, `expectedResult` 여섯 필드를 가진다. 기존 `QACaseBinding`의 UUIDv4·문자열·Selector·Secret 검증을 그대로 사용한다. 최대 256개이며 중복 Tool/Selector/Test ID 조합을 거절한다.

- 모든 보호 Tool/Selector에 케이스가 있어야 하며 보호되지 않은 Selector에 Host 케이스를 붙이지 않는다.
- 보호 Suite 참조는 하나로 일치하고 Scanner 프로필도 같은 참조를 사용해야 한다. 서로 다른 실제 Version/Rule은 정책 Hash에 포함한다.
- `requirementId`는 기존 등록 시나리오/해당 Run의 요구사항 ID와 맞춰야 한다. 생성 코드나 모델이 임의로 기준을 재정의하지 않는다.
- 실제 보호 테스트 bytes와 Suite는 기존 39번 Tool 설정에 둔다. 이 파일은 연결 정보일 뿐 새 테스트 코드·제품 평가 기준을 제작하지 않는다.
- 빈 예시는 보호 Selector가 없는 설정에서만 통과한다. 이를 보호 테스트 수행/요구사항 충족 증거로 주장하지 않는다.

실제 Run/Requirement 소속과 케이스 결과는 기존 QA 서비스가 실행 시 다시 검증한다. 해당 시나리오에 맞지 않는 Host 케이스를 자동 변환하거나 조용히 제거하지 않는다.

## 6. 같은 Run 정책과 예산

공개 RunConfiguration에 없는 Build argv·Tool timeout/resource·보호 bytes/case·Scanner Version/Rule도 재시작 때 바뀌면 안 된다. 승인 canonical 설정의 SHA-256만 private `run_host_policies`에 저장한다. Credential 원문·Source·정책 JSON을 이 테이블에 저장하지 않는다.

최초 고정은 유일한 Planner Step이 RUNNING인 초기 PLANNING Run에서 A2A 전송/Context/Artifact/Tool 실행 전에만 허용한다. 등록 후 preparation이 실패해도 고정된 Hash를 삭제하거나 바꾸지 않는다. 이미 고정된 같은 Hash는 재확인할 수 있지만 다른 Hash·늦은 자동 고정·누락 기록은 거절한다. 기존 기록이 없는 Legacy Run을 이번 Host로 자동 이관하지 않는다. 조회는 SQLite 읽기 전용으로 수행하여 누락 DB/테이블도 재생성하지 않는다. UPDATE/DELETE/REPLACE도 차단한다.

최초 Source 준비, Developer/QA/Security 서비스 발급, 실행을 수반하는 control preflight에 같은 정책 검사를 연결했다. 기존 Control의 GET-only 결과 회수와 불확실한 Send 처리·전체 예산 복원 정책은 변경하지 않았다. 새 canonical Workflow 상태나 공개 Trace 이벤트를 만들지 않는다.

Source 준비는 기존 `_host_call`을 통해 같은 deadline을 전달하고 취소된 동기 Worker도 끝까지 회수한다. MCP 연결/검색·Tool 세션·종료도 같은 Run deadline을 쓰되 AnyIO context의 enter/yield/exit는 같은 Task에서 실행한다. Tool의 승인 `maxCallSeconds`를 매번 남은 시간으로 덮어쓰지 않는다. Runtime Tool timeout과 이 값이 처음부터 같아야 한다. Deadline 소진은 기존 BUDGET 오류 경로를 사용한다.

취소/한도 초과 시 이미 진행 중인 OS I/O·DB lock·child cleanup을 끝까지 회수할 수 있으므로 전체 벽시계 시간보다 종료가 늦어질 수 있다. 정확한 강제 종료 Sandbox라고 주장하지 않는다. 기존 불명확한 Source write/Tool 물리 시도/Send를 자동 재실행하지 않는다.

## 7. Provider와 보안 증거 경계

기존 `provider_from_settings`의 OpenAI Responses **직접 REST/httpx 어댑터**를 재사용한다. 새 SDK나 모델을 선택하거나 자동 API 재시도를 추가하지 않았다. OpenAI Docs 스킬로 서버 환경변수 Key와 명시적 클라이언트 종료 기준을 확인했으며, Host에 secret binding과 비동기 종료 소유권을 명시하는 데 반영했다. [OpenAI API Quickstart](https://developers.openai.com/api/docs/quickstart), [공식 Python Client 종료 안내](https://developers.openai.com/api/reference/python)

factory 실패 전에는 생성한 Provider를 Host가 회수하고, 조립 성공 이후에는 기존 `OwnedAgentPlatform.aclose()`가 소유한다. 일부 Provider 생성 실패·조립 후 오류·중복 Provider·반복 cancel에서도 이미 만든 자원을 한 번씩 끝까지 닫는다. 정상 CLI 종료는 기존 Runner의 Orchestrator dispatch → Agent Worker/MCP → Provider 정리 순서를 따른다.

기본 CLI의 Security `proof_verifier`는 `None`이다. 기존 Scanner 실행 결과만으로 의미 취약점을 확정하거나 PASS로 변환하지 않으며, 증거 부족은 `UNVERIFIED/SUSPECTED`와 최종 `HUMAN_REVIEW` 경로를 유지한다. 신뢰된 Python Host만 기존 verifier callback을 주입할 수 있고 JSON/모델/CLI로 임의 모듈을 import하지 않는다.

이번 Hash의 semanticProof 값은 verifier의 **유무만** 나타낸다. 실제 의미 기준·verifier 버전·증거 소속을 고정한 기능이 아니므로 그 완료를 주장하지 않는다. 이를 구현하고 실제 신뢰 증거에 연결하는 것이 42번이다. 역할 4의 독립 테스트/판정 기준 개발은 별도다.

후속 변경: [42번 승인된 의미 검증 연결](42-approved-security-semantic-proof.md)에서 현재 factory는 raw callback 대신 승인된 descriptor만 받고 기준·구현/버전·요청/Receipt 소속까지 확인한다. 위 문단은 41번 시점의 경계이며 기본 `None`/미검증 동작은 유지한다.

## 8. 개발정의서 준수 점검

| 정의서 기준 | 확인 |
| --- | --- |
| §0·§3·§6·§7 A2A/상태 | 기존 공식 Protocol·Agent Card·Task·Message·Artifact·상태·폴링 계약 유지 |
| §2 책임 경계 | 역할 1·2의 실행 연결만. 제품 UI/API/DB·독립 평가/비교 제외 |
| §4 Fix/Retry | 최대 Fix 3회·MCP Retry 2회, 불명확한 작업 자동 재실행 금지 유지 |
| §5·§9 Snapshot/Version | 승인 초기 Source는 Candidate/Artifact가 아님. QA/Security는 같은 불변 SOURCE 선택 |
| §8-7·8-8 ACL/Secret | 실제 저장 Step 역할/Run/Workspace 소유권 재검사, 기존 ACL·Secret 없는 MCP 환경 유지 |
| §8-9·8-10 실행 격리 | 기존 승인 일회용 Container 경로 사용. Host Build/Test fallback·자동 설치 없음 |
| §10 최종 판정 | 설정 검사/서버 시작/Task COMPLETED를 SUCCESS로 바꾸지 않음. Security 의미 증거 부족 유지 |
| §11·11-A 재현성 | 공개 승인 기준 + private Tool 정책 고정, 같은 예산/누적 사용량/UTC deadline 복원 유지 |

보류한 URL Credential 보호 및 공유 Agent DB 초기화 경쟁은 수정하지 않았다. 실제 이미지·LLM 인증/모델 동작·Docker Build/Test·서비스 시연, 43번 실측 비용, 44번 실제 smoke, 역할 4 비교 실험을 완료로 표시하지 않는다.

## 9. 검증

임시 실제 Git/SQLite/Source와 합성 Provider·in-process HTTP·MockTransport·가짜 MCP/Container를 사용한 자체 회귀다. 로그의 OpenAI/Agent URL은 모의 요청이며 실제 유료 호출이나 Listener 실행의 증거가 아니다. 팀원 pytest 전체 평가를 수행하지 않는다.

```bash
PYTHONPATH=src:tests .venv/bin/python -m unittest test_owned_host test_owned_host_cli test_host_services test_host_policy_store -q
PYTHONPATH=src:tests .venv/bin/python -m unittest test_runtime_configuration test_tool_configuration test_starting_source test_owned_agent_composition test_workflow_repository test_openai_responses -q
PYTHONPATH=src:tests .venv/bin/python -m unittest test_owned_agent_pipeline test_owned_agent_fix_pipeline test_agent_platform_budgets test_owned_agent_controls -q
PYTHONPATH=src:tests .venv/bin/python -m unittest test_qa_services test_security_agent_services test_llm_configuration -q
PYTHONPATH=src .venv/bin/python -m compileall -q src tests
.venv/bin/pip check
git diff --check
```

최종 검증 결과는 아래에 기록한다. 재실행을 중복 합산하지 않는다.

- 신규 Host/CLI/Services/Policy **107개 / 21.098초 PASS**. 실제 임시 Git에서 승인 Source를 준비하고 같은 Run 정책을 고정하는 경로까지 검증했다. 모의 모델/전송을 사용하며 외부 Agent 호출은 하지 않는다.
- 기존 Runtime/Tool/Source 설정·플랫폼 조립·Workflow 저장·OpenAI MockTransport **241개 / 33.265초 PASS**.
- 기존 초기/수정 Pipeline·공유 RunBudget·실제 제어 연결 **43개 / 147.540초 PASS**. in-process Agent/A2A와 합성 Provider/Tool을 사용했다.
- 기존 QA/Security 서비스·LLM 설정 **61개 / 22.406초 PASS**.
- 합계 **452개 모두 통과**. 실제 외부 LLM·Docker·제품 코드는 실행하지 않았다.
- `compileall`, `pip check`, `git diff --check`와 신규 파일 trailing whitespace 검사 통과. pip cache 권한 경고는 있었지만 의존성 오류는 없다.
- `git check-ignore`로 `configs/owned-qa-cases.local.json` 제외를 확인했다. 실제 로컬 설정·승인 Repository·이미지를 생성하지 않았다.
- 미승인 원본 예시를 통합 CLI에 전달하면 exit 1 + `OWNED_HOST_CONFIGURATION_INVALID`이다. 의도된 검사 실패이며 실행 readiness를 주장하지 않는다.
- 독립 리뷰에서 비정규 primitive의 bool/int 동등 비교, 조립 이후 종료 소유권, CLI 취소/오류 우선순위, 자체 deadline과 원래 transport timeout 구분을 보완하고 회귀를 추가했다.

## 10. 커밋 메시지와 다음 작업

커밋 메시지: `실행 설정 기반 Host factory와 역할별 Agent·MCP 연결 추가`

다음 작업: **42번 — SecuritySemanticProof 신뢰 증거 연결**. 기본 UNVERIFIED를 임의 PASS로 바꾸지 않고 승인 기준·버전·증거 소속을 검증하는 Host 연결을 구현한다. 역할 4의 독립 평가 기준과 제품 통합은 포함하지 않는다.

프로젝트 Git commit/push는 수행하지 않는다. Git 생성/Commit은 자동 정리되는 임시 테스트 fixture에 한정한다.
