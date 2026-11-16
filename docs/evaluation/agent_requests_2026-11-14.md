# 에이전트 쪽 요청사항

> **지난 요청서(2026-11-14):** 같은 날 합의(4번 파트에 필요한 것은 4번이 정함)에 따라 여기 적은 요청은 대부분 4번이 직접 구현했다. 현재 상태는 [개발진행사항](development_progress.md)과 [dev 변경 공유](dev_changes_2026-11-14.md)를 따른다.

## 목차

- **가. 정한 것**
- **나. 구현 요청**
  1. 시나리오 계약에 연결 규격 추가
     - 1-1 넣을 내용
     - 1-2 적는 방식
     - 1-3 고칠 곳
     - 1-4 오래된 시험 패치
     - 1-5 확인
  2. Ver1 보안 보호 케이스 허용
  3. 실행 설정 준비
     - 3-1 실행 이미지
     - 3-2 시작 저장소
     - 3-3 도구 설정
     - 3-4 실행 설정 (시범 값)
     - 3-5 가격표
     - 3-6 Ver1·Ver2 설정
     - 3-7 확인
- **다. 운영 요청**
  4. 측정 Run 선언과 평가 인계 폴더 전달
  5. 측정 Run의 질문·보류는 재개하지 않기

**진행 순서:** 1·2·3 구현 → 시범 Run(조합마다 1회) → 공식 한도 확정 → 공식 Run(조합마다 5회). 4·5는 Run마다 적용

---

## 가. 정한 것

| 항목 | 정한 값 | 이유 |
|---|---|---|
| 모델 | `gpt-4.1-mini-2025-04-14`, temperature 0. Single·Multi 같음 | 통신 진단에서 지금 dev와 동작을 확인한 모델이에요 |
| 한도·시간·비용 | 시범 Run은 넉넉한 값(3-4)으로 돌리고, 실제 사용량을 보고 공식 값을 정해 드려요 | 의미 검토와 도구 호출 때문에 실제 사용량을 아직 몰라요. 너무 낮으면 한도 소진으로 끝나 비교가 왜곡돼요 |
| 반복 | 시범은 조합마다 1회. 공식은 조합(Single·Multi × Ver1·Ver2)마다 5회, 총 20 Run | 개발정의서 추천이 5회예요 |
| Ver1·Ver2 | 둘 다 써요. Ver1은 순환 안에 연습 시험 QA 6개·보안 3개를 넣어요 | 두 버전을 비교해서 순환 안 보호 테스트의 효과를 봐요 |
| 측정 Run 질문·보류 | 답하거나 재개하지 않고 종료로 집계 | 사람이 끼어들지 않아야 두 구조의 조건이 같아요. 평가 쪽이 멈춘 원인을 따로 기록해요 |

---

## 나. 구현 요청

### 1. 시나리오 계약에 연결 규격(`externalContract`) 추가

**이유**
- 숨긴 시험지와 Ver1 연습 시험은 생성물의 `main.app`, `database.get_db`, `models.User`, `POST /api/auth/signup`에 직접 붙어서 채점해요. 하나라도 다르면 연결 단계에서 전부 미검증이 되고, Single·Multi 정확도를 잴 수 없어요.
- 지금 dev는 이 규격을 에이전트에게 주지 않아요.
  - Planner는 요청문과 시나리오 계약을, Developer는 시나리오 계약과 계획만 받아요.
  - Developer 도구는 파일 읽기·쓰기 두 개뿐이라 폴더를 둘러볼 수 없어요.
- 11.13 dev부터 요청문은 승인 기준이 아니에요. 요청문에만 적으면 Planner가 승인 안 된 가정으로 보고 질문하며 멈추거나, 의미 검토에서 보류될 수 있어요. 그래서 동결 계약으로 넘겨야 해요.

**필요한 항목**

#### 1-1. 넣을 내용 (SCN-001)

| 항목 | 내용 |
|---|---|
| 앱 | `main.py`의 `app`(FastAPI). 서버는 `uvicorn main:app`. 준비 확인은 `GET /`가 2xx |
| DB | `database.py`의 `get_db`(동기 함수). DB 접근은 모두 `Depends(get_db)`. DB 위치는 환경 변수 `DATABASE_URL` |
| 사용자 모델 | `models.py`의 `User`(SQLAlchemy). 칸 이름은 `email`·`password_hash`. email UNIQUE는 모델 정의 안에 |
| 가입 API | `POST /api/auth/signup`, 본문 칸 `email`·`password`. 성공 2xx, 입력 오류·중복 4xx. 응답에 비밀번호·해시 없음 |
| 시작 골격 파일 | `main.py`, `database.py`, `models.py`, `tests/test_health.py`, `requirements.lock` |
| 패키지 | 실행 이미지에 든 것만 사용 |

골격 초안: [`examples/starting-source/scn001-signup`](../../examples/starting-source/scn001-signup/README.md)

#### 1-2. 적는 방식

- 칸 이름에 `password`·`비밀번호`처럼 비밀값 이름으로 보이는 말을 쓰지 말아 주세요. `redact_data`가 값을 가려서 승인 원장(`freeze_requirement_authority`)이 `REQUIREMENT_AUTHORITY_INVALID`로 거부하고, Planner 입력 검사와 의미 검토 기준 검사도 실패해요.
- `password: …`, `비밀번호=…`처럼 콜론·등호가 붙은 문장도 같은 이유로 쓰지 말아 주세요.
- 칸 이름은 값으로 적어 주세요. 예: `"bodyFields": ["email", "password"]`, `"columns": ["email", "password_hash"]`. 이 형태는 세 검사를 모두 통과했어요.
- 값은 문자열이나 문자열 목록으로 적어 주세요. 의미 검토는 기준(`policies`) 안의 문자열만 근거로 인용할 수 있어요.

#### 1-3. 고칠 곳

| 위치 | 할 일 | 안 하면 |
|---|---|---|
| `scenario_registry.py`의 `planner_contract()` | SCN-001 정의에 `externalContract` 추가 | 규격이 계약에 없어요 |
| `scenario_registry.py`의 `ScenarioDefinition.from_contract()` | 이 칸 유지 | 동결 계약을 복원할 때 빠져서 어느 역할 입력에도 안 들어가요. Planner는 받은 계약이 동결 계약과 달라 첫 입력부터 거부해요(확인함) |
| `validation_request.py`, `qa_context.py`·`security_context.py`의 `_dispatch_request` | QA·보안 정책 필터(지금은 `securityPolicy`·`emailPolicy`만)에 세 곳 똑같이 추가 | QA·보안에 규격이 안 가요. 한 곳만 바꾸면 QA·보안이 받은 요청을 자기 쪽 재현본과 비교해서 입력 불일치로 거부해요 |
| `agents/semantic_review.py`의 `approved_basis` | 의미 검토 기준 `policies`(지금은 `excludedFeatures`·`securityPolicy`·`emailPolicy`)에 추가 | 계획·요약에 규격이 나오면 승인 범위 밖으로 보류될 수 있어요 |

- 동결 계약은 dispatch의 `_scenario_for_run`과 에이전트 쪽 문맥(`planner_context._scenario`)이 `from_contract()`로 복원해요. 그래서 위 첫 두 줄을 고치면 Planner·Developer·수정 요청·Single 입력에는 자동으로 들어가요.
- `scenario_registry.py`는 지금처럼 표준 라이브러리만 쓰게 유지해 주세요. 새 dev의 제품 백엔드(`backend/project_scenarios.py`)가 이 파일만 따로 불러와요.

#### 1-4. 오래된 시험 패치 (같이 적용)

PR #17 뒤 갱신되지 않아 공유 dev에서 실패하는 시험 4개예요. 제품 결함은 아니에요.

| 시험 | 고칠 내용 |
|---|---|
| `test_owned_platform_tcp` 2개 | 의미 검토 호출 때문에 한도 8 → 16, 호출 수 기대값 6→10, 13→20 |
| `test_validation_revalidation_services` 2개 | 브라우저 QA 기대값을 시나리오 계약의 완료 조건 문장으로 |

- 파일: [`patches/2026-11-14_dev_stale_tests.patch`](patches/2026-11-14_dev_stale_tests.patch) (`git apply`로 적용)
- 공유 dev 복사본에서 두 파일의 시험 13개가 통과하는 것을 확인했어요.

#### 1-5. 확인

- 동결 계약 해시(`scenarioContractSha256`)에 새 칸이 들어가요. `planner_contract()`에 넣으면 자동이에요.
- Planner·Developer·QA·Security·Single 입력에 같은 `externalContract`가 들어가요(시험).
- 규격을 언급한 계획·요약이 의미 검토에서 보류되지 않아요(시험).
- 시범 Run 생성물에서 시험지 연결 미검증이 0건이에요.

### 2. Ver1 보안 보호 케이스 허용

**이유**
- Ver1은 순환 안에 연습 시험 QA 6개와 보안 3개를 넣기로 정했어요.
- Host 설정 검사(`host.py`의 `_policy_references`)가 Security 쪽 보호(PROTECTED) 범위를 거부해서, 지금은 Ver1 Run이 시작되지 않아요. 보안 보호 케이스 계약이 따로 없다는 이유로 막아 둔 상태예요.
- Security가 보호 케이스를 실행하고 측정 결과로 판정하는 경로는 이미 있어요(10.30 커밋 `716f04a`). 막힌 곳은 Host 설정 검사와 계약이에요.

**필요한 항목**
- 보안 보호 케이스 계약을 정해 주세요. 케이스 파일에 케이스별 역할 칸을 두거나, Security 전용 케이스 파일을 따로 받는 방식이에요.
  - 지금 평가 쪽 케이스 파일은 케이스마다 `toolName`·`selector`·`testId`·`requirementId`·`title`·`expectedResult`를 담아요. Security 케이스의 `selector`는 `protected-signup-security`예요.
  - 계약이 정해지면 평가 쪽 케이스 파일 생성을 그 형식에 맞출게요.
- Host 설정 검사가 그 계약을 따르는 Security 보호 범위를 받게 해 주세요.
- 시험: Security 보호 케이스는 Security 실행에만, QA 보호 케이스는 QA 실행에만 쓰여요.
- 확인: 평가 쪽이 만든 Ver1 설정(QA 범위 6개 + Security 범위 3개)이 Multi·Single 모두에서 Host 설정 검사를 통과하고, 시범 Run 보고서에 두 역할의 연습 시험 결과가 들어가요.

### 3. 실행 설정 준비 (Single·Multi 같은 값)

**이유**
- 개발정의서 11-A대로 시작 커밋·Snapshot·모델·한도가 같아야 비교가 유효해요.
- Host는 `--runtime`·`--tools`·`--source`가 모두 필수인데, 로컬 설정 파일이 아직 없고 예시 파일은 핵심 칸이 비어 있어요.
- 예시 한도로는 Run이 돌지 않아요. 호출 한도 8회로는 첫 바퀴도 못 끝나고, 시간 예산이 비어 있으면 Run 제출이 거부돼요.
- 평가 이미지도 실행 이미지와 같은 lock을 써야 같은 패키지 버전에서 채점할 수 있어요.
- 가격표가 없으면 비용이 모두 `PRICING_UNAPPROVED`(금액 없음)로 나와서 비용 비교를 못 해요.

**필요한 항목**

#### 3-1. 실행 이미지 (`docker/sandbox`)

- 준비물: `BASE_IMAGE` digest, `prepared/requirements.lock`, `prepared/wheelhouse`, `prepared/ms-playwright`
- lock에 playwright·bandit이 있어야 해요. Dockerfile이 설치를 확인해요.
- lock과 wheelhouse를 저에게도 공유해 주세요. 평가 이미지도 같은 lock·wheelhouse로 만들어요.
- 이미지를 누가 만들지 정해요.

#### 3-2. 시작 저장소 (`owned-source.local.json`)

- 골격을 별도 git 저장소로 커밋하고, 실행 이미지와 같은 `requirements.lock`을 넣어 주세요. 골격의 `requirements.in`은 후보 목록일 뿐이에요.
- `repositoryPath`·`repositoryId`·`commitHash`·`snapshotSha256`·`gitTimeoutSeconds`를 채워 주세요.
- 시작 커밋의 lock 해시는 도구 설정의 `dependencyLockHash`와 같아야 해요.
- 확인 명령:

```bash
python -m agents.platform.check_source --file <source 설정> --tools <tools 설정>
```

#### 3-3. 도구 설정 (`owned-tools.local.json`)

| 구역 | 채울 칸 |
|---|---|
| 공통 | `imageReference`(digest), `dependencyLockHash`, `hardwareProfile`, `maxCallSeconds` |
| build | `profile.argv`, `profile.limits` |
| unit | `limits` |
| browser | `service_argv`, `playwright_version`, `startup_timeout_seconds`, `action_timeout_ms`, `limits` |
| security | `profiles`의 `scanner_version`·`rule_ids`·`profile_ref`(한 종류만), `limits` |

- `service_argv`: `["/usr/local/bin/python","-m","uvicorn","main:app","--host","127.0.0.1","--port","8765"]`
- `base_url`(`http://127.0.0.1:8765`)과 `ready_path`(`/`)는 예시 값 그대로면 돼요.

#### 3-4. 실행 설정 (`owned-runtime` / `single-runtime` `.local.json`, 시범 값)

| 칸 | 시범 값 | 비고 |
|---|---|---|
| `model` | provider `openai`, modelId `gpt-4.1-mini-2025-04-14`, temperature 0 | modelRevision·seed는 비워도 돼요 |
| `llmLimits.max_model_calls` | 200 | Run 전체 합계예요. 의미 검토 호출도 들어가요 |
| `llmLimits.max_tool_calls` | 200 | Run 전체 합계라 예시 값 20회는 실제 Run에서 모자랄 수 있어요 |
| `llmLimits.max_output_tokens` | 8192 | 통신 재검증과 같은 값이에요 |
| `llmLimits.max_total_tokens` | 비움(제한 없음) | 시범에서 실제 사용량을 재요 |
| `llmLimits.model_timeout_seconds` | 120 | 긴 응답이 시간 초과로 끊기지 않게 해요 |
| `llmLimits.tool_timeout_seconds` | 도구 설정의 `maxCallSeconds`와 같은 값 | 다르면 Host가 거부해요 |
| `runtimeBudgetMs` | 3600000 (60분) | 필수예요 |

- Single·Multi는 tools·source 파일을 같이 쓰고, 위 값도 같게 해 주세요. runtime은 `agents` 구성과 DB·작업 폴더 경로만 달라요.
- 공식 값은 시범 결과를 보고 제가 정해 드릴게요.

#### 3-5. 가격표

- `owned-price-book.example.json`의 `entries`를 채워 `run_host`에 `--price-book`으로 주세요.

#### 3-6. Ver1·Ver2 설정

- Ver1·Ver2 도구 설정과 Ver1용 보호 케이스 파일은 3-3의 도구 설정으로 제가 만들어요. 두 버전은 unit 범위만 달라요.

```bash
python -m evaluation.experiment configure --tools <tools 설정> --source <source 설정> --out <폴더>
```

- Host 실행
  - Ver1: `--tools <폴더>/ver1/owned-tools.local.json --qa-cases <폴더>/ver1/owned-protected-cases.local.json`
  - Ver2: `--tools <폴더>/ver2/owned-tools.local.json` (보호 케이스 파일 없이)
  - 2번에서 Security 케이스 파일을 따로 받기로 하면 Ver1 실행 인자를 그에 맞게 바꿔요.
- 시작 코드·모델·한도·환경은 두 버전이 같아야 해요.

#### 3-7. 확인

- 제 쪽 `evaluation.preflight`(계획과 Host 설정 대조)를 통과해요.
- `evaluation.parity`(두 구조 같은 도구·한도, Run별 고정 해시)도 통과해요.

---

## 다. 운영 요청

### 4. 측정 Run 선언과 평가 인계 폴더 전달

**이유**
- 평가 비교는 같은 실험으로 선언된 Run끼리만 해요. `experimentId`가 Run마다 다르면 비교를 거부하고, 없으면 경고가 나요.
- 개발정의서 11-A대로 Run마다 실행 순서와 준비(Warm-up) 상태를 기록해야 해요.
- 채점과 Single·Multi·Ver1·Ver2 비교를 인계 폴더로 해요. Run마다 계획한 구조·버전을 알아야 실제 설정과 대조해서 잘못 집계되는 것을 막을 수 있어요.
- 수정 이력을 채점하려면 버전별 기록이 필요해요. 그래서 `--final-only`는 빼야 해요.

**필요한 항목**
- 측정 Run은 Run 생성 요청의 `configuration`에 아래 세 값을 넣어 실험 Run으로 선언해 주세요.
  - `experimentId`: 한 실험 전체에서 같은 값
  - `executionOrder`: 실행 순서
  - `warmupState`: 준비 상태
- Run이 끝날 때마다 아래 명령으로 만든 폴더를 넘겨주세요. 폴더마다 구조(Single·Multi)와 버전(Ver1·Ver2)을 같이 알려 주세요.
- DB 경로는 runtime 설정의 `orchestrator`·`agents`에 있는 `databasePath`예요.

Multi:

```bash
python -m agents.platform.evaluation_export --database <orchestrator DB> --run-id <Run ID> --out <폴더> \
  --store PLANNER=<planner DB> --store DEVELOPER=<developer DB> --store QA=<qa DB> --store SECURITY=<security DB>
```

Single:

```bash
python -m agents.platform.evaluation_export --database <orchestrator DB> --run-id <Run ID> --out <폴더> \
  --store SINGLE_AGENT=<single_agent DB>
```

### 5. 측정 Run의 질문·보류는 재개하지 않기

**이유**
- 측정 Run에는 답할 사람이 없어요. 답 내용이나 기다린 시간이 Run마다 달라지면 비교 조건이 깨져요.
- 평가 쪽은 멈춘 원인(Planner 질문, 의미 검토 보류)을 따로 기록하도록 이미 만들어 뒀어요.

**필요한 항목**
- 측정 Run이 입력 대기(`WAITING_INPUT`)나 사람 검토(`HUMAN_REVIEW`)로 멈추면 답하거나 재개(`/resume`)하지 말아 주세요.
- 그 상태 그대로 4번의 인계 폴더를 만들어 넘겨주세요.
