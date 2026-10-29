# 평가 모듈 (4번 Evaluation)

4번 담당 강석민의 평가 모듈입니다. 첫 대상은 회원가입 MVP(SCN-001)이고, 공통 계약 기준은 dev `caa4144`(dev `08542c6`까지 계약 변경 없음, 스키마 사본 4개는 `schemas/project` 원본과 같음을 확인)입니다.
평가 명세는 0.4이며, 이 모듈은 루트 `pyproject.toml`의 공통 패키지에 포함됩니다.

## 이 모듈이 하는 일

Agent가 생성한 최종 결과물을 같은 보호 테스트로 채점해 Multi-Agent와 Single-Agent를 비교하는 것이 목적입니다.
현재 코드는 받은 검사 결과의 정합성을 확인하고 보고서·지표를 만듭니다. 생성물을 실행하는 회원가입 보호 테스트(D2)와 결과 어댑터는 구현했고
합성 서비스로 검증했습니다. 실제 시작 코드 규격 확정과 최종 Snapshot 실행 연결(D5)은 아직입니다.
D1 보안 기준과 `assess()`는 개발 순환 안 Security 검증에도 제공하며 승인·Host 연결은 1·2번이 담당합니다.
순환용 보호 테스트와 최종 채점용 시험지의 공개 범위는 A/B/B′ 실험 정책에 따라 정합니다(명세 4.1절, OPEN-11·16). 이 모듈이 비공개를 강제하는 것은 아닙니다.
3번의 웹사이트는 Agent를 이용하는 플랫폼이며, 평가 대상 생성 서비스와 구분합니다.

## 파일 구성

```text
src/evaluation/
  aggregation.py    검사 계획과 결과를 맞춰 상태별 개수·통과율·실패/미검증 목록 계산
  manifest.py       검사 대상 코드·환경(Manifest) 형식 검사, 기준과 같을 때만 집계
  independent.py    독립 평가 보고서(INDEPENDENT_EVALUATION) 생성  ← 기본 경로
  reporting.py      1번이 받는 QA_REPORT / SECURITY_REPORT 변환      ← 에이전트 호환 보조 경로
  report_demo.py    공통 보고서 합성 예시 8개
  protected_tests/  회원가입 보호 테스트 묶음(D2): suite.json(검사 대응표), tests/signup/(연결부·QA·최종 전용 테스트)
  check_adapters.py unittest-v1 보고서·정적 판정 결과 → CheckResult (결과 어댑터)
  run_metrics.py    dev Run 조회 JSON → 지표(시간·수정·재시도·토큰·비용), 구조별 요약, 비교 가능 여부 검사, Regression
  trace_checks.py   Run 기록의 비밀번호·해시 노출(REQ-006 Trace 부분), 요구→개발→검증→수정 연결(REQ-008)
  final_evaluation.py D5 최종 채점기: Snapshot 확인·안전한 풀기 → 보호 테스트(DockerRunner) + 정적 판정 → 독립 평가 보고서
  summary.py        독립 평가 결과의 회의·발표용 Markdown 요약(판정·요구사항별 결과·실패→수정→재검증 흐름·지표)
  demo_index.py     시연 자료(로그·생성 파일·화면) 목록과 파일별 SHA-256, 비어 있는 분류 표시
  common.py         구조 이름·시각 파싱 공통 도우미
  security/         SCN-001 보안 판정(D1): 기준 JSON + dev 42번 assess() 진입점(소스 정적 분석)
  __main__.py       집계 합성 예시
  contracts/        schemas/project 공통 스키마 사본 4개 + 독립 평가 보고서 스키마 (설명은 contracts/README.md)
tests/evaluation/   평가 모듈 자체 테스트 (signup_examples.py = D3 정상·결함 예제, signup_service.py = D2 실행 시험용
                    최소 회원가입 서비스와 결함 변형, fixtures/ = 세화 backend 표본 복사본·dev 파이프라인 Run 기록 표본)
checks/             Python·가상환경·HTTP 라이브러리 확인 2개 (기본 pytest 대상 아님)
scripts/check_dev_reports.py   dev 보고서 모델이 합성 보고서를 받는지 확인
```

각 파일 맨 위에 "이 파일이 하는 일"과 용어 설명이 있고, 함수와 헷갈리기 쉬운 줄마다 초보자도 읽을 수 있는 한국어 주석이 달려 있습니다.
2026-10-14의 주석 정리 작업은 코드 구조(AST)를 바꾸지 않았다는 기록이며, 이후 보안 판정·지표 수정은 동작 변경을 포함합니다. 판정 기준 파일 `scn001_security_criteria.json`은 JSON이라 주석을 쓸 수 없어서 [기준표](../../docs/evaluation/scn001_criteria.md)에 설명이 있습니다.
처음 읽는다면
`aggregation.py` → `manifest.py` → `independent.py` 순서를 권합니다.

## 실행

저장소 루트에서 공통 환경을 한 번 설치합니다. Python 3.13과 uv를 사용하고(`.python-version`은 Git에 올리지 않아서 `--python 3.13`을 직접 적습니다),
PyCharm 인터프리터는 루트 `.venv/bin/python`입니다.

```bash
uv sync --frozen --python 3.13
.venv/bin/python -m pytest                     # 전체(평가 + dev)
.venv/bin/python -m pytest tests/evaluation    # 평가 테스트만
.venv/bin/python -m pytest checks              # 가상환경·라이브러리 확인
.venv/bin/python -m evaluation                 # 집계 합성 예시
.venv/bin/python -m evaluation.report_demo     # 보고서 합성 예시
.venv/bin/python scripts/check_dev_reports.py .venv/bin/python
```

## 기본 경로: 독립 평가 보고서

`evaluation.independent.build_evaluation_report`를 사용합니다. A2A Task ID가 필요 없습니다.

| 인자 | 내용 |
|---|---|
| `evaluation_id` | 이번 채점 실행의 UUIDv4. `source_run_id`와 달라야 함 |
| `source_run_id` | 채점 대상 개발 실행(Run)의 UUIDv4 |
| `architecture` | `SINGLE_AGENT` 또는 `MULTI_AGENT` |
| `suite_id`, `suite_version`, `suite_sha256` | 보호 테스트 묶음의 이름·버전·내용 해시(호출자가 계산) |
| `created_at` | 시간대 포함 ISO 시각. 예: `2026-10-14T12:00:00+09:00` |
| `required_requirement_ids` | 이번 평가가 다뤄야 하는 요구사항 UUID 목록 |
| `expected`, `observed` | 기준 Manifest와 결과가 실제로 나온 Manifest |
| `plan`, `results` | `CheckPlan`·`CheckResult` 목록 |
| `bindings` | `{검사 UUID: EvaluationBinding(requirement_id, perspective, title)}` |
| `sensitive_values` | 진단 설명에서 가릴 문자열(테스트용 비밀번호, 저장된 해시 등) |
| `source_run` | 선택. 대상 실행의 Orchestrator 기록. `SourceRunSummary.from_run_status(GET /api/v1/runs/{id} 응답)`으로 만든다. 주면 보고서 `sourceRun`에 최종 판정·수정 횟수·소요 시간(FINISHED·ABORTED만 종료로 보며 재개될 수 있는 HUMAN_REVIEW는 null)·`missedDefect`(Orchestrator SUCCESS인데 보호 테스트에서 필수 검사 FAIL이 확인됨. 다른 검사가 미검증이어도 해당)가 기록된다. runId와 코드 버전이 맞지 않으면 거부. startedAt은 Run의 createdAt이므로 durationMs는 생성~종료 시간이며 사건 기준 실행 시간과 구분 |

출력에는 검사별 결과·증거·사유, 요구사항별 요약, QA/SECURITY 관점별 요약,
검사 통과율과 요구사항 통과율이 따로 들어갑니다. 한 요구사항에 검사를 여러 개 연결할 수 있고,
검사가 없는 관점은 `null`입니다. 연결된 필수 검사가 모두 승인 N/A인 요구사항은 충족률 분모에서 제외하고, 적용 요구사항이 없으면 요구사항 충족률은 `null`입니다. `evaluationVerdict`는 Orchestrator의 `finalVerdict`를 덮어쓰지 않습니다.
필수 검사에 실패와 미검증이 섞이면 요약은 `UNVERIFIED`이고, 확인된 실패 ID는 따로 남습니다.
실패·미검증 ID 목록은 필수만 담고, 선택 검사는 `optionalConfirmedFailureIds`·`optionalUnverifiedTestIds`에 담깁니다.
보고서 예시는 명세 11.3절에 있습니다.

`CheckResult`에는 `expected_result`, `actual_result`, `reason`, `normalized_location`, `error_code`를
선택적으로 넣을 수 있습니다. 이 진단 정보는 두 보고서 경로 모두에 전달됩니다.

## 보조 경로: 에이전트 호환 보고서

`evaluation.reporting.build_report`는 1번 Orchestrator가 받는 dev 형식의 QA_REPORT / SECURITY_REPORT를 만듭니다.
실제 A2A Task·Artifact ID가 담긴 `context`, 필수 검사만의 계획, `TestBinding` 대응표,
PASS·FAIL 판정마다 도구 실행 근거(`tool_evidence`)가 필요합니다. SECURITY는 `findings`를 빈 목록이라도
명시해야 하고, 요구사항당 결과 1개만 받습니다. NOT_APPLICABLE은 거부합니다.
도구 실행 PASS는 "도구가 끝까지 돌았다"는 뜻이라 제품 검사 FAIL과 함께 올 수 있습니다.

## 보안 판정 (D1)

(이름은 단어 단위로 판정합니다. `passed`·`MIN_PASSWORD_LENGTH`는 비밀번호 값이 아니고, 요청 모델을 통째로 출력하거나 `model_dump()`로 펼치면 노출입니다.)

`evaluation.security.assess(request)`는 dev `SecurityAssessmentRequest`를 받아 `SecurityAssessment`를 돌려준다.
REQ-003(UNIQUE·정규화), REQ-005(Argon2id 수치·평문/일반 해시 저장), REQ-006(로그·응답·검증 오류 노출)을
생성 코드를 실행하지 않고 AST로 판정하고, Bandit 경고는 지원 규칙(B105~107, B303/B324)만 확정·오탐 처리한다.
승인 해시 도구는 argon2-cffi `PasswordHasher`, pwdlib(`Argon2Hasher`, `PasswordHash.recommended()`), passlib Argon2(`passlib.hash.argon2`, `.using()`, argon2가 기본인 `CryptContext`)다. bcrypt 등이 기본인 `CryptContext`로 비밀번호를 해시하면 FAIL이다.
기준은 `security/scn001_security_criteria.json`, 수치는 동결 Scenario 정책에서 읽는다. 모르는 구조·근거 참조 없음·
문법 오류는 PASS 대신 UNVERIFIED다. 코드가 너무 복잡해 분석 한도(작업량·재귀)를 넘어도 PASS를 만들지 않고 해당 요구사항만 UNVERIFIED(`ANALYSIS_TOO_COMPLEX`)로 답한다. 승인 등록 시 논리 이름은 영문자로 시작해야 한다(`__init__.py` → `security_init.py`).
Host 증거는 UTF-8 65,536바이트 한도에 맞춥니다. 한도를 넘는 경고 판정과 관측 근거는 함께 생략하고 `omittedFindings`에 개수를 남겨 미확인임을 표시합니다. 요구사항 근거만으로도 한도를 넘으면 모든 요구사항을 UNVERIFIED로 내리고 `EVIDENCE_LIMIT_EXCEEDED`를 기록합니다.
기준 JSON은 아직 wheel에 포함되지 않습니다(루트 `pyproject.toml` 변경은 팀장 승인 후). 검사 기준표는 [SCN-001 기준표](../../docs/evaluation/scn001_criteria.md).

## 회원가입 보호 테스트 (D2)와 결과 어댑터

`src/evaluation/protected_tests/`는 생성물을 실제로 띄워 요청·DB를 확인하는 Python unittest 묶음입니다(dev 26번 `run_unit_tests`의 PROTECTED 범위로 실행).

| 범위 | 파일(패턴) | 검사 | 쓰는 곳 |
|---|---|---|---|
| `in-loop` (selector `protected-signup`) | `protected_signup_qa.py` | QA-SIGNUP-001·002·003·004·006 | 개발 순환 안 QA 보호 케이스(QA 담당 요구사항만 가능) + 최종 평가 |
| `final` (selector `final-signup`) | `protected_signup_*.py` | 위 5개 + QA-SIGNUP-005(동시 가입), SEC-SIGNUP-002(해시 저장값), SEC-SIGNUP-005(응답·로그 노출) | 최종 독립 평가 |

- 연결부 `tests/signup/signup_link.py`만 생성물 규격에 맞춰 고칩니다(지금은 세화 backend 구조: `main.app`, `database.get_db`, `models.User(email, password_hash)`, `POST /api/auth/signup`). 테스트마다 임시 SQLite DB를 만들고 `get_db`를 바꿔 끼우며, 로그는 앱의 로그 설정을 바꾸지 않고 루트·독립 로거와 stdout·stderr에서 모읍니다.
- 연결·전제 조건을 확인하지 못하면(앱 import 실패, 경로 404·405, 정상 가입 실패 등) FAIL이 아니라 SKIP `UNVERIFIED:코드`이고, 결과 어댑터가 ERROR(미검증)로 옮깁니다. 실패 메시지에는 비밀번호·해시·salt 값을 넣지 않습니다. 비밀번호는 실행할 때 만듭니다.
- 잘못된 입력은 4xx로 거부해야 합니다(5xx는 처리하지 못한 서버 오류). 처리하지 못한 예외는 실제 서버처럼 500으로 보고 traceback을 서버 로그에 남겨 노출 검사에 포함합니다.
- 필요 라이브러리: fastapi(TestClient)·httpx·sqlalchemy·argon2-cffi. 컨테이너 이미지에 있어야 하고, 프로젝트 환경에는 sqlalchemy·argon2가 없어 실행 시험은 `uv run --with sqlalchemy --with argon2-cffi python -m pytest tests/evaluation/test_protected_signup.py`로 돌립니다.

```python
from evaluation import protected_tests as pt
pt.owned_qa_cases(scenario_contract)                       # dev configs/owned-qa-cases.local.json 내용
pt.unit_test_scope('in-loop', suite_ref='artifact://...')   # dev UnitTestScope(PROTECTED) 설정 한 개
inputs = pt.evaluation_inputs(scenario_contract)            # 최종 평가 계획·꼬리표·suite 해시(11개 검사, 요구사항 7개)
results = pt.final_check_results(inputs, unit_report=report, unit_evidence_id=...,
                                 assessment_requirements=static_json['requirements'], static_evidence_id=...)
```

`evaluation.check_adapters`는 unittest-v1 보고서와 정적 판정 결과를 `CheckResult`로 바꿉니다. 보고서가 없거나 형식이 틀리거나 대응표 밖의 줄(예: `setUpClass` 실패)이 섞이면 통과한 줄이 있어도 모든 검사를 미검증으로 둡니다.

## 최종 독립 채점기 (D5)

```python
from evaluation.final_evaluation import DockerRunner, evaluate_final_snapshot
output = evaluate_final_snapshot(
    archive=snapshot_tar_bytes,              # 최종 Snapshot의 dev canonical tar(인계 방법은 팀장 결정 대기)
    expected=expected_manifest,              # 신뢰 경로로 받은 기준 Manifest
    scenario_contract=contract, source_run_id=run_id,
    runner=DockerRunner('repo@sha256:...'),  # 해시로 고정한 이미지만 받음
    run_bundle=bundle, trace_documents=documents)   # 선택: Run 기록 추적·노출 검사
output['report']   # 독립 평가 보고서(11개 검사, 요구사항 7개)
```

- Snapshot tar의 SHA-256이 기준과 다르면 실행하지 않고, lock 파일(기준 lock 해시와 같은 파일)과 실행 이미지 해시를 실제로 확인했을 때만 결과를 판정에 씁니다. 확인할 수 없으면 Manifest 없음, 다르면 불일치로 UNVERIFIED입니다.
- 압축 파일은 일반 파일·상대 경로만 받습니다(../·절대 경로·링크·폴더 항목·압축 tar·중복·크기 초과 거부).
- `DockerRunner`는 dev 샌드박스와 같은 제한(네트워크 차단, 읽기 전용, 일반 사용자, 권한 제거, CPU·메모리·프로세스 제한)으로 dev의 `unit_runner.py`를 실행합니다. `LocalRunner(trusted=True)`는 합성 표본 시험용이며 이미지를 확인할 수 없어 결과가 판정에 쓰이지 않습니다. 생성 코드를 호스트에서 실행하지 않습니다.
- 정적 판정은 같은 Snapshot 소스로 `security.assess.evaluate()`를 부릅니다(dev Host 없이). 소스를 읽기만 하므로 컨테이너 밖에서 돌며 생성 코드를 실행·import하지 않습니다.
- 지금까지는 합성 서비스와 실행기 대역(가짜 docker, `LocalRunner`)으로 이 경로를 확인했습니다. 실제 Docker와 에이전트 생성물로는 아직 돌리지 않았습니다.
- `evaluation.summary.render_summary(output['report'], scenario_contract=..., metrics=..., run_bundle=..., trace=output['trace'], runner=output['runner'])`로 회의·발표용 요약을 만듭니다. 에이전트가 쓴 자유 글은 넣지 않습니다. `trace`(runId)·`runner`(evaluationId)·`metrics`·`run_bundle`이 보고서와 다른 Run·평가의 것이면 ValueError로 멈춥니다. 판정 줄에는 그 판정이 보호 테스트·정적 판정 몇 개의 결과인지 적고, 바로 아래에 판정에 들어가지 않는 Run 기록 검사(노출·추적) 결과와 REQ-006 전체 출력의 노출 검증 상태(기록에서 노출 발견, 기록 검사 없음, 보지 못한 출력 있음이면 "충족하지 않음"·"미완료")를 따로 적습니다. Run 기록 노출이 PASS가 아니면 요구사항별 표의 REQ-006 줄에도 덧붙입니다.
- `known_values=[...]`로 이 Run에서 쓴 시험 비밀값을 넘기면 Run 기록 노출 검사가 자유 글에서도 찾습니다(결과에는 개수·위치만 남음). 노출 결과의 `coverage`에 읽은 기록과 보지 못한 출력(A2A Message 본문·Artifact 파일 내용·에이전트 프로세스 출력)을 적어, PASS가 REQ-006 전체 충족이 아니라 부분 검사임을 드러냅니다.
- 팀장 전달 파일: `python -m evaluation.protected_tests export --out <폴더>`가 `owned-qa-cases.local.json`, `unit-protected-scope.json`(owned-tools의 unit.scopes에 추가), `handoff.json`(suite 지문·참조)을 만듭니다.

## 결과 지표와 시연 자료 (D4)

`evaluation.run_metrics`: dev Run 조회 JSON(`/runs/{id}`, `/events`, `/issues`, `/tool-attempts`, `/usage`, `/configuration`)을
지표로 바꿉니다. `fetch_run_bundle(get, run_id)`이 페이지를 이어 받고, `run_metrics(bundle)`은 한 Run의 수정 횟수·도구 재시도·시간
(사건 시각 차이)·토큰·비용을, `experiment_summary(runs, planned_runs, evaluations)`는 구조별 성공률·Verified Rate·비용 합계를,
`comparison_validity(entries)`는 두 구조를 비교해도 되는지(모델·한도·시작 코드·보호 테스트·suite 해시·환경 digest)를 계산합니다.
측정하지 못한 값은 0이 아니라 `None`이고, 성공률 분모는 시작하지 못한 Run을 포함한 계획 Run 수입니다.
모델 호출 수는 대상 Run이 모두 측정됐을 때만 `modelCalls`에 합계를 쓰고, 부분 합은 `knownModelCalls`, 완전성은 `modelCallsComplete`로 구분합니다. 놓친 결함 수는 연결된 Run의 SUCCESS와 독립 보고서의 확인된 필수 실패로 계산하므로 선택 항목 `sourceRun` 생략에 영향받지 않습니다.
API 페이지 요청에 실패하면 수집을 중단합니다. 빈 페이지 등으로 선언된 사건 수와 수집 수가 다르면 `EVENTS_INCOMPLETE`, 전체 사건 수가 없으면 `EVENTS_COMPLETENESS_UNKNOWN`이며, 두 경우 모두 사건 기반 시간은 `None`입니다. 시간 계산은 `RUN_STARTED`부터라 Run 생성 시각을 쓰는 `sourceRun.durationMs`와 시작점이 다릅니다.
`regression_count({코드 버전: {검사 번호: 상태}})`는 앞 버전 PASS → 다음 버전 FAIL을 Regression으로 세고, 비교할 수 없는 검사가 있으면 `complete=False`로 알립니다. `regression_from_reports(버전별 독립 평가 보고서들)`는 같은 Run·같은 보호 묶음인지 확인한 뒤 바로 계산합니다(버전별 Snapshot 인계 전이라 아직 실행 결과에서 자동으로 나오지는 않음).
`collaboration_metrics(events)`의 Step 구간은 시도·재개마다 끊습니다: 일을 넘기기 시작 ~ 끝 상태 또는 입력·인증 대기. 첫 응답이 이미 끝 상태여도 그 시각에 닫고, 사람을 기다린 시간은 에이전트 시간에 넣지 않습니다.

`evaluation.trace_checks`: `fetch_trace_documents(get, run_id)`로 events·telemetry(페이지 전부)·issues·artifacts·tool-attempts를 받아
`check_trace_exposure`가 비밀번호 필드 값(정적 판정과 같은 단어 단위 이름 규칙)·실제 해시 값 모양·알려 준 값을 찾고,
`check_traceability`가 요구사항 → 코드 버전별 Snapshot → QA·Security 검증 → Issue → 수정 요청·완료 → 재검증 → 판정 사건이 이어지는지 봅니다.
결과에는 위치와 근거 코드만 남깁니다. 추적 규칙은 dev 파이프라인 테스트가 만든 실제 Run 6개(SUCCESS·FAIL·HUMAN_REVIEW·빌드 실패 경로)로 확인했습니다.
A2A Message 본문은 조회 API가 없어 보지 못합니다.
`collaboration_metrics(events)`(run_metrics의 `collaboration`)는 `/events`의 사건 시각으로 역할별 LLM·도구 시간, 일을 넘기는 전달 지연, 에이전트가 일한 시간(병렬 구간은 한 번만), 병렬로 겹친 시간, 에이전트 밖 시간, 사람 대기(`humanWaitMs`, HUMAN_REVIEW·WAITING_INPUT에서 벗어난 구간의 합)를 계산합니다. 상태 변화 사건이 없거나 사건이 모자라면 None입니다.

`evaluation.demo_index.build_demo_index(root, run_id=..., created_at=...)`: `logs`·`source`·`screens`·`reports` 폴더의 파일을
해시와 함께 목록으로 만들고 파일별 `sensitiveScan`으로 알려진 비밀값 검사 완료·생략과 이유를 구분합니다. 목록 생성 성공만으로 모든 파일의 비밀값 검사가 완료됐다고 보지 않습니다. 사용법은 [시연 자료 정리](../../docs/evaluation/demo_materials.md)에 있습니다.
두 모듈 모두 합성 입력으로만 검증했고 실제 Run으로 돌려 본 적은 없습니다.

## 하지 않는 일과 주의사항

- A2A로 전송하거나 MCP 도구를 부르지 않습니다. 보호 테스트를 실행하는 곳은 D5 실행기뿐이고, 실제 생성 코드는 컨테이너(`DockerRunner`)에서만 실행합니다(순환 안에서는 dev 도구가 실행).
- 증거 URI를 열거나 ID가 등록됐는지 조회하지 않습니다. suite 해시는 보호 테스트 묶음에 대해서만 `protected_tests.suite_sha256`이 계산합니다.
- 민감값 치환은 알려 준 문자열을 진단 필드에서만 바꿉니다. 제목·suite 이름·findings·toolEvidence·context는
  호출자가 미리 정리해야 합니다.
- 알려진 제약과 결정이 필요한 항목은 [개발진행사항](../../docs/evaluation/development_progress.md) 4절에 있습니다.

## 문서

- [환경 설정](../../docs/evaluation/environment_setup.md): 설치·실행·문제 확인
- [명세서](../../docs/evaluation/evaluation_spec.md): 평가 기준·책임·협의 항목
- [개발진행사항](../../docs/evaluation/development_progress.md): 현재 상태·검토 결과·다음 작업
- [SCN-001 기준표](../../docs/evaluation/scn001_criteria.md): 요구사항별 검사·판정 조건
- [보안 판정 기준](../../docs/evaluation/share_security_criteria.md), [Single 비교 기준선](../../docs/evaluation/single_baseline.md), [시연 자료 정리](../../docs/evaluation/demo_materials.md): 팀 논의·전달용

공통 스키마 사본은 원본을 대체하지 않습니다. dev 계약이 바뀌면 사본과 호환 검사를 함께 갱신합니다.
`seokmin` 브랜치에서 개발하며 환경·DB·작업공간·비밀값·빌드 산출물은 Git에서 제외합니다.
