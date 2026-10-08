# 31. Developer Agent

작성 기준: 2026-10-09. 범위는 1번+2번의 **최초 코드 후보 구현과 실제 Build 근거를 담은 A2A 출력**이다. 3번 제품 웹서비스·4번 독립 평가·팀원 연결은 변경하지 않는다.

## 1. 구현 내용

- `DeveloperAgentExecutor`: 기존 LLMEngine·역할 Prompt·TrackedMCPExecutor를 사용하는 명시적 실행기.
- `DeveloperExecutionContext` / `SQLiteDeveloperContextLoader`: 완료된 Planner의 Requirement Artifact와 동결 Run 설정·현재 Developer Step을 읽기 전용으로 검증.
- `developer_contract.py`: 모델의 제한된 작업 Decision 계약. 모델이 Source Hash·변경 목록·Build 근거를 창작하지 못하도록 최종 Artifact와 분리.
- `GitDeveloperCheckpoint`: 실제 Source의 bytes/mode 차이를 계산하고 승인된 baseline을 부모로 하는 실제 Git commit object 생성.
- `DeveloperRuntimeServices`: candidate freeze → MCP Build → private 실행 기록 대조 → 기존 Artifact 세 개의 Host 조립.
- 명시적으로 Developer 실행기를 주입할 때만 Developer Skill·`executionReady=True` 광고. 기본 서버는 Bootstrap 유지.

```text
기존 A2A admission → SUBMITTED
→ Host Run/Step/Planner 결과/동결 설정/공유 예산 확인
→ 최초 승인 입력·현재 Task/Context·추가 답변 검증
→ WORKING
→ 승인된 Git baseline과 깨끗한 Source 확인
→ LLM이 MCP read_project_file / write_source_file로 Source 구현
→ 모델의 READY Decision 검증
→ Host가 실제 diff → Git commit object → 불변 Source Snapshot 생성
→ 동일 Snapshot의 MCP run_build
→ private Tool/Build Store의 실행 기록·Manifest·Profile 대조
→ 기존 Schema 및 Orchestrator 출력 parser 검증
→ Source/Change/Build Artifact 게시 → COMPLETED
```

현재는 `IMPLEMENTING`, `fix_attempt=0`, `codeVersion=1`만 허용한다. `FIXING`은 거절한다. Issue 기반 수정·이전 Artifact lineage·다음 코드 후보·자동 수정 루프는 **35번**에서 구현한다. Developer의 자체 Unit Test 자동 실행도 이번에 추가하지 않았다. 완료된 Build Report를 QA/Security 검증이나 제품 전체 SUCCESS로 바꾸지 않는다.

## 2. 입력과 보호된 기준

최초 입력은 기존 Orchestrator의 다음 여섯 필드를 그대로 사용한다.

| 필드 | 신뢰된 기준 |
| --- | --- |
| `workspaceId` | 현재 Run에 발급된 Workspace |
| `scenario` | Run 생성 시 동결한 Scenario 계약 |
| `runConfiguration` | 동결 모델·실행 예산·이미지/Lock/Network 환경 |
| `plan` | 완료된 Planner의 canonical requirements + implementationPlan |
| `sourceArtifact` | 기존 dispatch 이름을 유지한 **Planner REQUIREMENT 참조**. 최초 SOURCE 참조가 아님 |
| `outputContract` | 기존 Developer의 Source/Change/Build 출력 계약 |

입력 JSON 자체를 신뢰 근거로 삼지 않고 Host Context와 대조한다. SCN-001의 REQ-001~008 UUID·설명·Acceptance Criteria·제외 범위를 그대로 보존한다. 동결 Scenario가 누락되면 최신 Registry로 복구하지 않는다. Proto Struct의 정수형 double은 허용하지만 `true`를 숫자 `1`과 같다고 인정하지 않는다.

SQLite Loader는 하나의 읽기 snapshot에서 다음을 확인한다.

- 같은 Run/Scenario/Workspace, 유일한 RUNNING Developer Step, 현재 승인된 A2A `attempt`.
- 최초 구현 단계와 `codeVersion=1`, 아직 제품 출력 Artifact가 없는 상태.
- 완료된 유일 Planner와 그 Task·A2A Artifact·Project Requirement Artifact의 정확한 소유 관계.
- 이미 저장된 Developer Task/Context ID는 opaque 원값 그대로 일치. 최초 dispatch 관측 전에 미저장 ID는 허용.
- 동결 모델·환경·예산이 실제로 존재하고 `networkPolicy=DENY`인 상태.

Context/Executor/Service/Checkpoint 생성자는 실행을 시작하지 않는다. Context/서비스 factory는 신뢰된 Host capability다. 동기 factory는 Worker Thread에서 실행하지만 자체 강제 timeout이나 Thread 강제 종료는 제공하지 않는다. 운영자는 조회 시간을 제한해야 한다.

## 3. 모델 Decision과 Tool 권한

모델 Decision은 닫힌 Schema의 다음 세 필드만 허용한다.

| 필드 | 제한 |
| --- | --- |
| `kind` | `READY`, `INPUT_REQUIRED`, `REJECTED` |
| `summary` | READY일 때만 비어 있지 않은 제한된 설명; 실제 Build 근거가 아님 |
| `questions` | INPUT_REQUIRED일 때만 최대 8개; 그 외 빈 목록 |

모델 응답에 Artifact ID·Hash·변경 목록·Manifest·ToolEvidence·Build exit code 등의 추가 필드를 허용하지 않는다. 기존 제품 Artifact Schema를 LLM Decision Schema로 대체하거나 변경하지 않는다.

모델에 실제 노출하는 Tool은 `read_project_file`, `write_source_file` 두 개다. 기존 역할·Workspace/경로·비밀정보 검증과 TrackedMCPExecutor를 거친다. 최초 후보에는 승인된 frozen patch base가 없으므로 `apply_patch`는 노출하지 않는다. `run_build`는 모델이 임의 snapshotId로 호출하지 못하며 Host가 freeze 후 호출한다. 권한 없는 Tool이 같은 batch에 있으면 첫 파일 수정 전 전체 batch를 거절한다.

QA Test·Security 기준·Requirement 수정 권한을 추가하지 않는다. 모델이 READY를 반환했어도 실제 변경이 없으면 `DEVELOPER_CHECKPOINT_NO_CHANGES`로 실패하며 가짜 Change Report를 만들지 않는다.

## 4. 실제 Git checkpoint와 Snapshot

Host가 다음 값을 사전에 승인해야 한다.

- Source 저장소의 **완전한 baseline commit OID**(SHA1 40자 또는 SHA256 64자). `HEAD`·축약 Hash·Git revision 표현식 불허.
- 논리 `repository_id`, 정규화된 `lock_path`, 기존 Artifact Store의 export 한도.
- Source가 baseline과 bytes/mode까지 일치하는 깨끗한 상태. 기존 운영자 변경을 모델 작업으로 채택하지 않음.

Checkpoint는 파일 Tool과 같은 Workspace lock을 사용하고 descriptor 기반 읽기·`O_NOFOLLOW`·bytes/mode/inode 재확인·파일 개수/크기 제한을 적용한다. Symlink·hardlink·특수 파일·위험/비밀 경로·중첩 Git·MCP 임시 파일·casefold 충돌을 거절한다. dependency Lock의 bytes/mode 변경 또는 삭제도 거절한다.

실제 차이로 `ADDED/MODIFIED/DELETED`를 계산하며 Change Report 경로는 기존 계약대로 `source/…`이다. 고정 Git 실행기와 최소 환경으로 private index·plumbing 명령을 사용한다. Git hook·clean filter·fsmonitor·signing·외부 설정·Credential 환경변수를 실행/상속하지 않는다. 제품 코드를 Host에서 실행하는 경로는 없다.

**새 commit object는 실제로 생성하지만 Source 저장소의 branch/HEAD/기존 index를 이동하지 않는다.** 생성된 후보를 기존 GitSnapshotBuilder와 Artifact Store로 freeze하고 그 archive를 Build 입력으로 사용한다. 이번 작업은 제품 Git push/merge/배포 기능이 아니다. Workspace의 수정된 Source를 자동 원복하지도 않는다.

Checkpoint는 한 번만 사용한다. 실패한 object 생성 작업을 재시도하지 않는다. 객체가 일부 생성된 뒤 실패할 수 있으며 성공 후보 commit도 branch에서 참조되지 않는다. frozen archive는 Store에 남지만 Git object의 장기 보존/GC와 다음 작업의 baseline 채택은 Host의 후속 관리 영역이다.

협력 Tool의 동시 쓰기와 경로 교체를 검증하지만 같은 UID로 Source/`.git`/DB를 악의적으로 직접 교체하는 프로세스에 대한 OS 격리를 새로 만든 것은 아니다.

## 5. Build 근거와 A2A Artifact

Build는 working copy가 아닌 **방금 freeze한 동일 Source Artifact**로 기존 Container Sandbox에서 실행한다. 실행 명령·이미지·한도는 Host의 `BuildConfiguration`에서 가져온다. MCP 응답만으로 완료하지 않고 같은 SQLite의 private ToolExecutionStore·BuildOutputStore를 다시 읽어 다음을 확인한다.

- 동일 Run/Workspace/Developer Step/Source 및 실제 실행 ID.
- Tool 성공 기록, 저장된 Build output과 응답 일치.
- 동일 Source Manifest·이미지/Lock·Host Profile.
- MCP 호출 시간 상한에 맞춘 Build timeout의 기존 좁히기 규칙까지 일치. Profile의 다른 값은 변경하지 않음.

최종 결과는 기존 계약의 세 Artifact다.

| 파일 | 신뢰된 생성 근거 |
| --- | --- |
| `source-snapshot.json` | 실제 Git commit, frozen archive Hash, 이미지/Lock Manifest, Host 발급 Project UUIDv4 |
| `change-report.json` | 실제 baseline/candidate bytes·mode 차이 + 검증된 모델 summary |
| `build-report.json` | private Build receipt의 exit/duration/output refs/Manifest + 실제 ToolEvidence |

초기 `artifactVersion=1`, `previousArtifactId=None`, `codeVersion=1`을 유지한다. Task/Context ID는 기존 SDK의 opaque ID를 사용한다. A2A Artifact ID와 세 Project Artifact ID는 신뢰된 Host가 발급한다. metadata는 runId/workflowStepId/projectArtifactId/artifactVersion이다.

고정 저장소의 기존 JSON Schema 5개를 offline Registry로 검증한다. URN `$id`와 기존 sibling filename 참조의 로컬 alias를 등록하며 외부 문서를 fetch하지 않는다. 기존 `validate_completed_role_output()`으로 세 Artifact의 identity·Requirement·Source/Manifest 참조도 대조한다.

| Build 상황 | 결과 |
| --- | --- |
| 실제 Build exit 0 | Tool PASS + Build PASS, 세 Artifact → A2A COMPLETED |
| 정상 실행된 컴파일 오류/비영 exit | Tool PASS + Build FAIL/PRODUCT, 세 Artifact → A2A COMPLETED. Tool Retry 없음 |
| Docker/전송/timeout/저장 오류로 유효 receipt 없음 | A2A FAILED. exit/Manifest를 꾸며 완성 Artifact를 만들지 않음 |

COMPLETED는 Developer가 구현 결과와 실제 Build 실패까지 **정상 보고했다는 뜻**이지 제품 SUCCESS가 아니다. 프로젝트 Verdict·다음 Agent 호출·Project Artifact Registry 등록은 기존 Orchestrator의 책임이다. Service가 private Source content를 stage하는 것은 최종 Registry 등록과 다르다.

세 Artifact는 조립/검증과 MCP 정리가 끝난 후 SDK 이벤트로 게시한다. 게시 자체가 단일 DB transaction인 것은 아니다. 게시 중 오류 시 일부 Artifact 이벤트가 남을 수 있지만 세 개가 검증된 COMPLETED로 확정되지 않은 Task를 성공 결과로 소비해서는 안 된다.

## 6. 실패·추가 입력·취소·예산

| 상황 | A2A 처리 |
| --- | --- |
| Host Context/입력/서비스 불일치 | REJECTED, 모델 실행 없음 |
| 추가 설명 필요 | INPUT_REQUIRED + 제한된 질문, 완성 Artifact 없음 |
| Provider 인증 오류 | AUTH_REQUIRED, Credential 입력/출력 요청 없음 |
| 모델 거부/범위 밖 | REJECTED |
| Decision/실제 변경/Build receipt/예산 오류 | 안전한 상태 코드로 FAILED |
| 취소 | 기존 SDK Worker 종료 및 시작된 mutating 작업/Container 정리 후 CANCELED |

MCP child 종료를 마친 뒤에만 INPUT_REQUIRED/REJECTED를 게시한다. 느린 종료 중에는 WORKING을 유지하여 같은 Task의 재개 요청이 이전 Worker의 queue 정리와 겹치지 않게 한다. AUTH_REQUIRED도 MCP 종료 후 예외를 처리하여 게시한다.

깨끗한 상태의 INPUT_REQUIRED/AUTH_REQUIRED 재개는 durable Task history의 최초 입력을 복구하고 후속 답변을 별도 clarification으로 전달한다. 같은 Task/Context·공유 예산을 유지하며 Host가 저장된 Step의 RUNNING/attempt를 먼저 승인해야 한다. 보호된 plan/Scenario/Run 설정/Workspace/식별자 덮어쓰기는 거절한다.

**Source를 이미 수정한 뒤 질문/인증 대기에 들어간 경우 자동 재개하지 않는다.** 다음 실행의 clean-baseline 검사가 실패하고 `DEVELOPER_CHECKPOINT_BASELINE_MISMATCH`를 반환한다. 기존 변경을 삭제·재전송·채택하지 않는다. 운영자 reconciliation과 Workflow 입력 재개 제어는 36번 후속이다.

이번에 기존 Source/Build/Tool/Sandbox의 **Developer 경로만** A2A `attempt`와 `run.fix_attempt`를 분리했다. 질문 후 attempt가 1이어도 최초 후보 codeVersion은 1, fix_attempt는 0이다. 다른 Run/Step·중복 활성 Step·현재 Source/환경/코드 버전 검사와 QA/Security 경로는 유지한다. 공통 재시도/수정 상수는 바꾸지 않는다.

모델과 Host Build 모두 같은 `ExecutionBudget`을 사용한다. Host Build도 Tool 호출 예산을 소비하고 동일 deadline을 전달한다. factory 호출마다 예산을 새로 만들거나 재개할 때 deadline/사용량을 초기화하면 안 된다. Tool Retry는 기존 29번의 최대 2회 및 불명확한 write 재전송 금지 정책을 재사용한다. 모델 자동 Retry는 없다.

Git/파일/freeze/receipt 조립을 수행하는 기존 `_run_file_operation`은 취소 시 시작된 Worker Thread를 drain한다. Git subprocess와 private index를 정리하고 MCP/Container 정리 전에 완료를 광고하지 않는다. 안전 정리·DB 저장·신뢰된 Host factory까지의 hard wall-clock 상한이나 프로세스 강제 종료 후 자동 복구를 보증하지 않는다. 실패/취소 후 Source 변경·Git object·private Snapshot/Tool 기록이 남을 수 있으므로 자동 초기화하지 않는다.

Provider 오류 원문·Prompt·모델 JSON·Source·Credential을 새 로그/Trace에 추가하지 않는다. 알려진 Credential 검증과 기존 redaction을 사용하지만 모든 임의 비밀을 탐지한다고 주장하지 않는다. 정제된 UsageRecord 전달 외에 비용/통합 Trace 영속화는 이번에 추가하지 않았다.

## 7. 명시적 Host 실행 구성

```python
from agents.main import create_app
from agents.runtime.developer import DeveloperAgentExecutor
from agents.runtime.developer_context import SQLiteDeveloperContextLoader
from agents.runtime.developer_services import DeveloperRuntimeServices

# 운영자가 구성한 기존 provider/repository/workspace_registry/artifact_store,
# Developer 역할 MCPChildConfiguration + BuildConfiguration이 필요하다.
# shared_budget_resolver는 기존 Run 예산을 반환하며 새로 만들지 않는다.
# approved_baseline_commit는 source/ Git의 완전한 승인 commit Hash다.
def services_for(execution):
    return DeveloperRuntimeServices(
        repository, workspace_registry, artifact_store,
        mcp_configuration=approved_mcp_configuration_for(execution),
        baseline_commit_hash=approved_baseline_commit,
        repository_id=approved_repository_id,
        lock_path=approved_lock_path,
    )

developer = DeveloperAgentExecutor(
    provider=provider,
    context_factory=SQLiteDeveloperContextLoader(repository, shared_budget_resolver),
    services_factory=services_for,
)
app = create_app(developer_settings, executor=developer)  # role=DEVELOPER
```

이 예시는 준비된 Host 객체를 연결하는 방법이며 그대로 실행하는 독립 스크립트가 아니다. 실제 이미지/Build 명령/동결 환경/Source baseline/Provider 인증/실행 중 Step이 필요하다. Service의 기본 transport는 기존 local stdio MCP child다. `client_factory`는 신뢰된 테스트 transport seam이며 모델이 지정하지 않는다.

- 기본 `create_app(settings)` / `python -m agents`: Bootstrap, 실행 Skill 없음, `executionReady=False`.
- 실제 Developer 명시 주입: `measured-initial-implementation` Skill, `executionReady=True`.
- 다른 역할에 Developer 실행기를 주입하면 `AGENT_EXECUTOR_ROLE_MISMATCH`로 거절.
- executionReady는 경로 구성 여부이며 실제 Credential·Docker·Build 성공·제품 완성을 보증하지 않음.
- 기본 Provider/서버/Orchestrator 자동 연결은 34번, 수정 루프는 35번, 재개/취소 제어는 36번, 전체 Run 예산/Trace 영속화는 34/37번 후속.

## 8. 개발정의서 준수 점검

| 기준 | 이번 구현 |
| --- | --- |
| §1 회원가입 기준 | 동결 REQ-001~008·Acceptance Criteria·제외 범위 보존 |
| §2 책임 경계 | Developer Source 구현·Build 보고만. QA/Security/최종 Verdict 대체 없음 |
| §3/§6~7 A2A 1.0 | 기존 공식 SDK·HTTP+JSON·send/get/cancel·opaque Task/Context 유지 |
| §4 상태/재시도 | A2A attempt와 코드 수정 Cycle 분리, 기존 Tool Retry만, uncertain write 자동 replay 없음 |
| §5/§9 코드 전달 | 실제 commit → immutable Snapshot → 같은 Snapshot Build/Manifest, 모델 Hash 창작 금지 |
| §8 MCP/권한 | 기존 stdio MCP/SDK·Tool Schema 유지, Developer 권한만, Host 승인 Build Profile |
| §9 Artifact | 기존 Source/Change/Build Schema·metadata·초기 lineage 유지, Registry 책임 분리 |
| §10 판정 | Build PRODUCT FAIL과 Tool 실패 분리, A2A COMPLETED≠전체 SUCCESS |
| §11 개인정보/Trace | 새 raw 오류/Prompt/Source 로그 없음, 정제된 상태/Usage 재사용 |
| 1번+2번 범위 | 제품 서비스·Evaluation·development-log·의존성/Lock 변경 없음 |

이번으로 Developer의 **최초 구현 단계**를 추가했으며 1번+2번 전체가 완료된 것은 아니다. 비밀번호 보호 정책 보완·기존 Agent DB 초기 WAL 경쟁은 제외 범위를 유지한다. 실제 LLM 생성 품질·제품 회원가입 완성·Single/Multi 비교·기업 시연도 검증한 것으로 표시하지 않는다.

## 9. 검증과 인계

```bash
PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -p 'test_developer*.py' -q
PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -q
PYTHONPATH=src .venv/bin/python -m compileall -q src/agents src/mcp_tools src/orchestrator
.venv/bin/pip check
git diff --check
```

Developer 전용 신규 테스트는 **117개**(Decision 계약 31·Host Context 14·실제 Git checkpoint 31·Agent 통합 41)다. 실제 Git(SHA1/SHA256)·SQLite·FileTools·BuildTools·SandboxRuntime·private receipt·A2A HTTP Handler·기존 Orchestrator parser를 검증한다. 느린 MCP 종료 중에는 대기/거절 상태를 먼저 게시하지 않는 것도 확인한다. Provider와 Docker daemon은 Fake이며 Developer 통합의 MCP peer는 실제 Dispatcher를 호출하는 테스트 adapter다. **외부 LLM API·실제 Docker Build·Developer의 실제 stdio end-to-end 실행은 검증하지 않았다.** 기존 stdio 회귀는 별도로 포함한다.

기존 Source/Build Store 테스트의 A2A attempt=fix_attempt 가정을 수정하고, 정상 재개 및 잘못된 코드 버전/새 Fix Cycle/Task 불일치 거절 회귀 6개를 추가했다. Developer 전용 117개와 합쳐 이번 신규 테스트는 123개다.

- 최종 전체 회귀: **2,092개 / 254.365초 / OK**. MCP cleanup 순서 보완 및 Store의 attempt 분리 회귀까지 반영한 동결 코드 기준이다.
- Developer 통합 41개, Source/Build Store 90개, Git checkpoint 관련 77개도 각각 통과했다.
- `compileall`·`pip check`·`git diff --check` 및 신규 파일의 trailing whitespace 검사: 통과. 의존성 추가/Lock 변경은 없다.
- 기존 SDK event_stream/Streaming 경고와 의도적인 실패 fixture 로그는 테스트 실패가 아니다. pip의 기존 캐시 권한 경고도 의존성 충돌이 아니다.

Git commit/push는 수행하지 않는다. 테스트의 Git commit은 별도 임시 fixture 저장소에서만 실행한다. 쉬운 한 문장 설명은 사용자 요청대로 생략한다.

커밋 메시지: `실제 코드 변경과 Snapshot 빌드 근거를 연결하는 Developer Agent 구현`

다음 작업: **32번 — QA Agent 구현**.
