# 37. 실제 LLM/A2A/MCP Trace·사용량·비밀정보 마스킹

## 1. 이번 작업 범위

실제 네 Agent 실행에서 모델 호출·MCP 실행·사용량을 같은 Run/Step에 연결하고, 재시작해도 기존 호출 횟수와 Runtime Deadline을 초기화하지 않도록 영속 원장을 추가했다. 1번 Orchestrator와 2번 Agent/MCP만 변경했다. 웹 서비스·평가 시스템·팀원 서비스 연동은 포함하지 않는다.

이번 작업으로 기존 **1~37번 번호 로드맵의 마지막 단계**까지 구현했다. 실제 외부 LLM·Docker·제품 시연의 성공이나 4번 담당의 비교 실험 완료를 의미하지 않는다.

## 2. 기록 구조와 기존 계약

| 저장 대상 | 내용 | 경계 |
| --- | --- | --- |
| 기존 `trace_events` | LLM 시작/종료, 실제 MCP 시도 시작/종료, 기존 A2A/Workflow 이벤트 | 기존 닫힌 `TraceEvent` Schema를 유지한다. 모델·Tool 상세 필드를 임의 추가하지 않는다. |
| 새 `agent_runtime_events` | Trace의 eventId와 연결한 모델 선택·Token Usage·Tool 이름·실제 시도/결과/근거 참조 | 입력·Source·Tool 인자/결과 원문이 없는 typed allowlist. append-only, 동일 키의 동일 내용만 멱등 허용한다. |
| 새 `agent_budget_ledger` | 동결 설정 지문·LLMLimits·UTC Deadline·누적 호출/토큰·미완료 예약·무효화 | Revision 조건부 갱신. 조회를 위해 사용량을 0으로 초기화하거나 새 Deadline을 발급하지 않는다. |

새 테이블은 Host의 기존 Orchestrator SQLite DB에 추가한다. 기존 Run·Step·Artifact·Issue·Trace Schema를 바꾸거나 이전 기록을 삭제하지 않는다. `create_platform()` 구성 시 additive telemetry 테이블만 초기화하며, Agent DB 시작·HTTP listener·Workspace/Git 준비·모델 호출·MCP/Container 실행은 하지 않는다.

기존 공식 A2A HTTP+JSON 1.0 SDK와 Task/Message/Artifact 전달은 그대로다. Agent가 발급한 opaque Task/Context ID를 UUID로 바꾸지 않으며 Run/Step/Requirement/Artifact UUID와 구분한다. 공식 Task 상태와 프로젝트 Workflow/Verdict도 별개다.

## 3. 실제 호출 기록

### LLM

```text
같은 Run 예산에서 model sequence 예약을 먼저 저장
  → LLM_MODEL_CALLED + 모델 선택 상세를 원자 저장
  → 남은 Deadline 재확인
  → 실제 Provider 호출
  → 해당 sequence의 Token Usage 또는 unknown을 저장
  → LLM_CALL_FINISHED + 사용량 상세를 원자 저장
```

- 성공뿐 아니라 Provider 오류·인증 요구·거절·취소도 호출 예약을 보존한다.
- 정확한 Usage가 없으면 Token 값과 총 Token은 `null`이다. 실패한 호출을 무료/0 Token으로 기록하지 않는다.
- 응답이 알려준 모델 ID는 제한된 식별자 형식만 기록한다. 자유 문장·응답 본문을 모델 이름으로 저장하지 않는다.
- `costUsd`는 항상 `null`이다. 승인된 가격표와 실제 과금 근거 없이 비용을 추정하지 않는다.
- 시작 기록 저장에 실패하면 Provider를 호출하지 않는다. 사용량 기록/관찰 sink가 실패하면 기존 예산을 무효화하여 다음 호출을 막는다.

### MCP

`TrackedMCPExecutor`의 실제 물리 시도에 시작/종료 sink를 연결했다. 파일 읽기·파일 쓰기·Build·Unit/Browser Test·Security Scan을 포함한다. Agent가 나중에 완성 보고서를 제출해야만 생기는 실행 로그가 아니다.

- 논리 Tool 호출 ID와 물리 attempt ID, Tool 이름, 실제 상태·결과, 안정된 오류 코드, Retry 결정, delivery/resultUnknown, 실행 Manifest·Evidence 참조, 입력/config SHA를 기록한다.
- Canonical MCP Trace의 `attempt`는 기존 의미인 물리 Retry 번호 0~2다. 입력/인증 후속 Message 번호는 상세의 `workflowAttempt`로 구분한다.
- Trace 기록의 동기 worker와 비동기 sink도 종료까지 소유한다. 반복 취소/AnyIO 취소 중 기록 작업을 분리해 버리지 않는다.
- Tool의 실제 FINISHED receipt가 저장된 뒤 취소/Trace 장애가 발생해도 그 receipt를 취소·UNVERIFIED로 덮어쓰거나 Tool을 다시 실행하지 않는다.
- 결과 보고서 ingest 시에는 이미 있는 실제 MCP Trace를 논리 호출 ID·물리 attempt·eventType으로 확인하고, 같은 시작/종료를 중복 기록하지 않는다. 기존 외부 Agent 근거에 실제 runtime Trace가 없으면 이전 fallback 기록을 유지한다.

## 4. Run 전체 예산과 재시작

Owned Host의 `create_platform()`은 `RunBudgetRegistry(..., telemetry_store=store)`를 사용한다. Planner·Developer·QA·Security·수정·입력 재개가 하나의 기존 예산을 공유한다.

Deadline은 **Run.created_at + 최초 runtimeBudgetMs**다. Queue·Workspace 준비·입력/인증 대기·DB 기록 시간도 포함한다. 모델/Tool 예약은 외부 실행 전에 저장하며, 모든 역할과 수정 Cycle의 호출/토큰을 누적한다. Tool 예약에는 모델이 선택한 Tool뿐 아니라 Host가 요구한 Build/Test/Scan도 포함한다. 물리 Retry 횟수와 논리 Tool 예산 예약 수는 서로 다를 수 있다.

재시작 시 기존 원장·동결 설정 지문·LLMLimits·원래 UTC Deadline·Usage receipt를 확인하고 남은 시간만 monotonic deadline으로 옮긴다. 다음 경우 새 실행은 거부한다.

- 이전 원장이 없거나 손상됐고, 이미 시작한 Run에 새 예산이 필요한 경우
- 설정 지문/모델/예산 한도 변경, 원래 Deadline 만료, 관측한 UTC보다 시계가 뒤로 이동
- 종료한 Run, 미완료 모델/Tool 예약, 알 수 없는 사용량, 누락/불일치 Usage receipt, 무효화된 예산

불확실한 외부 실행을 재시작만으로 확인한 것으로 취급하지 않는다. 기록을 읽는 GET과 이미 알려진 Task를 GET으로 회수하는 기존 `/recover` 경로는 새 모델/Tool 실행과 구분한다. 원장 복원이 자동 Task 재전송이나 자동 Run 실행을 시작하는 기능은 아니다.

직접 생성하는 `RunBudgetRegistry(repository, limits=...)`는 기존 메모리 모드를 유지한다. 영속 store를 명시하지 않은 이전 Registry가 재시작 Run을 자동 승인하지 않으며, 기본 Bootstrap CLI도 실제 실행/telemetry 구성으로 자동 전환하지 않는다.

## 5. 사용량·상세 조회 API

```http
GET /api/v1/runs/{runId}/usage?limit=100&offset=0
GET /api/v1/runs/{runId}/telemetry?limit=100&offset=0
GET /api/v1/runs/{runId}/events?limit=100&offset=0
```

| API | 응답 |
| --- | --- |
| `/usage` | `runId`, 전체 Run `summary`, 해당 페이지 `records`, `total`, `limit`, `offset` |
| `/telemetry` | `runId`, 해당 페이지 `events`, `total`, `limit`, `offset` |
| 기존 `/events` | 기존 canonical Trace 응답. 새 상세 필드 없이 원래 Schema 유지 |

`limit`는 1~500, `offset`은 0 이상이다. Usage summary는 페이지 크기와 무관하게 전체 Run을 집계한다. 각 상세 기록은 `eventId`, `kind`, `recordType`, `binding`, `detail` 및 조회 편의를 위한 상세 필드를 제공한다. `eventId`로 canonical Trace와 연결할 수 있다.

Summary의 주요 필드:

- `modelCalls` / `toolCalls`: 원장의 누적 논리 예약 수
- `recordedModelCalls`: 저장된 Usage receipt 수
- `knownTotalTokens`: 확인된 부분 Token 합계
- `usageComplete` / `totalTokens`: 사용량이 모두 알려져 있을 때만 총 Token을 제공한다. unknown/pending Model은 총 Token이 `null`이다.
- `pendingModelSequences` / `pendingToolSequences`: 외부 결과 정합성을 확인하지 못한 예약
- `missingUsageSequences`: 예산 원장에는 있으나 별도 Usage receipt가 누락된 호출
- `executionBlocked`: 위 불확실성/원장 무효화에 따른 차단 표시. Run 상태·Deadline·권한까지 포함한 실행 가능 보증은 아니다.
- `costUsd`: `null`

알 수 없는 Run은 404, pagination 입력 오류는 422다. Owned telemetry store가 연결되지 않은 기본 서버는 503 `RUNTIME_TELEMETRY_NOT_CONFIGURED`를 반환한다. 저장소 장애는 503 `RUNTIME_TELEMETRY_UNAVAILABLE`이며 SQL/Provider/입력 원문을 응답에 붙이지 않는다. 이 읽기 API는 예산을 재발급하거나 실행하지 않는다.

## 6. 비밀정보·원문 로그 경계

- Telemetry에는 Prompt·모델 응답·Source·Tool 인자/출력·stdout·`.env` 원문을 넣는 필드가 없다. Task/Step/Artifact/Manifest와 SHA/근거 참조만 저장한다.
- API Key/Token은 AgentSettings의 SecretStr와 HTTP 인증 헤더 경계를 유지한다. Task 입력이나 새 Telemetry에 넣지 않는다.
- Host가 설정한 API Key/Bearer의 알려진 긴 값은 프로세스 메모리의 마스킹 목록에 등록하여 라벨 없는 문자열에도 대응한다. `.env` 파일을 별도로 읽거나 이 목록을 DB에 저장하지 않는다.
- 8자 미만의 값은 일반 식별자 손상을 피하려고 전역 부분 문자열 치환에 넣지 않는다. 해당 값은 기존 secret field/header 규칙과 원문 기록 금지로 보호한다. 이 기능이 임의의 자유 문장 속 모든 미지의 비밀을 자동 탐지한다는 뜻은 아니다.
- A2A/MCP SDK 로그는 모든 level에서 body/protobuf/원문 message·extra·stack·Provider 오류 설명을 생략한다. logger/level과 예외 타입만 남기며 상세 provenance는 canonical Trace에서 확인한다. MCP의 잘못된 peer 응답에 대한 Pydantic 오류 원문도 생략한다.
- 저장된 opaque A2A Task/Context/Artifact reference와 안전한 Snapshot/Git SHA는 마스킹 때문에 바꾸지 않는다.

실제 Tool proof/보고서의 기존 보호·마스킹 검증을 유지했다. 이번 Telemetry가 보고서·Source의 대체 저장소이거나 의미적 보안 검증기가 되는 것은 아니다.

## 7. 개발정의서 준수 점검

| 정의서 기준 | 확인 |
| --- | --- |
| §3·§6: 공식 A2A 1.0 Task/Message/Artifact, opaque ID | 기존 공식 SDK/HTTP+JSON·전달·조회·상태 유지. 새 상세는 프로젝트 보조 기록으로 분리 |
| §4·§7: Workflow·Task·최종 Verdict 분리, 수정 3회 | 새 상태/판정 정책 없음. 입력 재개·수정·기존 예산 누적 유지 |
| §5·§9: 동일 불변 Snapshot·Manifest·Lineage | 실제 Tool 기록의 measured Manifest/SHA와 기존 Artifact 참조 유지 |
| §8: MCP 권한·Container·Retry | 기존 역할별 allowlist, Container 실행, 동일 Deadline/Retry 0~2, 불확실 실행 반복 금지 |
| §10: Orchestrator의 최종 판정 | Token 수·COMPLETED·Trace 생성만으로 PASS/SUCCESS를 만들지 않음 |
| §11·§11-A: 닫힌 Trace Schema·상관관계·비밀 마스킹 | canonical Trace 변경 없음. 상세 원자 저장·실제 실행 시점·중복 방지·원문 제외 |
| 담당 범위 | 1·2번만 구현. 제품 UI/평가·Git commit/push·팀원 연결 없음 |

## 8. 검증과 한계

전용 테스트와 기존 전체 unittest 회귀로 영속 예산·사용량·실제 호출 sink·조회·입력 재개/수정/취소·로그 경계를 검증한다. 실제 역할 실행기·공식 SDK HTTP/ASGI·Git·SQLite·MCP Dispatcher를 쓰지만 Provider·Docker·의미 보안 Proof는 명시적인 fixture이다.

최종 전체 회귀: **2,633개 PASS / 679.004초**, 오류·실패·Skip 없음. 기존 2,563개와 새 전용 70개가 함께 통과했다. `compileall`, `pip check`, `git diff --check`도 통과했다. 이 결과는 unittest 기반 Orchestrator/Agent/MCP 검증이며 4번 담당의 pytest 평가 전체나 실제 품질 비교 실험 결과가 아니다.

새 전용 테스트는 총 **70개**다.

| 테스트 파일 | 수 | 핵심 확인 |
| --- | ---: | --- |
| `test_agent_durable_budgets.py` | 25 | 원래 Deadline·누적 예산·pending/unknown·UTC 역행·원자 저장·멱등/불변 원장 |
| `test_llm_runtime_telemetry.py` | 13 | 실제 Engine의 예약/Trace/Provider/Usage 순서, sink 장애·지연·실패·취소·정확한 토큰 대조 |
| `test_mcp_execution_trace.py` | 18 | 물리 Retry별 기록, sink 장애·반복/AnyIO 취소 drain, 실제 FINISHED receipt 보존 |
| `test_owned_agent_telemetry.py` | 8 | 네 실제 실행기·공식 SDK/ASGI·조회·중복 ingest·입력 재개·취소·기존 원장 복원 |
| `test_runtime_secret_logging.py` | 6 | 알려진 Host credential 마스킹, opaque reference 보존, A2A/MCP SDK 원문/오류 로그 제외 |

기본 통합 fixture에서는 모델 6회·논리 Tool 예약 5회·물리 MCP 호출 5회·확인된 Token 90개를 저장했다. 입력 재개 fixture는 같은 Task/Context로 모델 sequence만 이어졌고, 취소 fixture는 Usage 종료 기록이 RUN_ABORTED보다 먼저 저장됐다. 이 수치는 fixture 검증값이며 실제 서비스 비용/성능 결과가 아니다.

```bash
PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -q
```

- 단일 Host/단일 프로세스/단일 event loop 구성이다. SQLite 영속 원장 추가가 다중 replica 실행/분산 과금 제어 지원을 의미하지 않는다.
- DB 기록이 실패하면 실제 결과가 알려졌더라도 확인 가능한 원장 없이는 실행을 계속하지 않는다. 수동 조사 없이 불확실 예약을 삭제하거나 사용량을 임의 입력해 재실행하지 않는다.
- 실제 외부 LLM·Docker·회원가입 시연·모든 보안 요구사항의 의미 검증·Single vs Multi 비교 실험은 미검증이다.
- 보류된 비밀번호 보호 정책 보완과 Agent Task Store 공유 DB 초기화 WAL 경쟁은 이번 범위에 포함하지 않았다.

## 9. 커밋 메시지와 다음 작업

커밋 메시지: `실제 Agent 호출 Trace와 사용량 및 Run 예산 영속화 추가`

다음 작업 번호: **없음 — 기존 1~37번 로드맵 구현 종료**. 승인된 실제 LLM/Container Host 설정과 실행 검증은 별도 다음 범위이며 이번에 자동 수행하지 않았다. Git commit/push는 직접 수행하지 않았다.
