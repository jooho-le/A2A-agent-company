# 23. MCP stdio 서버·공통 Schema·역할별 Tool 권한

> 범위: 1번+2번 담당 중 MCP 통신 기반 및 서버 권한 강제. 3번 제품 웹 개발·4번 독립 평가/비교 실험 및 해당 연동은 변경하지 않는다.
>
> 실제 MCP 자식 프로세스 통신을 구현했다. 실제 파일 Read/Write/Patch는 24번, Build/Test/Scan은 25~28번이다. 미구현 Tool은 오류로 응답하며 제품 성공이나 PASS를 만들어 내지 않는다.

## 1. 이번에 구현한 것

- 공식 Python SDK v2의 Server/Client/stdio Transport를 이용한 로컬 Child Process 실행.
- MCP `2026-07-28` 고정, `server/discover`, `tools/list`, `tools/call` 진입점.
- 정의서 §8의 10개 Tool 입출력 JSON Schema 및 변경 불가능한 역할 정책.
- 목록에 없는 Tool을 직접 호출하는 우회까지 서버에서 차단.
- 신뢰된 Host가 선택한 Role·Run·Workspace 고정 및 Agent/MCP Role 일치 검사.
- 호출마다 실제 Workspace Registry·Run 소유권·준비 Marker를 재검증.
- Protocol Error와 Tool Execution Error 분리, 안전한 오류 코드만 반환.
- 크기 제한·UTF-8·중복 JSON Key·비정상 숫자·잘못된 취소 요청의 프레이밍 검사.
- 선택적으로 연결할 수 있는 LLM `ToolExecutor` 어댑터. 기본 Agent에는 아직 연결하지 않음.

설정/계약/Dispatcher/Client 구성만으로 DB·Workspace·자식 프로세스를 만들지 않는다. `open_mcp_client()`를 명시적으로 열 때 자식 프로세스가 시작되며, CLI는 Host가 지정한 기존 DB만 사용한다. Workspace를 자동 발급하거나 provision하지 않는다.

## 2. 프로토콜과 실행 방식

`pyproject.toml`에 `mcp>=2.3,<3`을 추가했고 `uv.lock`에서 `mcp==2.3.0`, `mcp-types==2.3.0` 및 관련 의존성을 고정했다. 프로젝트 Python 최소 버전 `>=3.10`은 유지했다. 기존 `httpx`를 제거하거나 v2로 교체한 것은 아니다.

2026-07-28 stdio에서는 예전 `initialize` → `notifications/initialized` handshake를 사용하지 않는다. 각 요청의 `params._meta`에 protocol version/client capabilities가 포함되는 최신 방식을 따른다. 프로젝트 Client는 고정 버전을 채택한 뒤 실제 `server/discover` 요청으로 상대 버전을 확인한다. 서버는 이전 프로토콜·누락된 metadata·`initialize`를 거부한다. 공식 기준은 [MCP 2026-07-28 stdio 명세](https://modelcontextprotocol.io/specification/2026-07-28/basic/transports/stdio)와 [공식 Python SDK](https://github.com/modelcontextprotocol/python-sdk)를 참고했다.

SDK의 고수준 자동 협상/재호출 대신 공개 Session API로 한 번씩 요청한다. 버전 fallback, HEADER_MISMATCH 재전송, input-required 반복, 자동 Tool Retry를 추가하지 않았다. 실제 재시도 정책은 29번에서 다룬다.

Host가 고정한 현재 Python 실행 파일·프로젝트 모듈·역할·Run/Workspace·DB/Workspace Root·호출 제한 시간만 런처 인자로 사용한다. 모델은 실행 파일·모듈·환경변수·argv·Host 경로를 지정하지 못한다. `-I` isolated mode와 고정 bootstrap으로 프로젝트 패키지를 로드하며, SDK가 기본 상속하는 환경변수도 안전한 값으로 덮어쓴다. Agent API Key/사용자 Shell/Python 설정 등을 MCP 자식에게 전달하지 않는다.

서버 stdout은 newline-delimited JSON-RPC 전용이다. 런처는 stderr를 버리고 CLI 실패는 일반 코드만 출력한다. stdin EOF로 정상 종료하고 SDK가 자식 프로세스를 회수한다. 정상 종료·오류·단일 취소 후 실제 자식 종료를 검증했다. SDK AnyIO 종료 보호가 반복적인 native `Task.cancel()`까지 완전하게 방어한다고 보장하지 않는다.

## 3. 역할별 목록과 직접 호출 강제

| 역할 | `tools/list`에 노출하는 계약 |
| --- | --- |
| Planner | 없음. 필요할 때 별도 문서 Read 계약을 정의해 추가 |
| Developer | `read_project_file`, `write_source_file`, `apply_patch`, `run_build`, `run_unit_tests` |
| QA | `read_project_file`, `write_test_file`, `run_unit_tests`, `run_browser_tests`, `read_test_report` |
| Security | `read_project_file`, `run_security_scan`, `read_security_report` |
| Orchestrator — Host 전용 관리 주체 | `run_build`, `read_test_report`, `read_security_report` |

`MCPBinding`은 Host에서 고정한 두 역할이 같을 때만 생성할 수 있다. 요청 metadata나 Tool arguments로 역할을 변경할 수 없다. QA가 Developer의 `write_source_file`을 직접 호출해도 JSON-RPC `-32602` 오류가 된다.

23번 최초 구현에서는 Orchestrator의 직접 MCP 호출을 생략했지만, 개발정의서 §8-3·§8-11과 차이가 있어 1~37번 전체 점검 후 보완했다. 현재는 기존 네 `AgentRole`을 유지하고 별도 `MCPHostPrincipal.ORCHESTRATOR`를 제공한다. Host가 고정한 Run·Workspace에서 위 세 Tool만 호출할 수 있으며 Agent identity는 없다(`agent_role=None`).

서버가 Tool별 고정 Workspace/Artifact 권한을 적용해 기존 Build·QA/Security Report 검사를 그대로 수행한다. 소스 Read/Write·Patch·Test 실행·Scan 실행 권한은 추가하지 않는다. LLM Tool adapter와 Agent 전용 Tracked 실행 원장에는 Host binding을 연결할 수 없다. 기본 파이프라인에 Build를 한 번 더 실행하는 동작이나 새로운 LLM 역할도 추가하지 않았다. 현재 사용법·안전 경계는 [전체 점검 후 보완](contract-compliance-audit-fixes.md)을 참고한다.

현재 목록은 실제 작업이 완성됐다는 표시가 아니라 계약 선언이다. 각 Tool의 `_meta["a2a-agent-company/implemented"]`는 기본 서버에서 `false`다. 역할·입력·Workspace 검사를 통과해도 Handler가 없으면 `TOOL_NOT_IMPLEMENTED`로 실패한다.

## 4. 입출력 Schema

모든 Input은 `workspaceId: UUID`를 요구한다. Input/Output의 최상위 객체는 `additionalProperties: false`, dialect는 JSON Schema 2020-12다. 기존 필드 이름을 변경하지 않고 아래 계약을 선언했다. MCP Tool/outputSchema의 공식 의미와 오류 구분은 [MCP Tools 명세](https://modelcontextprotocol.io/specification/2026-07-28/server/tools)를 참고했다.

| Tool | 공통 workspaceId 외 Input | Output |
| --- | --- | --- |
| `read_project_file` | `path` | `path`, `content`, `sha256`, `sizeBytes` |
| `write_source_file` | `path`, `content`, `expectedSha256?` | `path`, `sha256`, `sizeBytes`, `changed` |
| `write_test_file` | `path`, `content` | `path`, `sha256`, `changed` |
| `apply_patch` | `patch`, `baseSnapshotSha256` | `changedFiles`, `newHashes` |
| `run_build` | `snapshotId` | `exitCode`, `stdoutRef?`, `stderrRef?`, `durationMs`, `executionManifestId` |
| `run_unit_tests` | `snapshotId`, `testScope` | `total`, `passed`, `failed`, `skipped`, `reportRef`, `executionManifestId` |
| `run_browser_tests` | `snapshotId`, `testSuite` | `total`, `passed`, `failed`, `traceRefs`, `executionManifestId` |
| `run_security_scan` | `snapshotId`, `scannerProfile` | `findings`, `reportRef`, `executionManifestId` |
| `read_test_report` | `reportRef` | `testResult` |
| `read_security_report` | `reportRef` | `securityResult` |

`run_build`의 stdout/stderr Reference 선택 여부는 정의서 §8-5의 required 목록을 그대로 따른다. testScope/testSuite/scannerProfile의 실제 승인 목록 및 Report 내부 상세 구조는 후속 Tool/Profile 계약으로 정하며 임의의 제품 검사 기준을 새로 만들지 않았다. `testResult`, `securityResult`, `findings[]`는 유한한 크기/깊이의 JSON 객체다. `newHashes`는 상대 경로→SHA-256 동적 사전이다.

Source 문자열은 최대 UTF-8 1MiB, 경로/Reference는 4096자, Tool JSON은 8MiB 제한을 적용한다. Source의 제어 문자 JSON escape overhead를 고려해 전체 JSON을 파일 크기와 같은 1MiB로 제한하지 않았다. stdio frame은 envelope 여유 64KiB를 더한 상한을 적용한다. 비유한 숫자·중복 Key·과도한 깊이/노드는 SDK 파싱 전에 차단한다. 크기/시간 숫자는 이번 구현의 제한값이며 프로젝트 Build 시간을 측정해 확정한 운영 수치는 아니다.

## 5. Workspace·Source·오류 경계

Tool `workspaceId`를 Host Path로 사용하지 않는다. 바인딩된 Workspace ID와 다르면 `PERMISSION_DENIED`이며, 실제 Registry가 같은 Run에 발급한 Workspace인지 다시 확인한 후 역할별 `BoundWorkspace` capability를 Handler에 전달한다. `read/write` path는 기존 역할/상대 경로 정책을 적용한다. `.env` 등 Secret Path, Absolute Path, Traversal, QA의 Source Write는 거부한다.

`reportRef`는 `artifact://UUID/상대경로` 문법만 먼저 검사한다. 실제 Artifact 소유권·Report 존재 여부·동결 Snapshot 선택·파일 bytes/hash 검증은 21번 Store를 이용해 24~28번 실제 Handler에서 연결해야 한다. 이번 단계의 일반 Source Read 정책만으로 QA/Security가 Frozen Snapshot을 검사했다고 표시하지 않는다.

Source-aware 검사를 사용해 정상 제품 코드의 `password` 변수 같은 표현을 임의로 바꾸지 않는다. 알려진 credential literal을 입력할 때는 거부하고, 일반 결과는 민감정보 정제 및 출력 Schema 재검증 후 반환한다. 모든 가능한 비밀 문자열을 완벽하게 탐지한다는 보장은 아니다.

| 상황 | wire 응답 |
| --- | --- |
| 미등록/역할 외 Tool, Schema 오류 | JSON-RPC `error`, code `-32602`, 일반 메시지 |
| 잘못된 JSON/Envelope/Version | 일반 Protocol Error. 원문/예외/Host 경로를 응답하지 않음 |
| Workspace/경로/Secret 거부, 작업 Handler 없음, 실행 실패/시간 초과 | `resultType: complete`, `isError: true`, 안전한 Text 오류 코드 |
| 정상 Handler 결과 | `isError: false`, Schema 검증된 `structuredContent`, Text는 고정 `TOOL_COMPLETED` |

성공 Text에 Source JSON을 중복으로 싣지 않는다. Tool 호출 성공은 작업 결과 객체를 얻었다는 뜻이며 제품 QA PASS·최종 Verdict가 아니다. File/Build/Report별 상세 오류 코드와 실행 근거/안전 Retry는 실제 Handler 구현 및 29번에서 추가한다.

서버의 SDK 기본 Tool OpenTelemetry middleware는 검증 전 Tool 이름·요청 ID·예외 원문을 Trace에 넣을 수 있어 제거했다. SDK diagnostics를 Source/Tool 입력 로그로 이용하지 않는다. 프로젝트의 영속 MCP Trace·사용량·마스킹은 37번 후속 작업이다. 이번에는 Source 전체를 Trace에 저장하지 않는다.

## 6. 연결 지점

```python
from mcp_tools.client import MCPChildConfiguration, MCPClientError, open_mcp_client
from mcp_tools.runtime import MCPBinding
from orchestrator.domain.states import AgentRole

# Host가 기존 Run/Workspace Registry에서 받은 값과 경로를 사용한다.
# run_id/workspace_id/database_path/workspace_root는 모델 입력이 아니다.
binding = MCPBinding(
    role=AgentRole.DEVELOPER,
    agent_role=AgentRole.DEVELOPER,
    run_id=run_id,
    workspace_id=workspace_id,
)
configuration = MCPChildConfiguration(
    binding=binding,
    database_path=database_path,
    workspace_root=workspace_root,
    max_call_seconds=60,
)

async def inspect_contracts():
    async with open_mcp_client(configuration) as client:
        names = [tool.name for tool in client.list_tools()]
        try:
            await client.call_tool("read_project_file", {
                "workspaceId": str(workspace_id), "path": "source/main.py",
            })
        except MCPClientError as error:
            # 현재 실제 read Handler 없음: MCP_CLIENT_TOOL_FAILED.
            # 원문 Tool 응답/Source/Host 경로 대신 안전한 코드만 취급한다.
            return names, error.code
```

`client.list_tools()`는 상대가 임의로 보낸 annotations/instructions를 권한으로 신뢰하지 않고 로컬 계약으로 LLM ToolDefinition을 생성한다. 연결 과정에서 실제 서버의 목록·Schema를 로컬 계약과 비교한다. `client.execute()`는 19번 ToolExecutor 인터페이스에 맞춰 Role·Workspace·ToolCall 인자·deadline을 검사하지만, 역할 Agent가 이를 연결하는 작업은 30~34번이다.

신뢰된 Host 코드만 Dispatcher에 async Handler를 등록할 수 있다. 이 주입 인터페이스는 모델이 Host 코드를 실행하는 통로가 아니며 취소에 협조하는 내부 Handler를 전제로 한다. Build/Test/Scan Handler는 반드시 22번 Container Sandbox를 사용해야 한다. 무한 대기/취소 무시 Host callback을 OS 수준으로 강제 종료하는 기능은 아니다.

## 7. 변경 파일

| 파일 | 역할 |
| --- | --- |
| `src/mcp_tools/core/catalog.py` | 10개 Input/Output Schema, 유한 JSON 및 Source bytes 검사 |
| `src/mcp_tools/core/policy.py` | 기존 역할 정책 유지, 설명 갱신 |
| `src/mcp_tools/runtime.py` | Host 바인딩, 권한/Registry 재검증, Handler 결과/시간/취소 |
| `src/mcp_tools/server.py` | 공식 SDK Server 어댑터, 발견/호출 및 안전한 오류 |
| `src/mcp_tools/stdio.py` | bounded framing, stdout 분리, 잘못된 요청 선검사 |
| `src/mcp_tools/__main__.py` | 명시적 Host CLI 시작점 |
| `src/mcp_tools/client.py` | 고정 stdio 자식 런처, 고정 버전 확인, 선택형 ToolExecutor |
| `tests/test_mcp_*.py` | 계약·정책·서버·프레이밍·실제 자식 통신 검증 |
| `pyproject.toml`, `uv.lock`, `README.md` | SDK 의존성 고정 및 현재 상태 안내 |

## 8. 개발정의서 점검

| 기준 | 이번 구현/남은 경계 |
| --- | --- |
| §2 책임 분리 | MCP는 권한·작업 결과만 처리. 최종 Verdict/제품 구현·독립 평가는 추가하지 않음 |
| §8-1 Protocol/SDK/Transport/Schema | 2026-07-28, 공식 v2, local stdio, JSON Schema 2020-12 |
| §8-2 Workspace | 발급된 UUID+Run binding 및 실제 Registry 재검증. Tool 입력의 Host Root 금지 |
| §8-3~5 Tool 계약 | 이름/필드 유지, optional 필드 보존. 실제 작업은 24~28번 |
| §8-6 오류 구분 | JSON-RPC error와 isError Tool Result 분리 |
| §8-7/8 경로·민감정보 | 기존 ACL 경계 재사용, Source-aware 처리 및 안전한 일반 오류. 실제 파일 Handler/영속 Trace는 후속 |
| §8-9/10 Sandbox | Shell/Host 생성 코드 실행/Network 우회 없음. Build/Test/Scan은 다음 Handler에서22번 연결 |
| §8-11 역할별 권한 | 목록과 직접 호출에서 별도 강제. QA Source Write 차단 |
| §4 한도 | 수정 최대3회·MCP Retry 최대2회 기존 정책 변경 없음. 이번 Client는 자동 재시도하지 않음 |
| 담당 범위 | backend/frontend/evaluation 및 기존 Agent 실행 준비 상태 변경 없음 |

23번 구현 범위의 점검이며 1번+2번 전체 완료 선언이 아니다. QA/Security의 동일 Frozen Snapshot 실물 검사·MCP 실행 근거·실제 LLM 역할 실행·전체 Trace는 후속 번호에서 계속 점검한다. 기존에 사용자가 제외한 비밀번호 보호 정책 보완을 이번에 확대 적용하지 않았다.

## 9. 검증 결과와 제한

검증 명령:

```bash
PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -p 'test_mcp_*.py' -q
PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -q
.venv/bin/pip check
.venv/bin/uv lock --check --offline --python .venv/bin/python --cache-dir /private/tmp/a2a-uv-cache
git diff --check
```

새 MCP 테스트는 Catalog31 + Runtime41 + Client41 + Server31 + Framing13 = **157개**다. 실제 SQLite/Workspace를 사용한 역할·Run/Marker 변조, Tool Schema/민감정보·잘못된 입력, 시간/취소 및 실제 stdio 자식 통신/종료를 포함한다. 정상 작업 Handler 검사는 신뢰된 테스트용 Handler를 사용했으며 실제 제품 코드 실행을 의미하지 않는다.

최종 MCP 전용 **157개 모두 통과**(6.234초), 제한 밖에서 재실행한 전체 unittest **875개 모두 통과**(47.203초: 기존718 + 신규157). `compileall`, `pip check`, offline Lock 일치 검사 및 `git diff --check`도 통과했다. pytest 함수 기반 평가 테스트 전체를 실행한 것은 아니다.

- 초기 샌드박스 실행에서는 기존 Agent DB 동시 초기화 `PRAGMA journal_mode=WAL`의 `database is locked`와 Unix 소켓 생성 권한 오류가 관찰됐다. MCP 코드 수정으로 이 기존 DB 경쟁 문제를 해결했다고 주장하지 않는다. 소켓 검증은 권한을 받아 제한 밖에서 전체 재실행했다.
- 기존 A2A SDK의 event-stream/종료 큐 경고는 그대로 유지한다.
- 실제 외부 LLM 호출·Docker 격리·제품 Build/Test/Scan·회원가입 시연·독립 평가/비교 실험은 이번 검증에서 수행하지 않았다.
- 현재 실제 stdio 구현은 POSIX 환경용이며 macOS에서 확인했다. 임의 원격/제3자 MCP 서버용 범용 Client나 Windows stdio 지원을 제공한다고 표시하지 않는다.

## 10. 다음 작업

다음 작업: **24번 — 파일 Read/Write/Patch Tool.** 이번 계약과 권한 경계에 실제 파일 처리·크기/Hash·Write Conflict·Patch base 검증·Frozen Snapshot Read 및 파일별 오류를 연결한다.

커밋 메시지 제안: `MCP stdio 서버와 Tool 계약 및 역할별 권한 강제 구현`

Git commit/push는 직접 수행하지 않는다.
