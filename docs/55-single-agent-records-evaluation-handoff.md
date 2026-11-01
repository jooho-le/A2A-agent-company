# 55. Single-Agent 실행 기록·사용량·비용과 평가 인계

작성 기준일: 2026-11-01. 담당: 주호(역할 1·2).

## 1. 구현 범위

54번의 실제 Single 실행 기록에 승인 가격표 계상, Run 지표 조회, 평가용 파일 내보내기를 연결했다. 독립 시험지 작성·채점·비교 통계·웹 화면은 역할 3·4의 범위로 유지한다. 이 문서의 SUCCESS는 로컬 결정적 어댑터 검사 결과이며, 실제 모델·Docker 실행이나 독립 평가 통과를 뜻하지 않는다.

| 구현 | 위치 | 동작 |
| --- | --- | --- |
| Single 가격표 연결 | `agents/single/platform.py` | 선택적 `price_book`을 받고 첫 호출 전에 Run 정책을 고정 |
| 실제 정체성 계상 | `agents/platform/pricing.py`, `pricing_store.py` | `SINGLE_AGENT`를 허용하고 동결 architecture·Step·사용량·Trace·예산 원장을 함께 검사 |
| Run 기록·지표 | `agents/platform/run_records.py` | 전체 페이지 수집, 사실 지표 계산, 누락 기록을 오류 또는 미확인으로 표시 |
| 조회 API | `GET /api/v1/runs/{runId}/metrics` | 시간·수정·재시도·호출·토큰·비용 조회. 없는 Run은 404, 수집 실패는 503 |
| 읽기 전용 수집 | `agents/platform/export_readonly.py` | SQLite `mode=ro`·`query_only`, Host 읽기 트랜잭션 고정. 생성자 마이그레이션·쓰기 없음 |
| 기존 인계 형식 | `snapshot_export.py`, `a2a_export.py` | 로컬 `dev`의 내보내기 형식을 재사용하고 Single 정체성·무결성·크기 제한을 확장 |
| 통합 인계 CLI | `agents/platform/evaluation_export.py` | 기록·모든 후보 Snapshot·실제 A2A 기록을 하나의 새 폴더로 내보냄 |

브랜치 병합·전환·커밋·푸시·원격 조회는 하지 않았다. `dev` 평가 코드 전체를 복제하지 않았다.

## 2. 지표와 비용 규칙

- `wallClockMs`: `RUN_STARTED` → 최종 `VERDICT_CREATED`. 취소는 `RUN_ABORTED`까지 기록한다. 단계별 시간을 더하지 않는다.
- `firstBuildPassMs`: Run 시작 → 첫 `BUILD_PASSED`. 첫 Build PASS가 없으면 `null`이다.
- `humanWaitMs`: 입력 대기·사람 검토 상태의 대기 구간을 따로 합산한다. 측정 시작/종료가 없거나 시간이 역전되면 완료 시간으로 꾸미지 않는다.
- `observedElapsedMs`는 판정·취소 전에는 수집 시점까지의 경과다. 판정·취소 시각이 없는 실행의 `wallClockMs`는 `null`, `timingComplete`는 false다. `humanWaitOpen`은 현재 입력/사람 대기가 열려 있는지 표시한다.
- 모델·도구 호출 수와 토큰·비용은 같은 Run의 영속 원장을 사용한다. `recordedToolRetries`는 실제 MCP 재시도 기록의 `(logicalCallId, toolAttempt)`를 중복 제거한다.
- `inputTokens`·`outputTokens`·`totalTokens`는 사용량이 완전할 때만 전체 합을 제공한다. 확인된 일부 합과 완전 여부를 구분한다.
- 비용은 기존 승인 가격표·실제 Provider/Model/Tier·실측 사용량·불변 계상 영수증에 근거한다. 캐시 입력·Reasoning 토큰을 중복 과금하지 않는다.
- 승인 가격표 없음, 실제 모델/Tier 또는 캐시 계측 누락, 사용량 미확인·영수증 누락·미완료 호출은 전체 비용을 `null`로 유지한다. 0원 추정이나 과거 기록의 소급 가격 변경은 하지 않는다.
- 가격 검증에는 합성 가격표만 사용했다. 운영 모델·가격을 선정하거나 최신 요금을 확인한 결과가 아니다.

요구사항 충족률·보호 QA 통과율·독립 보안 판정·회귀 수·구조 간 비교는 역할 4가 같은 시험지로 계산한다. 이 API의 `issuesFound`를 독립 평가 오류 수나 보안 취약점 수로 바꾸어 해석하지 않는다.

## 3. 내보내기 방법과 파일 계약

실제 Single 실행이 저장된 뒤 신뢰된 Host 작업자가 실행한다. 경로는 실제 실행에 사용한 DB 경로로 바꾼다. 기본 Single DB는 `.data/agents/single_agent.sqlite3`이며, 기본 `python -m agents.single`은 Bootstrap이라는 기존 제한을 유지한다.

```sh
PYTHONPATH=src .venv/bin/python -m agents.platform.evaluation_export \
  --database .data/orchestrator.sqlite3 \
  --run-id '<실제 Run UUID>' \
  --store SINGLE_AGENT=.data/agents/single_agent.sqlite3 \
  --out .data/evaluation-handoffs/<새로운-폴더명>
```

| 파일 | 내용 |
| --- | --- |
| `run.json` | 기존 Snapshot 인계 형식: 실제 상태·Verdict·최종 코드 버전·내보낸 버전 목록 |
| `v1/source.tar`, `v1/manifest.json` 등 | 저장된 원본 tar와 Execution Manifest. 기본은 모든 후보, `--final-only`는 최종 후보만 |
| `bundle.json` | `dev`의 `run_metrics.fetch_run_bundle` 입력 구조. Run·전체 Trace·Issue·ToolAttempt·사용량·동결 설정 |
| `trace.json` | `dev`의 Trace 자료 구조. Artifact·전체 Telemetry 포함. A2A 본문은 별도 파일 |
| `a2a-messages.json` | 같은 Run의 실제 Context·Task·Message 수신·History·Artifact 포함 Task·상태 이벤트 |
| `metrics.json` | 위 조회 API와 같은 사실 지표. 역할 4의 `run_metrics` 보고서를 대체하지 않음 |
| `run-configuration.json` | 동결 실행 설정·시나리오 계약과 Hash |
| `handoff.json` | 파일별 SHA-256/크기, 실제 Agent와 단계·Task/Context 연결, 누락/불일치·완료 여부 |

Snapshot은 압축 해제·실행하지 않고 저장 내용·메타데이터·tar·Manifest Hash를 검증한 뒤 원본 바이트로 복사한다. Source가 없으면 `exportedCodeVersions: []`, `SNAPSHOT_NOT_FOUND`를 남긴다. 이전 버전만 존재하면 `FINAL_SNAPSHOT_NOT_FOUND`로 표시하며 최종 후보로 대체하지 않는다. 등록된 Source의 손상은 `SNAPSHOT_INTEGRITY_ERROR`로 실패한다.

성공뿐 아니라 FAIL·UNVERIFIED·HUMAN_REVIEW·입력 대기·ABORTED도 실제 상태로 보존한다. `readyForFinalEvaluation`은 최종 Source가 있고 Run이 종료되었으며 A2A 인계가 완전할 때만 true다. 이 값은 채점 통과나 D4 소비자 구현 완료를 뜻하지 않는다.

Host 자료는 한 읽기 트랜잭션으로 수집한다. Agent DB는 각자 별도 읽기 트랜잭션이므로 DB 간 원자성을 주장하지 않는다. `consistency.crossDatabaseAtomic`은 false이며, Host Step과 A2A Task의 ID·Context·단계 Metadata 차이는 `coverage`에 드러낸다. 진행 중 수집과 최종 평가 인계를 구분한다.

기존 출력 폴더 덮어쓰기를 거부하고 등록된 모든 Agent Workspace 내부 출력도 거부한다. 새 폴더는 0700, 자료 파일은 0600으로 만들며 `handoff.json`을 마지막에 쓴다. 쓰기 실패 시 이 명령이 만든 출력만 정리한다. 자료를 Agent에게 전달하거나 외부로 업로드하지 않는다.

## 4. 석민에게 전달할 Single A2A 계약

기존 Multi 출력은 `runId`, `roles`, `missingRoles` 구조를 유지한다. Single은 아래처럼 **실제 저장소 하나**를 표현한다. 각 rows의 내부 배열 형식은 기존 Multi와 같다.

```json
{
  "schemaVersion": 2,
  "architecture": "SINGLE_AGENT",
  "runId": "<UUID>",
  "roles": {
    "SINGLE_AGENT": {"contexts": [], "tasks": [], "history": [], "receipts": [], "events": []}
  },
  "missingRoles": []
}
```

위 빈 배열은 형식 예시이며 정상 실행 증거가 아니다. 저장소 미제공이면 `missingRoles: ["SINGLE_AGENT"]`다. 혼합 정체성·잘못된 저장소 역할·다른 Run의 Task 바인딩은 거부한다. 실제 Single을 네 역할 저장소로 복제하지 않는다.

**확인한 소비자 제한:** 로컬 `dev`의 `evaluation.trace_checks` D4는 여전히 `PLANNER/DEVELOPER/QA/SECURITY` 네 역할을 고정으로 기대한다. 따라서 Single A2A 파일은 그 D4에 그대로 넣으면 형식 오류가 된다. 역할 4는 다음 두 부분을 반영해야 한다.

1. 기대 저장소 목록은 동결 architecture로 결정한다. `agents.platform.a2a_export.expected_a2a_roles(architecture)`를 재사용하면 Single은 하나, Multi는 네 개를 반환한다.
2. Single의 처리 단계는 `handoff.steps[].phase`로 읽고 실제 주체는 `SINGLE_AGENT`로 본다. `coverage.a2aComplete=false`, 누락 저장소·Task, 빈 기록을 PASS로 처리하지 않는다. 기존 본문 유출 검사와 독립 채점 기준은 유지한다.

현재 작업에는 역할 4 D4 구현 변경·보호 시험지 실행·비교 실험을 포함하지 않았다. 기존 `evaluation.manifest.ExecutionManifest.from_payload`로 내보낸 Manifest를 읽는 경계는 로컬 검사에 포함했다.

## 5. 검증 결과

최종 검증은 `unittest` 네 묶음 **204개 통과**다. 중간 실패는 수정 후 해당 검사로 다시 확인했으며 중복 재실행 수를 합산하지 않았다.

| 검사 | 결과 |
| --- | --- |
| 새 통합 인계·지표·CLI 계약 | 13개 통과 |
| Single 비용 계상 + 기존 가격표·가격 원장 | 90개 통과. 이 중 Single 신규 4개 |
| 기존 Host·Priced Pipeline·Telemetry·Git 원본·API 회귀 | 72개 통과 |
| Single Pipeline·입력/취소/복구·영속 예산 회귀 | 29개 통과 |
| Python 3.10 구문 검사 | 변경 Python 파일 13개 통과 |
| 기타 | `git diff --check`, 인계 CLI `--help` 통과 |

통합 인계 검사에는 실제 로컬 A2A SDK/ASGI·SQLite·Git Snapshot을 사용했다. 한 정체성/Context, 전체 기록과 API 지표, 원본 Hash/Manifest 호환성, 합성 승인 가격표 계상, 입력 대기→재개, 취소와 Source 없음, 실패 후보 v1~v4, 손상 거부, 출력 권한·덮어쓰기/Workspace 거부, DB/Schema 불변, 전체 페이지 수집과 누락 거부를 확인했다. LLM과 Docker는 결정적 어댑터다.

```sh
PYTHONPATH=src:tests .venv/bin/python -W error::RuntimeWarning -m unittest -q \
  test_single_agent_evaluation_export
PYTHONPATH=src:tests .venv/bin/python -W error::RuntimeWarning -m unittest -q \
  test_single_agent_priced_telemetry test_priced_telemetry test_pricing_store test_pricing
PYTHONPATH=src:tests .venv/bin/python -W error::RuntimeWarning -m unittest -q \
  test_priced_host test_priced_pipeline test_owned_agent_telemetry test_llm_runtime_telemetry \
  test_git_object_export test_api_dependencies test_api_validation_errors
PYTHONPATH=src:tests .venv/bin/python -W error::RuntimeWarning -m unittest -q \
  test_single_agent_pipeline test_single_agent_controls test_single_agent_durable_budgets
```

실제 API 키·Docker daemon·외부 서버·D4 소비자 변경·보호 시험지 채점·Single/Multi 비교 실험은 실행하지 않았다. 인증·개인 설정·Web/Evaluation 구현·Git 이력은 변경하지 않았다.

## 6. 개발정의서 대조

| 기준 | 이번 대조 |
| --- | --- |
| §0·§3·§6·§7 A2A 식별·상태 | 공식 SDK가 저장한 실제 Task/Context/Message/Artifact·상태 기록을 인계. 가상 네 Agent 없음 |
| §4 수정·재시도 | Run 전체 Fix/Retry를 구분하고 실패·후보별 기록 보존 |
| §5·§9 코드 전달·버전 | 같은 Source/Manifest Hash를 확인. 손상·누락·이전 버전 대체를 성공으로 처리하지 않음 |
| §8 MCP·격리·Secret | Host 읽기 전용 수집, Agent Workspace 내부 출력 거부, 도구·인증·Sandbox 권한 유지 |
| §10 최종 판정 | 실제 Orchestrator 판정을 보존하고 독립 채점 결과와 구분. 수집/인계 완료를 제품 SUCCESS로 취급하지 않음 |
| §11·§11-A 기록·공정 비교 | Run 전체 시간·사람 대기·호출·실측 토큰·승인 비용·동결 설정 인계. 미확인은 null |
| 역할 경계 | 공통 시험지·D4 소비자 수정·독립 평가·통계는 역할 4. 실제 API/Docker/비교 실험은 별도 확인 |

커밋 메시지: `싱글에이전트 실행 기록·비용 계상과 평가 인계 연결`

다음 작업: **56번 — Single-Agent 전체 계약 최종 점검·실행 및 인계 안내 정리**.
