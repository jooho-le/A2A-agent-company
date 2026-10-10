# 1~37번 전체 점검 후 개발정의서 준수 보완

작성일: 2026-10-10

## 1. 범위

개발정의서 원문의 §3·§4-7·§8-3/8-11·§10/11·§11-A를 기준으로, 전체 점검에서 확인한 세 동작 문제와 한 권한 불일치를 수정했다. A2A 응답 metadata 방어도 추가했다. 기존 1~37번 로드맵 이후의 보완이며 새 38번 기능 단계는 아니다.

1번 Orchestrator와 2번 Agent/MCP만 변경한다. 3번 제품 화면·API·DB, 4번 제품 평가 테스트 내용·Single/Multi 실험·발표, 팀원 연결은 이번 구현에 포함하지 않는다. Git commit/push는 수행하지 않는다.

## 2. 수정 내용

| 문제 | 변경 | 정의서 기준 |
| --- | --- | --- |
| 같은 URI의 보호 테스트 내용·Scanner 규칙이 바뀌어도 허용 | 최초 검증 승인 시 Run·역할별 정책 Hash를 영속 고정하고 이후 변경 거절 | §11-A 동일 평가 조건 |
| 한 후보의 중복 Fingerprint가 반복 카운트를 0으로 초기화 | 바로 이전 codeVersion의 최대 반복 횟수만 조회. 현재 후보의 모든 중복 Issue는 동일 횟수 적용 | §4-7 반복 결함 |
| Trace가 1,000개를 넘으면 SUCCESS 대신 HUMAN_REVIEW | 1,000개씩 초기 append-only 전체 prefix를 확인. Trace 개수가 아닌 필수 근거로 판정 | §10·§11 성공 판정/Trace |
| Orchestrator의 MCP Build/Report 접근 생략 | 별도 Host principal에 명세의 세 Tool만 제공. 기존 네 LLM 역할은 유지 | §8-3·§8-11 역할별 Tool |
| 응답 Task가 잘못된 프로젝트 metadata를 제공해도 무시 | 제공된 프로젝트 필드를 Schema 검증하고 현재 Run·Step·attempt·Code/Artifact/Requirement와 비교. Agent 요청의 canonical 이름·선택 필드 non-null 형식도 검사 | §3 Project Metadata 오류 처리 |

### 평가 기준 고정

`EvaluationPolicyStore`는 기존 Orchestrator SQLite DB에 `run_evaluation_policies`를 추가한다. 생성자는 I/O를 하지 않고 첫 `prepare()`에서 기존 Run·Step·Workspace·Source 검사 후 승인된 정책을 원자 고정한다. 기존 Run Configuration Artifact와 canonical Trace Schema는 바꾸지 않는다.

- QA: 보호 파일의 UTF-8 bytes Hash·경로, selector·pattern, 보호 case/test/Requirement/예상 결과 연결, 승인된 Unit/Browser 실행 설정과 runner Hash·Browser 버전.
- Security: Scanner Version·Rule·Profile·실행 설정과 승인 runner Hash.
- Hash에서 Source/Step/attempt/codeVersion은 제외한다. 정상 수정·재검증·입력 재개는 가능하고 생성 QA 테스트 내용도 변경할 수 있다.
- 정책의 UPDATE/DELETE/REPLACE를 DB Trigger로 금지한다. 보호 코드·명령·Host 경로 원문은 새 원장에 저장하지 않는다.
- 기준이 없는 상태로 과거 QA/Scanner 실행 근거가 이미 있는 레거시 Run은 현재 설정을 과거 기준처럼 승인하지 않는다. 기존 기록을 수정하거나 자동 backfill하지 않고 새 Run을 사용해야 한다.
- Developer의 이전 Unit Test receipt는 QA 기준 수립을 막지 않는다.
- Hash는 Run 생성 시점이 아닌 **최초 평가 승인 시점**에 고정한다. 아직 평가하지 않은 역할의 기준이 이미 검증됐다고 표시하지 않는다.

독립 평가 담당자가 이후 Hash를 읽어 비교할 수 있는 Host 조회 기반을 제공한다. 조회는 원장·기준을 생성하거나 실행하지 않는다.

```python
from agents.runtime.evaluation_policy_store import EvaluationPolicyStore

records = EvaluationPolicyStore(repository).list_for_run(run_id)
# tuple[EvaluationPolicyRecord]: run_id, workspace_id, role, policy_sha256
```

### 반복 Issue와 Trace

중복 Finding을 삭제·합치지 않고 각 Issue와 추가 전용 이력을 유지한다. 한 후보의 중복 Issue는 `0,0 → 1,1 → 2,2`로 계산되어 기존 정책대로 수정 2회 후 HUMAN_REVIEW 대상이 된다. 중간 후보에서 해결된 뒤 재발하면 연속 카운트는 다시 시작하고 기존 Fingerprint 연결은 유지한다.

Trace는 첫 조회 시 존재하는 sequence 순서의 prefix를 bounded page로 읽는다. 이후 추가되는 Telemetry 때문에 조회가 끝없이 늘어나지 않는다. 실제 필수 근거가 없거나 페이지가 잘리면 계속 HUMAN_REVIEW이며, 개수만 많다는 이유로 실패하지 않는다. 닫힌 Trace Schema·기존 성공 조건·수정 최대 3회·Tool Retry 최대 2회는 변경하지 않았다.

### Orchestrator MCP 사용

```python
from mcp_tools.client import MCPChildConfiguration, open_mcp_client
from mcp_tools.core.policy import MCPHostPrincipal
from mcp_tools.runtime import MCPBinding

binding = MCPBinding(
    role=MCPHostPrincipal.ORCHESTRATOR,
    agent_role=None,
    run_id=run_id,
    workspace_id=workspace_id,
)
configuration = MCPChildConfiguration(
    binding=binding,
    database_path=repository.database_path,
    workspace_root=registry.base_path,
    # 실제 Build에는 별도로 승인된 BuildConfiguration을 지정한다.
)
async with open_mcp_client(configuration) as client:
    report = await client.call_tool("read_test_report", {
        "workspaceId": str(workspace_id), "reportRef": report_ref,
    })
```

- Host launcher만 주체를 선택한다. Tool arguments나 모델 metadata로 권한을 변경할 수 없다.
- `run_build`, `read_test_report`, `read_security_report` 세 Tool만 discovery/call 허용.
- 서버 내부 고정 매핑으로 기존 Workspace·Source grant·Report ACL·Sandbox 검사를 재사용한다.
- Build는 기존 IMPLEMENTING/FIXING의 active Developer Step과 같은 Snapshot에서만 실행한다. 미승인 Build 설정은 안전하게 실패한다.
- 소스 Read/Write·Patch·테스트 실행·추가 Scan 권한은 없다.
- Host binding을 LLM Tool adapter/Agent 전용 Tracked 실행기와 원장에 연결할 수 없다. 관리 호출을 Agent 실행으로 가장하지 않는다.
- 기존 Agent 호출 순서나 자동 Build 실행 횟수는 변경하지 않았다. Host 전용 호출을 실제 네 Agent 호출 예산/Trace처럼 집계한다고 주장하지 않는다.

### 선택적 Task metadata 검증

공식 Task metadata가 없는 응답은 계속 허용한다. unrelated extension도 허용하며, 공급된 프로젝트 필드만 검사한다. 올바른 일부 필드 echo도 허용하지만 잘못된 UUID·다른 Run/Step·stale attempt·다른 Source/Requirement 연결은 거부한다.

ProtoJSON Struct의 `1.0` 같은 정수 값은 허용하고 bool·문자열·소수·음수는 기존 정수 계약에 맞춰 거절한다. 선택적인 ID 배열이 공급되면 실제 JSON array여야 하며 `null`은 거부한다. 오류 메시지에는 응답 원문이나 제출 값을 붙이지 않는다. Send·Get·입력/Auth continuation에 같은 검사를 적용하며 Task/Context opaque ID는 그대로 보존한다.

Agent의 요청 metadata도 canonical 프로젝트 필드 이름만 허용한다. 내부 Python 모델의 snake_case alias나 선택 배열·codeVersion의 명시적 null을 wire 요청에서 받아들이지 않는다. 선택 필드가 없는 정상 요청과 ProtoJSON의 정수 float는 계속 허용한다. 공식 객체나 내부 모델의 optional 상태를 변경한 것이 아니라 wire Schema의 경계를 맞춘 것이다.

## 3. 변경 파일

- `src/orchestrator/application/{dispatch,a2a_tasks}.py`, `infrastructure/sqlite_workflows.py`: 전체 Trace 판정·반복 Issue·응답 metadata.
- `src/agents/runtime/evaluation_policy_store.py`, `qa_services.py`, `security_services.py`: 불변 평가 기준·검증 승인·조회.
- `src/agents/api/validation.py`: 요청 metadata의 canonical 프로젝트 필드와 선택 필드 JSON 형식.
- `src/mcp_tools/{runtime,client,__main__,execution_runtime}.py`, `core/{policy,config}.py`: Host principal·공식 stdio 전달·서버 권한·LLM 분리.
- `src/mcp_tools/tools/{build,unit,browser,security,test_reports}.py`: 허용된 관리 Tool의 기존 ACL 재사용.
- 전용 회귀 테스트 4개 파일과 README·23번 문서·이 문서.

## 4. 검증

| 전용 테스트 | 수 | 주요 조건 |
| --- | ---: | --- |
| `test_workflow_contract_regressions.py` | 8 | 1~3페이지 Trace·누락/잘림·수정 후 성공·중복 Finding·해결 후 재발 |
| `test_a2a_response_metadata.py` | 15 | 선택적 echo·extension·UUID·ProtoJSON 정수·배열 null·GET·stale continuation·요청 canonical 형식 |
| `test_evaluation_policy_pinning.py` | 28 | bytes/rules/version 변경·재시작·새 후보·동시 첫 승인·불변 DB·레거시·읽기 조회 |
| `test_orchestrator_mcp_capability.py` | 25 | 세 Tool만 노출·Run/Workspace/Source ACL·LLM 위장 금지·Build·실제 stdio report read |

전용 회귀는 총 76개다. Provider·Docker·제품 검증은 명시적인 fixture를 사용하며 실제 외부 LLM·Container·회원가입 품질 실험 결과가 아니다.

```bash
PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -q
PYTHONPATH=src:tests .venv/bin/python -m unittest tests.test_workflow_contract_regressions tests.test_a2a_response_metadata tests.test_evaluation_policy_pinning tests.test_orchestrator_mcp_capability -q
.venv/bin/python -m compileall -q src tests
.venv/bin/pip check
git diff --check
```

검증 기록:

- 전체 unittest 최초 실행: **2,704개 / 693.817초**. 2,700개 통과, 4개는 sandbox의 TCP/Unix socket `bind` 제한으로 `PermissionError` 발생. assertion 실패는 없다.
- 사용자 승인 후 중단된 4개를 포함하는 TCP·Workspace 묶음 **26개 / 16.577초 모두 통과**. 실제 localhost의 5개 HTTP 서버 실행·수정·재개·취소·listener 종료를 확인했지만 Provider·Docker는 fixture다.
- 전체 실행 시작 후 추가한 선택 배열 null 경계까지 포함한 전용 **72개 / 19.857초 통과**. 이어 요청 metadata 경계 4개를 더 추가했다.
- 최종 코드 기준: 전용 76개와 기존 Agent Task Store·공식 SDK 재개·QA/Security 선택 재개를 합친 **125개 / 19.815초 모두 통과**. 마지막 요청/응답 metadata 경계까지 포함한다.
- 컴파일·의존성·`git diff --check` 통과. 전용 테스트/마지막 경계는 별도 재실행했으므로 위 2,704개를 최신 전체 discovery 수나 단일 실행의 완전 PASS처럼 표시하지 않는다.

## 5. 유지한 제한과 다음 범위

- 사용자 요청으로 제외한 설정 URL Credential 보호와 공유 Agent DB 초기화 WAL 경쟁은 수정하지 않았다. URL에 실제 비밀번호·Token을 넣지 않는다.
- 실제 운영용 `approved_host` 설정과 외부 LLM·실제 Docker·회원가입 전체 실행은 별도 준비/검증이 필요하다. 자동 실행하거나 임의 설정을 승인하지 않는다.
- 기본 Security 의미 검증기 부재 시 UNVERIFIED 유지. 이번 정책 Hash가 임의 Host `proof_verifier` 구현의 정직성이나 코드 동일성을 증명하지 않는다.
- 승인된 가격/과금 기준이 없으므로 API 비용은 기존대로 `null`이다. 비용 계산이나 비교 실험 완료로 표시하지 않는다.
- Run 실행 중 신뢰된 플랫폼 runner/assets를 hot-deploy하지 않는 운영 원칙을 유지한다. 기준 Hash는 검증 승인 시 읽은 asset을 검사하며 동일 UID의 임의 파일변조와 원자적 코드 배포까지 격리하는 OS 보안 경계는 아니다.
- 제품 UI/API/DB, 독립 보호 테스트 내용, Single/Multi 공정 비교는 3·4번 담당 범위다.

## 6. 커밋 메시지와 다음 작업

커밋 메시지 제안: `개발정의서에 맞춰 평가 기준과 Orchestrator MCP 권한 및 판정 보완`

다음 작업 번호: **없음 — 기존 1~37번 로드맵 보완 완료**. 실제 운영 Host 설정·보안 의미 검증·과금 기준·실환경 확인은 별도 범위이며 완료로 간주하지 않는다. Git commit/push는 수행하지 않았다.
