# 32. QA Agent

작성 기준: 2026-10-09. 범위는 1번+2번의 **최초 Frozen Source 기능 검증과 실제 실행 근거를 담은 A2A 출력**이다. 3번 제품 웹서비스·4번 독립 평가·팀원 연결은 변경하지 않는다.

## 1. 구현 내용

- `QAAgentExecutor`: 기존 LLMEngine·역할 Prompt·TrackedMCPExecutor를 사용하는 명시적 QA 실행기.
- `QAExecutionContext` / `SQLiteQAContextLoader`: 완료된 Planner·Developer 결과, QA Step, 동일 Source, 동결 Run 설정과 공유 예산을 읽기 전용으로 검증.
- `qa_contract.py`: 모델은 승인된 테스트 케이스와 Requirement의 연결만 제안하고, 실제 결과·Manifest·ToolEvidence는 작성하지 못하는 제한된 Decision 계약.
- `QARuntimeServices`: 승인된 QA/Host 보호 테스트 실행 → private 입력·실행 기록 대조 → 기존 QA Report의 Host 조립.
- `QATestInputStore`: 실행 직전에 검증한 테스트 입력의 실제 bytes·Hash와 해당 실행 receipt 연결을 private SQLite에 불변 보존.
- 실제 QA 실행기를 명시적으로 주입할 때만 QA Skill·`executionReady=True` 광고. 기본 서버와 CLI는 Bootstrap 유지.

```text
기존 A2A admission → SUBMITTED
→ Host Run/QA Step/Planner·Developer 완료/동결 설정·공유 예산 확인
→ 최초 request·snapshot 입력과 현재 Task/Context·추가 답변 검증
→ WORKING
→ 같은 Source Snapshot의 QA READ_ONLY grant·Hash·Manifest 확인
→ LLM이 MCP로 Source 읽기 + QA 전용 테스트 작성
→ 모델의 READY 케이스 연결 검증
→ Host가 승인된 모든 QA 테스트 + Host 보호 테스트를 선택
→ 실행 입력 bytes·Hash를 private Store에 불변 저장
→ 동일 Source의 Unit/Browser 테스트 실행
→ private Tool/Test Store의 실행 기록·입력·Profile·Manifest 대조
→ 실제 case 결과로 PASS/FAIL/UNVERIFIED 조립
→ 기존 QA Schema·Orchestrator 출력 parser 검증
→ qa-report.json Artifact 게시 → COMPLETED
```

현재는 `VALIDATING`, `fix_attempt=0`, `codeVersion=1`만 허용한다. 수정 후보·이전 Report lineage·수정 후 재검증은 **35번**에서 구현한다. A2A 재개 `attempt`는 코드 수정 횟수와 별개다. QA의 COMPLETED나 기능 테스트 PASS를 제품 전체 SUCCESS로 바꾸지 않는다.

## 2. 입력과 보호된 기준

최초 입력은 기존 Orchestrator dispatch의 정확한 두 필드, `request`와 `snapshot`을 사용한다. `request` 안의 Run 설정·할당 Requirement·정책·보고서 기준과 `snapshot`의 논리 참조는 Host가 저장된 결과로 재구성해 대조한다. 임의 입력 JSON의 선언만으로 권한을 부여하지 않는다.

Host Context는 다음을 확인한다.

- 같은 Run/Scenario/Workspace의 유일한 RUNNING QA Step, `codeVersion=1`, 승인된 현재 A2A `attempt`.
- 초기 VALIDATING/fix 0, 아직 QA 완료 출력이 없는 상태, QA에 전달된 정확한 Source 참조.
- 완료된 Planner의 보호된 전체 Requirement·Acceptance Criteria, 완료된 Developer와 등록 Source의 Task·Artifact·출력 참조 관계.
- 동결 Scenario에 지정된 **QA 담당 Requirement만** 할당. SCN-001에서는 REQ-001·002·003·004·007의 다섯 UUID이며, 계약에 이 번호들을 하드코딩하지 않음.
- 최신 등록 Source와 private Source content의 동일성, QA READ_ONLY grant, 실제 Hash·이미지/Lock Manifest.
- 이미 저장된 Task/Context ID는 SDK의 opaque 원값 그대로 일치. 최초 dispatch 관측 전 아직 저장되지 않은 ID는 기존 정책대로 처리.
- 동결 모델·예산·실행 환경이 존재하고 `networkPolicy=DENY`인 상태. 동결 Scenario 누락 시 최신 Registry로 복구하지 않음.

REQ-005·006의 보안 판정은 Security Agent, REQ-008의 전체 추적은 Orchestrator 담당이다. QA Report의 coverage를 전체 여덟 Requirement coverage라고 표시하지 않는다. REQ-003의 QA 기능 검사도 별도의 Security 판정을 대신하지 않는다.

Context·Executor·Service·Test Store 생성자는 실행을 시작하지 않는다. Context/서비스 factory와 Provider는 신뢰된 Host capability다. 동기 factory는 Worker Thread에서 호출하지만 자체 강제 timeout이나 Thread 강제 종료를 제공하지 않는다. 운영자가 조회·설정 시간의 상한을 관리해야 한다.

## 3. 모델 Decision과 실제 Tool 권한

Decision은 닫힌 Schema의 다음 세 필드만 허용한다.

| 필드 | 제한 |
| --- | --- |
| `kind` | `READY`, `INPUT_REQUIRED`, `REJECTED` |
| `cases` | READY일 때만 최대 256개. `toolName/selector/testId/requirementId/title/expectedResult`의 여섯 필드만 허용 |
| `questions` | INPUT_REQUIRED일 때만 1~8개. 그 외 빈 목록 |

`toolName`과 `selector`는 Host가 승인한 QA_TESTS Unit scope/Browser suite 중에서만 선택한다. 서로 다른 Tool의 selector를 섞을 수 없다. `requirementId`는 현재 QA 담당 UUIDv4 enum에 정확히 포함되어야 하며 READY는 전체 할당 Requirement를 빠짐없이 연결한다. 같은 `(toolName, selector, testId)`를 두 Requirement에 중복 연결하지 않는다.

`title`과 `expectedResult`는 모델의 제한된 테스트 설명이며 보호된 Acceptance Criteria를 대체하지 않는다. 모델은 `outcome`, 실제 결과, Report/Artifact ID, Hash, Manifest, 실행 건수, ToolEvidence, 최종 Verdict 등의 추가 필드를 제출할 수 없다. 문자열·UTF-8 bytes·JSON 전체 크기·케이스 수를 제한하고 알려진 Credential과 제어 문자를 거절한다. 기존 제품 QA Artifact Schema를 Decision Schema로 변경하거나 약화하지 않는다.

모델에 실제 노출하는 Tool은 다음 세 개다.

- `read_project_file`: 같은 Frozen Source를 읽기 전용으로 읽는다. Working Source로 우회하지 않음.
- `write_test_file`: QA의 `outputs/qa/tests/` 안에만 테스트를 작성한다. Source/동결 Snapshot/Host 보호 테스트를 수정하지 않음.
- `read_test_report`: 승인된 기존 Unit/Browser 실행 보고서를 읽는다. 읽었다는 사실만으로 현재 완료 근거가 되지 않음.

`run_unit_tests`·`run_browser_tests`는 모델에 노출하지 않는다. READY 이후 Host가 고정한 Source와 승인된 설정으로 호출한다. 임의 Shell·서비스 명령·이미지·Network·모델 실행 설정을 Tool 인자로 받지 않는다. 파일 접근은 기존 MCP Schema·역할·Workspace·경로·비밀정보 검증을 계속 거친다.

## 4. 승인된 테스트와 불변 입력 보존

Host는 다음 두 테스트 집합을 분리해 설정한다.

| 집합 | 작성·변경 권한 | 실행 방식 |
| --- | --- | --- |
| `QA_TESTS` | QA가 전용 출력 영역에 작성 | Host가 scope/suite·경로·Runner·이미지·한도를 고정 |
| `PROTECTED` | Host가 승인한 고정 테스트 bytes | 모델에 선택·수정 권한 없음. Host의 `protected_cases` 연결로 실행 |

모델에는 생성할 테스트의 승인 경로·format·scope/suite 이름과 Browser JSON 형식 예시·허용 동작을 전달한다. 보호 테스트 원문과 Host 전용 케이스 연결은 모델 입력에 추가하지 않는다. 보호 테스트 참조는 동결 Run의 `protected_test_suite_ref`와 일치해야 한다. 동결 참조가 존재하는데 PROTECTED 설정이 하나도 없으면 실행 전에 거절한다. 참조 없는 일반 opt-in Run의 모델 작성 테스트만으로 독립 평가가 완료되었다고 주장하지 않는다. 모델의 READY에 특정 selector가 없더라도 **설정된 모든 QA_TESTS와 PROTECTED selector를 실행**한다. 검사를 생략해 PASS를 만드는 경로를 제공하지 않는다.

Unit 테스트는 기존 Python unittest Runner를 사용한다. Browser 테스트는 기존 `browser-suite-v1` 선언형 JSON·6개 승인 동작·Container 내부 local origin만 사용한다. pytest/Jest·임의 JavaScript·외부 서비스 접근을 추가하지 않았다.

각 실행 전 Host가 캡처한 Runner·테스트·승인 입력 bytes를 `QATestInputStore`에 불변 저장한다. 단순히 수정 가능한 Workspace 경로나 모델이 말한 Hash만 남기지 않는다. 실제 Tool의 private receipt가 동일 입력 Hash·파일 목록·Profile을 사용했는지 대조한 뒤 capture와 execution receipt를 연결한다. 캡처 후 파일이 바뀌어 실행 입력이 달라지면 실패한다.

Store는 같은 Run/Workspace/QA Step/Source와 READ_ONLY grant를 검사하고 기존 파일 수·크기 한도를 사용한다. UPDATE/DELETE/REPLACE로 기록을 덮어쓰지 못하도록 보호한다. 실행 실패·취소 뒤 receipt에 연결되지 않은 캡처가 남을 수 있으며 자동 삭제하지 않는다. 테스트 bytes는 private 저장소에만 보존하고 A2A Report·일반 로그·Trace에 원문을 넣지 않는다.

QA_TESTS는 모델이 작성한 테스트다. 실제 실행 기록과 변경 불가능한 입력은 **그 테스트를 실행했다는 근거**이지 테스트가 충분하거나 정직하다는 증명은 아니다. Host 보호 테스트·4번 독립 평가를 대체하지 않는다. 같은 UID의 프로세스가 DB/파일을 직접 악의적으로 교체하는 상황에 대한 새 OS 격리도 이번에 만들지 않았다.

## 5. 실제 실행 근거와 QA Artifact

테스트는 Working Source가 아닌 **QA에 전달된 동일 불변 Source**를 기존 Container Sandbox에서 실행한다. 설정·이미지·Lock·Network·실행 한도는 신뢰된 Host 정책과 동결 Run 설정에서 가져온다.

MCP 응답을 그대로 최종 근거로 채택하지 않는다. private ToolExecutionStore와 UnitTestOutputStore/BrowserTestOutputStore를 다시 읽어 다음을 대조한다.

- 현재 Run/Workspace/QA Step/Source, Tool 이름과 selector, 이번 논리 호출의 실제 실행 기록.
- Tool 성공 기록, 실제 저장된 Test output과 응답의 일치.
- 동일 ExecutionManifest·Host Profile·Runner/서비스 설정·이미지/Lock.
- 실행 직전 저장한 테스트 입력 bytes의 Hash·파일 목록과 receipt 입력의 일치.
- Browser는 승인 suite inventory·Host configuration·실제 case/step 결과도 일치.

최종 출력은 기존 `qa-report.json` Artifact 하나다. Host가 Project UUIDv4·A2A Artifact ID·시각·초기 `artifactVersion=1`·`previousArtifactId=None`·`codeVersion=1`을 작성한다. SDK의 opaque Task/Context를 보존하고 metadata의 runId/workflowStepId/projectArtifactId/artifactVersion을 기존 계약대로 유지한다.

각 보고서 testId는 Host가 `toolName:selector:실제RunnerTestId`로 구성하여 scope/suite 간 중복을 방지한다. Requirement와 title/expectedResult는 검증된 연결을 사용하고, 실제 결과는 private receipt에서만 가져온다. `actualResult/details`는 안전한 결과 코드이며 원본 stack trace·DOM·비밀번호·모델의 판정 prose를 넣지 않는다.

| 상황 | Report / A2A 처리 |
| --- | --- |
| 실제 case PASS | 해당 test PASS + 검증된 ToolEvidence |
| 정상 실행된 assertion FAIL | 해당 test FAIL + Tool PASS 근거. 전체 Report → A2A COMPLETED, Tool Retry 없음 |
| 계획한 case가 실제 보고서에 없음 | 해당 test UNVERIFIED, 실행 성공 증거를 그 case에 붙이지 않음 |
| Runner가 case를 SKIP으로 보고 | 해당 test UNVERIFIED, 해당 case ToolEvidence 없음 |
| 실제 실행 case의 Requirement 연결이 없음 | A2A FAILED / `QA_REPORT_BINDING_INVALID`, 완성 Artifact 없음. 성공·실패·SKIP case를 임의로 버리지 않음 |
| Docker/전송/timeout/입력/저장/receipt 검증 오류 | A2A FAILED, exit·결과·Manifest를 꾸민 Report 없음 |

기존 QA/ExecutionManifest/ToolEvidence Schema를 offline Registry로 검증하고 기존 `validate_completed_role_output()`으로 Source·Manifest·Requirement·식별자 관계를 대조한다. 외부 Schema URL을 fetch하지 않는다.

COMPLETED는 QA가 기능 검사 결과를 정상 보고했다는 뜻이다. Report에 FAIL/UNVERIFIED가 있어도 A2A 업무는 완료될 수 있다. 프로젝트 최종 Verdict·Issue 생성·수정 요청·다음 Agent 호출·Project Artifact Registry 등록은 **Orchestrator만** 담당한다. private 입력/receipt 저장은 최종 Registry 등록과 다르다.

## 6. 추가 입력·인증·실패·취소·예산

| 상황 | A2A 상태 |
| --- | --- |
| Host Context·최초 입력·서비스 factory 불일치 | REJECTED, LLM·테스트 실행 없음 |
| 추가 설명 필요 | INPUT_REQUIRED + 제한된 질문, 완성 Artifact 없음 |
| Provider 인증 오류 | AUTH_REQUIRED, Credential을 요청 본문으로 받지 않음 |
| 모델 거부/범위 밖 | REJECTED |
| Decision·입력 캡처·실행 근거·Report·예산 오류 | 안전한 상태 코드로 FAILED |
| 취소 | 기존 SDK Worker·시작된 파일/Store 작업·MCP/Container 정리 후 CANCELED |

INPUT_REQUIRED/AUTH_REQUIRED/REJECTED는 MCP teardown 뒤 게시하여 이전 Worker 정리와 재개 실행이 겹치지 않게 한다. 재개는 같은 Task/Context의 최초 입력을 보존하고 후속 답변을 clarification으로 분리한다. Source·Requirement·정책·Run 설정·Workspace·모델·Tool selector·식별자를 답변으로 덮어쓰는 것은 거절한다. Host가 현재 Step의 RUNNING/attempt를 승인해야 하며 재개 때 예산·deadline을 새로 만들면 안 된다.

QA 전용 파일·private 캡처·실행 기록이 중단 후 남을 수 있다. 자동 원복·삭제·기존 결과의 성공 근거 재사용은 하지 않는다. 이후 모델의 승인된 QA 파일 쓰기는 기존 MCP의 원자적 파일 쓰기·경로·역할 검사를 따른다. 재개 제어의 자동 Pipeline 연결은 후속 단계다.

모델과 Host Unit/Browser 호출은 같은 `ExecutionBudget`을 사용한다. Host 호출도 Tool 예산을 소비하고 동일 deadline을 전달한다. 모델 자동 Retry는 없으며 Tool Retry는 기존 29번의 최대 2회·오류/전달 상태 정책을 재사용한다. 정상 assertion 실패·SKIP은 제품 결과이므로 자동 재실행하지 않는다.

파일/입력/receipt 조립 Worker는 기존 취소 시 drain 정책을 사용한다. Container와 MCP child의 종료가 확인되기 전에 COMPLETED/CANCELED로 작업 종료를 광고하지 않는다. 신뢰된 factory·DB 저장·안전 정리까지 포함한 hard wall-clock 상한이나 프로세스 강제 종료 후 자동 복구를 보증하지 않는다.

Provider 오류 원문·Prompt·모델 Decision·Source·테스트 원문·Credential을 새 일반 로그/Trace에 추가하지 않는다. 알려진 Credential 탐지와 기존 redaction은 임의 비밀 전체를 탐지한다는 보증이 아니다. 정제된 UsageRecord 외에 비용/통합 Trace 영속화는 이번에 추가하지 않았다.

## 7. 명시적 Host 실행 구성

```python
from agents.main import create_app
from agents.runtime.qa import QAAgentExecutor
from agents.runtime.qa_context import SQLiteQAContextLoader
from agents.runtime.qa_services import QARuntimeServices

# 운영자가 구성한 기존 provider/repository/workspace_registry/artifact_store,
# QA 역할 MCPChildConfiguration + Frozen Source 참조,
# QA_TESTS/PROTECTED Unit/Browser 설정이 필요하다.
# shared_budget_resolver는 기존 Run 예산을 반환하며 새로 만들지 않는다.
# approved_protected_cases는 QACaseBinding의 Host 승인 tuple이다.
def services_for(execution):
    return QARuntimeServices(
        repository, workspace_registry, artifact_store,
        mcp_configuration=approved_qa_mcp_configuration_for(execution),
        protected_cases=approved_protected_cases,
    )

qa = QAAgentExecutor(
    provider=provider,
    context_factory=SQLiteQAContextLoader(repository, shared_budget_resolver),
    services_factory=services_for,
)
app = create_app(qa_settings, executor=qa)  # role=QA
```

이 예시는 준비된 Host 객체를 연결하는 방법이며 그대로 실행하는 독립 스크립트가 아니다. 실제 Provider 인증·고정 이미지·Runner/서비스 정책·동결 환경·완료된 Developer Source·실행 중 QA Step이 필요하다. Service의 기본 transport는 기존 local stdio MCP child이고 `client_factory`는 신뢰된 테스트 seam이며 모델이 지정하지 않는다.

- 기본 `create_app(settings)` / `python -m agents`: Bootstrap, 실행 Skill 없음, `executionReady=False`.
- 실제 QA 명시 주입: `measured-initial-qa` Skill, `executionReady=True`.
- 다른 역할에 QA 실행기를 주입하면 `AGENT_EXECUTOR_ROLE_MISMATCH`로 거절.
- 실행 준비 표시는 소프트웨어 경로의 구성 여부이며 Credential·Docker·테스트 PASS·제품 완성의 보증이 아님.
- 기본 Provider/서버/Orchestrator 자동 연결은 34번, 수정·재검증 루프는 35번, Workflow 재개/취소 제어는 36번, 전체 Run 예산/Trace 영속화는 34/37번 후속.

## 8. 개발정의서 준수 점검

| 기준 | 이번 구현 |
| --- | --- |
| §1 회원가입 기준 | 동결 기준 보존, QA 담당 다섯 Requirement만 검사·보고 |
| §2 책임 경계 | QA의 테스트 작성·실행·보고만. Developer/보안 판정/독립 비교 평가 대체 없음 |
| §3/§6~7 A2A 1.0 | 기존 공식 SDK·HTTP+JSON·opaque Task/Context·metadata 유지 |
| §4 상태/재시도 | 초기 codeVersion과 A2A 재개 attempt 분리, 기존 Tool Retry 정책 사용 |
| §5/§9 코드 전달 | 같은 등록 Source의 READ_ONLY grant·실제 Hash·Manifest 유지 |
| §8 MCP/권한 | 기존 stdio MCP·Tool Schema·QA 출력 경로 제한, Source/보호 기준 쓰기 불허 |
| §9 QA Artifact | 기존 Schema·초기 lineage·실제 입력/receipt로 Host 조립, 모델 outcome 금지 |
| §10 판정 | assertion FAIL과 Tool 오류 분리, missing/SKIP UNVERIFIED, COMPLETED≠전체 SUCCESS |
| §11 개인정보/Trace | 테스트 bytes는 private 불변 저장, 새 raw 로그 없음, 정제된 상태/Usage 재사용 |
| 1번+2번 범위 | 제품 서비스·Evaluation·development-log·의존성/Lock 변경 없음 |

이번으로 QA의 **최초 검증 단계**를 추가했으며 1번+2번 전체가 완료된 것은 아니다. 비밀번호 보호 정책 보완과 기존 Agent DB 초기 WAL 경쟁은 제외 범위를 유지한다. 실제 회원가입 품질·Security PASS·Single/Multi 비교·기업 시연 완료를 검증한 것으로 표시하지 않는다.

## 9. 검증과 인계

```bash
PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -p 'test_qa*.py' -q
PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -p 'test_agent_bootstrap.py' -q
PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -q
PYTHONPATH=src .venv/bin/python -m compileall -q src/agents src/mcp_tools src/orchestrator
.venv/bin/pip check
git diff --check
```

- QA 전용 **129개 통과**(55.276초): Context 26, Decision 39, Services 25, private 입력 Store 14, 실제 SDK HTTP/MCP/SQLite/Git 경계 통합 25개. 가짜 결과 차단·QA 담당 UUID coverage·보호 기준 누락 차단·불변 입력/receipt·실패/SKIP/누락·재개/취소/완료 재실행 차단을 검사한다.
- Bootstrap/명시적 실행 준비 **11개 통과**(0.145초): QA opt-in·역할 불일치·기존 Planner/Developer Skill·기본 CLI·기본 QA 거절·공식 Card/auth.
- Unit/Browser를 포함한 기존 MCP private Store 회귀 **263개 통과**(65.129초). QA 재개 attempt가 증가해도 동일 codeVersion의 실제 receipt 조회·보고가 가능한지 추가 확인했다.
- 프로젝트 전체 회귀 **2,234개 통과**(338.891초). 기존 Orchestrator·Planner·Developer·MCP·Snapshot·Sandbox·상태/권한/Artifact 회귀를 포함한다.
- Python compile·`pip check`·`git diff --check` 통과. 의존성/Lock은 변경하지 않았다.
- 테스트의 Provider/Docker는 Fake다. **외부 LLM API·실제 Docker·Chromium·실제 QA stdio end-to-end 실행은 검증하지 않았다.** 기존 stdio 회귀와 Fake transport 검사는 실제 제품 기능 검증과 구분한다.

전체 회귀의 기존 MCP local socket/stdio 검증은 실행 권한을 승인받아 수행했다. 생성 Source와 테스트는 Host에서 실행하지 않았으며 승인된 Container 정책의 실제 호환성은 별도 운영 환경에서 확인해야 한다. 기존 SDK의 cross-replica streaming 미구성 경고는 남아 있으며, 이번 비스트리밍 실행을 분산 streaming 지원으로 표시하지 않는다.

Git commit/push는 수행하지 않는다. 테스트가 사용하는 Git 저장소는 별도 임시 fixture이며 프로젝트 branch를 변경하지 않는다.

커밋 메시지: `실제 테스트 근거로 QA 보고서를 생성하는 QA Agent 구현`

다음 작업: **33번 — Security Agent 구현**.
