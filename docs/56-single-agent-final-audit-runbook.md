# 56. Single-Agent 최종 점검·실행 및 인계 안내

작성 기준일: 2026-11-01. 담당: 주호(역할 1·2).

## 1. 현재 완료 범위

47~55번의 Single 계약·설정·A2A 서버·단일 Prompt/기록·MCP·기획/구현/검증/수정·제어/복구·예산·비용·평가 인계를 대조했다. 이번에는 실제 설정 파일에서 실행기를 구성하는 **Single Host CLI**와 누락된 **Security 자체 테스트 프로필 전달**을 보완했다.

**Single 실행 코드와 이번 내부 검증은 완료했다. 실제 API·Docker에서의 성공, 팀원 서비스 연결, 독립 채점·비교 실험은 아직 확인하지 않았다.** 기본 통신 서버가 작업을 거절하는 Bootstrap인 것과 실행기가 없는 것은 구분한다.

| 실행 방식 | 실제 동작 |
| --- | --- |
| 기본 Orchestrator | Single 실행 미구성이면 등록을 503으로 거절 |
| `python -m agents.single` | 통신 확인용 Bootstrap. `executionReady=false` |
| `create_single_platform(...)` | 신뢰된 Python Host 조합으로 실행기 연결 |
| `python -m agents.platform.run_host --architecture SINGLE_AGENT ...` | 승인 파일로 Orchestrator + Single 두 서버 구성. 실제 기동은 `--start` 필요 |
| 기존 `run_host`의 기본 모드 | 기존 Multi. 네 Agent와 Orchestrator 유지 |

## 2. 이번에 보완한 구현

### Single 파일 설정과 Host 조립

- `OwnedSingleRuntimeConfiguration`은 `agents.SINGLE_AGENT` 하나만 받는다. Multi의 네 역할 Schema는 유지한다.
- `load_runtime_configuration(..., architecture="SINGLE_AGENT")`, `inspect_host_configuration(...)`, `create_host(...)`가 명시적 Single 선택을 처리한다.
- 파일 검사·Git 기준 Source·도구/보호 QA 정책·가격표·경로 분리·환경변수 Secret 해석·Run 정책 고정을 기존 Host와 공유한다.
- 실제 Provider와 Agent 앱은 하나씩 생성한다. 기존 네 단계 URL/Token 필드는 승인한 단일 서버를 가리키는 내부 경로다. 네 Provider/Context를 만들지 않는다.
- 초기화 실패·종료 시 같은 자원 정리 규칙을 적용한다. 모드 불일치, 혼합 역할, 포트/DB 충돌, 누락 자격증명은 실행 전 거절한다.

### Security 자체 테스트 프로필 전달

기존 Security 실행기는 자체 테스트를 측정할 수 있었지만 파일 기반 Host의 도구 설정이 그 프로필을 넘기지 않았다. `OwnedToolConfiguration.unit_configuration_for(SECURITY)`와 `HostRuntimeServices`를 연결해 **Single·Multi 모두 같은 승인 범위**를 사용하게 했다.

`owned-tools.example.json`의 `unit.scopes`에 다음 자체 테스트 scope를 추가했다.

```json
{"name": "security-tests", "kind": "QA_TESTS", "role": "SECURITY", "source_directory": "tests", "pattern": "test_*.py"}
```

Security 테스트는 `outputs/security/tests/`를 사용하고 QA 테스트와 섞이지 않는다. 기존 로컬 파일에 이 scope가 없으면 기존 Scanner/Assessor 경로를 유지하며 검증 근거 부족을 PASS로 바꾸지 않는다. **이 Host의 보호 case 파일은 QA 순환용 계약**이므로 별도 case 계약이 없는 Security `PROTECTED` scope는 구성 단계에서 거절한다. 최종 보호 공통 시험지는 역할 4가 개발 순환 밖에서 실행한다.

변경 위치: `src/agents/platform/{configuration,host,host_services,tool_configuration,run_host,check_configuration}.py`, `configs/single-runtime.example.json`, `configs/owned-tools.example.json`.

## 3. 47~56번 구현 확인표

| 번호 | 확인한 내용 | 근거 |
| --- | --- | --- |
| 47 | 한 Agent·이어지는 기록·공정한 조건·역할 경계 | [실행 계약](47-single-agent-execution-contract.md) |
| 48 | Single/Multi Run 설정·저장·JSON Schema·Ver2 계약 | [실행 설정](48-single-agent-run-configuration.md), `test_run_configuration` |
| 49 | 별도 정체성·Card·Task/Context·저장소·인증/상태 | [서버](49-single-agent-a2a-server.md), `test_single_agent_server` |
| 50 | 단일 System Prompt·같은 Context 기록 복구·출력 계약 | [Prompt/기록](50-single-agent-prompts-context.md), `test_single_agent_prompts` |
| 51 | 전체 도구 기능·단계별 권한·Source/테스트 경로·stdio 검증 | [MCP](51-single-agent-mcp-permissions.md), `test_single_agent_mcp` |
| 52 | Planner → 구현/Build → QA → Security, 하나의 Provider | [최초 실행](52-single-agent-initial-pipeline.md), `test_single_agent_pipeline` |
| 53 | 같은 기록으로 최대 3회 수정·v1~v4·재검증·반복 Issue | [수정](53-single-agent-fix-revalidation.md), `test_single_agent_fix_pipeline` |
| 54 | 입력/인증 재개·취소·재시작·영속 deadline/사용량·불확실 실행 차단 | [제어/예산](54-single-agent-controls-durable-budgets.md), controls/durable_budgets 검사 |
| 55 | 사실 지표·승인 비용·모든 후보·전체 기록·실제 단일 A2A 인계 | [평가 인계](55-single-agent-records-evaluation-handoff.md), evaluation_export/priced_telemetry 검사 |
| 56 | 파일 기반 Single 실행·Security 프로필 전달·Multi 회귀·실행/인계 안내 | 이 문서, `test_single_agent_host`, Host/Tool/Runner 회귀 |

업무 단계의 `PLANNER/DEVELOPER/QA/SECURITY`와 실제 서비스 정체성 `SINGLE_AGENT`는 다르다. 기존 Artifact/영수증의 업무 범주를 네 독립 Agent가 실행됐다는 증거로 사용하지 않는다.

## 4. 실제 실행 준비 — 사용자가 나중에 진행

### 4-1. 로컬 설정 파일

아래는 **수행 방법**이며 이번 작업에서 개인 설정을 만들거나 키·Docker를 변경하지 않았다. 프로젝트 루트에서 실행한다. 기존 `.local.json`은 덮어쓰지 않는다.

```sh
cp -n configs/single-runtime.example.json configs/single-runtime.local.json
cp -n configs/owned-tools.example.json configs/owned-tools.local.json
cp -n configs/owned-source.example.json configs/owned-source.local.json
```

설정 예시는 승인되지 않은 `null`을 포함하므로 복사만으로 실행되지 않는다. 기존 파일을 수정할 때는 먼저 본인 설정을 별도 백업한다.

| 파일 | 정해야 할 값 |
| --- | --- |
| `single-runtime.local.json` | 승인 Model ID/Temperature/지원 Revision, Run 전체 예산·호출/토큰 한도, 두 포트, DB/Workspace, 환경변수 이름 |
| `owned-tools.local.json` | 실제 이미지 Digest·Dependency Lock Hash·Hardware·Docker endpoint·Build/Test/Browser/Scanner 프로필·실행 한도 |
| `owned-source.local.json` | 별도 기준 서비스 Git 저장소·Repository ID·Full Commit·Snapshot SHA-256·Lock 경로 |
| 보호 QA case 파일(해당 구성만) | 승인된 순환용 보호 QA scope와 정확히 같은 case/Requirement 매핑 |
| 승인 가격표 파일(비용을 계상할 경우) | 실제 사용 Model/Tier/과금 범위에 맞는 승인 가격표 |

`single-runtime.example.json`은 기본 Multi 데이터와 구분한 `.data/single/` 경로를 사용한다. 모든 상대 경로는 해당 설정 파일의 디렉터리를 기준으로 해석한다. 기준 서비스 저장소·Host 설정/DB·Agent Workspace는 분리한다. Source 준비는 승인된 Git 객체를 새 Workspace로 복원하며 기존 Source를 강제로 초기화하지 않는다.

예시의 호출 한도는 개발 예시다. Single/Multi 비교에서는 모델·시작 Commit/Snapshot·요구사항·전체 Tool 기능·이미지/Lock/Scanner·Network/Secret·예산·평가 방식을 같은 승인값으로 맞춘다. 보안 자체 테스트 scope도 양쪽에 동일하게 적용한다.

### 4-2. 먼저 파일 검사

```sh
PYTHONPATH=src .venv/bin/python -m agents.platform.run_host \
  --architecture SINGLE_AGENT \
  --runtime configs/single-runtime.local.json \
  --tools configs/owned-tools.local.json \
  --source configs/owned-source.local.json
```

기본 검사에는 자격증명이 필요 없다. DB/Workspace/서버/모델 호출을 시작하지 않으며 `executionReady=false`, `DockerChecked=false`를 반환한다. 보호 QA를 쓰면 `--qa-cases <승인파일>`, 비용을 계상하면 `--price-book <승인파일>`을 검사와 실제 기동 모두에 같은 값으로 추가한다.

실행 터미널의 환경변수에 설정에서 참조한 LLM API 키와 Single Agent Bearer Token을 준비한다. 예시 이름은 `A2A_PLATFORM_LLM_API_KEY`, `A2A_PLATFORM_SINGLE_AGENT_TOKEN`이다. JSON에는 실제 비밀값을 쓰지 않는다. Host는 참조한 프로세스 환경변수만 읽고 `.env`나 다른 키로 대체하지 않는다.

위 검사 명령에 **`--check-secrets`**를 추가하면 로컬 값의 존재/형식을 확인한다. 계정 인증·모델 사용 권한·과금·Docker 실행 성공까지 확인하는 기능은 아니다.

### 4-3. 명시적으로 기동

Docker·이미지·기준 서비스·승인 설정·환경변수를 준비한 뒤 실행한다. 파일 검사와 동일한 옵션에 `--start`를 추가한다. `--check-secrets`와 `--start`는 함께 쓰지 않는다.

```sh
PYTHONPATH=src .venv/bin/python -m agents.platform.run_host \
  --architecture SINGLE_AGENT \
  --runtime configs/single-runtime.local.json \
  --tools configs/owned-tools.local.json \
  --source configs/owned-source.local.json \
  --start
```

예시 포트를 유지했다면 Swagger는 `http://127.0.0.1:8000/docs`, Single health는 `http://127.0.0.1:8105/health`다. 이 Host의 Single health가 `executionReady=true`여도 실제 LLM/Docker 작업 성공을 미리 보장하지 않는다. 기본 Bootstrap과 동시에 같은 포트를 사용하지 않는다. 종료는 실행 터미널에서 Ctrl+C로 요청한다.

## 5. 요청 생성·확인·기록 인계

### 승인 설정으로 요청 만들기

같은 파일 검사 결과에서 Run Configuration을 가져오면 Model/환경/시작 Hash를 손으로 다시 입력할 필요가 없다. 아래는 요청 파일을 만드는 로컬 Python 예시다. 운영 키를 읽거나 서버를 시작하지 않는다. 이미 있는 `request.json`은 덮어쓰지 않는다.

```sh
PYTHONPATH=src .venv/bin/python - <<'PY'
import json
from pathlib import Path
from agents.platform.host import inspect_host_configuration

plan = inspect_host_configuration(
    architecture="SINGLE_AGENT",
    runtime_file="configs/single-runtime.local.json",
    tools_file="configs/owned-tools.local.json",
    source_file="configs/owned-source.local.json",
    # 보호 QA/가격표를 승인했다면 기동 명령과 같은 파일 인자를 여기에 추가한다.
)
request = {
    "scenarioId": "f7f9e5c3-ffc3-4b3f-918b-21e1b956ce76",
    "requestText": "동결된 회원가입 요구사항을 구현해 주세요.",
    "configuration": plan.approved_configuration.model_dump(mode="json", by_alias=True),
}
path = Path(".data/single/request.json")
path.parent.mkdir(parents=True, exist_ok=True)
with path.open("x", encoding="utf-8") as stream:
    json.dump(request, stream, ensure_ascii=False, indent=2)
PY
```

등록된 Scenario ID를 사용한다. 추가 Scenario와 실험용 `experimentId`·순서·Warm-up·동일한 loopPolicy 설정은 팀의 승인 계약으로 정하고 같은 Host 승인 조건을 지킨다. 단순 모드 선택만으로 Single/Multi의 공정한 실험이 성립하지 않는다.

```sh
curl -sS -X POST http://127.0.0.1:8000/api/v1/runs \
  -H 'Content-Type: application/json' \
  --data-binary @.data/single/request.json
```

응답의 `run.runId`를 저장한다. Swagger에서도 같은 요청을 사용할 수 있다.

| 조회/제어 | 확인할 내용 |
| --- | --- |
| `GET /api/v1/runs/{runId}` | 실제 Workflow 상태·최종 Verdict·Fix/코드 버전 |
| `.../{runId}/steps` | 한 서비스의 단계별 Task/Context·시도·진행 |
| `.../{runId}/events` | 요청→검증 실패→수정→재검증 순서. 전체 자료는 페이지를 끝까지 조회 |
| `.../{runId}/artifacts`, `/issues`, `/tool-attempts` | Source/보고서·Issue·실제 실행 근거 |
| `.../{runId}/usage`, `/telemetry`, `/metrics` | Run 전체 호출·토큰·비용·시간·불완전성 |
| `POST .../{runId}/resume` | `workflowStepId`, 필요한 `inputData`로 같은 작업 재개. 인증 비밀값은 별도 Host 환경에서 준비 |
| `POST .../{runId}/recover` | 현재 Step의 저장된 Task를 조회해 확인. 임의 재전송 아님 |
| `POST .../{runId}/cancel` | `{"reason":"사용자가 실행 중단 요청"}`처럼 비어 있지 않은 사유 필요 |

완료된 A2A Task를 곧바로 제품 SUCCESS로 읽지 않는다. 보고서의 같은 Source/Manifest와 실제 검증 결과가 최종 판정의 근거다. 중단·사용량 미확인·실행 결과 불명확 시 DB/예산을 삭제해 새 실행처럼 이어가지 않는다.

### 평가 자료 내보내기

Single 예시 경로 기준이다. 기존 출력은 덮어쓰지 않으므로 새 폴더명을 사용한다.

```sh
PYTHONPATH=src .venv/bin/python -m agents.platform.evaluation_export \
  --database .data/single/orchestrator.sqlite3 \
  --run-id '<실제 Run UUID>' \
  --store SINGLE_AGENT=.data/single/agents/single_agent.sqlite3 \
  --out '.data/evaluation-handoffs/<새로운-폴더명>'
```

[55번 파일 계약](55-single-agent-records-evaluation-handoff.md)에 따라 모든 후보 tar/Manifest, Run 설정, Trace·Issue·사용량·A2A 본문·파일 Hash와 누락 여부를 인계한다. FAIL·UNVERIFIED·HUMAN_REVIEW·ABORTED도 보존한다. 최종 Snapshot이 없거나 기록이 불완전하면 정상 채점 가능 자료로 꾸미지 않는다.

## 6. 팀원에게 전달할 내용

**세화(역할 3):** Single Host와 Multi Host는 같은 Run API/결과 구조를 사용한다. 요청의 `configuration.architecture`와 실제 기동한 Host 모드를 일치시킨다. Single 화면에서는 한 직원이 여러 업무 단계를 수행하는 것으로 표시한다. 서비스 화면·API·DB 연결과 실제 시연은 별도 통합 작업이다.

**석민(역할 4):** Single A2A는 `schemaVersion=2`, 실제 `roles.SINGLE_AGENT` 하나다. `expected_a2a_roles(architecture)`와 `handoff.steps[].phase`로 정체성/단계를 구분한다. 로컬 `dev`의 기존 D4 네 역할 고정 검사를 확장해야 한다. `coverage`의 누락·빈 기록을 PASS로 만들지 않고, 같은 보호 공통 시험지와 Manifest로 독립 채점한다. 자체 테스트 결과·Orchestrator Verdict와 독립 `evaluationVerdict`는 별개다.

Git 병합·팀원 모듈 수정·연락 전송은 이번 작업에 포함하지 않았다.

## 7. 이번 최종 검증 결과

이번 실행에서 네 묶음 **426개 통과**를 확인했다. 과거 문서의 검사 수치를 합산하지 않았고 중간 실패/중복 재실행도 포함하지 않았다.

| 묶음 | 결과 |
| --- | --- |
| Run 설정·Single 서버/Prompt/MCP·영속 예산·가격표 | 48개 통과 |
| Single 최초 실행·Build/QA/Security 수정·제어/복구·평가 내보내기 | 42개 통과 |
| 신규 Single Host·기존 Runtime/Host/CLI/가격표/서비스/Tool 회귀 | 211개 통과 |
| 기존 Multi Pipeline·비용 원장·Platform Runner | 125개 통과 |

재현 명령:

```sh
PYTHONPATH=src:tests .venv/bin/python -W error::RuntimeWarning -m unittest -q \
  test_run_configuration test_single_agent_server test_single_agent_prompts \
  test_single_agent_mcp test_single_agent_durable_budgets test_single_agent_priced_telemetry
PYTHONPATH=src:tests .venv/bin/python -W error::RuntimeWarning -m unittest -q \
  test_single_agent_pipeline test_single_agent_fix_pipeline \
  test_single_agent_controls test_single_agent_evaluation_export
PYTHONPATH=src:tests .venv/bin/python -W error::RuntimeWarning -m unittest -q \
  test_single_agent_host test_runtime_configuration test_owned_host test_owned_host_cli \
  test_priced_host test_host_services test_tool_configuration
PYTHONPATH=src:tests .venv/bin/python -W error::RuntimeWarning -m unittest -q \
  test_owned_agent_pipeline test_priced_pipeline test_pricing test_pricing_store \
  test_priced_telemetry test_agent_platform_runner
```

실제 로컬 SDK/ASGI·SQLite·Git·Snapshot/영수증 경계를 검사했다. 신규 파일 기반 Single Host도 기준 Source 준비→인증된 단일 A2A→모델 호출을 확인했고 잘못된 Draft가 SUCCESS가 되지 않음을 검사했다. 완성/수정 흐름 검사는 결정적 LLM·Docker 어댑터를 사용했다. 로컬 설정·실제 키·Docker daemon·외부 모델 호출·독립 시험지·비교 실험·브라우저 시연은 실행하지 않았다. 전체 저장소의 모든 검사를 실행했다는 의미도 아니다.

추가로 변경 Python 파일 **9개**의 Python 3.10 구문, 설정 예시 **2개**의 JSON, 수정 문서/README의 로컬 Markdown 링크 **73개**, 두 CLI의 `--help`, `git diff --check`를 확인했다. 개인 설정·인증 파일·디자인·Web/Evaluation 구현·Git 브랜치/이력은 변경하지 않았다.

## 8. 개발정의서 최종 대조

| 원문 기준 | 구현/검증 근거 |
| --- | --- |
| §0 공식 규격 | 기존 A2A 1.0 HTTP+JSON/REST·MCP stdio·고정 의존성 재사용. 프로토콜 버전 변경 없음 |
| §1 MVP | 동결 회원가입 Scenario/Requirement 기준 사용. 서비스 화면·실제 제품 시연 완료로 확대 해석하지 않음 |
| §2 책임 경계 | 실행·정체성·권한·증거는 역할 1·2, 서비스는 3, 보호 시험지·독립 비교는 4 |
| §3 요청/Artifact | 프로젝트 Schema·같은 opaque Task/Context·UUID 바인딩 유지 |
| §4 실패/수정/Retry | Fix 최대 3회·Code v1~v4, Infra Retry와 제품 실패 구분, 불확실 재실행 차단 |
| §5 코드 전달 | 같은 불변 Source·Execution Manifest로 Build/QA/Security 연결 |
| §6·§7 A2A/공식 상태 | `returnImmediately`/GET·저장된 원래 Task/Context·공식 상태 사용. Single은 서비스 정체성 |
| §8 MCP | 단계/경로/승인 프로필·보호 영역·Sandbox·Network/Secret 경계 유지 |
| §9 Artifact/버전 | 수정마다 새 후보·불변 Registry/BLOB·Hash 검증·원본 tar 인계 |
| §10 최종 판정 | 실제 근거로 기존 Verdict 계산. 모델 READY·Task COMPLETED·형식 검사만으로 SUCCESS 금지 |
| §11 Trace | Run/Step/Task/Context/Artifact/Issue/수정/재검증·실제 Single 주체·마스킹·완전성 |
| §11-A 비교 | 같은 모델/코드/요구사항/도구 기능/환경/한도와 별도 보호 공통 평가, 전체 시간·실측 사용량·승인 비용·실패 Run 보존 |

위 기준의 Single 구현 경로와 이번 내부 검증은 확인했다. 실제 환경·역할 3/4 통합까지 정의서의 모든 제품 성공 조건을 실증했다는 뜻은 아니다.

## 9. 이후 남은 작업

1. **주호:** 승인 설정·실제 키·Docker/이미지·기준 서비스를 준비하고 이 문서대로 Single/Multi를 실제 환경에서 실행한다. 모델/도구 호환성과 실제 결과는 그때 기록한다.
2. **주호·세화:** 실제 대상 서비스와 요청/상태/산출물을 연결해 시연한다.
3. **석민:** Single D4 소비자 대응과 동일 보호 시험지 채점·실험 집계를 수행한다. 실패/미검증 Run을 제외해 유리한 결론을 만들지 않는다.

**47~56번 번호 개발은 여기서 마친다.** 다음은 별도 번호를 자동 추가하기보다 보류한 실제 환경 준비·실행 확인이다.

커밋 메시지: `싱글에이전트 실행 진입점과 최종 점검·인계 안내 완성`

다음 작업: **실제 API·Docker 환경 준비 및 Single/Multi 실행 확인 — 사용자 준비 후 진행**.
