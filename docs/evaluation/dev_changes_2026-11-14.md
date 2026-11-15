# dev 변경 공유 (2026-11-14)

작성: 4번 · 기준: 공유 dev `ea06d12` (최신 dev `6c7f709`의 `src`·`configs`·`docker`·`tests`도 같음) · 상태: 작업 폴더에 반영, 커밋 전

11.14 합의(4번 파트를 구현하는 데 필요한 것은 4번이 정한다)에 따라, 평가에 필요한 dev 변경을 4번이 정해 반영했다. 1~3절은 11.14 변경이고, 4절은 11.13 증거용 수정이다. **11.15 추가 구현은 아래 최신 항목과 개발진행사항을 우선한다.** 이전 검사 수치는 당시 기록이다.

## 2026-11-15 dev `3147711` 병합 (주호 83~88번)

작업 브랜치를 새 dev(Host 서비스 인증·승인 설정 조회·Run 중복 생성 방지·접수 조회·Source ZIP)로 앞당기고 4번 변경을 다시 얹었다. dev 코드 자체는 바꾸지 않았다.

- 겹친 파일: `src/agents/platform/host.py`는 같은 자리에 양쪽이 import를 더한 충돌 1곳만 있어 둘 다 남겼다(4번의 `ObservationStore` + dev의 Host 토큰·비밀값 등록). `src/orchestrator/api/routes/runs.py`는 4번의 패키지 생산 상태 조회와 dev의 중복 제출 방지가 자동으로 합쳐졌다.
- 4번 쪽 대응: 측정 실행기(`evaluation.experiment_run`)와 `experiment fetch`가 `A2A_HOST_API_TOKEN` Bearer 헤더를 보내고, Run 생성에 `Idempotency-Key`를 쓴다. 응답 유실 복구는 `GET /api/v1/submissions/{key}`로 접수 기록을 조회해 잇고, `SUBMISSION_NOT_FOUND`일 때만 같은 키·같은 본문으로 다시 보낸다. 자세한 내용은 [개발진행사항](development_progress.md) 최신 절.
- 확인: 새 dev 시험과 Host 관련 시험 145개, 측정 실행기 111개, 실험 이미지 채점 13개 통과. 사전점검은 두 계획 모두 ready=true.

## 2026-11-15 브라우저 실행기 Chromium 샌드박스 (사용자 승인, 주호 확인 필요)

**바꾼 것:** `mcp_tools/tools/browser_runner.py`의 `chromium.launch(..., chromium_sandbox=True)`를 `False`로 바꿨다. 기존 주석은 그대로 두고 사유 주석을 더했다. dev 시험 `tests/test_mcp_browser_runner.py`의 기대값 두 곳도 맞췄다(실패 시 다른 옵션으로 다시 띄우지 않는 것은 그대로).

**원인(실측):** 실험용 공통 이미지에서 dev 샌드박스와 같은 컨테이너 옵션(`--cap-drop ALL`·`no-new-privileges`·`--network none`·읽기 전용·`--ipc none`)으로 실제 실행기를 돌리면 Chromium이 `No usable sandbox!`로 끝난다. 같은 옵션에 seccomp만 풀면 성공한다. 따라서 Docker 기본 seccomp가 Chromium 샌드박스에 필요한 사용자 네임스페이스 생성을 막는 것이 유일한 원인이다. setuid 샌드박스는 `no-new-privileges`로 쓸 수 없다.

**판단 근거:** 브라우저 시험 컨테이너는 에이전트가 만든 서버 코드를 이미 같은 UID·권한으로 실행한다. Chromium 렌더러 샌드박스는 페이지 내용으로부터 브라우저를 보호하지만, 그 페이지를 내는 쪽이 같은 컨테이너의 신뢰하지 않는 코드라 더해지는 격리가 거의 없다. 반대로 샌드박스를 살리려고 seccomp에 사용자 네임스페이스를 허용하면, 신뢰하지 않는 서버 코드도 네임스페이스를 만들 수 있게 되어 커널 공격면이 넓어진다. 격리 경계는 컨테이너 옵션으로 그대로 유지한다.

**다시 볼 조건:** 이 판단은 "신뢰하지 않는 서버 코드와 브라우저가 같은 컨테이너·같은 권한에서 돈다"와 "컨테이너 제한이 지금처럼 엄격하다"는 두 전제에 기대고 있다. 아래 중 하나라도 바뀌면 Chromium 샌드박스를 다시 켜는 방안(사용자 네임스페이스만 허용하는 별도 seccomp 등)을 다시 검토한다.
- 컨테이너 제한이 느슨해진다: 네트워크 허용, 권한(capability) 추가, 쓰기 가능한 루트, Host 경로·소켓 연결 등
- 브라우저가 신뢰하지 않는 서버와 다른 컨테이너·다른 권한으로 분리된다(그때는 렌더러 샌드박스가 실제로 막는 범위가 생긴다)
- 브라우저가 로컬 생성 서비스가 아닌 외부 페이지를 연다

이번 변경은 고정 설정 한 줄이며, 실패 뒤 다른 옵션으로 다시 띄우는 자동 우회(`--no-sandbox` fallback)는 여전히 없다.

**주호 확인 필요:** [27번](../27-browser-test-tool.md)·[39번](../39-sandbox-image-tool-profiles.md) 문서는 `chromium_sandbox=True` 고정과 끄지 않는 원칙을 적고 있다. 두 문서는 수정하지 않았다. 이 결정에 동의하면 문서를 갱신하고, 동의하지 않으면 seccomp 프로필 방식(샌드박스 실행 코드의 보안 옵션 검증 변경 필요)을 따로 정한다.

**재발 방지:** 4번 사전점검(`evaluation.preflight`)에 `hostBrowser.<구조>` 항목을 더했다. dev `SandboxRuntime._create`가 만드는 옵션 그대로 실제 브라우저 실행기로 시험 하나를 돌린다. 바꾸기 전에는 `HOST_BROWSER_START_FAILED`로 ready=false였고, 바꾼 뒤 두 시범 계획 모두 ready=true다.

## 2026-11-15 점검 수정 (관찰 기록 실패 비전파)

관찰 기록은 평가 증거이고 제품 실행의 일부가 아니다. 기록 저장이 실패해도 Run은 바뀌지 않게 4번이 넣은 관찰 코드만 고쳤다(팀장 기존 코드·주석은 그대로).

| 파일 | 바뀐 동작 |
|---|---|
| `orchestrator/observability.py` | 시작 표식을 못 쓰면 관찰 없이 실행, 로그 영수증 실패는 예외 대신 실패 수만 세고 구간 끝을 `INCOMPLETE`로 표시, 끝 기록 실패도 원래 결과를 유지 |
| `orchestrator/core/logging.py` 경유 | 로그 호출이 영수증 저장 실패로 예외를 던지지 않는다(위 `observe_log`) |
| `orchestrator/sandbox/runtime.py` | 도구 stdout·stderr 영수증 저장 실패를 도구 실패로 바꾸지 않는다 |
| `agents/platform/telemetry.py` | 모델 입력 해시·출력 영수증 저장 실패가 모델 호출을 멈추지 않는다 |
| `agents/api/sqlite_task_store.py` | A2A 출력 영수증을 savepoint 안에서 저장해 실패해도 Task 저장은 유지 |

빠진 영수증과 `INCOMPLETE` 구간은 평가가 노출 검사·과정 지표의 미검증으로 본다. 모든 영수증 저장이 실패하는 상황에서 실제 파이프라인을 돌려 Run 결과가 같고 평가가 미검증으로 보는지 시험한다(`tests/test_observation_failure_isolation.py`, 고치기 전 코드에서는 실패). 관문 거부 사건 `PROCESS_GATE_REJECTED`의 dev 기록·평가 집계 시험도 추가했다(`tests/test_process_gate_event.py`).

## 2026-11-15 Docker 재허용 이후 점검

- D5는 소스의 고정 `requirements.lock`, 실제 이미지 ID, 이미지에 보관된 lock의 바이트 해시를 대조한다. 패키지 설치는 이미지 준비 단계의 hash-locked 설치·의존성 검사·버전 확인으로 별도 증명한다.
- 재생 입력은 컨테이너에서 읽을 권한을 명시하고 호스트 보관본은 비공개 권한을 유지한다. timeout 정리 실패는 후속 평가를 중지한다. 일부 파일만 얻은 커버리지는 전체 결과로 표시하지 않는다.
- Agent 평가표는 다른 Snapshot·누락/중복 검사·다른 요구사항의 검사 재사용으로 PASS를 만들지 않는다. 빌드·요구사항 누락을 분모에 유지한다.
- 과정 계수는 `handoff.steps.attempt`(A2A 재개 횟수)를 수정 차수로 해석하지 않는다. 실제 수정 시작 사건과 검증 시작 사건의 step·버전·Source를 연결하고 같은 수정 주기의 파일/QA 변화는 한 번만 센다. 검증 충돌도 같은 실제 주기의 QA/Security끼리 비교한다.
- 정적 보안 보조 판정의 SQL·동적 필드 평문 덮어쓰기와 다른 요청 모델의 정규화 코드 오인을 보완했다. 증명하지 못하는 저장 경로는 미검증이며 최종 제품 점수는 계속 독립 실행 검사로 정한다.

검증용 이미지/lock은 로컬 Linux arm64 환경의 증거다. 공식 Host 이미지·공통 시작 커밋을 대신 동결하지 않았으며 실제 모델 실행도 하지 않았다. [상세 기록](docker_verification_2026-11-15.md)을 따른다.

## 2026-11-15 추가 연결

- `HostPackageService`와 `package_records`가 최종 보고서·완료 기록·사건을 같은 DB 트랜잭션으로 저장한다. Host 시작 시 복구하며 인계는 저장된 증거만 읽는다. `/api/v1/runs/{id}/package-production`은 조회 전용이고 측정 실행기는 저장 중이면 제한 시간 동안 기다린다.
- `dispatch`는 작업 → 출력 관찰 확정 → 패키지 저장 callback → 제어권 반환 순서다. 취소도 종료 이후 callback을 연결한다. 입력 대기·사람 보류·보조 재개를 자동 완료로 바꾸지 않는다.
- `orchestrator.observability`, Agent task store, LLM engine/telemetry, 도구 runtime, logging에서 실제 공개 출력 경계와 정책을 기록한다. 원문 대신 해시·크기·발견 유형 및 시작/종료 범위를 저장한다. 비용 영수증을 출력 기록 실패 때문에 버리지 않는다.
- `evaluation_export`는 관찰 기록과 기존 Snapshot·QA 테스트 입력 파일을 해시 대조해 자동 인계한다. 완전성 없는 기록은 0건이나 PASS로 승격하지 않는다. A2A/LLM 자동 재시도 비활성·재계획 미지원도 사전 정책과 전체 자동 구간 증거가 있을 때만 0건이다.
- 실제 시험 입력 저장·재생은 평가기 내부 기능이다. `.private-inputs`와 `.evaluation-history`는 Git에서 제외하고 Agent 시작 코드에도 섞지 않는다. `--force` 재채점은 이전 입력·결과·계약을 보존한다.
- 공통 보고서 스키마의 필수 칸은 추가하지 않았다. 새 DB 증거와 export 부가 정보를 통해 연결한다. 모델·역할 분리·QA/Security 병렬 구조를 변경하지 않는다.
- 전체 검사에서 발견한 재개 응답 경쟁도 수정했다(`agents/api/handler.py`). 새 요청은 이미 접수됐는데 이전 AUTH/INPUT_REQUIRED 상태가 늦게 반환되던 경우, 같은 Task·Context의 정확히 직전 attempt이고 나머지 metadata가 일치할 때만 현재 승인 상태를 다시 읽는다. 요청을 재제출하지 않으며 다른 Run·Task·Context·미래 attempt·임의 metadata 불일치는 계속 거부한다. 기존 continuation 11개+신규 3개, Agent API/저장소/실행 계약/Host 연동 110개가 통과했고 독립 읽기 검토를 마쳤다.

검증 결과와 실제 실험 전제조건은 [개발진행사항의 최신 절](development_progress.md)을 따른다. Docker 재허용 전에는 연결하지 않았다. 이후 2026-11-15 사용자 요청으로 로컬 D5 컨테이너 검증을 수행했으며, [검증 기록](docker_verification_2026-11-15.md)에 범위와 환경을 남겼다. 실제 모델 실험은 아직이다.

**검증:** 저장소 전체 시험 5,141 통과 / 1 실패 / 1 건너뜀.
- 실패 1개는 `test_mcp_execution_runtime`의 시간 시험이다. 따로 돌리면 통과하고, 그 파일과 MCP 코드는 바뀌지 않았다.
- 건너뛴 1개는 Docker가 필요한 시험이다.

## 1. 연결 규격(`externalContract`)을 시나리오 계약에 추가 (D0)

숨긴 시험지와 Ver1 연습 시험은 생성물의 `main.app`, `database.get_db`, `models.User`, `POST /api/auth/signup`에 직접 붙어 채점한다. 이 규격을 동결 시나리오 계약에 넣어 모든 역할에 같은 내용으로 전달한다.

| 파일 | 변경 |
|---|---|
| `orchestrator/domain/scenario_registry.py` | `ScenarioDefinition.external_contract_json` 칸 추가. `from_contract()`가 칸을 유지하고 `planner_contract()`가 `externalContract`로 낸다. SCN-001 값 등록. 파일은 계속 표준 라이브러리만 쓴다(새 dev 제품 백엔드가 이 파일만 따로 불러온다) |
| `orchestrator/application/validation_request.py`, `agents/runtime/qa_context.py`·`security_context.py`의 `_dispatch_request` | QA·보안 요청의 정책 필터에 `externalContract` 추가. 세 곳이 같아야 QA·보안이 받은 요청을 재현본과 비교해 받는다 |
| `agents/semantic_review.py`의 `approved_basis` | 의미 검토 기준 `policies`에 `externalContract` 추가 |
| `orchestrator/application/control_input.py` | 답변(`inputData`)의 보호 키에 `externalContract` 추가. 답변으로 규격을 바꿔치기할 수 없다 |

**SCN-001 값:**

| 항목 | 내용 |
|---|---|
| app | `main.py`의 `app`(FastAPI), `uvicorn main:app`, `GET /`에 2xx |
| database | `database.py`의 `get_db`(동기 함수), DB 접근은 모두 `Depends(get_db)`, `DATABASE_URL`, SQLite에서 동작 |
| userModel | `models.py`의 `User`(SQLAlchemy), 칸 `email`·`password_hash`, email UNIQUE는 모델 안에 |
| signupApi | `POST /api/auth/signup`, JSON 본문 칸 `email`·`password`, 성공 2xx, 입력 검증 실패·중복 4xx |
| responseRule | 응답 본문에 password와 password_hash 값을 넣지 않는다 |
| startingFiles | `main.py`, `database.py`, `models.py`, `tests/test_health.py`, `requirements.lock` |
| packages | 실행 이미지에 설치된 패키지만 사용 |

- **적는 방식:** 칸 이름에 `password` 같은 비밀값 이름을 쓰지 않고 값으로 적었다(`"bodyFields": ["email", "password"]`). 칸 이름으로 쓰면 `redact_data`가 값을 가려 승인 원장·Planner 입력 검사·의미 검토 기준 검사가 모두 거부한다. 값은 의미 검토가 인용할 수 있게 문자열·문자열 목록만 쓴다.
- **예전 Run:** 이 칸이 생기기 전에 동결된 계약은 칸 없이 그대로 복원된다.
- **평가 쪽 대조:** 평가는 동결 계약의 연결 규격이 시험지 연결부와 다르면 설정·채점을 만들지 않는다(`CONTRACT_NOT_SUPPORTED`).
- **시험:** `tests/test_scenario_external_contract.py` 8개. 동결·복원, Planner 입력 일치, QA·보안 요청과 재현본 일치, 의미 검토 기준, 답변 보호 키를 본다.

## 2. Ver1 보안 보호 케이스 계약 (D2)

Ver1은 순환 안에 연습 시험 QA 6개·보안 3개를 넣는다. Security가 보호 케이스를 실행하는 경로(10.30 `716f04a`)는 있었고, Host 설정 검사가 Security 보호 범위를 계약이 없다는 이유로 막고 있었다.

| 파일 | 변경 |
|---|---|
| `agents/platform/host.py` | 보호 케이스 파일 schemaVersion 2를 받는다. 2판은 케이스마다 `role`(`QA`·`SECURITY`)을 적는다. Security 보호 범위는 모든 케이스의 `role`이 그 케이스가 가리키는 보호 범위의 역할과 같을 때만 받는다. 역할이 없는 1판은 지금처럼 QA만 받고 Security 보호 범위는 거부한다 |

- QA 케이스에서 Security 케이스를 추론하지 않는다는 기존 원칙은 그대로다. 역할은 파일에 명시되고 Host가 검사한다.
- `_policy_references` 위의 기존 주석("a separate protected Security case contract is not implemented here")은 고치지 않았다. 이제 케이스 파일 2판이 그 계약이다. 주석 정리는 주호님 판단에 맡긴다.
- 평가 쪽 Ver1 설정(`experiment configure`, `protected_tests export`)은 2판 파일을 만든다.
- **시험:** `tests/test_owned_host_security_cases.py` 6개. 1판 거부, 2판 허용, 역할 불일치·잘못된 역할 거부, Security 범위 없는 2판을 본다. 평가 쪽이 만든 실제 Ver1 설정이 Host 검사를 통과하는지도 본다.

## 3. 오래된 시험 4개 패치 적용

PR #17의 의미 검토·기대값 규칙 뒤에 갱신되지 않은 시험이다. 제품 결함은 아니다. [`patches/2026-11-14_dev_stale_tests.patch`](patches/2026-11-14_dev_stale_tests.patch)를 작업 폴더에 적용했다.

| 시험 | 변경 |
|---|---|
| `test_owned_platform_tcp` 2개 | 의미 검토 호출 때문에 한도 8 → 16, 호출 수 기대값 6→10, 13→20 |
| `test_validation_revalidation_services` 2개 | 브라우저 QA 기대값을 시나리오 계약의 완료 조건 문장으로 |

## 4. 작업 폴더의 커밋 전 dev 수정 (평가 증거용, 11-13)

평가가 "모델이 실제로 받은 입력"과 "실행 한도"를 확인하려고 만든 수정이다. 공유 dev에는 아직 없다.

| 파일 | 내용 |
|---|---|
| `src/agents/llm/input_evidence.py` (새 파일) | 모델 호출 직전 입력의 해시를 계산한다(metadata·작업 입력·첫 입력·system·입력 항목). 원문은 저장하지 않는다 |
| `src/agents/llm/engine.py` | 위 관측을 호출 직전에 부른다. **관측에 실패하면 호출을 막고 Run 예산을 무효로 한다** |
| `src/agents/platform/telemetry.py`, `telemetry_store.py` | `MODEL_INPUT_OBSERVED` 사건 저장. 해시만 받고, 같은 호출 번호의 시작 기록과 Run·단계·Task가 같은지 검사한다 |
| `src/agents/runtime/planner.py`, `developer.py`, `qa.py`, `security.py`, `src/agents/single/runtime.py` | 작업 호출에 위 기록 함수를 연결한다. 의미 검토 호출은 대상이 아니다 |
| `src/agents/platform/run_records.py` | 인계 기록에 예산 장부(`budgetEvidence`)를 붙인다 |
| `src/agents/platform/package_records.py` (새 파일), `evaluation_export.py` | 선택 인자 `--package-records`: Host가 남긴 완료 기록·최종 보고서를 해시 대조한 뒤 인계에 붙임. 11.15에는 `HostPackageService`의 DB 자동 생성·인계도 연결 |
| `tests/test_model_input_evidence.py` (새 파일), `tests/test_single_agent_evaluation_export.py` | 위 동작 시험 |

- **정할 것:** 관측 실패 때 제품 Run을 멈출지(지금 구현), 아니면 증거만 실패로 남기고 계속할지.
- **커밋:** 1~3절과 평가 변경과 분리한다.

## 5. 11.14에 남겼던 dev 쪽 일과 현재 상태

- **Host 과정 기록:** 11.15에 정책·중앙 관문·테스트 변경 자동 수집을 구현했다. 실제 Run 검증은 별도다.
- **출력 증거:** 11.15에 경계 관찰과 기존 산출물 바이트 자동 수집을 구현했다. 범위 밖 출력과 사후 임의값 검사의 한계는 명시적으로 남긴다.
- **최종 보고서·패키지 완료 기록:** 11.15에 실제 저장·사건 발행·자동 인계를 구현했다. 과거 Run에 완료 시각을 소급 생성하지 않는다.
- **전자문서 등록:** 개발정의서에 개발 부분이 없다. 범위(편집 vs 제출·조회)가 정해진 뒤 등록한다. 초안은 [`scn002_edocument_draft.md`](scn002_edocument_draft.md)에 있다.
