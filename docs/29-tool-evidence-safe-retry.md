# 29. Tool 실행 근거·오류 분류·안전 Retry

작성 기준: 2026-10-08. 범위는 1번+2번의 MCP 실행 어댑터와 실행 근거다. 3번 제품 서비스, 4번 독립 평가/비교 실험 및 해당 연결은 변경하지 않는다.

## 1. 구현 내용

- Client 오류에 승인된 Tool 오류 코드와 요청 전달 상태를 보존한다. 기존 성공 dict와 `MCPClientError.code`는 유지한다.
- `execution_policy.py`: 입력 Hash, 제품 결과/Tool 오류 구분, 안전 확인 및 최대 2회 Retry 정책을 구현한다.
- `execution_store.py`: 논리 호출·시작·종료를 SQLite에 불변 저장하고 실제 Build/Unit/Browser/Security 실행 기록을 대조한다.
- `execution_runtime.py`: 실제 MCP Client를 감싸 기록·안전 Retry를 수행하는 선택적 `TrackedMCPExecutor`를 구현한다.
- 기존 `ToolExecutionEvidence`/`ToolAttemptEvidence`로 변환할 수 있도록 연결한다. 새로운 제품 Artifact Schema나 Tool 이름을 만들지 않는다.

기본 Agent의 `executionReady=False`는 유지한다. 이 단계는 도구를 사용할 실행 기반이며 Planner/Developer/QA/Security 역할 구현은 30~33번, 기본 Pipeline 연결은 34번이다.

## 2. 호출과 근거의 식별자

| 구분 | 식별자/의미 |
| --- | --- |
| 논리 Tool Call | Host가 발급한 UUIDv4. 최초 호출과 Retry를 하나로 묶음 |
| Attempt | `0`, `1`, `2`. 코드 수정 횟수인 `fix_attempt`와 별개 |
| Attempt ID | 각 시도의 Host 발급 UUIDv4 |
| 물리 실행 ID | 22~28번 Sandbox가 발급한 별도 UUIDv4 |
| `executionManifestId` | 실제 Build/Test/Scan Store에 저장된 실행 기록 UUIDv4 |
| Source Manifest | Source Artifact/Commit/Tree/Snapshot Hash/Image/Lock/Code Version의 기존 고정 계약 |

`ToolExecutionEvidence.executionId`는 이 어댑터의 **논리 호출 UUID**다. 실제 Container 실행 ID와 실행 기록 UUID는 원장의 Attempt에 별도로 연결한다. 실패가 Container 시작 이전에 발생했다면 존재하지 않는 물리 실행 ID를 만들지 않는다.

## 3. 실행 순서

```text
Host Client + 현재 Workflow Step
→ 기존 Tool Schema/역할/Workspace/비밀정보 확인
→ 동일한 논리 입력의 canonical Hash + Host 설정 Hash 고정
→ Tool 이름·Run/Step/역할·Source·동결 환경을 원장에 저장
→ 실제 Client 호출 전에 Attempt STARTED를 원자적으로 저장
→ 하나의 실제 MCP 요청
→ 정상 결과 / 승인된 오류 / 결과 불명 구분
→ 성공이면 실제 불변 실행 기록·Source·설정 선택자 대조
→ Attempt FINISHED를 저장
→ 안전한 인프라 오류에 한하여 같은 입력·같은 호출 ID로 Retry
```

재시도는 최초 호출 1회 + 최대 2회, 총 최대 3회다. 입력·Tool·Step·Source·Host 설정을 바꿔서 같은 호출을 이어갈 수 없다. 각각의 Client 호출 전에 저장한 STARTED가 남아 있으면 다음 Attempt를 허용하지 않는다.

전체 논리 호출은 하나의 monotonic deadline을 공유한다. 기본 총 시간은 `MCPChildConfiguration.max_call_seconds`이며, LLM의 더 짧은 deadline이 있으면 더 짧은 값을 사용한다. Retry마다 시간을 초기화하지 않는다. 남은 시간이 부족하면 새 Attempt를 만들지 않고 중단한다.

이 deadline은 새 SDK 요청과 Retry의 실행 예산이다. 소유 Container 취소 정리·SQLite 완료 저장을 강제 중단하는 wall-clock 보장은 아니며, 안전한 정리/기록을 기다리는 시간은 이를 초과할 수 있다.

## 4. 오류와 재시도

`MCPDeliveryState`는 결과 지식이지 rollback 증명이 아니다.

| 전달 상태 | 의미 |
| --- | --- |
| `NOT_SENT` | 현재 Tool 요청이 SDK 호출에 진입하지 않음 |
| `REPLIED` | 응답을 수신함. 작업 성공/복구를 의미하지 않음 |
| `UNKNOWN` | 요청 처리 여부/결과를 확인할 수 없음 |

Client는 `isError`의 TextContent가 정확히 하나이고 전체 문자열이 승인된 코드와 일치할 때만 `tool_error_code`를 보존한다. 부가 설명·공백·JSON 문구·두 번째 메시지·임의 structured payload·peer metadata는 재시도 권한이 아니다. Protocol 오류는 승인된 표준 숫자 코드만 보존하며 SDK timeout/connection 오류의 원문을 저장하지 않는다.

| 상황 | 정책 |
| --- | --- |
| 확실히 미전송된 일시적 Startup/Resource 오류 | 최대 2회 Retry 후보 |
| 비쓰기 Tool의 승인된 Startup/Resource 오류 응답 | 최대 2회 Retry 후보 |
| Tool Timeout/Transport 결과 불명 | 안전 확인이 없으면 `INSPECT_STATE` |
| Schema/Permission/Secret/Path/Unsupported 오류 | `DO_NOT_RETRY` |
| Profile 누락·Scanner/Runner/Build의 일반 실행 오류 | 원인을 추측하지 않고 `DO_NOT_RETRY` |
| 응답 Schema/의미/무결성 불명 | `INSPECT_STATE`, 안전 확인 Boolean만으로 Retry 불가 |
| 정상 nonzero Build | Tool 완료 + 제품 Build 실패. Retry 없음 |
| 정상 QA Assertion 실패 | Tool 완료 + QA 실패. Retry 없음 |
| 정상 Scanner Finding | Tool 완료 + 보안 의심 항목. Retry 없음 |
| 미확인 파일 Write/Patch 결과 | `WRITE_RESULT_UNKNOWN` + `INSPECT_STATE` |

`PROCESS_STARTUP_FAILURE`/`RESOURCE_BUSY`는 서버의 승인된 인프라 코드다. 기존 `SANDBOX_ERROR`나 오류 문구를 이 코드로 임의 변환하지 않는다. SDK Client에는 숨은 Retry/재협상/요청 재전송을 추가하지 않는다.

### 조건부 안전 확인

`RetrySafetyConfirmation`은 신뢰된 Host의 **읽기 전용 상태 확인 결과**다. 논리 호출 UUID·현재 Attempt·정확한 입력 Hash가 같고, 실행 정리가 끝났으며 결과가 적용되지 않았음을 확인해야 한다. 기본 `safety_verifier=None`에서는 Timeout/Transport 불명 요청을 재실행하지 않는다.

모델이 전달한 `retrySafe`, peer metadata 또는 단순히 “읽기 전용 Tool이니까 안전하다”는 추측을 사용하지 않는다. Hash/ID가 다르거나 확인 함수가 실패/시간 초과하면 Retry하지 않는다. 동기 확인은 Worker Thread로 실행하여 이벤트 루프를 막지 않으며, deadline 이후 결과는 재실행 권한으로 사용하지 않는다. 확인 함수 자체가 읽기 전용이어야 하고 timeout으로 이미 실행 중인 Thread를 강제 종료할 수는 없다.

Write/Patch가 전송된 뒤 결과가 불명확하면 위 Boolean 확인만으로 재전송하지 않는다. Host가 현재 파일 Hash·원래 기대 Hash·원하는 결과를 먼저 비교하고 사람이 판단하거나 후속 제어 흐름에서 처리해야 한다. 자동 Hash reconciliation/rollback/Write 재개는 이번 구현에 없다. 다중 파일 Patch를 부분 적용 여부 확인 없이 재실행하지 않는다.

## 5. 불변 저장과 실제 결과 검증

실행 원장은 다음 세 테이블로 나뉜다.

- `tool_execution_calls`: 호출 ID, Run/Workspace/Step/역할, Tool, 입력·Host 설정·검사 선택자 Hash, 동결 Run 설정 Hash, Source Manifest.
- `tool_execution_attempt_starts`: Attempt ID/번호, 시작 시간, 기존 해당 실행 Store의 Run별 rowid 경계.
- `tool_execution_attempt_finishes`: 결과/오류 종류/전달 상태/Retry 판단/시간, 정제된 출력 Hash, 실제 실행 기록 ID·물리 실행 ID·Artifact 참조.

SQL UPDATE/DELETE/REPLACE, 중복 ID, 건너뛴 Attempt, 4번째 실행, 완료되지 않은 Attempt의 재시도를 거부한다. 각 claim은 하나의 transaction에서 실행하므로 동시에 호출해도 같은 Attempt를 두 번 시작할 수 없다. 과거 기록 조회는 활성 상태가 아니어도 가능하지만 Run/Workspace/Step/역할·동결 설정·Source/grant·내용 Hash를 다시 검증한다.

Build/Test/Scan 성공은 아래 조건을 만족해야 저장한다.

- Tool 응답을 실제 해당 Store의 기록과 완전히 대조한다.
- Run/Workspace/Step/역할·Source·Manifest가 동일해야 한다.
- 선택한 Host Build Profile 또는 입력 `testScope`/`testSuite`/`scannerProfile`의 Hash가 실제 실행 Profile 이름의 Hash와 같아야 한다.
- 실제 실행 기록이 현재 claim 이후에 저장되어야 한다. 과거 미사용 기록을 새 호출의 결과로 연결하지 못한다.
- 같은 실제 실행 ID/기록 ID를 다른 논리 호출에서 재사용하지 못한다.
- 논리 호출/Attempt/기존 Source·제품 Artifact·실행 Store의 ID 충돌을 차단한다.

rowid 경계는 기존 실행 Store의 append-only 정책을 전제로 한다. DB 복원/마이그레이션으로 rowid를 바꾸는 운영 작업은 별도 무결성 점검이 필요하다. 이 경계와 결과 대조는 이전 결과의 재사용을 막는 기능이지 원격 실행의 암호학적 attestation/요청 nonce 계약은 아니다.

공개 입력 Hash는 Client에서 Schema/비밀정보 검증 후 계산한다. 원장 자체가 원본 argument를 다시 보관하거나 Hash에서 내용을 복원하지는 않는다. 실제 테스트 파일·Runner·Scanner 정책/버전/Input Hash는 25~28번의 기존 실행 기록에 남아 있다.

### PASS와 제품 성공 구분

`ToolExecutionOutcome.PASS`는 Tool 실행과 결과 검증을 완료했다는 뜻이다. Build exitCode가 nonzero여도, QA 실패/Skip이 있거나 Scanner 경고가 있어도 정상 보고서를 받았으면 Tool PASS다. 제품 결과는 기존 Build/QA/Security Report와 역할 Executor가 판단한다.

인프라 실패는 Attempt `UNVERIFIED`다. 안전한 인프라 Retry 2회까지 모두 실패하면 `ToolExecutionEvidence.retries_exhausted=True`가 된다. 원장이 최종 Workflow를 `SUCCESS`/`FAIL`/`UNVERIFIED`로 바꾸거나 `fix_attempt`를 늘리지는 않는다. 기존 Orchestrator가 실제 Report의 근거를 받아 최종 판정한다.

## 6. 조회·취소·재실행 금지

| 참조 | 조회 결과 |
| --- | --- |
| `artifact://<논리 UUID>/tool-execution-evidence.json` | `ToolExecutionStore.read_evidence()`의 안전한 원장 JSON |
| `artifact://<Attempt UUID>/tool-attempt.json` | 해당 STARTED/FINISHED Attempt의 안전한 JSON |
| `record.to_tool_evidence()` | 기존 `ToolExecutionEvidence` 객체. Source Manifest와 모든 완료 Attempt가 있어야 함 |

위 참조는 실제 SQLite 원장에 연결된 사설 Host 참조다. 제품/A2A Artifact를 새로 등록하거나 URL을 다운로드하지 않는다. 파일/보고서 읽기·쓰기에도 실제 호출 원장은 남기지만 Source Manifest가 없으므로 Build/Test/Scan 근거를 꾸며내지 않는다. SDK 호출 이전에 거절된 잘못된 Schema/역할/Workspace 요청은 실제 실행 Attempt로 만들지 않는다.

완료·실패·불명확한 논리 ID를 `invoke(logical_call_id=...)`에 넣으면 진단용 참조만 확인하고 `MCP_EXECUTION_REPLAY_DENIED`로 거절한다. 자동 Retry는 최초 `invoke()` 내부에서만 진행한다. 호출을 새 ID로 포장해 Retry 한도를 초기화하는 재개 정책은 추가하지 않는다.

실행 중 취소는 기존 Sandbox의 소유 Container 정리를 기다리고 가능하면 `CANCELLED`/결과 불명 기록을 남긴다. SQLite claim/완료 transaction 도중 취소·저장 실패·프로세스 crash는 STARTED 또는 실제 완료 기록이 남을 수 있으므로 상태를 추정하지 않는다. STARTED는 성공 근거로 변환할 수 없고 자동 재호출도 금지한다. 실제 daemon orphan 복구나 불명확한 Write 해결은 후속 제어/운영 작업이다.

## 7. Host 사용 예시

실제 provision된 Workspace·현재 실행 중인 Step·동결 Source/환경·승인된 Host 설정이 먼저 필요하다. 모델이 이 설정을 만들거나 DB/Step를 임의 선택하지 않는다.

```python
from mcp_tools.client import open_mcp_client
from mcp_tools.execution_runtime import TrackedMCPExecutor, TrackedMCPError
from mcp_tools.execution_store import ToolExecutionStore

store = ToolExecutionStore(repository)
async with open_mcp_client(host_configuration) as client:
    executor = TrackedMCPExecutor(
        client, store, workflow_step_id=current_step.workflow_step_id,
    )
    try:
        result = await executor.invoke("run_build", {
            "workspaceId": str(run.workspace_id),
            "snapshotId": str(frozen_source.artifact_id),
        })
        tool_data = result.data
        tool_evidence = result.to_tool_evidence()
    except TrackedMCPError as error:
        if error.logical_call_id is not None:
            observed = store.get(host_configuration.binding, error.logical_call_id)
        # 오류/불명확 상태를 새 ID로 재전송하지 않는다.
```

`list_tools()`/`execute()`는 기존 LLM `ToolExecutor` 인터페이스와 연결할 수 있다. LLMEngine의 전체 batch 검증·모델 callId 중복 방지·예산을 우회하지 않는다. 모델에는 기존 Tool dict만 반환하고 실행 근거는 Host가 원장에서 조립한다. LLMEngine이 오류를 기존 generic `LLM_TOOL_EXECUTION_FAILED`로 처리해도 Host의 `logical_call_ids`와 원장에는 분류한 실패가 남는다.

자동 세션 재개/Child Process 재연결/모델 Retry/누적 Run 예산/최종 Artifact 조립/실시간 A2A-MCP 통합 Trace는 완료한 것으로 표시하지 않는다. 30~37번 후속 구현과 구분한다.

## 8. 개발정의서 준수 점검

| 기준 | 확인 |
| --- | --- |
| §2 역할 경계 | Host가 상태/근거 관리, Tool은 제품 판정·코드 수정 Cycle 관리하지 않음 |
| §4-5~6 Retry | 동일 논리 호출, 최대 2회 Retry, 불명확 Timeout/Write의 무조건 재전송 금지 |
| §5 동일 Source/환경 | 현재 Step·Source/grant·Run 동결 환경/Manifest 및 실제 실행 기록 대조 |
| §8 Protocol/Schema/권한 | MCP 2026-07-28/SDK v2/stdio·기존 10개 Tool 입출력·역할 강제 유지 |
| §8-6 오류 | Protocol/Tool/제품 실패/결과 불명 구분, 원문 오류 추론 금지 |
| §8-8/§11 개인정보 | Source/패치/파일 내용/SDK 오류/Tool 원문을 원장에 저장하지 않음 |
| §10 최종 판정 | Tool PASS와 제품 PASS 분리, Workflow/최종 Verdict 임의 변경 없음 |
| §11 실행 근거 | 실제 저장된 UUIDv4/참조/Hash·시도 기록, 기존 Evidence 계약 변환 |
| 담당 범위 | 1번+2번만. 웹 서비스/독립 평가/팀원 연결 미변경 |

기존 Orchestrator의 `ingest_tool_evidence()`는 후속 역할 Report 연결 때 재사용한다. 이번은 실제 MCP 실행 원장으로서의 시간/오류/실행 근거이며, 37번의 실시간 Trace/Usage 통합을 대신하지 않는다. 기존 비밀번호 보호 정책 보완·WAL 초기 경쟁도 해결 범위가 아니다.

## 9. 검증과 인계

```bash
PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -p 'test_mcp_execution*.py' -q
PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -p 'test_mcp_client*.py' -q
PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -p 'test_mcp_tracked_llm.py' -q
PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -q
PYTHONPATH=src .venv/bin/python -m compileall -q src/mcp_tools
.venv/bin/pip check
git diff --check
```

이번 단계 신규 테스트는 총 **171개**다: Client 오류 26개, 순수 정책 48개, 원장 Store 63개, 실행 Runtime 29개, LLM 어댑터 흐름 5개. 모두 통과했다. 실행 Policy/Store/Runtime 묶음은 **140개 통과**, 기존 Client 포함 묶음은 **67개 통과**했다.

- 전체 회귀: **1,868개 통과**, 208.556초. 기존 28번 기준 1,697개에 신규 171개를 추가했다.
- `compileall`: 통과.
- `pip check`: 의존성 오류 없음. macOS Cache 권한 경고는 실행 결과와 무관하다.
- `git diff --check` 및 신규 파일의 후행 공백 점검: 통과.
- 전체 회귀의 로컬 Unix 소켓/stdio 검증은 승인된 sandbox 밖 테스트 실행을 사용했다. 외부 LLM API·실제 Docker daemon을 호출하지 않았다.

LLM 흐름 검증은 실제 `LLMEngine`·MCP Dispatcher·SQLite Store를 사용하고, 모델 응답과 Docker 실행은 Fake다. 모델의 논리 Tool Call 1회에 물리 MCP 시도 3회가 대응하는 경우, 재시도 소진·영구 오류·응답이 유실된 파일 쓰기의 Host 근거 보존을 확인했다.

Git/SQLite/Workspace/SDK stdio는 실제 임시 fixture다. 제품 생성 코드·Bandit·Chromium을 Host에서 실행하지 않으며 실제 Container/기업 시연/독립 비교 실험 완료로 표시하지 않는다.

새 의존성/Lock 변경, `development-log.md`·웹 서비스·Evaluation 변경은 없다. Git commit/push는 수행하지 않는다. 사용자 요청에 따라 **29번부터 개별 완료의 쉬운 한 문장 설명은 생략하고, 추후 번호를 묶어서 설명한다.**

커밋 메시지: `MCP 실행 근거 원장과 오류 분류 및 안전 재시도 구현`

다음 작업: **30번 — Planner Agent 구현**.
