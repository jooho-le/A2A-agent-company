# 34. 기존 Orchestrator와 실제 네 Agent 연결

작성 기준: 2026-10-09. 범위: 1번 Orchestrator + 2번 본인 Agent/MCP의 최초 실행 연결. 3번 제품 서비스·4번 독립 평가·팀원 통합은 변경하지 않는다.

이 문서는 34번 완료 시점의 기록이다. 최초 후보·수정 gate 제한은 [35번 실제 수정·재검증](35-fix-revalidation-loop.md)에서 확장했으며 현재 연결은 그 문서를 함께 따른다.

## 1. 구현 내용

- `create_platform`: 기존 Orchestrator Dispatcher와 Planner/Developer/QA/Security 실제 실행기를 한 Host 구성으로 연결한다. 네 Agent의 Context Loader는 같은 Repository와 같은 `RunBudgetRegistry.resolve`를 사용한다.
- `RunBudgetRegistry`: Orchestrator가 새 Run의 최초 Planner Step을 durable claim한 뒤 예산을 한 번 발급한다. 모델·Tool 호출과 사용량을 역할별로 다시 시작하지 않는다.
- 제출 검증: owned 구성에서는 모델·실행환경·시간 예산·시작 Commit·Scanner Profile 참조가 빠지거나 승인된 역할 설정과 모델이 다르면 Run 저장 전에 422를 반환한다.
- 준비 Hook: 공식 Agent 호출 전에 동결 설정을 재검사하고 신뢰된 Host Workspace 준비 함수를 실행한다. 준비 실패는 `OWNED_AGENT_PREPARATION_FAILED` 이벤트와 `HUMAN_REVIEW`로 남긴다.
- `run_platform` / CLI: 단일 프로세스·단일 event loop에서 역할별 공식 HTTP 서버 네 개와 기존 Orchestrator 서버를 실행한다. 승인된 Host factory를 명시하지 않으면 시작하지 않는다.
- 최초 실행 능력 경계: 기존 Dispatcher의 자동 수정은 owned 구성에서만 끈다. 기존 판정이 `FIX_REQUIRED`인 경우 실제 제품 FAIL과 Issue를 보존하고 멈춘다. 기존 Dispatcher의 기본 동작은 유지한다.

패키지 import나 `create_platform` 자체는 Agent DB 초기화·Workspace 생성·Git 준비·HTTP listener·모델 호출·MCP/Container 실행을 하지 않는다. 생성 함수에 전달하는 기존 Host 저장소 객체의 초기화는 그 객체를 준비한 호출자의 책임이다.

## 2. 연결 방식

```text
승인된 Host factory
  ├─ 기존 Orchestrator HTTP :8000
  ├─ 실제 Planner HTTP     :8101
  ├─ 실제 Developer HTTP   :8102
  ├─ 실제 QA HTTP          :8103
  └─ 실제 Security HTTP    :8104

하나의 프로세스 / 하나의 event loop
  ├─ 공통 Repository · WorkspaceRegistry · ArtifactStore
  └─ Run별 공유 Budget 하나

POST /api/v1/runs
→ 동결 설정 제출 검증 · 저장
→ 최초 Planner Step claim
→ 예산 승인 · Host Workspace 준비
→ 공식 A2A Planner → Developer → 실제 Build
→ 동일 Frozen Source를 QA / Security에 전달
→ 실제 Tool/보고서 근거 등록
→ 기존 Orchestrator 판정
```

Agent 호출을 Python 함수 직접 호출로 바꾸지 않았다. 기존 A2A 1.0 Card/HTTP+JSON/`message:send`/opaque Task ID/폴링/Artifact 회수 흐름을 유지한다. owned 연결은 `trust_env=False`로 로컬 요청이 HTTP_PROXY/ALL_PROXY를 타지 않게 하며 같은 Client factory를 기존 제어 API에도 적용한다. 기본 원격/legacy 연결의 프록시 정책은 바꾸지 않는다. MCP의 실제 기본 Client는 기존 로컬 stdio다.

Orchestrator가 Task·Registry·Workflow·Verdict를 관리하고 Agent는 역할별 Artifact만 반환한다. 같은 DB 접근이 역할 권한을 넓히는 것은 아니며 기존 Source 읽기/쓰기·검사 Profile·실행 원장 검증을 그대로 적용한다.

## 3. Host가 명시적으로 준비할 항목

| 항목 | 조건 |
| --- | --- |
| 저장소 | 동일 Repository 객체를 사용하는 WorkspaceRegistry/ArtifactStore. 설정의 DB/Workspace 경로도 일치 |
| 역할 설정 | 네 역할 모두 지정. 포트 충돌·역할 불일치·Agent DB 중복·Orchestrator DB 혼용 금지 |
| Agent URL/인증 | Orchestrator URL과 각 Agent 주소가 정확히 일치. 같은 역할의 Bearer 설정도 일치 |
| 모델 | 네 역할의 Provider/model/revision/temperature/seed 동일. 동결 Run 모델과도 동일 |
| 전체 예산 | Host `LLMLimits`를 네 AgentSettings에도 동일하게 지정. Run에는 양의 `runtimeBudgetMs` |
| 시작 코드 | 승인된 full Git Commit과 잠금 파일. Workspace 준비 함수는 그 기준의 Git 저장소를 준비 |
| 환경 | 명시적 image digest·lock hash·hardware profile·Network DENY |
| Developer 서비스 | 승인 Build Profile, baseline Commit/repository ID/lock path, 실제 역할-bound MCP 설정 |
| QA 서비스 | 승인 Unit/Browser selector, 필요한 보호 테스트 bytes와 Case binding. 동결 protected suite가 있다면 생략 금지 |
| Security 서비스 | 동결 `scannerProfileRef`와 일치하는 승인 Scanner Profile 전체 |

서비스 팩토리는 기존 `DeveloperRuntimeServices`, `QARuntimeServices`, `SecurityRuntimeServices`의 정확한 타입과 동일 Host 저장소 객체를 반환해야 한다. 실제 Profile·이미지·Lock·Source·Requirement 일치 검사는 기존 서비스의 prepare/finalize에서 다시 수행한다. 제출 검사가 Docker 존재나 원격 API Key의 실제 유효성을 확인한 것으로 해석하면 안 된다.

`prepare_workspace(run_id)`는 신뢰된 운영자 함수이며 필수다. 필요하면 기존 Registry의 명시적 provision을 사용하고 승인된 Git baseline을 준비한다. 연결 모듈은 자동 Git init/clone/checkout·임의 Source 덮어쓰기·제품 scaffolding을 제공하지 않는다. 안전하지 않은 Host 코드 실행이나 자동 의존성 설치로 빈 설정을 보충하지 않는다.

## 4. 구성/실행 방법

Host 모듈에서 앞 단계에 준비한 실제 객체와 승인한 팩토리를 조립한다. 아래 코드는 구성 관계를 보여주는 예시이며 변수의 실제 설정은 운영자가 준비해야 한다.

```python
from agents.platform.composition import create_platform

def build_platform():
    return create_platform(
        repository=repository,
        workspace_registry=workspace_registry,
        artifact_store=artifact_store,
        orchestrator_settings=orchestrator_settings,
        agent_settings=role_settings,             # AgentRole → AgentSettings, 네 역할
        providers=approved_providers,              # AgentRole → 기존 LLMProvider
        developer_services_factory=developer_for,
        qa_services_factory=qa_for,
        security_services_factory=security_for,
        prepare_workspace=prepare_approved_workspace,
        limits=shared_host_limits,
    )
```

각 서비스 팩토리의 입력은 실제 동결 Execution Context다. `MCPBinding`의 Run/Workspace/role과 QA/Security의 `FrozenSourceSelection`은 이 Context에서 가져오며 모델이나 최신 Working Tree에서 고르지 않는다. 구체적 서비스 설정은 31~33번 문서의 구성 예시를 따른다.

승인한 모듈을 Python import 경로에 둔 뒤:

```bash
PYTHONPATH=src .venv/bin/python -m agents.platform --factory approved_host:build_platform
```

`approved_host`는 예시 이름이며 저장소의 기본 파일이 아니다. CLI는 운영자가 직접 승인한 `module:callable`만 받는다. 해당 모듈 import는 신뢰된 Host 코드 실행이므로 A2A 사용자/모델 입력으로 factory를 선택하면 안 된다. filepath/URI/eval 입력은 지원하지 않는다.

시작 후 기존 Orchestrator `/docs`, `/api/v1/scenarios`, Run/Step/Event/Artifact 조회 API와 Agent `/health`를 사용한다. `/health`의 `executionReady=True`는 실제 실행기 주입을 나타내며 제품 PASS·Docker/LLM 실사용 검증을 뜻하지 않는다.

기존 `python -m agents`는 Bootstrap으로 유지한다. `.env`의 모델 값만 채우거나 기존 Bootstrap 서버 네 개를 띄우는 것으로 이 플랫폼이 자동 구성되지 않는다. 이 모드에서 `--reload`, 여러 worker, 별도 Agent 프로세스는 사용하지 않는다.

## 5. 공유 예산과 재시작

- Run 생성부터의 대기·Workspace 준비 시간을 `runtimeBudgetMs`에서 차감한다. 이후 Deadline은 monotonic이며 다음 Agent나 재개에서 연장하지 않는다.
- 네 Context Loader는 이미 승인된 동일 예산 객체만 조회한다. 조회 함수는 새 예산을 만들지 않는다.
- 모델/Tool reservation 및 토큰 집계는 잠금으로 보호한다. QA/Security 병렬 소비도 같은 전체 cap을 사용한다.
- 누적 모델 호출·Tool 호출·알려진 토큰을 유지한다. 사용량을 알 수 없는 응답이 있다면 토큰 총량을 임의로 0으로 간주하지 않는다.
- 전체 Run Configuration Artifact의 ID·동결 Scenario bytes·설정 fingerprint가 달라지면 기존 예산 조회/재승인을 거부한다.
- 프로세스 재시작 후 예전 Run의 사용량과 Deadline은 복원하지 않는다. 예전 Run 또는 승인되지 않은 Task 조회로 새 예산을 발급하지 않는다.

현재 예산은 단일 Host 메모리이며 durable/distributed budget store가 아니다. Host의 세부 `LLMLimits`도 이번 단계에서 새로운 Run Configuration Schema 필드로 영속화하지 않는다. 전체 LLM 사용량·Trace 영속 연결과 재시작 복원은 37번 후속이다. 기존 durable A2A/Tool 기록이 있다고 과거 모델 소비량까지 복원 가능하다고 주장하지 않는다.

## 6. 시작·종료·실패 처리

- 모든 포트를 먼저 확보한다. 하나가 실패하면 확보한 listener만 닫으며 DB/역사/Workspace를 지우지 않는다.
- 네 Agent의 공식 SDK Task Store/lifespan이 준비된 후 Orchestrator가 새 Run을 받는다.
- 한 서버의 시작 실패·비정상 종료는 같은 Host의 나머지 서버에도 안전한 종료를 요청한다.
- 종료 때 먼저 Orchestrator의 새 요청을 막고 이미 받은 dispatch를 정리한 뒤 Agent Worker/SDK Store를 정리하고 Provider를 닫는다.
- 반복 SIGINT/SIGTERM도 동일 정리 요청이며 lifespan을 건너뛰는 force exit를 사용하지 않는다.
- 준비/서비스/Provider 종료 중 동기 Worker는 취소되더라도 회수한다. Timeout은 실패를 표시하지만 이미 시작한 안전 정리를 버리고 hard wall-clock 종료를 보장하지 않는다.
- 플랫폼 생성이 성공한 경우 Provider 종료 소유권을 전달한다. 공유 Provider 객체는 한 번만 닫고 Host 저장소는 임의로 닫거나 삭제하지 않는다.

시작/종료 설정 오류는 안정된 코드만 출력하며 Credential·제출값·원문 Provider 예외를 CLI 오류에 포함하지 않는다.

## 7. 이번 단계가 성공이라고 주장하지 않는 것

- 네 A2A Task COMPLETED는 제품 SUCCESS가 아니다. 기본 Security는 독립 의미 검증 근거가 없으면 Requirement UNVERIFIED를 유지하고 기존 판정은 HUMAN_REVIEW 등으로 멈춘다.
- 기존 판정이 `FIX_REQUIRED`이면 새로운 Developer Fix Task를 자동 발행하지 않는다. 기본 Security UNVERIFIED처럼 필수 결과에 검증 근거가 빠진 경우에는 실제 QA FAIL이 있어도 현재 판정상 `HUMAN_REVIEW`가 될 수 있으며 실제 QA Issue는 보존한다. 실제 수정 후보·Lineage·동일 Snapshot 재검증은 35번이다.
- 전체 Run 제어의 실제 재개/취소/HumanReview 연결 완성은 36번이다. 기존 API/단일 실행기 제어를 전체 통합 완료로 재표현하지 않는다.
- SCN-001 전체 보안 의미 검증기, 실제 LLM·Docker 실행, 팀원 서비스 시연 성공, 4번 비교 실험은 완료로 주장하지 않는다.

## 8. 개발정의서 준수 점검

| 기준 | 확인 |
| --- | --- |
| §0/6/7 A2A | 기존 공식 1.0 SDK·HTTP+JSON·Card·opaque ID·폴링·Artifact 회수 유지 |
| §2 책임 경계 | Orchestrator만 상태/Registry/Issue/Verdict 관리. Agent/MCP 역할·쓰기 권한 유지 |
| §4 실패/수정 | 원장·Issue·실제 FAIL 보존. 기존 판정이 FIX_REQUIRED면 수정 발행 없이 정지, mixed FAIL/UNVERIFIED는 HUMAN_REVIEW 가능. 최대 수정 3회 정책 미변경 |
| §5 Snapshot/환경 | Build/QA/Security 동일 actual Source/Manifest, READ_ONLY grant와 기존 receipt 검증 유지 |
| §8 MCP/격리 | 기본 stdio·역할 Tool·승인 Profile 유지. 생성 코드를 Host 실행/import하지 않음 |
| §9/10 Artifact/판정 | Task COMPLETED ≠ PASS, 근거 없는 SUCCESS/보안 PASS 없음 |
| §11 예산/Trace | 한 Run의 실제 역할 공유 예산·기존 A2A/MCP/Artifact 이벤트 유지. 전체 영속 사용량은 후속으로 명시 |
| 담당 범위 | 1번+2번만 변경. 제품/독립 평가/팀원 통합/비밀번호 보호 정책 보완/WAL 초기 경쟁 미변경 |

## 9. 검증

```bash
PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -p 'test_agent_platform*.py' -q
PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -p 'test_owned_*.py' -q
PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -q
PYTHONPATH=src .venv/bin/python -m compileall -q src/agents src/mcp_tools src/orchestrator
.venv/bin/pip check
git diff --check
```

- 신규 연결/예산/수명주기 테스트 **89개 통과**: Budget 13, Runner 32, Composition 29, 실제 실행기 ASGI Pipeline 5, Dispatcher/제어 경계 9, 실제 TCP Pipeline 1.
- 기존 인증 설정 테스트에 프록시 정책 회귀 2개 추가. 해당 묶음 4개 통과. 기존 LLM Runtime 58개·Dispatcher 31개·A2A Client 13개 집중 재실행도 통과했다.
- 전체 회귀는 **2,448개 실행, 2,447개 통과, 기존 테스트 1개 ERROR**(396.732초)였다. 실패는 `test_concurrent_initial_roles_cannot_share_a_new_database`에서 기존 `PRAGMA journal_mode=WAL` 초기화 경쟁으로 `sqlite3.OperationalError: database is locked`가 발생한 것이다.
- 실패 이후 기존 Task Store 묶음 단독 재실행 **33개 통과**(0.495초). 단독 통과를 전체 회귀 통과로 재표현하지 않는다. 해당 Store/테스트는 이번 단계에서 변경하지 않았고 기존에 제외한 WAL 초기 경쟁 수정·Retry/Timeout 완화도 하지 않았다.
- 새 owned 구성은 Agent DB 중복을 시작 전 거부하며 각 역할에 다른 DB를 사용한다. 따라서 실패한 기존 shared-new-DB 경쟁 조건을 이 구성에서 허용하지 않는다. 이것이 기존 Store 경쟁 문제 자체를 해결한 것은 아니다.
- 실제 TCP 집중 검증 **1개 통과**(5.374초): 실제 Uvicorn 서버 다섯 개·공식 SDK HTTP 요청/폴링·동일 Source 보고서·공유 모델 호출 6회·취소 종료 후 포트 회수를 확인했다. LLM/Docker는 Fake다.
- `compileall`, `pip check`, `git diff --check`, 신규 파일 trailing whitespace 점검 통과. 테스트 실행을 위해 로컬 TCP/MCP stdio 권한 승인을 받았다.

읽기 전용 독립 검토에서 발견한 환경 프록시 위험은 owned Client의 `trust_env=False` 및 기존 제어 API의 동일 factory 재사용으로 보완했다. mixed QA FAIL/Security UNVERIFIED를 무조건 FIX_REQUIRED로 표현했던 문구도 실제 기존 HUMAN_REVIEW/Issue 보존 동작으로 정정하고 회귀 테스트를 추가했다.

연결 테스트는 실제 네 실행기·기존 Dispatcher·공식 SDK HTTP API·SQLite·Git·Artifact·MCP Dispatcher·실행 Receipt를 사용한다. Provider와 Docker는 Fake, MCP는 in-process Dispatcher adapter이며 실제 LLM/Container/네 Agent 전체 stdio 실행 성공을 뜻하지 않는다.

## 10. 인계

다음 작업: **35번 — 실제 수정·동일 Snapshot 재검증 루프**.

커밋 메시지: `기존 Orchestrator와 실제 네 Agent를 공유 예산으로 연결`

Git commit/push는 수행하지 않는다.
