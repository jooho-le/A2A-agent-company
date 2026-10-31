# 51. Single-Agent MCP 기능·단계·경로 권한 연결

작성일: 2026-10-31. 선행 작업: [50번 Prompt와 Context](50-single-agent-prompts-context.md).

## 1. 구현 내용

Single-Agent의 MCP 서비스 주체를 `SINGLE_AGENT`로 추가하고, 기존 MCP Client·stdio 서버·Dispatcher·실제 도구 Handler를 연결했다. 새 AI 역할이나 Agent 서버를 네 개 만드는 방식이 아니다.

[정책](../src/mcp_tools/core/policy.py)의 `SINGLE_TOOL_NAMES`는 기존 네 역할 도구의 합집합인 10개다. Shell·임의 명령·외부 네트워크·Host 파일 접근 기능은 추가하지 않는다. [Host 설정 함수](../src/agents/single/mcp.py)가 현재 업무 단계에 맞는 프로파일과 자체 테스트 범위를 선택한다.

## 2. 정체성과 단계별 권한

[MCPBinding](../src/mcp_tools/runtime.py)에 Single 정체성·Run·Workspace·`single_phase`를 불변으로 고정한다. `SinglePhase`는 공통 identity 모듈로 옮겼고 기존 `agents.single.contracts.SinglePhase` import도 유지한다.

| 단계 | 호출 가능한 기능 | 쓰기 범위 |
| --- | --- | --- |
| PLANNING | 기존 Planner와 같이 제품 도구 없음 | 없음 |
| IMPLEMENTING / FIXING | Source 읽기·쓰기·patch·Build·자체 Unit Test, QA/Security 결과 읽기 | `source/` |
| QA | Snapshot 읽기·자체 테스트 작성·Unit/Browser Test·테스트 결과 조회 | `outputs/qa/tests/` |
| SECURITY | Snapshot 읽기·자체 테스트 작성·Unit Test·보안 Scan·보안 결과 조회 | `outputs/security/tests/` |

합집합은 Run 전체의 기능 동등성이다. 각 단계에서 도구를 무제한 호출할 수 있다는 뜻은 아니다. MCP discovery는 전체 10개 계약을 유지하고, `single_phase_tool_definitions()`는 모델에 현재 단계의 부분집합을 제공한다. Client와 서버도 목록 표시와 별개로 직접 호출을 검사한다.

`--role SINGLE_AGENT --agent-role SINGLE_AGENT --single-phase <단계>`는 신뢰된 Host가 자식 프로세스 인자로 고정한다. 도구 인자의 `role`, `singlePhase`, `command`, `hostPath` 등으로 권한을 바꿀 수 없다. 기존 네 역할과 Orchestrator 전용 MCP 주체의 계약은 별도로 유지한다.

## 3. 실제 Handler와의 연결

외부 MCP binding과 모델의 `ToolContext`는 Single 정체성을 유지한다. 각 도구 내부에서는 기존 Workspace/Snapshot/보고서 권한 검사를 재사용하기 위해 고정된 업무 역할로 범위를 좁힌다. 예를 들어 QA 단계의 파일 도구는 QA 경로 규칙을, Security 단계의 Unit Test는 Security 검사 규칙을 적용한다.

이 내부 역할은 새 Agent가 아니라 도구의 접근 권한·검사 범주다. Handler Context의 `principal_binding`에는 원래 Single 정체성과 단계를 남긴다. 기존 도구 영수증의 역할 범주는 재사용하고, 실제 Agent 호출·전체 Trace/Usage 연결은 52·55번에서 Single 정체성과 연결한다. A2A에서 네 독립 Agent가 실행된 것처럼 기록하지 않는다.

서버는 각 호출 전에 실제 저장된 RunConfiguration이 `SINGLE_AGENT`인지, Run·Workspace가 일치하는지, 현재 Workflow 상태가 단계와 맞는지 확인한다. 종료되거나 검증 단계로 넘어간 Run에 예전 개발 단계 Client를 재사용해 Source를 고칠 수 없다.

## 4. Snapshot·파일·보호 테스트 경계

- QA/Security 단계는 Host가 선택한 Frozen Source 없이 구성할 수 없다. Source 읽기는 기존 SnapshotReader를 사용하며 변경 중인 Working Tree로 대체하지 않는다.
- 검사 도구의 `snapshotId`는 고정된 선택과 일치해야 한다. 검증 단계에서는 Source의 코드 버전과 Run의 현재 코드 버전도 확인해 이전 후보 재사용을 막는다.
- Source 쓰기·patch는 구현/수정 단계만 가능하다. QA/Security는 자기 테스트 폴더에만 쓸 수 있고 보고서 파일·상대 역할 폴더·Frozen Snapshot·보호 경로는 변경할 수 없다.
- 기존 Secret·경로 순회·symlink·CAS·Snapshot Hash 검사·Docker 격리와 출력 정제 규칙을 재사용한다. 생성된 Source나 테스트를 Host에서 실행하지 않는다.
- 모델용 Unit 설정에는 자체 테스트만 포함한다. Host 원본의 `PROTECTED` 범위는 별도 승인 설정에 남기고, Single 자식 설정에 직접 넣으면 Client 구성과 서버 CLI에서 거절한다.
- 보호 테스트를 모델에 주지 않는 것과 보호 검증을 생략하는 것은 다르다. Ver1의 Host 전용 보호 검사 및 동일한 피드백 정책은 실제 Host 연결 단계에서 연결해야 한다. 최종 공통 시험지는 계속 개발 순환 밖에서 채점한다.

도구 실행의 시간·실패·전송 불명확 여부는 기존 Client 계약을 따른다. 자동 재전송이나 새 Run 예산을 추가하지 않았다. `ToolContext`의 남은 deadline을 전달할 수 있으며 전체 Single Run의 예산·재개 연결은 후속 단계다.

## 5. 석민의 dev 변경 반영 범위

작업 시작 시 `jooho`에는 `dev`의 Security 테스트 도구 변경이 없었다. `dev` 커밋 `a99c59c`의 다음 MCP 구현 6개를 대조해 반영하고, 공통 정책에 같은 Security 도구를 추가했다. Git 브랜치·커밋 이력을 병합한 작업은 아니다.

| 파일 | 반영 내용 |
| --- | --- |
| `mcp_tools/tools/files.py`, `file_io.py` | Security가 자기 `outputs/security/tests/`에 테스트 작성 |
| `mcp_tools/tools/unit.py` | Security의 승인 Unit Test 실행 |
| `mcp_tools/tools/unit_config.py` | 선택적 `role=SECURITY` 범위. 기존 역할 생략 설정은 QA 의미와 직렬화 유지 |
| `mcp_tools/tools/unit_inputs.py` | Security scratch에서 테스트 입력 수집 |
| `mcp_tools/tools/unit_store.py` | Security Snapshot grant·활성 단계·실제 실행 근거·영수증 검증 |

전체 Security Agent의 측정 판정 모드와 Host factory를 모두 옮긴 것은 아니다. 그 코드는 실제 실행기를 연결할 52번에서 `dev`와 계속 대조해야 한다. 기존 50번의 Security Draft 옵션도 지원하지 않는 방식으로 조용히 대체하지 않는다.

회귀 검사에서 `dev`의 하위 파일 쓰기 함수가 Security 출력 영역 전체를 허용하는 차이를 발견했다. 필요한 테스트 작성 기능에 맞춰 이번 구현은 하위 함수에서도 `outputs/security/tests/`만 허용하도록 제한했다. 기존 보고서 경로 쓰기 거부 검사는 유지하고 정상 테스트 파일 쓰기 검사를 추가했다.

## 6. 검증 기록

검증에는 실제 임시 SQLite·Workspace·파일·Git Snapshot과 로컬 MCP stdio 자식 프로세스를 사용했다. 테스트용 Git 저장소는 임시 폴더에만 만들었으며 프로젝트 Git 이력은 변경하지 않았다.

`test_single_agent_mcp`는 다음을 확인한다.

- 네 역할의 전체 기능 합집합과 단계별 권한의 불변성.
- 실제 Source 쓰기·patch, 단계 변경 후 이전 Client의 호출 거부.
- 검증 단계의 Frozen Source 읽기와 QA/Security 테스트 폴더 분리.
- 다른 Run/Workspace/Snapshot·이전 코드 후보·추가 권한 필드 거부.
- Client 제한을 우회한 실제 stdio 직접 호출도 서버에서 Source 수정 거부.
- Security 자체 테스트 입력 수집·같은 Manifest의 실행 영수증·컨테이너 정리 경계. Docker는 모의 실행기를 사용하며 실제 Docker 검증은 아님.
- 테스트 FAIL이 도구 통신 실패나 프로젝트 최종 Verdict로 바뀌지 않음.
- 보호 테스트가 모델용 자식 설정에 포함되지 않고 Host 원본은 유지됨.

최종 관련 회귀 명령:

```bash
PYTHONPATH=src:tests .venv/bin/python -W error::RuntimeWarning -m unittest -q \
  test_single_agent_mcp test_single_agent_server test_single_agent_prompts \
  test_mcp_catalog test_mcp_runtime test_mcp_server test_mcp_client \
  test_mcp_client_failures test_mcp_stdio test_mcp_files test_mcp_file_io \
  test_mcp_file_snapshots test_mcp_patching test_mcp_build test_mcp_unit \
  test_mcp_unit_config test_mcp_unit_inputs test_mcp_unit_store test_mcp_unit_runner \
  test_mcp_browser test_mcp_security test_mcp_security_store \
  test_orchestrator_mcp_capability test_agent_mcp_settings \
  test_agent_role_contracts test_agent_role_outputs test_llm_runtime \
  test_runtime_configuration test_tool_configuration test_owned_host
```

최종 회귀 **963개가 모두 통과**했다. 실패·오류·건너뜀은 없으며 앞선 집중 검사와 합산하지 않았다. 새 문서 링크 4개(README 변경 부분 포함 6개), Markdown 형식, `git diff --check`, 변경 Python 파일 22개의 3.10 구문 파싱도 확인했다. 실제 테스트 인터프리터는 Python 3.11이며 Python 3.10 실행을 검증한 것은 아니다.

실제 외부 LLM·Docker·Single 제품 개발 Run·팀원 서비스·비교 실험은 실행하지 않았다. 의존성·SDK lock·개인 설정·자격증명·프로젝트 Git 브랜치/이력은 변경하지 않았다.

## 7. 개발정의서 점검

| 기준 | 반영 |
| --- | --- |
| §0·§8 MCP | 기존 MCP 2026-07-28 로컬 stdio·고정된 SDK/lock 유지 |
| §2·§11-A 단일 주체·도구 동등성 | 외부 주체 Single 유지, 전체 기능 합집합과 내부 ACL 구분 |
| §4 수정·재시도 | 기존 Fix 3회·Retry 2회 변경 없음. 자동 재전송 없음 |
| §5·§9 동일 Snapshot | Host 선택·읽기 전용 Source·현재 코드 버전 대조, 임의 후보 교체 차단 |
| §8 권한·격리·비밀정보 | Client/서버·역할/경로 검사, 보호 테스트 분리, 기존 Sandbox·Secret 정책 유지 |
| §10 성공 판정 | 실제 도구 결과와 제품 결과·최종 Verdict 구분. 도구 목록이나 연결 성공을 제품 SUCCESS로 해석하지 않음 |
| §11 추적 | 원래 Single principal 보존. 전체 Agent/Trace/Usage 연결은 후속 작업으로 명시 |

커밋 메시지: `싱글에이전트 MCP 도구와 단계별 권한 및 스냅샷 보호 연결`

다음 작업: **52번 — 실제 Single-Agent LLM 실행기와 Orchestrator 연결**. 현재 Single A2A 서버의 `executionReady=false`와 Single Run API의 503 거절은 유지한다.
