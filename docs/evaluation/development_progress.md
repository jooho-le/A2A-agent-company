# 개발진행사항

최종 갱신: 2026-10-14 · 평가 명세 0.4 · 작업 브랜치 `seokmin` (dev `caa4144` 기준 + 평가 변경)
공통 계약 기준: dev `caa414432af52f838ef5b62c993246b48013178c`

이 문서는 4번 평가 모듈의 **현재 상태**를 정리한다. 평가 기준·정책은 [명세서](evaluation_spec.md),
요구사항별 검사는 [기준표](scn001_criteria.md), 설치·실행은 [환경 설정](environment_setup.md)에서 관리한다.
팀 논의용 요약은 [보안 판정 기준](share_security_criteria.md), [Single 비교 기준선](single_baseline.md),
시연 자료 정리는 [시연 자료](demo_materials.md)에 있다.

## 1. 평가 구조 요약

4번 평가는 개발 순환(Planner → Developer → QA/Security Agent → 수정) **밖에서** 최종 결과물을 채점하는
독립 평가다. 순환 안의 QA·Security Agent는 2번이 만드는 참가자이고, 4번은 같은 보호 테스트로
Multi·Single 두 구조의 결과물을 채점해 비교한다. 채점 결과는 Agent에게 돌려주지 않는다(명세 4.1절, 팀 확정 대기, OPEN-11).

팀장 카톡(2026-10-14) 기준 4번의 일: ① QA·Security 평가 기준과 검사 항목 정리(미팅에서 논의), ② 평가 모듈에 실행 정보·결과물 정보 연결 준비,
③ 시연 실행 로그·생성 파일·화면 정리(12월 발표용, 팀장에게 전달), ④ 실제 테스트와 Single/Multi 비교는 미팅 이후 진행할 수 있게 준비.
다음 기업 미팅(10월 말~11월 초)은 테스트를 제외한 시연 영상으로 진행하고, 19일 주에 팀 내부 회의가 있다.

| 관점 | 요구사항 | 검사 (기준표) |
|---|---|---|
| QA | REQ-001·002·003·004·007 | QA-SIGNUP-001~006 (실행) |
| SECURITY | REQ-003·005·006 | SEC-SIGNUP-001·003·004 (정적), SEC-SIGNUP-002·005 (실행) |
| Orchestrator | REQ-008 | 독립 평가 범위 밖. Trace로 D4에서 확인 |

관점별 요구사항은 dev `scenario_registry.py`의 SCN-001 등록 정보와 같다.

## 2. 현재 상태

| 기능 | 상태 | 파일 | 범위 |
|---|---|---|---|
| 검사 집계 | 구현 | aggregation.py | 필수·선택 분리, 누락·중복·증거 없는 판정·승인 없는 제외 처리, 실패와 미검증 동시 보존, 진단 정보·민감값 치환 |
| Manifest | 구현 | manifest.py | 형식 검사, 기준·관측 불일치 시 결과 미사용·UNVERIFIED·통과율 null |
| 독립 평가 보고서 | 구현 | independent.py | 평가 ID·대상 Run 분리, suite 해시, 요구사항 범위 검사, 요구사항별·관점별 요약, 검사·요구사항 통과율 분리, 대상 Run 기록(`sourceRun`), 스키마 검증 |
| 에이전트 호환 보고서 | 구현(보조) | reporting.py | dev QA_REPORT/SECURITY_REPORT 변환, ID 대응표·도구 실행 근거·재시도 규칙·스키마 검사 |
| 보안 판정(D1) | 구현 | security/ | 기준 JSON + `assess()`, REQ-003·005·006 정적 판정, Bandit 지원 규칙 판정, dev 승인 로더 통과. 승인 등록·Host 연결은 팀장·주호 |
| 정상·결함 예제(D3) | 구현 | tests/evaluation/signup_examples.py | 정상 1 + 결함 18 + 정상 변형 9. 합성 소스이며 실제 생성물이 아님 |
| 기준표(D0) | 초안 | scn001_criteria.md | 요구사항별 검사·증거·판정 조건, 합의 필요 항목, 생성물 연결 규격 초안 |
| 결과 수집·지표(D4) | 구현(합성 입력) | run_metrics.py | dev `/runs/{id}`·events·issues·tool-attempts·usage·configuration JSON → 지표(사건 시각 차이로 시간, 비용·토큰 미확인은 None), 구조별 요약(성공률 분모=전체 계획 Run), 비교 가능 여부 검사(모든 Run 대조, 환경 digest 불일치는 해당 Run 재실행). 실제 Run으로 검증하지 않음 |
| 시연 자료 정리(D4) | 구현 | demo_index.py | 로그·생성 파일·화면·보고서 목록과 파일별 SHA-256(청크 단위), 빈 분류 표시, 심볼릭 링크·알 수 없는 폴더·알려진 비밀값 거부 |
| 공유 문서 | 작성 | share_security_criteria.md, single_baseline.md | 팀장 논의용(보안 판정 기준, Single 비교 기준선) |
| 회원가입 실행 검사(D2) | 대기 | protected_tests/ (예정) | 시작 코드·API/DB 규격이 정해져야 연결 가능. 지금은 검사 로직 틀만 가능. unittest로 쓰고 연결부(가입 요청·저장 조회·DB 초기화·로그 수집)만 교체 가능하게 하며, QA 순환 연결용 `owned-qa-cases` 초안(케이스 6필드: toolName·selector·testId·requirementId·title·expectedResult)을 함께 만든다 |
| 결과 어댑터 | 대기 | — | dev 26·28번 결과 형식은 확인했으나 정규화 코드는 아직 없음 |
| 최종 독립 채점기(D5) | 대기 | — | 최종 Snapshot 소스 인계 방법·종료 후 실행 권한 필요 |
| Single 비교 실험(D6) | 설계만 | single_baseline.md | Single 실행기가 없음(`architecture`는 `MULTI_AGENT`만 허용) |

## 3. 모듈별 입력과 출력

**`aggregate(plan, results, *, sensitive_values=())`**
검사 계획(`CheckPlan`)과 결과(`CheckResult`)를 받아 상태별 개수, 필수 통과율, 확인된 실패 ID,
미검증 ID(필수만, 선택은 `optional…` 목록), 검사별 행을 돌려준다. 결과가 없으면 NOT_RUN, 증거 없는 PASS·FAIL은 ERROR,
승인 없는 적용 제외는 ERROR로 바꾼다. 필수 검사에 ERROR·NOT_RUN이 있으면 UNVERIFIED다.

**`aggregate_report(plan, results, *, expected, observed, sensitive_values=())`**
Manifest가 같을 때만 `aggregate`를 적용한다. 다르거나 없으면 결과를 쓰지 않고 UNVERIFIED, 통과율 null,
검사별 `errorCode`(MANIFEST_MISSING/MISMATCH)와 `manifestCheck`를 남긴다.

**`build_evaluation_report(...)`** (독립 평가, 기본 경로)
`evaluation_id`, `source_run_id`, `architecture`, suite 이름·버전·SHA-256, 시간대 포함 `created_at`,
필수 요구사항 범위, Manifest, 계획·결과, `EvaluationBinding(requirement_id, perspective, title)` 대응표, 선택으로 `SourceRunSummary`를 받는다.
요구사항마다 필수 검사가 하나 이상 있어야 하며, 한 요구사항에 검사를 여러 개 연결할 수 있다.
출력은 `INDEPENDENT_EVALUATION` 보고서(스키마 검증)이고 `evaluationVerdict`는 Orchestrator의 `finalVerdict`와 별개다.
`sourceRun.missedDefect`는 Orchestrator SUCCESS인데 보호 테스트 필수 검사 FAIL이 확인된 경우이고,
HUMAN_REVIEW는 재개될 수 있어 종료 시각을 기록하지 않는다.

**`build_report(...)`** (에이전트 호환, 보조 경로)
실제 A2A Task·Artifact ID가 담긴 context, 필수 검사만의 계획, `TestBinding` 대응표, 도구 실행 근거를 받는다.
ERROR·NOT_RUN을 UNVERIFIED로 바꾸고 원래 상태·사유는 `details`에 보존한다.
SECURITY는 요구사항당 결과 1개만, NOT_APPLICABLE은 거부한다.

**`security.assess(request)` / `security.evaluate(request)`**
dev 42번 `SecurityAssessmentRequest`를 받아 REQ-003·005·006을 소스 정적 분석으로 판정한다. 생성 코드는 import·실행하지 않는다.
모르는 구조는 UNVERIFIED, 허용 증거 참조가 없으면 UNVERIFIED로 내린다. `assess`는 내부 오류를 `None`으로 돌려주므로
원인을 보려면 `evaluate`를 직접 호출한다.

**`run_metrics.fetch_run_bundle(get, run_id)` / `run_metrics(bundle)` / `experiment_summary(runs, planned_runs, evaluations)` / `comparison_validity(entries)`**
Run 조회 JSON 수집(페이지 이어 받기), 한 Run의 지표 계산, 구조별 요약, 두 구조 비교 가능 여부 검사.
측정하지 못한 값은 0이 아니라 None이고, 성공률 분모는 시작하지 못한 Run을 포함한 전체 계획 Run이다.

**`demo_index.build_demo_index(root, *, run_id, created_at, ...)`**
`logs/source/screens/reports` 폴더의 파일을 해시와 함께 목록으로 만든다.

모든 함수는 테스트·도구를 실행하지 않고(`demo_index`·`run_metrics`는 네트워크도 쓰지 않음), 증거 URI를 열거나 ID 등록 여부를 조회하지 않는다.

## 4. 코드 검토 결과와 알려진 제약

2026-10-06과 2026-10-14에 검토한 항목이다. 상세는 명세 15.2절.

| 우선 | 항목 | 상태 |
|---|---|---|
| 결정 필요 | 에이전트 호환 경로에서 "도구 정상 종료 + 결과 파싱 실패"는 보고서 생성 오류가 된다(dev 계약과 같은 규칙). 독립 평가 경로는 해당 없음. | OPEN-13 |
| 완료 | 필수·선택 실패/미검증 ID 분리, 가상환경 확인 테스트를 기본 pytest에서 분리 | 2026-10-06 |
| 완료 | Manifest 불일치 시 통과율 0.0 → null, 검사별 errorCode를 Manifest 오류로 기록 | 2026-10-14 |
| 완료 | D1 오탐(`passed`·`MIN_PASSWORD_LENGTH`·`exc.errors()` 변수 처리·pydantic 정규화·`CREATE UNIQUE INDEX`)과 미탐(요청 모델 통째 로깅·`model_dump()`·`sys.stderr.write`·`__dict__` 응답·해시 호출 후 원문 반환·무관한 컬럼 UNIQUE) | 2026-10-14 |
| 완료 | Bandit CONFIRMED는 코드 근거가 있을 때만(필드 이름 문자열 `"password"`는 오탐), 파싱 중 `RecursionError`는 해당 파일만 분석 불가로 기록, `await` 해시 호출 인정 | 2026-10-14 |
| 완료 | `missedDefect`가 평가 UNVERIFIED일 때 확인된 FAIL을 놓치던 문제, HUMAN_REVIEW를 종료로 처리하던 문제 | 2026-10-14 |
| 완료 | `comparison_validity`가 구조당 보고서 하나만 비교해 입력 순서에 결과가 달라지던 문제(모든 Run 대조) | 2026-10-14 |
| 참고 | 민감값 치환은 호출자가 알려 준 문자열만, 진단 필드에서만 수행한다. 제목·suite·findings·toolEvidence 제외. | 호출자가 사전 정리 |
| 참고 | `created_at`은 3.10에서도 읽히는 `fromisoformat` 기준이라 일부 ISO 표기를 못 읽는다. | `YYYY-MM-DDTHH:MM:SS+09:00` 사용 |
| 참고 | `assess()`는 내부 오류를 `None`으로만 알린다(dev 승인 계약상 로그·전역 상태 금지). | `evaluate()`로 디버깅 |
| 참고 | D1 이름 판정은 설명 단어(text, name, key 등)가 낀 이름을 비밀번호 값이 아닌 것으로 본다. 정적 분석은 호출 흐름·동시성·런타임을 보지 않는다. | 실행 검사로 보완 |
| 참고 | `security/*.py`를 고치면 dev 승인 해시가 달라진다. | 코드 동결 뒤 승인 |
| 참고 | `tests/evaluation/fixtures/reference_backend`는 세화 backend 표본 복사본이다(회귀 테스트용, 생성물 정답·규격 아님). | 필요하면 표본 갱신 |

## 5. 팀원 정보 상태

| 담당 | 받은 정보 | 남은 정보 |
|---|---|---|
| 1번 | ID 규칙, Artifact·Manifest, 수정 3회·도구 재시도 2회, Run 조회 API와 Run Configuration, 보고서 수신·판정 코드(dev 45번) | 실행 종료 시 최종 Snapshot 소스 인계 방법(Artifact API는 메타데이터만 제공), Single 실행기 |
| 2번 | QA 보호 케이스 연결 규격(unittest·읽기 전용 `/inputs/tests`·6필드, REQ-001·002·003·004·007만), Security 승인 함수 계약(42번) | 평가 전용 보호 테스트를 컨테이너에서 독립 실행하는 방법과 종료 후 실행 권한 |
| 3번 | 플랫폼 backend의 `POST /api/auth/signup`(email·password), 응답 201/422/409/500, 실행 방법, `users` 테이블, pwdlib Argon2(`3번필요사항중간정리본`) | 생성물 시작 코드(플랫폼 backend는 생성물 규격이 아님), 회원가입 서비스 코드·API/DB 규격·의존성, 테스트 DB 초기화·서버 로그 수집 |

3번 구현과 개발정의서의 차이(이메일 앞뒤 공백, canonical_email 컬럼, Argon2 수치, 동시 가입 응답)는 OPEN-01·02·14 안건이다.

## 6. 지켜야 할 dev 제약

- QA 순환 안 PROTECTED 케이스는 QA 담당 요구사항(REQ-001·002·003·004·007)에만 연결할 수 있다(`qa_services.py`). REQ-005·006 실행 검사는 최종 평가용이다.
- 보호 테스트: Python unittest만, `/inputs/tests` 읽기 전용, import 기준 `/snapshot`, 네트워크 차단, 보호 bytes 포함 Unit 설정 64KiB, 테스트 0개·import 오류는 TEST_RUNNER_ERROR, 시험용 비밀번호는 실행 시 생성(literal 금지, dev 19번).
- assess: 인자 하나인 최상위 함수, 생성 코드 import/exec 금지, PASS/FAIL/CONFIRMED/FALSE_POSITIVE에는 허용 참조와 비어 있지 않은 관측 JSON, 기준 64KiB, 구현 32파일·4MiB, 논리 이름은 영문자 시작.
- 지표: `costUsd=null`을 0으로 쓰지 않음, 병렬 Step 시간 합산 금지, 성공률 분모 = 전체 계획 Run, UNVERIFIED Run 삭제 금지.
- 범위 밖(2차 미팅): SQL Injection·XSS 정밀 검사, 깊은 보안 분석, 다수 화면 테스트.

## 7. 검증 기록

| 날짜 | 결과 |
|---|---|
| 2026-10-14 | 점검·수정 후 평가 테스트 231개 통과(합성 입력), `pytest checks` 2개, `scripts/check_dev_reports.py`로 dev 원본 모델이 합성 보고서 8개 수용. 저장소 `.py` 358묶음을 D1에 넣어 예외·2초 초과 없음. 저장소 전체 pytest는 수정 전(`-x`) 2461개 통과·dev 테스트 1개 실패(`test_planner_developer_continuation`, 원인 미확인), 수정·주석 작업을 마친 뒤 전체 3539개 모두 통과(22분 32초, 실패 없음. 앞의 1개 실패는 재현되지 않았고 원인은 확인하지 못했다) |
| 2026-10-14 | Python 3.13, dev `caa4144` 기준 평가 테스트 146개 통과. D1이 dev 승인 로더(`load_approved_assessor`) 통과 |
| 2026-10-09 | 평가 테스트 126개, 전체 298개 통과. dev `WorkflowRun` 응답 5가지 상태로 호환 확인 |
| 2026-10-06 | 기본 pytest 285개 통과(평가 113 + dev 172). 공통 스키마 사본 4개와 dev 원본 동일. dev 실제 보고서 모델이 합성 보고서 8개 수용 |
| 2026-10-05 | dev 공통 환경 통합 후 pytest 263개 통과 |
| 2026-10-04 | 평가 wheel 빌드, 작업 폴더 밖에서 스키마 포함 import·보고서 생성 확인 |

모델 수용·합성 입력 통과는 A2A 전송·실제 서비스 실행 검증을 뜻하지 않는다. 예시 출력은 모두 mock이다.

## 8. 다음 작업

1. 19일 주 팀 회의: 보안 판정 기준과 합의 항목(OPEN-01·05·11·14~16)을 [보안 판정 기준](share_security_criteria.md)으로 논의. 세화에게 시작 코드·API/DB 규격, 주호에게 최종 Snapshot 인계 방법 요청.
2. 규격이 정해지면 공통 QA·보안 평가 테스트(D2)를 연결부만 교체할 수 있게 작성하고 고장 낸 버전으로 FAIL 검출 확인.
3. 최종 독립 채점기(D5): 소스 인계 → 고정 환경 실행 → 결과 정규화 → 독립 평가 보고서.
4. 시연 로그·생성 파일·화면을 받아 `demo_index`로 정리(12월 발표).
5. 파일럿으로 시간·비용을 재고 Single 실행기 담당이 정해지면 Single/Multi 비교(D6).

일정(19일 주 팀 회의, 10월 말~11월 초 기업 미팅, 12월 최종 발표)은 팀장 카톡 기준이며 납기 확정이 아니다.

## 9. 변경 이력

| 날짜 | 변경 |
|---|---|
| 2026-10-04 | 초기 집계·Manifest 비교, dev 호환 해시 형식 검사, 문서 구조 정리, 공통 보고서 어댑터·합성 예시, 패키지·lock·스키마 동봉 |
| 2026-10-05 | 최신 dev `2b4fc60`에서 `seokmin`을 새로 만들고 평가 코드를 `src/evaluation/`로 이동. 루트 pyproject·uv.lock·.venv 하나로 통일 |
| 2026-10-06 | 독립 평가 보고서(`independent.py`), 검사별 진단 정보·민감값 치환, 필수·선택 목록 분리, 주석 정리, 문서를 독립 평가 구조와 명세 0.3에 맞게 재구성 |
| 2026-10-09 | 2차 기업미팅·팀장 오더 반영: 독립 평가 보고서에 `sourceRun` 추가 |
| 2026-10-14 | 평가 코드(`src/evaluation`, `tests/evaluation`, `scripts/check_dev_reports.py`)의 기존 주석을 쉬운 말로 다시 쓰고, 파일 머리말(역할·용어)과 함수·복잡한 줄 주석을 초보자 기준으로 추가. 코드 구조(AST)는 29개 파일 모두 이전과 같고 테스트 231개 통과 |
| 2026-10-14 | Python 3.13·dev `caa4144` 기준으로 정리. 기준표(D0)·Security 정적 판정(D1)·정상/결함 예제(D3)·결과 지표와 시연 자료 목록(D4)·공유 문서 3종 추가. 점검 수정(D1 오탐·미탐, 통과율 null, `missedDefect`, 비교 가능 검사 순서 의존, `HUMAN_REVIEW` 처리, 파싱 오류 처리). 명세 0.4. 인계용 문서(`claude_code_handoff.md`)는 내용을 이 문서로 옮기고 삭제 |
