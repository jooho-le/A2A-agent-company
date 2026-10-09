# 33. Security Agent 및 실제 보안 검사 근거 연결

작성 기준: 2026-10-09. 범위: 1번+2번의 최초 Security 실행기. 3번 제품 서비스, 4번 독립 평가/비교 실험, 팀원 통합은 변경하지 않는다.

## 1. 구현 내용

- `SecurityAgentExecutor`: 공식 A2A Task를 받아 동결된 Security 요구사항·Snapshot을 검사하고 기존 `security-report.json` Artifact 하나를 반환한다.
- `SecurityExecutionContext` / `SQLiteSecurityContextLoader`: Orchestrator의 기존 Security handoff와 실제 DB의 현재 Step·Requirement·Source 생산자·READ_ONLY 권한·환경·공유 예산을 대조한다.
- `security_contract.py`: 모델은 Requirement/Finding 분석 제안과 읽은 코드 위치만 제출한다. Artifact ID·Manifest·Scanner 실행 기록·Severity·최종 Verdict를 만들 수 없다.
- `SecurityRuntimeServices`: 승인된 모든 Scanner Profile을 먼저 실행하고 실제 Tool/Scanner 기록·Host 입력 해시·Source inventory를 검증한다. 이후 코드 읽기 근거와 선택적 Host 의미 검증을 대조하여 보고서를 조립한다.
- Agent Card: 실제 Security 실행기를 명시적으로 주입했을 때만 `measured-initial-security` Skill과 `executionReady=True`를 광고한다.

기본 CLI/서버는 여전히 Bootstrap이다. LLM 환경변수만 설정했다고 자동으로 네 Agent가 실행되지는 않는다. 기본 연결은 34번이다.

## 2. 실행 흐름

```text
공식 SDK가 저장한 SUBMITTED Task 및 최초 Snapshot handoff
→ 읽기 전용 DB Context / 동결 요구사항·정책·공유 예산 검증
→ WORKING
→ Host 승인 Scanner Profile 전체 실행
→ 실제 불변 Scan Receipt + Tool Journal + 동일 Source/환경 대조
→ 모델에 실제 Finding ID·검사 요약·Source 파일 목록 전달
→ 모델이 허용된 읽기 Tool로 Frozen Source/Scan Report 검토
→ 폐쇄형 Review Draft 전체 Requirement/Finding coverage 검증
→ 실제 코드 읽기·존재하는 줄·파일/줄 해시 검증
→ 선택적 독립 Host 의미 검증과 동일 Run/Step/Source Proof 대조
→ 기존 Schema/Role Parser 검증 및 MCP 세션 종료
→ security-report.json 1개 + COMPLETED
```

현재는 최초 `VALIDATING`, `fix_attempt=0`, `codeVersion=1`, 첫 Artifact 버전만 지원한다. 수정 후보·이전 보고서 Lineage·재검증은 35번이다.

Security는 Run·Step·Issue·Artifact Registry·최종 Verdict를 직접 변경하지 않는다. A2A `COMPLETED`는 보고서 작성 완료이며 제품 보안 PASS가 아니다.

## 3. Scanner 실행과 보안 판정을 구분

| 실제 근거 | 처리 |
| --- | --- |
| 정상 검사, 경고 0개 | Tool 성공. 보안 요구사항 PASS를 자동 생성하지 않음 |
| 정상 검사, 경고 있음 | 모든 경고를 보존. 기본 `SUSPECTED`, 제품 실패로 Tool 재시도하지 않음 |
| 모델이 PASS/CONFIRMED/FALSE_POSITIVE 제안 | 제안일 뿐. 존재하는 코드 위치·그럴듯한 설명만으로 승격하지 않음 |
| 승인된 Host 검증기가 실제 코드 근거를 독립 검증 | 동일 Run/Step/Source로 범위가 맞는 Proof에 한하여 Requirement PASS/FAIL 또는 CONFIRMED/FALSE_POSITIVE 허용 |
| 의미 검증 근거 부족 | Requirement `UNVERIFIED`; 경고는 `SUSPECTED` 또는 명시적으로 `UNVERIFIED` |
| Scanner/Container/Receipt 무결성 실패 | Task `FAILED`, 완성 Artifact 없음. 기존 실행 원장의 실패/불명확 기록은 유지 |
| 정책·추가 정보 필요 | `INPUT_REQUIRED`; 질문은 동결 정책 변경이나 검사 생략을 승인하지 않음 |

`UNVERIFIED` Requirement에 정상 Scanner의 Tool PASS 근거를 붙여 검증 완료처럼 표시하지 않는다. 원장의 실제 검사 성공 기록은 별도로 남는다.

Severity는 실제 Scanner 기록에서 가져온다. Bandit의 LOW/MEDIUM/HIGH를 모델이 CRITICAL로 바꾸거나 낮출 수 없다. CONFIRMED/FALSE_POSITIVE 승격에는 실제 경고 파일·줄을 포함하는 읽기 근거와 독립 Host 검증을 모두 요구한다.

최종 HIGH/CRITICAL 차단, Explicit Requirement FAIL, MEDIUM 수용 정책 등은 기존 Orchestrator 판정 규칙의 책임이다. 이 Agent가 MEDIUM 수용 정책이나 최종 SUCCESS를 선택하지 않는다.

## 4. 모델 출력과 실제 코드 근거

모델의 Decision 종류는 `READY`, `INPUT_REQUIRED`, `REJECTED`다. READY는 할당된 모든 Security Requirement와 실제 Scanner Finding ID를 정확히 한 번씩 포함한다. 없는 Finding 생성·경고 누락·다른 Requirement 추가는 거부한다.

분석 제안 필드는 다음과 같다.

- `requirementReviews`: `requirementId`, `proposedOutcome`, `rationale`, `references`.
- `findingReviews`: `findingId`, `proposedDisposition`, `rationale`, `references`.
- 코드 참조: Source 상대 `path`, 포함 범위 `startLine`, `endLine`. 참조당 최대 200줄, Review당 최대 32개.
- 질문: 최대 8개. 각 객체는 추가 필드를 허용하지 않으며 문자열·JSON·UTF-8·정수 타입·Secret/경로 제한을 검사한다.

허용된 파일명이나 범위만 제출했다고 실제 읽기 근거가 되는 것은 아니다. Runtime은 해당 파일을 같은 실행의 `read_project_file`로 실제 읽었는지, 반환 bytes/hash가 immutable Source와 같은지, 저장된 입력/출력 해시가 같은지, 줄이 존재하는지 다시 확인한다.

모델에 제공하는 Tool은 `read_project_file`, `read_security_report`뿐이다. Source/테스트 쓰기, 명령 실행, Profile/Rule 선택, Scanner 반복 실행은 모델 권한이 아니다. 승인된 Profile 전체는 Host가 실행하므로 모델이 검사 일부를 생략할 수 없다. Scan Report 읽기도 이 실행에서 발급된 실제 보고서 참조만 허용한다.

공개 보고서에는 원본 코드·모델 설명·Raw Scanner prose를 복사하지 않는다. 안전한 상태 코드, 실제 Rule/위치, Source 참조, 파일/줄 해시, 읽기 기록 참조를 기존 `details`/`description` 문자열에 보존한다. 코드 참조 URI fragment는 내부 근거 식별자이지 새 HTTP 다운로드 API가 아니다.

## 5. 독립 Host 의미 검증 경계와 남은 한계

`SecurityRuntimeServices(..., proof_verifier=None)`가 안전한 기본값이다. 이 경우 Scanner 경고는 찾고 보고하지만 Requirement 결과는 전부 UNVERIFIED다. 모델이 코드를 읽고 PASS를 제안해도 자동 승인하지 않는다.

선택적 `proof_verifier(execution, decision, measured)`는 모델이 호출·교체할 수 없는 신뢰된 Host 설정이다. 입력으로 실제 Frozen Source bytes·Scan Receipt와 동결 기준을 받고, 검증한 항목만 `SecuritySemanticProof`로 반환한다. Proof에는 Run/Step/Source UUID·Snapshot hash와 Requirement outcome/Finding disposition이 연결된다. 다른 후보·없는 ID·읽지 않은 코드·미확인 제안의 임의 승격은 거부한다.

Proof 타입이나 Scope 일치 자체는 검증기 구현의 정확성을 증명하지 않는다. 모델 제안을 그대로 반환하는 callback을 실제 검증기로 사용하면 안 된다. 승인된 코드 분석이나 실제 격리 실행 근거를 확인해야 하며 생성 Source를 Host에서 import/exec하는 fallback을 추가하면 안 된다.

**이번 단계는 의미 검증기의 공통 연결 경계까지 구현했다. SCN-001의 DB UNIQUE 동시성, 승인 Password Hash 알고리즘·파라미터, API/A2A/로그/Trace 전체 미노출을 입증하는 실제 검증기·실행 근거는 제공하지 않는다. 따라서 현재 기본 Security 실행만으로 회원가입 보안 전체 PASS를 보장할 수 없다.** 이 범위는 실제 제품 기준/격리 검증 근거가 준비되어야 완성할 수 있으며, 테스트용 Proof fixture를 그 증거로 대체하지 않는다.

Scanner 지원 범위도 28번과 동일하게 Python Bandit AST 검사다. JS/TS, CVE, DAST, 권한 우회 재현이나 Bandit이 찾지 않은 모든 논리 취약점을 검증했다고 주장하지 않는다. 공개 Finding은 현재 승인 Scanner가 실제 반환한 후보만 다룬다.

## 6. 명시적 Host 구성

기존 Repository·Workspace·Artifact Store, 등록된 Source와 동결 Run Configuration, 승인 이미지·Scanner Profile, 실제 Provider, 기존 공유 Run 예산을 준비한 경우에만 구성한다.

```python
from agents.core.config import AgentSettings
from agents.main import create_app
from agents.runtime.security import SecurityAgentExecutor
from agents.runtime.security_context import SQLiteSecurityContextLoader
from agents.runtime.security_services import SecurityRuntimeServices
from mcp_tools.client import MCPChildConfiguration
from mcp_tools.runtime import MCPBinding
from mcp_tools.tools.snapshots import FrozenSourceSelection
from orchestrator.domain.states import AgentRole

loader = SQLiteSecurityContextLoader(repository, existing_run_budget_resolver)

def services_for(execution):
    child = MCPChildConfiguration(
        binding=MCPBinding(
            role=AgentRole.SECURITY,
            agent_role=AgentRole.SECURITY,
            run_id=execution.metadata.run_id,
            workspace_id=execution.configuration.workspace_id,
        ),
        database_path=repository.database_path,
        workspace_root=workspace_registry.base_path,
        frozen_source=FrozenSourceSelection(
            project_artifact_id=execution.source.artifact_id,
            snapshot_sha256=execution.source.snapshot_sha256,
        ),
        security_scan_configuration=approved_scan_configuration,
    )
    return SecurityRuntimeServices(
        repository, workspace_registry, artifact_store,
        mcp_configuration=child,
        # Default: no fabricated semantic PASS/confirmation.
        proof_verifier=None,
    )

executor = SecurityAgentExecutor(
    provider=approved_provider,
    context_factory=loader,
    services_factory=services_for,
)
app = create_app(
    AgentSettings(role="SECURITY", database_path=security_task_database),
    executor=executor,
)
```

위 변수는 이미 준비된 신뢰된 Host 객체다. 예시는 자동 설정·실제 실행 성공을 뜻하지 않는다. Scanner Profile 참조는 frozen `scannerProfileRef`와 일치해야 하며 정확한 버전·Rule·이미지/Lock 기준이 필요하다. 모델이 설정을 보충하거나 DB/환경의 최신값으로 fallback하지 않는다.

`existing_run_budget_resolver`는 재개 때도 기존 Deadline/누적 사용량을 유지해야 한다. 새 예산 생성·DB/이미지 준비·Provider 자동 연결은 이 예제의 역할이 아니다. 독립 Agent Task DB와 Orchestrator DB를 혼동하지 않는다.

## 7. 상태·재개·취소·예산

- SUBMITTED → WORKING → COMPLETED/INPUT_REQUIRED/AUTH_REQUIRED/REJECTED/FAILED를 공식 SDK/기존 durable Task Store로 처리한다.
- INPUT_REQUIRED 재개는 같은 opaque Task/Context·Snapshot·공유 예산을 유지한다. 원래 handoff와 별도의 답변만 전달하며 Run 설정·정책·Scanner 목록은 교체하지 못한다.
- `attempt`는 A2A 재개 횟수, `fix_attempt`와 `codeVersion`은 코드 수정 횟수다. Security Scan Store·Tool Journal·Sandbox에서 두 값을 같다고 요구하던 부분을 분리했다. 현재 Step identity의 attempt/입력/Task 검증은 유지한다.
- 재개한 실행은 새 실제 Scanner 기록을 만든다. 이전 실행의 Receipt를 새 실행 근거로 재사용하거나 안전 Retry로 가장하지 않는다.
- Scanner 선실행 및 모델의 읽기 모두 공유 Tool 예산을 차감한다. 기존 29번의 승인된 Infrastructure Retry 규칙을 유지하며 경고·의미 검증 실패는 Retry 대상이 아니다.
- MCP 세션 종료 후 대기/완료 상태를 게시한다. 취소 중 격리 실행과 동기 I/O를 정리/회수하며 터미널 Task는 재실행하지 않는다.
- Host 동기 검증기의 hard wall-clock·프로세스 crash 후 모든 orphan 정리는 보장하지 않는다. 검증기는 제한된 실행을 제공해야 한다. 전체 Run 예산/Trace 영속 연결은 34/37번 후속이다.

## 8. 개발정의서 준수 점검

| 기준 | 점검 결과 |
| --- | --- |
| §0/6/7 A2A 공식 경계 | 기존 A2A 1.0 SDK·HTTP+JSON·opaque Task/Context·공식 상태 보존 |
| §2 역할 책임 | Security 읽기/검토/보고만 수행. Source·정책·Registry·Workflow·최종 Verdict 변경 없음 |
| §4/5 고정 검증 대상 | 실제 등록 Source·생산자·private bytes/grant·Manifest·이미지/Lock·할당 Requirement 대조 |
| §8 MCP | 기존 MCP Tool 이름·입출력 Schema·stdio Client·서버 권한 유지 |
| §8 안전 경계 | Read-only Source, 승인 Scanner Container, Network DENY, 임의 Shell/Host 제품 실행/자동 설치 없음 |
| §9 Artifact | Project/A2A ID 구분, Host 생성 ID, 첫 버전/동일 codeVersion, Security Report 정확히 1개 |
| §10 판정 | 경고 0개 ≠ PASS, 경고 ≠ CONFIRMED. 근거 부족은 UNVERIFIED/SUSPECTED, 최종 Verdict는 Orchestrator |
| §11 재현/로그 | 실제 Scanner/Profile/입력 해시·Tool 기록·Source 읽기 해시 연결. 전체 Trace 영속 완성은 주장하지 않음 |
| 담당 범위 | 1번+2번만 변경. 제품·독립 평가·팀원 통합·비밀번호 보호 정책 보완·WAL 초기 경쟁 미변경 |

추가 읽기 전용 코드 검토에서도 현재 Step identity, Snapshot/Role/환경, 대기 전 세션 종료와 취소 경계에 새로운 확정 결함은 발견하지 못했다. 이는 실제 제품 보안 검증 완료를 의미하지 않는다.

## 9. 검증

```bash
PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -p 'test_security_agent*.py' -q
PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -p 'test_agent_bootstrap.py' -q
PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -q
PYTHONPATH=src .venv/bin/python -m compileall -q src/agents src/mcp_tools src/orchestrator
.venv/bin/pip check
git diff --check
```

- Security 신규 테스트 **122개 통과**: Context 28, Draft 계약 46, Services 28, 실제 SDK HTTP 호출 흐름 20. Services의 최종 Proof 재검증 보완 이후 28개 재실행도 통과(12.488초).
- 기존 Security Store에 재개 횟수와 코드 버전 분리를 검증하는 회귀 테스트 1개 추가. 기존 Bootstrap 11개도 통과(0.135초).
- 최종 전체 회귀 **2,357개 통과**(370.945초). 기존 Orchestrator/Agent/MCP/Artifact/Sandbox 및 위 신규 테스트를 포함하며 별도 재실행 수치를 중복 합산하지 않는다.
- 첫 전체 회귀에서는 기존 Docker CLI 환경 검사 테스트 1개가 프로세스 Timeout으로 실패했다. 해당 묶음 단독 재실행 24개 통과(2.981초), 최종 전체 재실행도 모두 통과했다. 재현 원인은 확정하지 않았으며 관련 코드나 제한 시간을 완화하지 않았다.
- `compileall`, `pip check`, `git diff --check`, 신규 파일 trailing whitespace 점검 통과. 로컬 MCP socket/stdio 테스트는 환경 실행 승인을 받아 수행했다.

실제 A2A SDK HTTP/SQLite/Git/Artifact Store/MCP Dispatcher 경계는 임시 fixture로 검증하며 Provider와 Docker/Bandit은 Fake다. 실제 LLM·Docker·Security Agent 전체 stdio 경로·회원가입 보안 검증 성공을 주장하지 않는다. 기존 SDK의 cross-replica streaming 경고는 남아 있고 Agent Card는 streaming 미지원으로 유지한다.

## 10. 인계

다음 작업: **34번 — 기존 Orchestrator와 본인이 개발한 실제 Agent 연결**.

커밋 메시지: `실제 검사와 코드 근거를 연결하는 Security Agent 구현`

Git commit/push는 수행하지 않는다.
