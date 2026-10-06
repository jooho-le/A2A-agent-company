# 15. Agent/MCP 패키지 및 실행 설정 기반

> 범위: 1번 Orchestrator의 기존 구현을 유지하면서 2번 Agent/MCP 개발 기반 추가.
> 기준: 사용자 개발정의서, 기존 Project Schema 및 01·14번 계약.
> 이번 단계는 실행 설정과 패키지 구조만 구현한다. 실제 서버·LLM·MCP Tool 실행 완료를 뜻하지 않는다.

## 1. 개발한 내용

- Agent의 공통 설정·A2A 계약과 향후 API/LLM/역할 구현 패키지를 분리했다.
- MCP의 공통 설정·역할별 Tool 정책과 향후 Tool 구현 패키지를 분리했다.
- 기존 `AgentRole`과 Tool 재시도 상수, A2A SDK의 공식 프로토콜 상수를 재사용한다. 새 공식 Task/Message/Artifact 모델이나 Schema를 복제하지 않는다.
- `AGENT_`, `MCP_` 환경변수를 추가했다. 기존 `ORCHESTRATOR_` 설정·DB·호출 조건은 변경하지 않았다.
- 역할 미설정·잘못된 역할·지원하지 않는 MCP 전송/버전·잘못된 포트는 설정 단계에서 거부한다. 설정 객체는 생성 후 불변이다.
- API Key와 Bearer Token은 `SecretStr`로 보관하고 일반 `repr`, `model_dump`, `model_dump_json`에서 제외한다. ValidationError의 문자열에는 입력값을 표시하지 않는다. 원본 `errors()`/`json()`을 로그·응답으로 그대로 내보내는 것은 별도 문제이므로 후속 오류 경계에서도 입력 제외·마스킹을 적용해야 한다.
- 설정 import/생성은 서버, DB, Workspace, LLM 요청 또는 MCP subprocess를 시작하지 않는다.

## 2. 패키지 구조

```text
src/
├── orchestrator/             # 기존 1번 구현, MCP를 직접 호출하지 않음
├── agents/
│   ├── core/
│   │   ├── config.py         # AgentSettings, 모델 선택 누락 검사
│   │   └── contracts.py      # A2A SDK 상수, 역할별 예약 포트
│   ├── api/                 # 16번 서버 구현 위치, 현재 빈 기반
│   ├── llm/                 # 19번 LLM 연결 위치, 현재 빈 기반
│   └── roles/               # 실제 네 Agent 구현 위치, 현재 빈 기반
├── mcp_tools/
│   ├── core/
│   │   ├── config.py         # MCPSettings
│   │   └── policy.py         # 프로젝트 MCP 기준, 역할별 Tool 목록 선언
│   └── tools/               # 실제 Tool 구현 위치, 현재 빈 기반
└── evaluation/              # 팀원 소유, 이번 작업에서 변경하지 않음
```

기존 setuptools가 `src`의 패키지를 자동 탐색하므로 의존성 선언이나 `uv.lock`은 바꾸지 않았다. 향후 실제 MCP/LLM SDK가 필요한 단계에서 명세 호환성과 버전을 확인한 뒤 설치·Lock한다. 이번 단계에서는 MCP SDK v2가 설치되었거나 프로토콜 협상이 구현되었다고 주장하지 않는다.

## 3. 설정 계약

### AgentSettings

| 환경변수 | 기본값·규칙 |
| --- | --- |
| `AGENT_ROLE` | 필수. `PLANNER`, `DEVELOPER`, `QA`, `SECURITY` 중 하나 |
| `AGENT_HOST` | `127.0.0.1`. 현재 로컬 전용으로 `localhost`, `::1`도 허용 |
| `AGENT_PORT` | 생략 시 역할별 8101/8102/8103/8104. 지정 시 1~65535 |
| `AGENT_ENVIRONMENT` | `local`. development/test/production도 허용하나 외부 공개 기능은 없음 |
| `AGENT_LOG_LEVEL` | `INFO` |
| `AGENT_BEARER_TOKEN` | 미설정. 해당 역할의 Orchestrator HTTP 인증값과 일치하도록 운영자가 설정 |
| `AGENT_LLM_PROVIDER` | 미설정. 선택한 Provider 이름, 공백 값 금지 |
| `AGENT_LLM_MODEL_ID` | 미설정. 실제 선택한 모델 ID, 공백 값 금지 |
| `AGENT_LLM_API_KEY` | 미설정. 실제 Provider의 인증 방식은 19번에서 검증 |

`listen_port`, `agent_base_url`은 읽기 전용 속성이다. `task_database_path`는 `.data/agents/{role}.sqlite3`라는 17번의 예약 저장 위치일 뿐, 현재 파일이나 테이블을 생성하지 않는다. 이 경로는 제품 Workspace가 아니다.

`require_llm_configuration()`은 Provider/모델 선택 누락을 명시적 오류로 반환한다. 지원 Provider·자격증명·모델 접근 권한 검증과 실제 실행 차단은 19번의 Provider 어댑터가 추가로 책임진다. 이 메서드가 통과해도 LLM 호출 가능이나 작업 성공을 의미하지 않는다. 가짜 모델·API Key 또는 Mock 성공을 기본값으로 넣지 않는다.

### MCPSettings

| 환경변수 | 기본값·규칙 |
| --- | --- |
| `MCP_ROLE` | 필수. Agent와 동일한 기존 `AgentRole` |
| `MCP_ENVIRONMENT` | `local` |
| `MCP_LOG_LEVEL` | `INFO` |
| `MCP_TRANSPORT` | `stdio`만 허용 |
| `MCP_PROTOCOL_VERSION` | 프로젝트 정의서의 `2026-07-28`만 허용 |

프로세스를 시작하는 신뢰 런처가 Agent와 MCP에 같은 역할을 전달해야 한다. 모델이나 Tool Input이 역할을 선택·변경하는 구조가 아니다. 실제 런처와 연결 단계의 일치 검사는 23번에서 구현한다.

| 역할 | `allowed_tool_names` 정책 선언 |
| --- | --- |
| Planner | 없음. 필요 시 별도 문서 Read Tool 계약을 추가하며 제품 Source Tool은 주지 않음 |
| Developer | `read_project_file`, `write_source_file`, `apply_patch`, `run_build`, `run_unit_tests` |
| QA | `read_project_file`, `write_test_file`, `run_unit_tests`, `run_browser_tests`, `read_test_report` |
| Security | `read_project_file`, `run_security_scan`, `read_security_report` |

목록은 불변 tuple·읽기 전용 Mapping으로 보관한다. 이것은 **권한 정책 선언**이지 `tools/list`, `tools/call`, 파일 ACL 또는 Sandbox의 실제 강제 구현이 아니다. `max_tool_retries`는 기존 상수 2를 재사용하며 이번 단계에서 실제 재시도는 하지 않는다. Orchestrator는 앞으로도 MCP를 직접 호출하지 않는다.

## 4. 기존 코드에 영향 없이 사용하는 방법

모든 설정은 공통 `.env`의 다른 접두사 변수를 무시한다. 각 Agent는 프로세스별 `AGENT_ROLE`을 지정하고, 자식 MCP에는 해당 역할을 넘기는 방식을 사용한다. 네 프로세스를 한 번에 실행할 때 공통 `.env`의 역할을 반복 수정하지 않는다.

`.env.example`의 새 값과 기존 Agent URL은 모두 주석 예시로 둔다. 아직 없는 Planner URL을 켜서 Run 생성이 자동 dispatch를 시작하게 하지 않는다. 인증값을 Message·Artifact·Agent Card·일반 설정 응답에 넣지 않는다. 원본 Secret 값은 향후 인증 어댑터 경계에서만 꺼낸다.

기존 의존성이 설치된 환경에서 아래 코드로 설정만 확인한다.

```python
from agents.core.config import AgentSettings, AgentConfigurationError
from mcp_tools.core.config import MCPSettings
from orchestrator.domain.states import AgentRole

agent = AgentSettings(role=AgentRole.DEVELOPER, _env_file=None)
mcp = MCPSettings(role=agent.role, _env_file=None)

assert agent.agent_base_url == "http://127.0.0.1:8102"
assert "write_source_file" in mcp.allowed_tool_names

try:
    agent.require_llm_configuration()
except AgentConfigurationError:
    pass  # 미설정 모델을 실행 가능·성공으로 취급하지 않음
```

`_env_file=None`은 dotenv 파일만 생략한다. 프로세스 환경변수는 계속 적용되므로 위 포트 예시는 `AGENT_PORT`를 따로 지정하지 않은 경우다. 실제 Agent 실행 명령은 16번에서 추가한다. 현재는 `agents.main:app`이나 MCP 서버 실행 모듈이 존재하지 않는다.

## 5. 개발정의서 준수 점검

| 점검 항목 | 이번 단계 결과 |
| --- | --- |
| A2A 1.0 / HTTP+JSON / application/a2a+json | 기존 SDK 상수 재사용. 실제 서버 API는 16번 |
| MCP 2026-07-28 / local stdio | 설정값 제한. 세션·SDK 연결은 23번 |
| 기존 역할·식별자·공식 Schema 보존 | AgentRole 재사용. 새로운 ID 발급/공식 객체 복제 없음 |
| Orchestrator와 Agent/MCP 책임 분리 | 기존 Orchestrator 코드 변경 및 직접 MCP 호출 없음 |
| Credential 비밀 보관 | SecretStr + 일반 출력/직렬화 제외. 실제 인증 강제는 후속 서버/Provider에서 구현 |
| Workspace는 Orchestrator 발급 | Agent/MCP에 임의 Host Workspace Root 설정·발급 기능 없음. 실제 Registry는 20번 |
| 모델·실행환경 미설정 값 유지 | 미설정 LLM은 null. 가짜 모델/이미지/Hash를 채우지 않음 |
| QA/Security Source 수정 금지 | Tool 목록에서 Source Write/Patch 제외. 실제 파일 권한 강제는 후속 |
| 수정/Retry 한도 보존 | 기존 최대 수정 3회 정책 변경 없음. MCP Retry 2회 상수 재사용 |
| 3번·4번 및 해당 연동 제외 | backend/frontend/evaluation/평가 테스트 코드 변경 없음 |

원래 정의서의 모든 기능이 끝났다는 뜻이 아니다. 이번 단계에서 구현한 설정 기반이 해당 계약을 위반하지 않는지 점검한 결과다. 실제 테스트 실행 결과는 아래에 기록한다.

## 6. 검증

```bash
PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -v
git diff --check
```

확인 대상: 역할별 포트, 필수 역할·모델 설정, 접두사 격리, 공통 dotenv, 비밀값 일반 출력 제외, MCP 프로토콜·Tool 목록, 기존 Orchestrator 회귀.

검증 결과:

- 새 설정 테스트 27개 통과.
- 전체 unittest 199개 통과: 기존 Orchestrator 172개 + 새 설정 27개.
- 새 인터프리터에서 선택적 LLM/MCP SDK import와 소켓 연결을 차단한 상태에서도 설정 import/생성 성공. 작업 경로에 DB·Workspace 파일이 생기지 않음을 확인.
- setuptools 탐색에서 신규 Agent/MCP 패키지 8개 포함 확인.
- `git diff --check` 통과.

unittest 검증은 기존 Orchestrator와 새 설정 기반을 대상으로 한다. pytest 함수 기반 평가 테스트 전체, 실제 LLM/Agent/MCP, 제품 회원가입 시연·보안 검증을 완료했다는 의미가 아니다.

## 7. 커밋과 다음 작업

커밋 메시지 제안: `Agent 및 MCP 패키지 구조와 실행 설정 기반 추가`

**다음 작업: 16번 — 공통 A2A Agent 서버.** Agent Card와 HTTP+JSON send/get/cancel 진입점을 기존 A2A Client 계약에 맞춰 구현한다. Task 영속화·본격 비동기 실행은 17번, 역할 Prompt는 18번, 실제 LLM 연결은 19번에서 이어간다. Git commit/push는 직접 수행하지 않는다.

## 8. 확정 후속 번호 — 1번+2번만

이 번호는 이후 작업 기준이며, 3번 제품 웹 개발·시연 배포와 4번 독립 평가/비교 실험 및 그 연동은 포함하지 않는다. 26~28번의 Tool 개발과 32~33번의 Agent 구현은 2번 담당 범위다.

| 번호 | 작업 |
| --- | --- |
| 16 | 공통 A2A Agent 서버 |
| 17 | Agent Task 생명주기·영속화·Context·중복 방지 |
| 18 | 역할별 Prompt 및 출력 계약 |
| 19 | 공통 LLM 연결·구조화 응답·Tool Loop·예산·사용량 |
| 20 | 실제 Workspace Registry·경로·역할 권한 |
| 21 | 실제 Snapshot/Artifact 저장소·Hash·접근 제어 |
| 22 | Container Sandbox |
| 23 | MCP stdio 서버·공통 Schema·역할별 Tool 노출·권한 강제 |
| 24 | 파일 Read/Write/Patch Tool |
| 25 | Build Tool |
| 26 | Unit/API Test 실행 Tool·보고서 |
| 27 | Browser Test 실행 Tool |
| 28 | Security Scan Tool |
| 29 | Tool 실행 근거·오류 분류·안전 Retry |
| 30 | Planner Agent |
| 31 | Developer Agent |
| 32 | QA Agent |
| 33 | Security Agent |
| 34 | 기존 Orchestrator와 본인이 개발한 실제 Agent 연결 |
| 35 | 실제 수정·동일 Snapshot 재검증 Loop |
| 36 | 실제 입력 재개·취소·Human Review 제어 |
| 37 | 실제 LLM/A2A/MCP Trace·사용량·비밀정보 마스킹 |
