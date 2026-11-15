# 평가 모듈 (4번 Evaluation)

4번 담당 강석민의 평가 모듈입니다. 첫 대상은 회원가입 MVP(SCN-001)이고, 공통 계약 기준은 dev `caa4144`(dev `08542c6`까지 계약 변경 없음, 스키마 사본 4개는 `schemas/project` 원본과 같음을 확인)입니다.
평가 명세는 0.4이며, 이 모듈은 루트 `pyproject.toml`의 공통 패키지에 포함됩니다.

## 이 모듈이 하는 일

Agent가 생성한 최종 결과물을 같은 시험지로 채점해 멀티에이전트 Ver1(순환 안 보호 테스트 + 최종)과 Ver2(순환용 보호 테스트 없이 최종만, 에이전트 자체 검사는 있음)를 비교하는 것이 지금 목적입니다. 10.29 회의록의 Single vs Multi 비교 방안 1·2(방식 검토 중)를 회의 뒤 방침으로 먼저 멀티에이전트에 적용한 것이고, Single 실행기는 주호가 구현했습니다(같은 방안 안에서 구조별 비교). 11.2 회의 뒤에는 Agent별 평가표(작업 정확성·정보 전달·처리시간과 오류)와 실제 API 테스트를 먼저 합니다.
QA·Security 요구사항을 모두 실행 테스트로 판정합니다. 생성물을 실행하는 회원가입 보호 테스트(D2), 최종 채점기(D5), 실험 도구(설정 쌍·채점 보관·비교)는 구현했고
합성 서비스와 dev 파이프라인 시험으로 검증했습니다. 2026-11-11에는 최신 dev `c0c8ee8`의 인계 폴더를 받아 채점·Agent 표·버전 비교를 한 번에 만드는 경로를 연결했습니다. 모델·시범 호출/시간 한도는 정해졌으며, 2026-11-15에는 합성 서비스로 실제 Docker 채점·입력 재생·회귀 확인을 수행했습니다. 실제 비교용 시작 커밋·Host 도구 환경 동결과 LLM 생성물 실험은 별도입니다. A2A/API 진단 실행과 생성 서비스 전체의 평가 실험은 구분합니다.
D1 정적 판정(`assess()`)은 점수에 넣지 않는 보조 점검입니다. 순환 안 Security는 정적 증명 대신 자체·보호 테스트를 실행해 측정 결과로 판정합니다(dev 변경, 4번).
순환용 시험지(practice)와 채점용 시험지(pilot·final)는 같은 검사를 다른 값·모양으로 냅니다. 채점용 시험지는 에이전트에게 주지 않고, 시작 코드·채점 Snapshot에 섞이면 거부합니다(`exam_exposure`).
3번의 웹사이트는 Agent를 이용하는 플랫폼이며, 평가 대상 생성 서비스와 구분합니다.

## 파일 구성

```text
src/evaluation/
  aggregation.py    검사 계획과 결과를 맞춰 상태별 개수·통과율·실패/미검증 목록 계산
  manifest.py       검사 대상 코드·환경(Manifest) 형식 검사, 기준과 같을 때만 집계
  independent.py    독립 평가 보고서(INDEPENDENT_EVALUATION) 생성  ← 기본 경로
  reporting.py      1번이 받는 QA_REPORT / SECURITY_REPORT 변환      ← 에이전트 호환 보조 경로
  report_demo.py    공통 보고서 합성 예시 8개
  protected_tests/  회원가입 보호 테스트 묶음(D2): suite.json(검사 대응표), tests/signup/(연결부·검사 내용·시험지 세 벌)
  check_adapters.py unittest-v1 보고서·정적 판정 결과 → CheckResult (결과 어댑터)
  run_metrics.py    dev Run 조회 JSON → 지표(시간·수정·재시도·토큰·비용), 구조별 요약, 비교 가능 여부 검사, Regression
  workflow.py       Host evaluation_export → 파일 무결성 확인 → 모든 버전 채점·Agent 평가표·끝 상태 원인·결과 보관
  outcomes.py       끝 상태 원인(보류·질문·API 장애·한도 초과·중단)·의미 검토 호출 분리·자동 완료 판정
  parity.py         Single·Multi 도구 권한 합집합·LLM 한도·실행 설정 동일 확인, Run별 고정 설정 해시 대조
  preflight.py      실험 등록값·시작 커밋·시험지 격리·평가 이미지와 패키지 준비 점검
  output_evidence.py 지정한 산출물/프로세스 출력 파일 수집, 시험용 비밀값 파일 입력
  comparison_report.py 비교 집계 JSON → 발표용 Markdown(미확인·불일치·표본 수 보존)
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
기준 JSON과 보호 테스트 묶음은 wheel에 포함됩니다(2026-10-30). 검사 기준표는 [SCN-001 기준표](../../docs/evaluation/scn001_criteria.md).

## 회원가입 보호 테스트 (D2)와 결과 어댑터

`src/evaluation/protected_tests/`는 생성물을 실제로 띄워 요청·DB를 확인하는 Python unittest 묶음입니다(dev 26번 `run_unit_tests`의 PROTECTED 범위로 실행).

검사는 9개(QA-SIGNUP-001~006, SEC-SIGNUP-002·003·005)이고 시험지 세 벌(practice·pilot·final)이 모두 같은 검사·입력 종류를 다른 값·모양·순서로 냅니다. 입력 종류가 같은지는 시험(`test_every_set_uses_the_same_input_kinds`)이 검사마다 확인하고, 정책 칸·완료 조건마다 어기는 구현을 잡는 검사·합성 변형은 [기준표 5절](../../docs/evaluation/scn001_criteria.md)에 표로 있습니다.

| 범위 | 시험지 | 파일(패턴) | 검사 | 쓰는 곳 |
|---|---|---|---|---|
| `in-loop-qa` (selector `protected-signup-qa`) | practice | `practice_qa.py` | QA-SIGNUP-001~006 | Ver1 순환 안 QA 보호 케이스 |
| `in-loop-security` (selector `protected-signup-security`, role SECURITY) | practice | `practice_security.py` | SEC-SIGNUP-002·003·005 | Ver1 순환 안 Security 보호 케이스 |
| `practice` (selector `practice-signup`) | practice | `practice_*.py` | 9개 | 최종 때 순환용 시험지 참고 채점(차이의 차이 계산, 점수 제외) |
| `pilot` (selector `pilot-signup`) | pilot | `exam_pilot.py` | 9개 | 시범 채점(조정용) |
| `final` (selector `final-signup`) | final | `exam_final.py` | 9개 | 공식 채점(Ver1·Ver2 비교) |

- 연결부 `tests/signup/signup_link.py`는 공개 규격 `main.app`, `database.get_db`, `models.User(email, password_hash)`, `POST /api/auth/signup`을 사용합니다. 검사마다 새 `DATABASE_URL`에서 원래 앱의 초기화·DB 연결·로그 설정을 실행합니다. 평가기는 테이블이나 정상 DB 연결을 대신 만들지 않으며 별도 읽기 전용 연결로 저장 결과를 조회합니다. 앞 검사의 생성물 모듈을 잊고 앱을 다시 불러오며, 동시 요청도 같은 client(같은 lifespan 상태·이벤트 루프)로 보냅니다.
- 로그는 앱의 로그 설정을 바꾸지 않고, 표준 logging handler가 필터·형식을 적용해 실제로 내보내는 글(루트로 올려 보내지 않는 로거, 앱이 시작·요청 중에 새로 만든 로거 포함)과 stdout·stderr를 모읍니다. 비밀번호를 필터로 가린 뒤 내보내면 노출이 아닙니다. 앱 코드의 info·debug 기록처럼 수준 때문에 지금은 나가지 않는 글도 수준만 바꾸면 나가므로 같은 필터·형식을 적용해 봅니다. 노출 검사는 앱을 정상 종료한 뒤 판정해 종료할 때 남기는 로그도 봅니다. 파일에 직접 쓴 글, 다른 로깅 라이브러리가 앱보다 먼저 잡아 둔 출력 통로, 종료 뒤 남는 스레드 출력은 범위 밖입니다.
- 연결·전제 조건을 확인하지 못하면(앱 import 실패, 경로 404·405, 정상 가입 실패 등) FAIL이 아니라 SKIP `UNVERIFIED:코드`이고, 결과 어댑터가 ERROR(미검증)로 옮깁니다. 실패 메시지에는 비밀번호·해시·salt 값을 넣지 않습니다. 비밀번호는 실행할 때 만듭니다.
- QA-SIGNUP-004는 길이와 문자 구성을 따로 봅니다. 계약(REQ-004)은 길이만 정하므로 8자 이상이면 소문자만·숫자만이어도 가입돼야 합니다(시험지마다 같은 구성 종류, 다른 길이·순서). SEC-SIGNUP-003은 점·+태그만 다른 두 주소(정책상 다른 주소)가 각각 가입되고 그대로 저장되는지도 봅니다. SEC-SIGNUP-005는 실패·거부 요청에서 새로 만든 해시처럼 미리 모르는 값도 찾도록 글 속 base64·16진수 후보를 한 번 풀어 해시 모양을 봅니다(후보 길이·개수 상한, 중첩 풀기 없음).
- 잘못된 입력은 4xx로 거부해야 하고, 거부된 요청은 사용자 표를 바꾸지 않아야 합니다(새 행도, 기존 계정의 값 변경도 없음. 5xx는 처리하지 못한 서버 오류). 처리하지 못한 예외는 실제 서버처럼 500으로 보고 traceback을 서버 로그에 남겨 노출 검사에 포함합니다.
- 필요 라이브러리: fastapi(TestClient)·httpx·sqlalchemy·argon2-cffi. 컨테이너 이미지에 있어야 합니다. 프로젝트 dev 그룹에도 들어 있어 `uv run python -m pytest tests/evaluation`이 실행 시험까지 돌립니다.

```python
from evaluation import protected_tests as pt
pt.owned_cases(scenario_contract, 'QA')                    # 순환 안(Ver1) QA 보호 케이스('SECURITY'면 Security)
pt.unit_test_scope('in-loop-security', suite_ref='artifact://...')  # dev UnitTestScope(PROTECTED, role SECURITY) 한 개
inputs = pt.evaluation_inputs(scenario_contract, 'final')   # 시험지(practice·pilot·final) 하나의 계획·꼬리표·suite 해시(검사 9개, 요구사항 7개)
results = pt.final_check_results(inputs, unit_report=report, unit_evidence_id=...)
```

`evaluation.check_adapters`는 unittest-v1 보고서와 정적 판정 결과를 `CheckResult`로 바꿉니다. 보고서가 없거나 형식이 틀리거나 대응표 밖의 줄(예: `setUpClass` 실패)이 섞이면 통과한 줄이 있어도 모든 검사를 미검증으로 둡니다.

## 최종 독립 채점기 (D5)

```python
from evaluation.final_evaluation import DockerRunner, evaluate_final_snapshot
output = evaluate_final_snapshot(
    archive=snapshot_tar_bytes,              # 최종 Snapshot의 dev canonical tar(dev snapshot_export로 꺼냄)
    expected=expected_manifest,              # 신뢰 경로로 받은 기준 Manifest
    scenario_contract=contract, source_run_id=run_id,
    runner=DockerRunner('repo@sha256:...'),  # 해시로 고정한 이미지만 받음
    exam='final',                            # 채점 시험지: 'pilot'(시범·조정용) 또는 'final'(공식 비교용)
    run_bundle=bundle, trace_documents=documents)   # 선택: Run 기록 추적·노출 검사
output['report']           # 독립 평가 보고서(실행 검사 9개, 요구사항 7개)
output['score']            # 요구사항별 같은 비중 점수·검사 완료율·확인된 보안 위반(scoring.exam_score)
output['referenceReport']  # 순환용 시험지 참고 채점(차이의 차이 계산용, 점수 제외)
output['supplementary']    # 보조 점검: 테스트가 지나가지 않은 줄(coverage), 정적 판정(static)
```

- Snapshot tar의 SHA-256이 기준과 다르면 실행하지 않고, `requirements.lock`의 해시와 실행 이미지 해시, 이미지 안에 보관한 lock 바이트의 해시를 대조했을 때만 결과를 판정에 씁니다. 확인할 수 없으면 Manifest 없음, 다르면 불일치로 UNVERIFIED입니다.
- 압축 파일은 일반 파일·상대 경로만 받습니다(../·절대 경로·링크·폴더 항목·압축 tar·중복·크기 초과 거부).
- `DockerRunner`는 dev 샌드박스와 같은 제한(네트워크 차단, 읽기 전용, 일반 사용자, 권한 제거, CPU·메모리·프로세스 제한)으로 dev의 `unit_runner.py`를 실행합니다. `LocalRunner(trusted=True)`는 합성 표본 시험용이며 이미지를 확인할 수 없어 결과가 판정에 쓰이지 않습니다. 생성 코드를 호스트에서 실행하지 않습니다.
- 정적 판정은 같은 Snapshot 소스로 `security.assess.evaluate()`를 부르는 보조 점검입니다(점수·판정에 넣지 않음). 소스를 읽기만 하므로 컨테이너 밖에서 돌며 생성 코드를 실행·import하지 않습니다. 줄 기록은 채점 시험지를 `coverage_runner.py`로 한 번 더 돌려(컨테이너 안) 생성물 코드 중 테스트가 지나가지 않은 줄을 셉니다.
- 2026-11-15 실제 Linux/arm64 Docker에서 정상·결함 합성 서비스를 채점하고 입력 재생·두 버전 회귀·격리·timeout 정리를 확인했습니다. [Docker 검증 기록](../../docs/evaluation/docker_verification_2026-11-15.md)을 참고합니다. 실제 LLM 생성물과 전체 Host 순환은 아직 실험 전입니다.
- 이미지 lock 관찰(`APPROVED_IMAGE_LOCK_BYTES`)은 이미지에 보관된 파일의 일치 확인입니다. 패키지 설치 상태는 이미지 준비 시 hash-locked 설치·`pip check`·버전 대조로 별도 확인합니다. 이미지에 lock이 없거나 다르면 본시험을 실행하지 않습니다.
- timeout 뒤 컨테이너 삭제를 확인하지 못하면 후속 시험을 중지하고 원인을 보존합니다. Python 파일 일부가 커버리지에서 누락되면 보조 지표는 `COVERAGE_INCOMPLETE`이며 제품 점수에는 섞지 않습니다.
- `evaluation.summary.render_summary(output['report'], scenario_contract=..., metrics=..., run_bundle=..., trace=output['trace'], runner=output['runner'])`로 회의·발표용 요약을 만듭니다. 에이전트가 쓴 자유 글은 넣지 않습니다. `trace`(runId)·`runner`(evaluationId)·`metrics`·`run_bundle`이 보고서와 다른 Run·평가의 것이면 ValueError로 멈춥니다. 판정 줄에는 그 판정이 보호 테스트·정적 판정 몇 개의 결과인지 적고, 바로 아래에 판정에 들어가지 않는 Run 기록 검사(노출·추적) 결과와 REQ-006 전체 출력의 노출 검증 상태(기록에서 노출 발견, 기록 검사 없음, 보지 못한 출력 있음이면 "충족하지 않음"·"미완료")를 따로 적습니다. Run 기록 노출이 PASS가 아니면 요구사항별 표의 REQ-006 줄에도 덧붙입니다.
- `known_values=[...]`로 이 Run에서 쓴 시험 비밀값을 넘기면 Run 기록 노출 검사가 자유 글에서도 찾습니다(결과에는 개수·위치만 남음). 노출 결과의 `coverage`에 읽은 기록과 보지 못한 출력(A2A Message 본문·Artifact 파일 내용·에이전트 프로세스 출력)을 적어, PASS가 REQ-006 전체 충족이 아니라 부분 검사임을 드러냅니다. A2A 본문 문서를 주면 본 것으로 세지만, 저장된 기록은 이미 가린 값이라 "가리기 전 송수신 본문"은 보지 못한 출력으로 남고, Run 기록상 일을 한 역할의 저장소가 비어 있으면(엉뚱한 저장소 연결) 미검증입니다. 비밀값이 JSON 이름(키)에 들어 있으면 위치에 `[SENSITIVE_KEY]`로 적습니다.
- 팀장 전달 파일(Ver1에만): `python -m evaluation.protected_tests export --out <폴더>`가 `owned-protected-cases.local.json`(QA·Security 보호 케이스, schemaVersion 2로 케이스마다 역할 명시), `unit-protected-scopes.json`(owned-tools의 unit.scopes에 추가할 두 범위), `handoff.json`(범위별 suite 지문·참조)을 만듭니다. Ver2는 셋 다 넣지 않습니다. 기본 suite 참조(`protected_tests.loop_suite_ref`)는 순환용 시험지 지문을 담아, 최종 때 참고 채점한 순환용 시험지가 개발 중에 준 것과 같은지 Run 설정만으로 대조할 수 있습니다. `--suite-ref`로 다른 참조를 주면 이 대조를 할 수 없어 `compare`가 비교를 무효로 표시하므로 실험에는 기본 참조를 씁니다.
- 보내는 보호 파일은 주석을 뺀 사본입니다(코드는 같음, 시험으로 AST 대조). dev는 보호 파일까지 담은 Unit 설정 전체를 64KiB로 받기 때문입니다(순환 안 두 범위 합 약 50KB, 실제 suite 참조 기준). 시험지 지문(suite SHA-256)은 실행 파일과 그 범위의 검사 정의(요구사항 연결 포함)를 함께 담아, 요구사항 연결만 바뀌어도 다른 시험지로 봅니다.

## Ver1·Ver2 실험 도구

```bash
python -m evaluation.experiment configure --tools configs/owned-tools.local.json --source configs/owned-source.local.json --out <폴더>
python -m agents.platform.snapshot_export --database <Host DB> --run-id <Run> --out <export 폴더>
python -m evaluation.experiment fetch --api http://127.0.0.1:8000 --run-id <Run> --out <폴더>   # bundle.json·trace.json. 받는 도중 Run이 바뀌면 다시 받음
python -m evaluation.experiment score --export <export 폴더> --exam final --image <repo@sha256:...> --results experiments/scn001/final/<날짜-이름> --bundle <bundle.json> --pair-id p1 --loop-policy IN_LOOP_AND_FINAL [--all-versions]
python -m evaluation.experiment record --bundle <bundle.json> --results <결과 폴더> --pair-id p1 --loop-policy FINAL_ONLY
python -m evaluation.experiment compare --results <결과 폴더> --planned IN_LOOP_AND_FINAL=5 --planned FINAL_ONLY=5
```

- `configure`: Host 도구 설정에 Security 자체 테스트 범위(`security-tests`, role SECURITY)를 두고, Ver1에는 순환용 보호 범위 두 개와 보호 케이스 파일을 더한 설정 쌍(`ver1/`·`ver2/`)을 만듭니다. `--source`를 주면 시작 코드 커밋을 dev와 같은 방식으로 꺼내 시험지·평가 코드가 섞였는지 먼저 확인하고, 섞였으면 만들지 않습니다. `dependencyLockFile`이 상대 경로면 원래 도구 설정 파일 위치 기준의 절대 경로로 바꿔 씁니다(Host는 설정 파일 위치 기준으로 읽으므로).
- `score`: 내보낸 최종 Snapshot을 D5로 채점해 `<결과 폴더>/<Run>/`에 `output.json`·`summary.md`·`meta.json`(시험지·쌍 번호)·`bundle.json`을 보관합니다. 채점할 Snapshot에 시험지가 섞였으면 실행·점수를 만들지 않습니다(`EXAM_EXPOSED`). 같은 Run의 결과가 이미 있으면 `--force` 없이는 채점을 시작하기 전에 멈춥니다(`OUTPUT_EXISTS`). 결과 한 세트를 임시 폴더에 다 만든 뒤 한꺼번에 바꿔 넣으므로, 다시 채점해도 예전 `regression.json`·`versions/`·`bundle.json`이 남지 않고, 채점 중 실패하면 예전 결과가 그대로 남습니다.
- `record`: 채점하지 못한 Run(빌드 실패·중단·Snapshot 없음 등)의 Run 기록(`bundle.json`)과 쌍 번호·버전을 결과 폴더에 남깁니다. 채점 파일이 없다고 Run을 빼면 실패 Run이 "시작 안 함"으로 바뀌고 이미 쓴 비용·토큰이 사라지기 때문입니다. 나중에 채점하면 그 폴더는 채점 결과로 바뀌고 쌍 번호·버전과 Run 기록(`--bundle`을 주지 않았을 때, `fetch`로 그 폴더에 받아 둔 것 포함)을 이어받습니다. 모양이 틀린 Run 기록은 `BUNDLE_INVALID`로 멈춥니다.
- 시나리오 정책이 보호 테스트가 전제로 하는 정책(`protected_tests.SUPPORTED_POLICY`: Argon2 19456/2/1, 이메일 앞뒤 공백 제거·소문자만)과 다르면 `configure`·`score`가 `CONTRACT_NOT_SUPPORTED`로 멈춥니다(채점 전).
- 결과를 바꾸다 실패하면 예전 결과를 원래 자리로 되돌립니다(`RESULT_REPLACE_FAILED`). 되돌리기도 실패하면 백업 폴더(`.<Run>.old-…`)가 남고 `RESULT_RESTORE_NEEDED`로 멈추며, `compare`도 그런 백업이 있으면 멈춥니다.
- `score`·`record`의 `--loop-policy`: 실험 계획상 버전(IN_LOOP_AND_FINAL=Ver1, FINAL_ONLY=Ver2)을 적어 두면 `compare`가 동결 설정(보호 테스트 참조 유무)과 대조해, 계획과 다르게 돈 Run이 다른 버전으로 집계되는 것을 막습니다. 적지 않으면 동결 설정으로 읽고 주의로 알립니다.
- 같은 품질 기준 대비 시간(10.29 회의록 방안 1의 지표): 요약의 `qualifiedVerdictMs`·`qualifiedRate`는 최종 독립 평가 PASS인 Run만 셉니다. `successVerdictMs`(Orchestrator가 SUCCESS라고 한 Run)에는 최종 평가에서 FAIL·미검증인 Run의 시간도 들어가므로, 속도 비교는 기준 달성 시간으로 합니다. 기준(최종 독립 평가 PASS)은 공식 비교 전에 고정합니다.
- `compare --group-by architecture`: 같은 방안(보호 테스트 조건이 같은 Run들) 안에서 Single·Multi를 비교합니다(회의록의 본래 비교, Single 실행기는 주호가 구현). `score`는 보고서의 구조를 Run 기록에 동결된 구조로 적고, `--architecture`로 다른 구조를 주면 채점 전에 멈춥니다(`ARCHITECTURE_MISMATCH`). 이때 차이의 차이는 계산하지 않고, 보호 테스트 조건이 다른 Run이 섞이면 비교가 무효입니다.
- `compare`: Run 기록이 있는 모든 Run(채점 여부와 상관없이)으로 Ver1·Ver2 비교 가능 여부, 정책별 점수·시간·토큰, 차이의 차이(보조 지표), Regression 합계를 `comparison.json`에 씁니다. 시범(pilot)과 공식(final) 채점을 섞으면 거부합니다. 읽을 때 보고서 스키마·Run, 최종·참고 보고서의 시험지 지문(지금 묶음의 final·pilot과 practice), `regression.json`의 Run·시험지·최종 버전을 다시 대조하고, Ver1 Run 설정의 참조가 지금 순환용 시험지 지문이 아니면 비교를 무효로 표시합니다. 차이의 차이는 두 시험지 모두 미검증 없이 끝난 Run으로만 확정 값을 내고(뺀 Run 수·사유, 짝 Run도 뺌), 미검증이 있는 Run까지 넣은 가능한 범위는 `practiceEffectRange`로 따로 적습니다. 범위도 같은 쌍 기준이라 짝 Run이 점수 없음·미채점이면 범위에서도 뺍니다(`rangeExcludedRuns`). 보관 결과끼리 맞지 않으면(예: Run의 최종 코드 버전이 아닌 버전을 채점한 보고서) `RESULTS_INCONSISTENT`로 멈춥니다. 성공률 분모는 시작·채점하지 못한 Run까지 포함한 계획 Run 수라 `--planned`로 줍니다. 주지 않으면 결과 폴더 수로 대신하고 `plannedRunsKnown: false`로 표시합니다.
- `--all-versions`: 중간 코드 버전도 같은 시험지로 채점해 `versions/v<N>.json`과 `regression.json`(수정으로 새로 생긴 실패 수)을 남깁니다. 내보내기도 `snapshot_export --all-versions`로 해야 합니다. 수정 없이 첫 버전에서 끝난 Run은 비교 쌍 0개로 정상 채점하고, 최종 버전이 2 이상인데 앞 버전이 없으면 `EXPORT_HAS_NO_EARLIER_VERSIONS`로 거부합니다. 버전별 결과는 개발 순환에 전달하지 않습니다.
- 결과 보관: 저장소의 `experiments/scn001/<시험지>/<날짜-이름>/`에 커밋합니다(에이전트는 시작 코드 커밋만 보므로 결과도 보지 못함). 내보낸 Snapshot tar는 `experiments/**/export/`에 두면 `.gitignore`로 빠집니다. 시험지 노출 확인(`exam_exposure`)은 결과 폴더의 결과 파일도 찾아, 시작 코드나 채점 Snapshot에 섞이면 거부합니다.

## Agent별 평가표 (11.2 회의)

```bash
python -m agents.platform.evaluation_export --database <Host DB> --run-id <Run> --out <export 폴더> --store PLANNER=<저장소> ...   # dev(주호)
python -m evaluation.agent_eval --export <export 폴더> [--report <D5 output.json>] --out <Run 결과 폴더>   # agent_tables.json·agent_tables.md
python -m evaluation.agent_eval --summarize <결과 폴더> ... --out <폴더>   # 여러 Run 모으기: agent_summary.json·agent_summary.md
```

11.2 텔로스 회의·자문의 "에이전트별 작업 정확성, 정보 전달 과정, 처리시간과 오류"를 한 Run마다 표 세 개로 만듭니다(`evaluation.agent_eval`). 기록을 읽기만 하고 판정·점수는 바꾸지 않습니다.

- ① 작업 정확성(에이전트별): Planner는 요구사항과 작업의 연결·의존관계 구조를 봅니다. 계획의 내용 타당성은 별도 검토가 필요해 자동 PASS로 만들지 않습니다. Developer는 버전별 빌드·최종 결과물·수정 해결, QA·Security는 같은 버전·산출물의 독립 평가와 판정 일치 여부 및 Issue 근거를 봅니다. 에이전트 FAIL·외부 PASS는 `agentFailExternalPass`로 기록하며 오탐 확정이 아닙니다. 같은 Snapshot 해시 또는 산출물 ID를 확인할 수 없거나 외부 검사·환경 확인이 없으면 미검증입니다. 이 일치 여부의 FAIL은 제품/에이전트 오류가 확정되었다는 뜻이 아닙니다.
- ② 정보 전달(단계별): 받은 입력(A2A 요청의 요구사항 ID·Artifact ID·코드 버전)이 규칙상 받아야 할 입력과 같은지(전달 완전성), 받은 요구사항을 모두 다루고 받은 Snapshot을 검사했는지(해석 정확성)를 봅니다.
- ③ 처리시간·오류(에이전트별): 작업 수, A2A Task 접수→끝 시간, LLM 호출·토큰·비용, 도구 호출·시간·재시도, 오류(단계 실패·A2A 실패·LLM 실패). 모르는 값은 0이 아니라 "미확인"입니다.
- 오류 발생 지점은 처음 전달·해석이 어긋난 단계(없으면 처음 에이전트 오류)이고, QA·Security가 찾은 제품 결함은 에이전트 오류가 아니라 따로 적습니다.
- 시나리오 정책은 Run이 시작할 때 고정한 정책(`bundle.configuration.scenarioContract`, 없으면 Run의 scenarioId로 등록 시나리오)을 씁니다. 그래서 전자문서 같은 다른 시나리오도 dev에 등록되면 그대로 됩니다. `--contract`로 준 정책이 Run의 정책과 요구사항이 다르면 멈춥니다.
- 평가 기준이 없는 새 phase(예: 검토 중인 팀장 보조 에이전트)는 다른 역할로 채점하지 않고 미검증(`PHASE_NOT_MAPPED`)으로 두되, 시간·오류 표에는 따로 남깁니다.
- `--summarize`는 Run마다 만든 `agent_tables.json`을 (시나리오, 구조)별로 모읍니다: 항목별 PASS·FAIL·미검증 Run 수, 역할별 전달·해석 결과 단계 수, 처리시간·LLM 사용량의 Run별 합의 중앙값(모르는 Run은 빼고 "알려진 k/n Run"으로), 오류 발생 지점 분포. 발표의 Single·Multi 비교(정확도·시간·오류 발생 과정)에 씁니다. 같은 Run이 두 번 들어오면 멈춥니다.
- Single은 에이전트가 하나라서 단계 역할(기획·구현·수정·QA·Security)을 행으로 같은 표를 만듭니다. dev 시험 하네스로 만든 Multi(QA 실패 → 수정 → 재검증)·Single Run 내보내기로 확인했고(`tests/evaluation/fixtures/agent_eval_runs.json`), 실제 LLM Run은 아직입니다.

## 통합 평가 실행과 준비 점검 (2026-11-11)

`examples/evaluation/experiment-plan.example.json`은 **미확정 값을 null로 둔 준비 양식**입니다. 승인된 시작 저장소·커밋·lock 경로, 고정 이미지 digest, 모델·호출/시간 한도, 구조별 계획 Run 수, 순환 테스트 조건, 순서·환경을 채운 뒤 점검합니다.

```bash
.venv/bin/python -m evaluation.preflight --plan /absolute/path/experiment-plan.json
```

2026-11-14부터 계획의 `hostConfigurations`(구조별 Host 실행 설정 파일 `runtime`·`tools`·`source` 경로, 계획 파일 폴더 기준 상대 경로 가능)도 대조합니다.
- 계획과 같은지 보는 것: 모델(공급자·모델·버전·temperature·seed), 호출·시간 한도, 도구 시간 한도와 `maxCallSeconds`, 이미지 digest, 하드웨어 프로필, 시작 저장소·커밋·lock 경로, 시작 커밋 안 lock 파일의 해시
- 두 구조 설정이 같은지: `evaluation.parity`와 같은 기준
- 호출 한도가 의미 검토 호출까지 센 최소 흐름(시험 하네스 기준 수정 없이 10회)에 못 미치면 미준비로 둡니다. 예시 설정의 8회가 여기에 걸립니다.
- 수정 1회 흐름(20회)에 못 미치면 막지는 않고 `warnings`로 알립니다. 이 하한은 하네스 기준이라 실제 Run은 도구 호출만큼 더 씁니다.

종료 코드 0은 준비 항목 통과, 2는 미확인 항목 존재, 1은 입력 오류입니다. 모델 호출·개발 실행은 하지 않습니다. 준비 통과도 Provider 인증·가격표·실제 생성물 연결의 검증을 대신하지 않습니다. 모델·한도 값은 Host 실행 설정에도 똑같이 적용해야 합니다.

시작 코드의 현재 공개 연결 조건은 `main.app`(FastAPI), `database.get_db`(SQLAlchemy 세션 의존성), `models.User`의 `email`·`password_hash`, `DATABASE_URL`로 시험 DB 선택, `POST /api/auth/signup`의 `{email, password}` 본문입니다. 이 인터페이스와 요구사항은 공개하되 정답 구현·보호 시험지는 시작 저장소에 넣지 않습니다. 다른 인터페이스를 선택하면 `signup_link.py`를 공식 실험 전에 맞추고 시험지 지문을 다시 동결해야 합니다. 서로 다른 생성 코드라도 같은 공개 요구사항을 만족하는지 평가합니다.

`docker/evaluation/Dockerfile`은 평가 이미지 준비 레시피입니다. 승인한 Linux Python base를 digest로 고정하고 `docker/evaluation/prepared/requirements.lock`(모든 의존성 버전·해시 포함)과 `prepared/wheelhouse/*.whl`을 준비해야 합니다. lock에는 FastAPI·httpx·SQLAlchemy·argon2-cffi와 전이 의존성이 필요하며 생성물의 lock과 같아야 합니다. 이 폴더만 build context로 사용하고, 완성 이미지의 digest를 Host 환경 설정과 평가 실행에 동일하게 등록합니다. 현재 실제 이미지를 빌드·검증한 상태는 아닙니다.

Host의 `evaluation_export`는 기본으로 모든 코드 버전을 내보냅니다(`--final-only`를 사용하지 않음). Multi는 네 에이전트 저장소를, Single은 `--store SINGLE_AGENT=...`를 넘깁니다. 이후 한 명령으로 최종/이전 버전 채점과 Agent별 평가표를 만듭니다.

```bash
.venv/bin/python -m evaluation.workflow --export /absolute/path/host-export --results experiments/scn001/pilot/session-name --image 'repository@sha256:IMAGE_DIGEST' --exam pilot --pair-id pair-01
.venv/bin/python -m evaluation.experiment compare --results experiments/scn001/pilot/session-name --group-by architecture --planned SINGLE_AGENT=3 --planned MULTI_AGENT=3
```

위의 경로·digest·Run 수는 사용 예시이며 실험 결정값이 아닙니다. 공식 실험은 별도 폴더와 `--exam final`을 씁니다. `workflow`는 `handoff.json`의 파일 크기·해시, Run·구조·버전 일치를 먼저 확인합니다. 이는 인계 파일의 일관성 확인이며, 서명 인증을 의미하지는 않습니다. v1부터 최종 버전까지 빠진 코드가 있으면 채점 전에 거부합니다. 최종 코드가 없는 종료 Run도 기록·비용·미검증 사유는 남깁니다. 출력은 `output.json`, `summary.md`, `agent_tables.json/md`, `versions/`, `regression.json`, `evaluation_status.json`입니다. 비교는 `comparison.json`과 `comparison.md`를 만듭니다.

`HUMAN_REVIEW`는 명세대로 그 시점의 자동 실행 구간을 보존하고 채점합니다. `automaticSegmentClosed: true`로 남기며, 재개 후 변경된 기록으로 이 결과를 바꾸지 않습니다. 첫 인계가 이미 재개 후라면 원본의 `WORKFLOW_RESUMED` 사건을 확인해 `QUARANTINED_ASSISTED`로 보관하고 최종 시험을 실행하지 않습니다. 자동 구간의 결과를 복원할 근거가 없으므로 정확도·시간·비용을 0이나 자동 성공으로 처리하지 않습니다. 해당 슬롯은 계획·실행 수에 유지하고 사람 보조 기록과 사용량은 별도로 표시합니다. 비교 집계도 원본 재개 사건을 다시 확인하므로 과거에 자동 성공으로 저장된 재개 Run이 섞이지 않습니다. 진행 중인 Run은 받지 않습니다. 생성 코드를 Host에서 실행하는 우회 경로는 제공하지 않습니다.

추가 노출 검사는 `workflow`와 `experiment score`의 다음 인자를 사용합니다.

- `--secrets-file /absolute/path/canaries.json`: 시험용 비밀번호·해시 등 **알려진 시험값의 JSON 문자열 배열**. 실제 Provider API 키를 시험용 값으로 사용하지 않습니다. 명령줄과 결과 보고서에 값을 복사하지 않습니다.
- `--output-root /absolute/path/outputs --output-manifest /absolute/path/output-list.json`: 목록의 `{runId, files:[{kind, path}]}`를 검사합니다. `kind`는 `ARTIFACT_FILE_CONTENT` 또는 `AGENT_PROCESS_OUTPUT`, `path`는 root 아래 상대 경로입니다. 상위 경로·링크·특수 파일·과대 파일을 거부하고, 못 읽은 파일은 미검증으로 남깁니다.

추가 출력은 읽은 파일의 해시·크기와 수집 상태만 결과에 남깁니다. 원문은 저장하지 않습니다. `DECLARED_FILES_ONLY` 범위이므로 목록에 없는 출력이나 마스킹 전 A2A 본문까지 검사했다고 주장하지 않습니다. 수집한 원문과 Host 원본 인계 폴더의 보관 정책은 별도로 관리해야 합니다.

입력·출력 토큰은 전체 호출 영수증을 페이지 끝까지 받아 중복·누락·변경 및 총합 일치를 확인한 경우에만 확정합니다. 빠진 값은 `null`, 확인한 부분합은 `knownInputTokens`·`knownOutputTokens`입니다. 캐시 입력·추론 토큰을 입력/출력에 또 더하지 않습니다. 비교표의 통과 1회당 비용에는 실패·중단 비용도 포함하며, 통과 0회 또는 비용 불완전이면 미확인입니다.

## 최신 dev 대응과 남은 지표 (2026-11-13, dev `ea06d12`)

최신 dev는 에이전트 결과마다 같은 모델·같은 Run 예산으로 의미 검토 호출을 하나씩 붙인다. 검토 보류나 Planner 질문이면 Run이 입력 대기(`WAITING_INPUT`)로, 개발·검증 단계 보류나 모델 호출 오류면 사람 검토(`HUMAN_REVIEW`)로 멈춘다. 평가 쪽은 다음처럼 맞췄다.

- `workflow`는 `WAITING_INPUT`도 자동 구간이 닫힌 끝 상태로 받는다. 코드가 만들어지기 전에 멈춘 Run은 dev 내보내기에서 `finalCodeVersion`이 `null`이고 내보낸 버전이 없다. 이 경우를 형식 오류로 거부하던 것을 고쳤다(실제 dev 하네스 기록으로 재현·회귀 시험).
- Run마다 `outcome.json`을 남긴다. 담는 것은 끝 상태 원인(`cause`), 비교 유효성(`comparisonValidity`), 재개 방식(`resume`), 의미 검토 사용량, 자동 완료 판정이다. `evaluation_status.json`에도 원인 요약을 적는다.
- 원인은 근거가 직접적인 순서로 읽는다: 에이전트 상태 메시지 code → 의미 검토 기록 → 호출 영수증 → Host 사건. 근거가 없으면 `UNKNOWN_HOLD`·`DISPATCH_REVIEW`로 두고 추측하지 않는다.
- 비교 유효성은 개발정의서 11-A를 따른다. 모델 API 장애·인증·설정 문제는 `UNVERIFIED`(구조와 무관한 환경 문제)로 표시한다. 호출 시간 초과·도구 실행 실패는 원인을 하나로 정할 수 없어 `REVIEW_REQUIRED`로 둔다(팀 판단 전).
- 의미 검토 호출은 `AGENT_SEMANTIC_REVIEWED` 기록의 호출 번호로 작업 호출과 나눈다. 검토가 가리킨 영수증이 하나라도 빠지면 합계를 확정하지 않는다. Agent별 평가표 ③에도 역할별 "그중 의미 검토 호출"을 적는다.
- 자동 완료(시험지 v4)는 다섯 조건이 모두 확인될 때만 PASS다: 제품 시험 통과, 내부 SUCCESS, 시간 한도 안 종료, 결과 패키지(요구사항·최종 코드·변경·빌드·QA·Security 보고서와 전체 기록), 입력 대기·사람 검토·재개 없음. 11.15에 Host 최종 보고서·패키지 완료 사건 자동 생성을 연결했다. 실제 완료 증거가 없는 구형 Run이나 저장에 실패한 Run은 패키지 조건을 미확인으로 남기며 자동 완료 PASS를 부여하지 않는다.
- Agent별 평가표 ②에 "수신 기록 대조"를 더했다. 에이전트 저장소의 첫 메시지 접수 기록이 Task 입력 metadata·Context와 같은지 본다. 기록이 없으면 FAIL이 아니라 미검증이고, 접수 기록은 내용 이해나 제품 성공의 증거가 아니다.
- `experiment compare` 요약에 새 칸을 더했다.
  - `accuracy`: ACC-01 요구사항 완전 충족률 하한·상한과 분포(STAB-03), ACC-04 판정 가능률, QA 통과율, 보안 차단 결함 수(확인된 보안 필수 검사 실패 수)
  - `stability`: 중단·멈춤·미시작·외부 미검증 비율(STAB-02), 전체·제품 통과 Run 시간 분포(STAB-04)
  - `outcomes`·`semanticReview`·`autoCompletion`(STAB-06 = 자동 완료 통과 Run 수 / 계획 Run 수)
  - 구조 비교에서는 `pairComparison`(STAB-05): 같은 `pairId`의 Single·Multi 쌍별 Multi − Single 차이. 양쪽 값을 모두 확인한 지표만 계산하고, 쌍이 깨졌거나 조건이 다르면 제외 사유를 남긴다.
- 도구·LLM 한도 동일 확인: 아래 명령의 결과를 `<results>/parity.json`으로 두면 `compare --group-by architecture`가 FAIL을 비교 무효로, 미확인·미실행을 주의로 표시한다.

```bash
.venv/bin/python -m evaluation.parity \
  --single runtime=/abs/single-runtime.json,tools=/abs/owned-tools.json,source=/abs/owned-source.json \
  --multi runtime=/abs/owned-runtime.json,tools=/abs/owned-tools.json,source=/abs/owned-source.json \
  --runs /abs/runs.json --out experiments/scn001/pilot/session-name/parity.json
```

`--runs`는 `[{"runId", "architecture", "hostDatabase"}]` 목록이다(선택). 주면 Host DB의 `run_host_policies`를 읽기 전용으로 열어, Run마다 고정된 설정 해시가 그 구조의 설정 파일로 dev `inspect_host_configuration`이 다시 만든 해시와 같은지 본다. 이 해시는 LLM 한도까지 포함한다. `--runs` 없이 돌리면 파일 비교만 한 것이며 각 Run이 그 파일로 돌았다는 증명은 아니다. Single 구현·수정 단계가 보고서 읽기 도구 두 개를 더 갖는 것은 합집합 규칙의 위반이 아니라서 참고(`phaseDifferences`)로만 남긴다.

`tests/evaluation/test_dev_compatibility.py`는 고정 기록 대신 그때의 dev 시험 하네스로 Run 세 개를 새로 만들어 평가 코드에 넣는다. dev 출력이 바뀌면 여기서 먼저 드러난다. 하네스를 만들지 못하면 이유를 남기고 건너뛴다.

## 결과 지표와 시연 자료 (D4)

`evaluation.run_metrics`: dev Run 조회 JSON(`/runs/{id}`, `/events`, `/issues`, `/tool-attempts`, `/usage`, `/configuration`)을
지표로 바꿉니다. `fetch_run_bundle(get, run_id)`이 페이지를 이어 받고, `run_metrics(bundle)`은 한 Run의 수정 횟수·도구 재시도·시간
(사건 시각 차이)·토큰·비용을, `experiment_summary(runs, planned_runs, evaluations)`는 구조별 성공률·Verified Rate·비용 합계를,
`comparison_validity(entries)`는 두 구조를 비교해도 되는지(모델·한도·시작 코드·보호 테스트·suite 해시·환경 digest)를 계산합니다.
측정하지 못한 값은 0이 아니라 `None`이고, 성공률 분모는 시작하지 못한 Run을 포함한 계획 Run 수입니다.
모델 호출 수는 대상 Run이 모두 측정됐을 때만 `modelCalls`에 합계를 쓰고, 부분 합은 `knownModelCalls`, 완전성은 `modelCallsComplete`로 구분합니다. 놓친 결함 수는 연결된 Run의 SUCCESS와 독립 보고서의 확인된 필수 실패로 계산하므로 선택 항목 `sourceRun` 생략에 영향받지 않습니다.
API 페이지 요청에 실패하면 수집을 중단합니다. 빈 페이지 등으로 선언된 사건 수와 수집 수가 다르면 `EVENTS_INCOMPLETE`, 전체 사건 수가 없으면 `EVENTS_COMPLETENESS_UNKNOWN`이며, 두 경우 모두 사건 기반 시간은 `None`입니다. 끝 사건이 시작보다 빠르게 기록되면 그 시간은 0으로 보정하지 않고 `None`이며 `EVENT_TIME_REVERSED`를 남깁니다(협업 지표의 `wallToVerdictMs`도 같음). 시간 계산은 `RUN_STARTED`부터라 Run 생성 시각을 쓰는 `sourceRun.durationMs`와 시작점이 다릅니다.
`regression_count({코드 버전: {검사 번호: 상태}})`는 앞 버전 PASS → 다음 버전 FAIL을 Regression으로 세고, 비교할 수 없는 검사가 있으면 `complete=False`로 알립니다. 이는 관찰한 결과 변화이며 수정이 원인인지 확정하는 판정은 아닙니다. `regression_from_reports(버전별 독립 평가 보고서들)`는 같은 Run·같은 보호 묶음인지 확인한 뒤 계산합니다. 요약에 넣을 때는 최종 코드 버전까지 대조합니다(`experiment score --all-versions`가 이 함수로 `regression.json`을 씁니다. 실제 Run으로는 아직 돌리지 않았습니다).
`collaboration_metrics(events)`의 Step 구간은 시도·재개마다 끊습니다: 일을 넘기기 시작 ~ 끝 상태 또는 입력·인증 대기. 첫 응답이 이미 끝 상태여도 그 시각에 닫고, 사람을 기다린 시간은 에이전트 시간에 넣지 않습니다.

`evaluation.trace_checks`: `fetch_trace_documents(get, run_id)`로 events·telemetry(페이지 전부)·issues·artifacts·tool-attempts를 받아
`check_trace_exposure`가 비밀번호 필드 값(정적 판정과 같은 단어 단위 이름 규칙)·실제 해시 값 모양·알려 준 값을 찾고,
`check_traceability`가 요구사항 → 코드 버전별 Snapshot → QA·Security 검증 → Issue → 수정 요청·완료 → 재검증 → 판정 사건이 이어지는지 봅니다.
결과에는 위치와 근거 코드만 남깁니다. 추적 규칙은 dev 파이프라인 테스트가 만든 실제 Run 6개(SUCCESS·FAIL·HUMAN_REVIEW·빌드 실패 경로)로 확인했습니다.
A2A Message 본문은 Run 조회 API로 볼 수 없어, dev `python -m agents.platform.a2a_export --run-id <Run> --out a2a.json --store QA=<QA 작업 저장소> ...`로 꺼내 `experiment score --trace trace.json --a2a a2a.json`에 넘기면 노출 검사가 본문까지 찾습니다. 네 역할 중 빠진 저장소가 있으면 미검증입니다.
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
- [dev 변경 공유](../../docs/evaluation/dev_changes_2026-10-30.md): 10.29 회의록의 석민 담당(테스트에 필요한 dev 수정)으로 4번이 고친 dev 코드 정리(팀장 전달용)
- [Single 비교 기준선](../../docs/evaluation/single_baseline.md)(Single 실행기 구현됨, 실제 비교 Run 전), [시연 자료 정리](../../docs/evaluation/demo_materials.md): 팀 논의·전달용

공통 스키마 사본은 원본을 대체하지 않습니다. dev 계약이 바뀌면 사본과 호환 검사를 함께 갱신합니다.
`seokmin` 브랜치에서 개발하며 환경·DB·작업공간·비밀값·빌드 산출물은 Git에서 제외합니다.

## 2026-11-13 추가 구현: 판정·시간·과정·사람 검토·재채점

공통 시작 코드와 모델·예산의 실제 값 없이 구현한 평가 기능입니다. Host나 Agent 실행 정책은 바꾸지 않습니다.

- `outcomes.py`: Host 예산 장부·영수증을 대조해 LLM/시간 한도 준수를 판정합니다. 장부 누락·불일치는 미검증입니다. 별도 최종 보고서·패키지 완료 증거가 없으면 자동 완료 PASS를 만들지 않으며 제품 시험 통과와 구분합니다.
- `experiment.py` / `comparison_report.py`: 구조 비교의 `readiness`는 도구·한도 동일성 PASS 및 **비교 대상 모든 Run의 고정 설정 해시 대조**를 요구합니다. 설정 파일만 비교했거나 미검사이면 공식 비교를 보류합니다. `validity`는 기존 조건 불일치 진단, `readiness`는 공식 비교 준비 여부입니다.
- `evidence_metrics.py`: TIME-05는 Run 종료 후 버전별 보고서와 `SNAPSHOT_FROZEN` 사건을 연결해 계산합니다. 동일 Run·시험지·환경, 코드 버전·Source Artifact·Snapshot 해시와 시각을 확인합니다. 앞 버전이 미검증이라 통과 가능성을 배제할 수 없거나 기록이 빠지면 최초 통과 시간을 만들지 않습니다. `timing_metrics.json`에 저장하고 비교 때 원본 기록으로 재계산합니다.
- `planner_review.py`: 독립 사람 검토를 Run ID·계획 Artifact ID·전체 계획 내용 해시에 연결합니다. 검토자·시간·항목별 근거가 필요하며 다른 계획에 재사용하면 거부합니다. 아직 계획 Artifact가 여러 개인 Run은 자동으로 하나를 고르지 않습니다.
- `repeatability.py`: 같은 Run·Snapshot·시험지·환경의 독립 채점 결과를 비교합니다. `replay.py`는 별도로 실제 비공개 입력을 재생합니다. 두 번 모두 미검증인 결과를 재현성 PASS로 처리하지 않습니다. 실행 방법과 한계는 아래 '실제 입력 저장·재생'을 따릅니다.

### Planner 검토

```bash
python -m evaluation.planner_review --export <인계폴더> --out <검토양식.json>
python -m evaluation.workflow --export <인계폴더> --results <평가결과폴더> \
  --image <승인한이미지@sha256:...> --exam pilot --planner-review <작성한검토양식.json>
```

양식은 처음에 reviewer/reviewedAt가 비어 있고 세 항목이 미검증입니다. 검토자가 직접 다음을 기록합니다.

| 항목 | 기록할 근거 |
|---|---|
| requirementPreservation | 승인된 조건이 보존됐는지, 빠지거나 임의로 추가한 조건이 있는지 |
| taskSpecificity | 구현·검증 가능한 구체적인 작업으로 나눴는지 |
| dependencyFeasibility | 작업 순서와 의존관계가 실행 가능한지 |

각 항목에 PASS/FAIL/UNVERIFIED와 비어 있지 않은 evidence를 넣습니다. 이 입력 체크리스트는 팀의 기획 채점 정책 승인을 대신하지 않습니다. 자동 제품 점수에 가산하지 않으며 Agent 작업 평가표의 별도 칸에 표시합니다. 기존 결과를 보관한 채 검토표만 새로 만들려면 `evaluation.agent_eval --export ... --planner-review ... --out <별도폴더>`를 사용합니다.

### 추가 과정 기록 입력 계약 v1

`workflow --process-evidence <JSON>`으로 명시한 추가 기록을 받습니다. 생략하면 `host_process.py`가 Host 원본 사건·원격 측정·보고서에서 증명 가능한 지표를 자동 추출합니다. 별도 JSON은 수집자의 완전성 선언이며 암호학적 인증이 아닙니다. 명시한 JSON은 자동 추출을 대체하므로 별도 수집 자료와 원본을 함께 검토해야 합니다.

```json
{
  "schemaVersion": 1,
  "runId": "대상 Run UUID",
  "completeKinds": ["REPAIR", "RETRY", "GATE_REJECTION", "CONFLICT"],
  "events": []
}
```

`completeKinds`에는 해당 Run에서 빠짐없이 수집한 종류만 넣습니다. 이 선언과 빈 events를 함께 제공하면 해당 종류 0건을 주장하는 것이므로 실제 무발생을 확인한 경우에만 사용합니다. 명시한 종류가 빠지면 그 종류를 0건으로 처리하지 않습니다. 부분 수집은 `completeRoutes`(수정 경로), `completeChannels`(재시도 통로)로 선언할 수 있습니다.

모든 사건에는 eventId·runId·kind가 필요합니다. 같은 eventId의 내용이 바뀌면 거부하며 재전송은 중복 집계하지 않습니다.

| kind | 추가 필드와 규칙 |
|---|---|
| REPAIR | operationId, roundId, route(IMPLEMENTATION/REPLAN/TEST_CHANGE), action(STARTED/COMPLETED). 재계획과 후속 구현은 같은 roundId. 완료만 있고 시작이 없으면 미검증 |
| RETRY | operationId, channel(TOOL/A2A/LLM), attempt(1부터 첫 재시도). 최초 호출은 여기에 넣지 않음 |
| GATE_REJECTION | stage, operationId, rejectionId. 같은 거부의 재전송은 같은 rejectionId |
| CONFLICT | qaReportId, securityReportId, reasons 배열(QA_PASS_SECURITY_BLOCK/VERSION_MISMATCH/JUDGEMENT_CONTRADICTION). 같은 보고서 쌍은 한 건, 판정 모순은 그중 별도 계수 |

입력과 계산 결과를 `process_evidence.json`, `process_metrics.json`에 보존합니다. 비교표의 확정 합계는 계획된 모든 Run의 해당 종류가 완전할 때만 표시하며, JSON에는 확인된 부분합과 미측정 Run 수를 함께 둡니다. 별도 JSON의 충돌은 수집자의 기록을 집계합니다. 자동 추출은 같은 검증 시도의 QA/Security 보고서에서 버전·Manifest 불일치 및 QA 전체 PASS/Security 차단 조합을 찾습니다. 같은 시험 입력에 대한 판정 모순까지 증명하지 않으므로 `contradictions`는 미검증입니다.

### 같은 생성물 재채점 비교

같은 인계 폴더를 서로 다른 results 폴더에 두 번 채점한 후 다음을 실행합니다. 개발 Run이나 모델 호출을 다시 수행하는 명령이 아닙니다.

```bash
python -m evaluation.repeatability \
  --first <첫결과폴더/RunID/output.json> --second <두번째결과폴더/RunID/output.json> \
  --out <별도폴더/repeatability.json>
```

원본과 기존 비교 파일을 덮어쓰지 않습니다. 같은 evaluationId를 복사한 것은 두 번째 실행으로 인정하지 않습니다. 결과 PASS는 관찰된 판정 일치, FAIL은 확인 가능한 판정 변화, UNVERIFIED는 조건 불일치·미검증을 뜻합니다. 합성 서비스의 실제 Docker 반복 채점은 2026-11-15 확인했습니다. 실제 LLM 생성물의 반복 측정은 별도 실험으로 남아 있습니다.


### Host 증거 자동 연결 (2026-11-13)

- `bundle.json.budgetEvidence`: Host 읽기 트랜잭션에서 예산 장부를 함께 꺼냅니다. 평가기는 Run·고정 설정 지문·생성/종료/마감 시각, 모델 호출별 사용량 영수증, 미정산 호출, 호출·출력·전체 토큰 한도를 대조합니다. 금액 상한 준수를 증명하는 기능은 아니며 설정되지 않은 상한을 임의로 만들지 않습니다.
- `process_metrics.json`: 자동 추출 시 `coverage=HOST_RECORDS`. 구현 수정·MCP 재시도·보고서 쌍 충돌과 중앙 관문의 거부 사건, 고정 Snapshot 및 실제 QA 도구 입력의 테스트 변경을 집계합니다. 변경을 실제 Host 수정 시작·검증 주기의 step/버전/Source에 연결할 수 있을 때만 같은 round로 확정하며, 독립 테스트 수정의 시작/완료·roundId 기록이 없으면 관찰 진단과 미검증으로 남깁니다. A2A 재개 attempt는 수정 차수가 아닙니다. 실행 전 고정한 정책과 전체 자동 구간 관찰이 있을 때만 비활성 A2A/LLM 자동 재시도·미지원 재계획을 0건으로 확정합니다. 정책/기록이 빠진 구형 Run이나 부분 구간은 미검증입니다. polling과 다음 정상 모델 호출은 재시도가 아닙니다.
- `MODEL_INPUT_OBSERVED`: Multi의 Planner/Developer/QA/Security 및 Single의 실제 업무 모델 호출 직전에 해시만 저장합니다. metadata, 업무 입력, 최초 user 입력, System, 현재 입력 이력의 해시를 기록하며 원문·도구 출력·추론 텍스트는 남기지 않습니다. 의미 검토 호출은 별도 목적이므로 이 대조에서 제외합니다. 기록 실패 시 공급자를 호출하지 않고 예산을 무효화합니다.
- Agent 표의 `modelInput`: 같은 Run·단계·Task·Context·주체 및 모델 호출 번호에 연결된 해시를 A2A Task metadata와 대조합니다. 원본 기록이 없으면 미검증입니다. PASS는 공급자 호출 경계까지 전달된 metadata의 일치이며, 모델의 이해·모든 소스 파일 읽기·출력 정확도를 보증하지 않습니다. 업무 입력 해시는 후속 원본 대조를 위한 참조이며 A2A payload 전체 동일성을 단정하지 않습니다.

### TIME-02 패키지 완료 기록 계약 v1

2026-11-15: Host의 `HostPackageService`가 실제 최종 보고서 바이트를 저장한 뒤 완료 시각을 읽고 `RESULT_PACKAGE_COMPLETED` 사건·완료 증거와 함께 원자적으로 확정합니다. 동일 Run 재처리·재시작 복구는 기존 완료 시각과 보고서를 유지합니다. 필수 결과물 누락·입력 대기·사람 보류·보조 재개는 자동 완료로 만들지 않습니다. FAIL/UNVERIFIED/ABORTED도 자료가 충분하면 패키지를 보관할 수 있지만 자동 성공과 구분합니다. 내보내기 시각으로 과거 완료 사건을 만들지 않습니다.

Host의 자동 보고서·결과물 저장 단계가 실제로 끝난 뒤 `RESULT_PACKAGE_COMPLETED` 사건을 기록합니다. 사건의 actor는 `Orchestrator`이며 runId, codeVersion, snapshotSha256, occurredAt, eventId를 포함합니다. 자동 패키지 참여는 실행 전에 고정하며 재시작 복구도 참여 기록이 있는 Run만 대상으로 합니다. 새 서비스를 설치했다고 구형 종료 Run의 완료 사건을 소급 생성하지 않습니다. 사람이 재개한 Run이나 운영자가 뒤늦게 만든 패키지는 자동 구간 시간으로 인정하지 않습니다.

기본 내보내기는 Host DB에 저장한 보고서·완료 기록을 자동 첨부합니다. `GET /api/v1/runs/{runId}/package-production`으로 저장 상태를 읽습니다. 측정 실행기는 종료 직후 `PENDING`이면 최대 30초 기다리고, 시간 초과 시 같은 Run의 인계를 재시도할 수 있도록 실패 사유를 보존합니다. `NOT_COMPLETED/ERROR`는 완료로 바꾸지 않습니다. 이전 별도 파일 방식도 다음과 같이 사용할 수 있으나 DB의 미완료 상태를 우회할 수 없습니다.

```bash
python -m agents.platform.evaluation_export --database <HostDB> --run-id <RunID> \
  --store PLANNER=<PlannerDB> --store DEVELOPER=<DeveloperDB> \
  --store QA=<QADB> --store SECURITY=<SecurityDB> \
  --package-records <완료기록폴더> --out <새인계폴더>
```

Single은 실제 `--store SINGLE_AGENT=<SingleDB>` 하나를 사용합니다. 기존처럼 `--package-records`를 생략할 수 있으며 이 경우 TIME-02는 미측정입니다.

`final-report.json`은 `artifactType=FINAL_REPORT`, `runId`, `codeVersion`, `snapshotSha256`, `verdict`와 보고서 본문을 포함합니다. 이 검사는 보고서 존재·대상 일치 검사이며 본문의 정확도 채점은 아닙니다.

`package-completion.json` 필드:

| 필드 | 값 |
|---|---|
| schemaVersion / producer | `1` / `HOST_AUTOMATIC` |
| runId / codeVersion / snapshotSha256 | 최종 Run·Snapshot 정체성 |
| eventId / completedAt | 실제 Host 완료 사건 ID·시각(시간대 포함) |
| finalReportPath | `final-report.json` |
| files | `final-report.json`, `vN/source.tar`, `vN/manifest.json` 각각의 `{sha256, sizeBytes}` |

수집기는 DB의 완료 사건과 기존 Snapshot 파일을 대조한 후 두 파일을 인계 목록에 포함합니다. 평가기는 인계 전체 해시, 최종 Source/Manifest/보고서 일치, 시작→판정 종료→패키지 완료 시각 순서를 다시 검사합니다. 심볼릭 링크·누락·변경 파일은 거부합니다. 완료 시점이 시간 한도를 넘으면 자동 완료의 한도 조건은 FAIL입니다. `outcome.json.packageTiming`, 비교표의 중앙값·표본 수·미검증 수, Single/Multi 짝별 시간 차이에 연결됩니다. 해시는 변경 탐지이며 작성자 인증이나 시계 정확성 증명은 아닙니다.

최종 보고서 본문도 지정 산출물 노출 검사에 포함합니다. 해시를 대조한 JSON을 그대로 검사하고 검사 직전에 파일을 다시 읽지 않습니다. 평가 결과 폴더에는 완료 기록·Manifest·보고서 해시 참조만 추가하며 본문 원문을 재복사하지 않습니다.

### 실제 입력 저장·재생 (2026-11-15)

일반 `experiment score`와 `workflow`는 시험이 만든 실제 이메일·비밀번호·요청 본문·요청 순서를 평가 담당 전용 파일로 저장합니다. seed만 맞추는 방식이 아닙니다. 공개 결과는 입력 파일의 해시·개수·상태만 가리킵니다. 원본 값은 Agent 메시지·시작 코드·공개 결과에 넣지 않습니다.

- 입력: `<results>/.private-inputs/<RunID>/<evaluationId>.json` (파일 0600, Run 폴더 0700). 입력과 평가 이력 폴더는 Git에서 제외하며 시작 Snapshot에 섞이면 평가를 거부합니다.
- 원본 결과·계약: `<results>/.evaluation-history/<RunID>/<evaluationId>/{output.json,contract.json}`. `--force`로 현재 결과를 교체해도 이전 입력과 재현 기준을 보존합니다.
- 현재 최종 결과: `<results>/<RunID>/output.json`. `--all-versions`의 앞 버전 전체 결과는 `version_outputs/vN.json`, 기존 보고서 전용 경로 `versions/vN.json`도 유지합니다.
- 입력 일부 누락·실행기 오류는 `inputRecord.status=UNVERIFIED`이며 완전 재현으로 인정하지 않습니다. 직접 Python API를 쓰면 `private_input_dir`를 명시해야 합니다.

고정된 실행 이미지가 준비된 환경의 재생 명령입니다. 2026-11-15에는 같은 재생 API를 실제 Docker 통합 검사에서 실행해 정상 재현과 두 버전 각 2회 회귀를 확인했습니다. 아래 경로는 실제 평가 자료에 맞춰 지정합니다.

```bash
python -m evaluation.replay \
  --baseline <history/evaluationId/output.json> \
  --inputs <private-inputs/RunID/evaluationId.json> \
  --snapshot <export/vN/source.tar> --contract <history/evaluationId/contract.json> \
  --image <고정된-image@sha256:digest> --out <새폴더/replay.json>
```

회귀 확인은 여기에 `--candidate-snapshot <vNext/source.tar> --candidate-manifest <vNext/manifest.json> --repetitions 2`를 추가합니다. 같은 입력으로 이전·수정 버전을 각각 최소 두 번 실행합니다. 원본 결과/입력 해시, Run, 시험 지문, 계약, 이미지·lock, 실행기와 자원 조건이 맞아야 합니다. 같은 Snapshot의 재생은 Snapshot도 일치해야 합니다. 확인된 실패로 시험이 조기 종료되면 `MATCHED_FAILURE_PREFIX`로 구분하며 전체 입력 소비라고 표시하지 않습니다. 판정이나 소비 구간이 반복 중 달라지거나 미검증이면 회귀를 확정하지 않습니다. 동시 요청의 스케줄과 해시 salt는 고정하지 않으므로 유한 반복으로 모든 비결정성을 배제할 수는 없습니다.

### 실행 중 출력 관찰 범위

Host·Agent의 Run에 연결된 Python 표준출력·오류·로그, 모델의 공개 출력·도구 인자, A2A 저장 전 출력과 도구 stdout/stderr를 해당 경계에서 검사합니다. 원문을 새 로그 저장소에 쌓지 않고 해시·바이트 수·발견 유형을 남깁니다. 숨겨진 모델 추론은 수집하지 않습니다. 범위 시작/종료 사건이 짝을 이루지 않거나 출력이 잘리면 미검증/부분 수집입니다.

등록 Snapshot과 실제 저장된 QA 테스트 입력 파일은 바이트를 자동으로 인계해 알려진 값 대조에도 사용합니다. 반면 런타임 영수증에는 원문이 없으므로 나중에 제공한 임의 비밀값을 대조할 수 없습니다. 그런 요청은 미검증으로 남기며, 발견된 실제 노출은 FAIL이 우선합니다. 전체 OS 출력·직접 파일 디스크립터 쓰기·범위 밖 스레드·중단되어 회수 못한 도구 출력을 관찰했다고 주장하지 않습니다. 범위 내 패턴 검사 PASS는 모든 경로의 무노출 증명이 아닙니다.

### Docker 없이 준비 파일 검사

```bash
python -m evaluation.preflight --no-docker \
  --plan examples/evaluation/scn001-pilot.ver1.plan.json --out /tmp/scn001-preflight-offline.json
```

`--no-docker`는 daemon·이미지 조회·컨테이너 실행을 모두 생략합니다. 해당 검사들은 `DOCKER_CHECK_DISABLED` 미검증이며 `ready=false`를 유지합니다. 모델 호출도 하지 않습니다. 시범 계획은 승인된 모델·호출/시간 한도를 유지하며 [단가표](../../configs/owned-price-book.scn001.json)를 연결합니다. 단가는 [OpenAI 공식 모델 문서](https://developers.openai.com/api/docs/models/gpt-4.1-mini)의 2026-11-15 확인값이며 실제 청구는 사용량 영수증과 함께 계산합니다. 단가표는 금액 상한을 새로 승인하는 설정이 아닙니다.

## 4번 파트 마무리 구현 (2026-11-14)

11.14 합의(4번 파트 구현에 필요한 것은 4번이 정한다)에 따라 SCN-001과 공통 평가 도구 범위의 D0~D6 연결을 구현했습니다. 독립 테스트 수정 경로의 Host 계측과 실제 비교 실험까지 모두 완료했다는 뜻은 아닙니다. SCN-002 전용 평가는 회원가입 MVP 이후의 확장 측정 과제이며 현재 MVP의 필수 누락으로 세지 않습니다. dev 쪽 변경은 [`docs/evaluation/dev_changes_2026-11-14.md`](../../docs/evaluation/dev_changes_2026-11-14.md)에 있습니다.

### 연결 규격 대조 (D0)

- dev 시나리오 계약에 연결 규격(`externalContract`)을 넣었습니다. 앱·DB 의존성·User 모델·가입 API·시작 파일·패키지 규칙이 모든 역할 입력과 의미 검토 기준에 같은 내용으로 들어갑니다.
- `protected_tests.CONNECTOR`는 숨긴 시험지 연결부(`signup_link.py`)가 붙는 위치입니다. `check_policy`가 동결 계약의 연결 규격을 이 값과 대조하고, 다르면 `configure`·`score`가 `CONTRACT_NOT_SUPPORTED`로 멈춥니다. 연결 규격이 없는(그 전에 동결된) 계약은 그대로 받습니다.
- 시험 `tests/evaluation/test_external_contract_link.py`가 연결부 상수·CONNECTOR·dev 계약 세 곳이 같은지 봅니다.

### Ver1 보호 케이스 파일 2판 (D2)

- `protected_tests.protected_case_file(contract)`는 QA 6개·보안 3개 케이스에 역할(`role`)을 적은 schemaVersion 2 파일을 만듭니다. `experiment configure`와 `protected_tests export`가 이 파일을 씁니다.
- dev Host는 Security 보호 범위를 케이스의 역할이 범위의 역할과 모두 같을 때만 받습니다. 역할이 없는 1판은 QA 보호 범위만 받습니다.

### 공통 시작 저장소 (D0)

```bash
python -m evaluation.starting_source build --skeleton examples/starting-source/scn001-signup --lock <실행 이미지 requirements.lock> --out <새 저장소 폴더> --config configs/owned-source.local.json
```

- 저장소에는 연결 규격의 시작 파일(`startingFiles`)만 넣습니다. 골격의 README 같은 팀용 문서는 넣지 않습니다.
- 시험지·평가 코드가 섞이면 만들지 않습니다. 고정 작성자·시각으로 커밋해, 같은 입력이면 같은 커밋이 나옵니다.
- dev와 같은 방법으로 Snapshot 해시를 계산해 Host 시작 코드 설정을 쓰고, 도구 설정에 적을 lock 해시(`dependencyLockHash`)를 출력합니다.

### 측정 Run 실행 (D6, 개발정의서 11-A)

```bash
python -m evaluation.experiment_run runtime --plan examples/evaluation/scn001-pilot.ver1.plan.json --architecture MULTI_AGENT --example configs/owned-runtime.example.json --out configs/owned-runtime.local.json
python -m evaluation.experiment_run schedule --plan examples/evaluation/scn001-pilot.ver1.plan.json
python -m evaluation.experiment_run all --plan examples/evaluation/scn001-pilot.ver1.plan.json --out experiments/scn001/pilot/<날짜-이름>/runs
```

- `runtime`: 계획의 모델·한도·시간 예산을 Host 실행 설정 예시에 넣고 dev 설정 읽기로 확인합니다. 두 구조가 같은 값을 씁니다.
- `schedule`: 반복마다 구조 순서를 번갈아 실행 순서를 매깁니다(1회차 Single→Multi, 2회차 Multi→Single). `RANDOMIZED_RECORDED`는 experimentId로 정한 시드를 남깁니다.
- `run`·`all`: dev Host 설정 검사(읽기 전용)로 승인 설정을 얻고 `experimentId`·`executionOrder`·`warmupState`만 더해 제출합니다. 승인 설정의 보호 테스트 참조가 계획 버전과 다르면 제출하지 않습니다(`LOOP_POLICY_MISMATCH`).
- 끝(`FINISHED`·`ABORTED`)이나 멈춤(`WAITING_INPUT`·`HUMAN_REVIEW`)까지 기다리고, 멈춘 Run에 답하거나 재개하지 않습니다. 시간 예산 + 5분이 지나도 진행 중이면 `TIMEOUT`으로 남기고 `RUN_WAIT_TIMEOUT`으로 배치를 중단합니다. 같은 기록 폴더에 미종료·제출 불명확 슬롯이 있으면 다른 순서로 새 Run을 제출하지 못합니다. 기존 Run을 임의 취소하거나 재제출하지 않으며, 같은 순서를 다시 실행해 종료 여부를 확인합니다.
- Run마다 dev `evaluation_export`와 같은 인계 폴더(모든 코드 버전)를 만들고 `runs.jsonl`에 단계별 사건을 남깁니다. 채점은 그 폴더로 `evaluation.workflow`를 돌립니다.
- 계획의 구조별 `hostConfigurations`에는 runtime·tools·source 외에 `api`(Host 주소), `protectedCases`(Ver1), `priceBook`을 둡니다.

### 정한 실험 값 (시범)

[`scn001-pilot.ver1.plan.json`](../../examples/evaluation/scn001-pilot.ver1.plan.json)·[`scn001-pilot.ver2.plan.json`](../../examples/evaluation/scn001-pilot.ver2.plan.json)은 같은 experimentId를 쓰고 버전만 다릅니다.

| 항목 | 값 |
|---|---|
| 모델 | `gpt-4.1-mini-2025-04-14`, temperature 0 (Single·Multi 같음) |
| 시범 한도 | 모델 호출 200, 도구 호출 200(둘 다 Run 전체 합계), 출력 토큰 8,192, 총 토큰 제한 없음, 모델 응답 120초, 도구 120초(도구 설정 `maxCallSeconds`와 같아야 함), 시간 예산 60분 |
| 반복 | 시범은 조합마다 1회, 공식은 조합(Single·Multi × Ver1·Ver2)마다 5회. 공식 한도는 시범 사용량으로 정한다 |
| 측정 Run 질문·보류 | 답하거나 재개하지 않고 종료로 집계한다 |
| 버전 | Ver1·Ver2 둘 다. Ver1은 순환 안에 연습 시험 QA 6개·보안 3개 |

### 외부 점검 8건 수정 (2026-11-14)

- **원래 DB 연결 검사:** 최초 보완은 QA-SIGNUP-001에 원래 연결로 가입하는 단계를 더한 0.3.0이었습니다. 후속 0.4.0은 모든 요청에서 생성물의 원래 DB 연결·초기화·SQL 로그 설정을 사용합니다. 평가기는 DB나 테이블을 대신 만들지 않습니다. 실패 응답이면 FAIL, 성공했는데 지정한 DB(`DATABASE_URL`)에 없으면 `DATABASE_URL_NOT_USED` 미검증입니다.
- **요구사항 기준:** `suite.json`의 `requirements`가 시험지가 전제로 하는 SCN-001 요구사항(설명·완료 조건·검증 담당)입니다. `check_requirements`가 동결 계약과 글자 그대로 대조하고, 다르거나 늘거나 줄면 `configure`·`score`·보호 케이스 생성이 거부합니다. 이 기준은 시험 묶음 지문에 들어갑니다.
- **비용:** Agent별 평가표는 Host의 십진 문자열 비용(`"0.001"`)을 `Decimal`로 정확히 더합니다. 형식이 틀린 값은 0이 아니라 모름입니다.
- **측정 Run 기록:** `runs.jsonl`은 사건 기록입니다(SUBMITTING → SUBMITTED → ENDED → EXPORTED 또는 EXPORT_FAILED). 제출 전에 의도와 요청·설정 지문을 기록하고, 수락 응답을 받으면 Run ID를 남깁니다. 같은 순서를 다시 실행하면 그 Run의 대기·인계를 이어 합니다. 응답이 불명확하면 아래 복구 절차를 따릅니다.
- **계획 대조:** `run`은 제출 직전에 계획 전체(모델, LLM 한도 6칸, 시간 예산, 수정·재시도 한도, 시작 커밋, 이미지, 하드웨어)를 실제 Host 설정과 대조하고, 다르면 `PLAN_CONFIGURATION_MISMATCH`와 다른 칸 이름을 내고 멈춥니다. `preflight`도 LLM 한도 전체를 대조합니다.
- 준비 검사에서는 선언된 `protectedCases`와 `priceBook`도 읽어 두 구조의 내용을 대조합니다. Ver1은 보호 케이스 파일이 필요하고 Ver2에는 포함하지 않습니다. 선언한 파일을 읽지 못하거나 내용이 다르면 준비 완료로 처리하지 않습니다.
- **`runtime --force`:** 임시 파일에서 dev 설정 읽기로 검사한 뒤 바꿔 끼웁니다. 검사에 실패하면 기존 파일은 그대로입니다.
- **멈춘 Run의 시간:** 질문·보류로 멈춘 Run은 멈춘 상태로 바뀐 사건 시각까지를 전체 시간으로 잽니다(`endedBy: PAUSED`, 정상 종료는 `ENDED`).
- **Ver1·Ver2 동일 조건:** `python -m evaluation.parity --group-by loopPolicy --ver1 <파일들> --ver2 <파일들> --architecture MULTI_AGENT [--runs <JSON>] --out <결과>/parity.json`. 같은 구조의 두 설정이 순환용 보호 범위·보호 케이스 파일만 다르고 실행 설정·나머지 도구 설정·시작 코드·가격표가 같은지 보고, Run별 고정 해시를 버전에 맞는 설정으로 대조합니다(`runs` 항목에 `loopPolicy`). `compare`와 비교 보고서는 구조 비교·버전 비교 모두 이 비교 기준으로 만든 parity.json이 PASS이고 모든 Run의 고정 정책이 확인돼야 준비 PASS로 봅니다.

### 측정 실행의 중복 방지와 복구 (2026-11-14 후속)

- 같은 출력 폴더의 실행은 프로세스 간 잠금으로 하나씩 처리합니다. 기록은 임시 파일의 저장을 완료한 뒤 원자적으로 교체합니다. 잠금 중 다른 실행은 `EXPERIMENT_RUN_BUSY`로 끝납니다. 다른 출력 폴더까지 Host 전체의 중복을 막는 기능은 아닙니다. 같은 실험을 복구할 때 출력 폴더를 바꾸지 않습니다.
- 실제 Host(dev 83번)는 API마다 서버 전용 Bearer 토큰을 요구합니다. 실행기는 환경 변수 `A2A_HOST_API_TOKEN`만 읽어 헤더로 보내고, 기록·결과 파일에 남기지 않습니다. 토큰이 없으면 제출 의도를 남기기 전에 `HOST_API_TOKEN_REQUIRED`로 멈춥니다. `experiment fetch`도 같은 토큰을 씁니다.
- 요청 전 `SUBMITTING`을 남깁니다. 이때 dev 85번의 중복 방지 키(`Idempotency-Key`, UUIDv4)와 Host 요청 지문(`hostRequestSha256`)을 함께 기록하고(`runs.jsonl` 형식 3), 그 키로 Run을 생성합니다. 같은 키·같은 본문은 Host가 같은 Run을 200으로 재생하므로 200도 성공입니다. 응답 유실·중단으로 Run ID를 확인하지 못하면 `SUBMISSION_OUTCOME_UNKNOWN` 또는 다음 실행의 `SUBMISSION_REQUIRES_RECONCILIATION`으로 멈추고 자동 재제출하지 않습니다.
- 이때 기존 `run`·`all` 명령에 `--reconcile-submission`을 붙이면 같은 키로 Host 접수 기록(`GET /api/v1/submissions/{key}`, dev 86번)을 조회합니다. 기록이 있으면 요청 지문을 대조해 그 Run을 잇고, `SUBMISSION_NOT_FOUND`가 확인될 때만 계획·Host 설정에서 다시 만든 본문이 처음 기록과 같은지 본 뒤 같은 키로 다시 보냅니다. 지문이 다르면 `SUBMISSION_RECONCILIATION_CONFLICT`, 조회 API가 없거나 장애면 `SUBMISSION_RECONCILIATION_UNAVAILABLE`로 멈추고 새 Run을 만들지 않습니다. Host가 키로 중복을 막으므로 어느 경우든 Run은 하나입니다.
- 계획 전체·유효 요청문·실행 순서·경로 기준을 지문으로 묶습니다. 같은 실험/순환 정책의 계획을 바꾸면 완료 Run 재사용과 다음 순서 제출 모두 `LEDGER_PLAN_CHANGED`로 거부합니다. 지문 없는 구형 기록과 키가 없는 형식 2 기록도 `LEGACY_OR_UNBOUND_LEDGER`로 거부하므로 기존 측정 기록을 지우고 같은 슬롯을 다시 제출하지 않습니다.
- `run --order N`은 다음 순서의 신규 실행 또는 이미 제출한 Run의 복구에 사용합니다. 신규 제출 때 앞 슬롯 전부의 수락·종료/보류 기록이 없으면 `PREVIOUS_ORDER_NOT_COMPLETED`로 중단합니다. 앞 Run이 종료됐고 인계만 실패했다면 다음 슬롯을 실행할 수 있으며, 기존 Run 인계 복구도 계속 가능합니다.
- 인계 폴더가 이미 있으면 모든 목록 파일의 해시, Run·구조·상태·고정 설정, v1부터 최종 버전까지의 코드·Manifest를 확인합니다. 정상 폴더는 기록만 복구하고, 불완전한 폴더는 `.incomplete-…` 이름으로 보존한 뒤 같은 Run을 다시 내보냅니다. 다른 Run·설정의 폴더는 거부합니다.

현재 보호 시험 묶음은 0.7.0이며(0.5.2 앱 import 단계 로그 수집 → 0.6.0 검사별 세부 관찰 기록 → 0.7.0 FAIL 뒤에도 독립 세부 항목 계속 검사, 9개 검사·요구사항·점수 산식은 그대로) 최종 지문은 [최종 시험지](../../docs/evaluation/final_exam.md)에 기록합니다. 0.6.0 이하 설정·채점 결과를 새 지문의 결과와 섞지 않으며, 새 측정 전에 보호 케이스와 실험 등록표를 현재 버전으로 준비합니다(`python -m evaluation.protected_tests export --out configs/experiment/ver1 --force`).

### 검사별 세부 결과 (`check_details`, 0.6.0~0.7.0)

최종 채점은 9개 검사마다 세부 항목(응답 상태, 사용자 수 증가량, Argon2 설정값 등)의 기대값·실제값·상태를 `check_details.json`·`check_details.md`로 함께 남깁니다. 비밀번호·해시·이메일·응답 본문은 담지 않고 참/거짓·정수·응답 코드만 담습니다. 공식 판정과 점수는 기존 9개 검사 결과만 쓰며 세부 항목 수로 바뀌지 않습니다. 세부 기록이 없거나 상위 판정과 어긋나면 그 검사의 세부 기록을 미검증으로 표시합니다. 정의와 예시는 [세부 결과 설명](../../docs/evaluation/check_details.md)에 있습니다.

실행기와 dev Host 사이 계약은 가짜 Host뿐 아니라 실제 dev 앱에 붙여서도 시험합니다(`tests/evaluation/test_experiment_run_host_contract.py`: 토큰·중복 방지 키·200 재생·접수 조회 지문·복구).

### 점검 수정 (2026-11-15, 0.5.1)

- **저장 누락은 FAIL:** QA-SIGNUP-001은 성공 응답 뒤 사용자 행이 없을 때, 그 요청 중 앱이 평가용 DB(`DATABASE_URL`)에 SQL을 실행했으면 저장 누락(커밋 누락 등)으로 FAIL입니다. 평가용 DB를 전혀 쓰지 않았을 때만 `DATABASE_URL_NOT_USED` 미검증입니다. 다른 DB에만 SQL을 실행한 경우는 이전처럼 요청 단계에서 미검증입니다.
- **관찰 기록 실패는 Run을 바꾸지 않음:** 로그·도구 출력·모델 입출력·A2A 출력 영수증을 저장하지 못해도 제품 실행은 계속합니다. 그 구간은 `INCOMPLETE` 표식(또는 끝 표식 없음)이나 빠진 영수증으로 남아 노출 검사·과정 지표가 미검증으로 봅니다. 증거 저장 실패를 제품 실패로 세지 않고, 증거 없이 PASS로 올리지도 않습니다.
- **Run 기록 노출 판정 기준:** 모델 출력·도구 인자·생성 파일·로그 등 기록 어디에서든 완전한 비밀번호 해시 형식 값이 나오면 출처와 상관없이 노출로 봅니다. 테스트용 예시 해시도 예외가 아닙니다. 기록만으로 실제 사용자 해시와 구분할 근거가 없기 때문입니다. 이 Run 기록 검사는 제품 점수·비교 지표에 들어가지 않고 요약에 따로 표시됩니다.
- **관문 거부 사건:** Orchestrator가 Run을 HUMAN_REVIEW로 보낼 때 남기는 `PROCESS_GATE_REJECTED`는 과정 지표에서 `ORCHESTRATOR_OUTPUT` 단계의 관문 거부 1건으로 셉니다. dev 저장소 기록과 평가 집계를 함께 시험합니다.

### 실험 환경 준비 (`evaluation.environment`, 2026-11-15)

Host 샌드박스와 D5 채점이 함께 쓰는 실행 이미지, 같은 lock의 시작 저장소, Ver1·Ver2 도구 설정, 계획의 환경 칸을 한 번에 준비합니다. 다운로드 단계(`wheels`·`browsers`·`image`)는 `--allow-network`를 준 경우에만 실행하고, `status`·`configure`는 네트워크를 쓰지 않습니다. 정한 값(Playwright 1.65.2, Bandit 1.9.4, email-validator 포함, 도구 한도·argv)은 모듈 상단 상수에 있습니다. 실행 순서와 Host 시작 명령은 [시범 실험 실행 절차](../../docs/evaluation/experiment_runbook.md)를 따릅니다. 사전점검(`evaluation.preflight`)은 설정 대조 외에 dev 샌드박스와 같은 컨테이너 옵션으로 실제 브라우저 도구를 한 번 돌려 봅니다(`hostBrowser.<구조>`).

### 기록 누락과 상태 집계 (2026-11-15)

- Agent별 사용량은 영수증 개수·일관성·고유 호출 번호·Run/단계 배정을 확인합니다. 불완전한 호출·비용을 0으로 넣지 않고 미확인으로 표시하며, 확인된 부분값은 `known…`에 보존합니다. 수정 완료는 Issue가 가리킨 요구사항 전체의 재검증 결과가 있어야 판정합니다.
- FAIL 검사와 미검증 검사가 섞인 요구사항은 원본의 `UNVERIFIED`를 유지합니다. 미검증 안의 확인된 실패 개수·요구사항 ID를 따로 표시하고, 그 항목은 완전 충족률 상한에도 포함하지 않습니다.
- 비교는 결과 폴더 부족분을 미시작으로 단정하지 않습니다. `unrecordedRuns`는 결과 미수집, `startedWithoutResults`는 제출 수락이 확인된 미수집 Run, `unknownExecutionSlots`는 실행 여부 미확인입니다. 실행 여부가 불명확하면 `notStarted`와 해당 비율은 null입니다. 계획 분모와 비용 미확인 처리는 유지됩니다.
- 결과 루트에 `runs.jsonl`이 있으면 자동으로 읽습니다. 실행 원장과 평가 결과 폴더가 다르면 `compare --run-journal /path/to/execution/runs.jsonl`을 추가합니다(여러 원장은 반복 지정). 원장만 있고 결과가 없는 Run도 상태·사유를 별도 행으로 보존합니다. 다른 실험이나 설정의 원장은 거부하며, 계획 슬롯 결과가 빠져 있으면 공식 비교 준비를 보류합니다. 원장에 아무 기록도 없다는 사실만으로 미시작을 확정하지 않습니다.
- `workflow`는 과정·완료 판정 등 부가 파일까지 모두 만든 뒤 결과 폴더를 교체합니다. 저장 도중 실패하면 이전 결과를 보존하고 재시도할 수 있습니다. 닫힌 자동 구간은 직접 `record/score --force`로도 재개 자료로 바꿀 수 없습니다. 동일한 원본 Run·상태·Snapshot 재채점은 허용하며 `automatic_boundary.json`으로 경계 표시를 유지합니다.
- Single/Multi 짝 비교는 모델·한도·시작 코드·실험·환경 등 동결 조건을 대조합니다. 다른 조건 또는 조건 증거가 없는 쌍은 차이 계산에서 제외하고 사유를 기록합니다. 전체 비교 준비 판정과 개별 쌍 계산에 같은 조건 규칙을 적용합니다.

```bash
python -m evaluation.experiment compare --results experiments/scn001/results --group-by architecture --planned SINGLE_AGENT=5 --planned MULTI_AGENT=5 --run-journal experiments/scn001/runs/runs.jsonl
```
