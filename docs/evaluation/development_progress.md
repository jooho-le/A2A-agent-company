# 개발진행사항

최종 갱신: 2026-10-06 · 평가 명세 0.3 · 작업 브랜치 `seokmin`
공통 계약 기준: dev `2b4fc6037e1a7d8296040c01404c56e8a2a0f22e`

이 문서는 4번 평가 모듈의 **현재 상태**를 정리한다. 평가 기준·정책은 [명세서](evaluation_spec.md),
설치·실행은 [환경 설정](environment_setup.md)에서 관리한다.

## 1. 평가 구조 요약

4번 평가는 개발 순환(Planner → Developer → QA/Security Agent → 수정) **밖에서** 최종 결과물을 채점하는
독립 평가다. 순환 안의 QA·Security Agent는 2번이 만드는 참가자이고, 4번은 같은 보호 테스트로
Multi·Single 두 구조의 결과물을 채점해 비교한다. 채점 결과는 Agent에게 돌려주지 않는다.
자세한 원칙은 명세 4.1절이며 팀 확정 대기 중이다(OPEN-11).

| 관점 | 요구사항 | 보호 테스트(예정) |
|---|---|---|
| QA | REQ-001·002·003·004·007 | QA-SIGNUP-001~007 |
| SECURITY | REQ-003·005·006 | SEC-SIGNUP-001~003 (+004·006·007 연결 방식 결정 필요) |
| 1번 담당 | REQ-008 | Trace 복원 |

관점별 요구사항은 dev `scenario_registry.py`의 SCN-001 등록 정보와 같다.

## 2. 현재 상태

| 기능 | 상태 | 파일 | 범위 |
|---|---|---|---|
| 검사 집계 | 구현 | aggregation.py | 필수·선택 분리(실패·미검증 ID 목록 포함), 누락·중복·증거 없는 판정·승인 없는 제외 처리, 실패와 미검증 동시 보존, 진단 정보·민감값 치환 |
| Manifest | 구현 | manifest.py | 형식 검사, 기준·관측 불일치 시 결과 미사용·UNVERIFIED |
| 독립 평가 보고서 | 구현 | independent.py | 평가 ID·대상 Run 분리, suite 해시, 요구사항 범위 검사, 요구사항별·관점별 요약, 검사·요구사항 통과율 분리 |
| 에이전트 호환 보고서 | 구현(보조) | reporting.py | dev QA_REPORT/SECURITY_REPORT 변환, ID 대응표·도구 실행 근거·재시도 규칙·스키마 검사 |
| 코드 설명 주석 | 정리 | src/evaluation, tests/evaluation | 모듈·함수 단위의 간결한 한국어 설명과 처리 단계 표시 |
| 회원가입 보호 테스트 | 시작 가능 | protected_tests/ (예정) | 3번 정보 수령 완료. 로컬 실행으로 먼저 작성 |
| 결과 어댑터 | 대기 | — | 2번의 결과 JSON 형식 필요 |
| 자동 채점 연결·평가 기록 저장 | 대기 | — | 1번의 실행 종료 전달 지점, 2번의 컨테이너 실행 방법 필요 |
| 비교 실험 | 미착수 | experiments/ (예정) | 통합 이후 |

## 3. 모듈별 입력과 출력

**`aggregate(plan, results, *, sensitive_values=())`**
검사 계획(`CheckPlan`)과 결과(`CheckResult`)를 받아 상태별 개수, 필수 통과율, 확인된 실패 ID,
미검증 ID(필수만, 선택은 `optional…` 목록), 검사별 행을 돌려준다. 결과가 없으면 NOT_RUN, 증거 없는 PASS·FAIL은 ERROR,
승인 없는 적용 제외는 ERROR로 바꾼다. 필수 검사에 ERROR·NOT_RUN이 있으면 UNVERIFIED다.

**`aggregate_report(plan, results, *, expected, observed, sensitive_values=())`**
Manifest가 같을 때만 `aggregate`를 적용한다. 다르거나 없으면 결과를 쓰지 않고 UNVERIFIED와
`manifestCheck`(오류 코드, 다른 필드 목록)를 남긴다.

**`build_evaluation_report(...)`** (독립 평가, 기본 경로)
`evaluation_id`, `source_run_id`, `architecture`, suite 이름·버전·SHA-256, 시간대 포함 `created_at`,
필수 요구사항 범위, Manifest, 계획·결과, `EvaluationBinding(requirement_id, perspective, title)` 대응표를 받는다.
요구사항마다 필수 검사가 하나 이상 있어야 하며, 한 요구사항에 검사를 여러 개 연결할 수 있다.
출력은 `INDEPENDENT_EVALUATION` 보고서이고 `evaluationVerdict`는 Orchestrator의 `finalVerdict`와 별개다.

**`build_report(...)`** (에이전트 호환, 보조 경로)
실제 A2A Task·Artifact ID가 담긴 context, 필수 검사만의 계획, `TestBinding` 대응표, 도구 실행 근거를 받는다.
ERROR·NOT_RUN을 UNVERIFIED로 바꾸고 원래 상태·사유는 `details`에 보존한다.
SECURITY는 요구사항당 결과 1개만, NOT_APPLICABLE은 거부한다.

모든 함수는 테스트·도구를 실행하지 않고, 증거 URI를 열거나 ID 등록 여부를 조회하지 않는다.

## 4. 코드 검토 결과 (2026-10-06)

검토에서 확인한 항목과 처리 상태다. 상세는 명세 15.2절.

| 우선 | 항목 | 조치 |
|---|---|---|
| 높음 | SEC-SIGNUP-004·006·007에 연결할 요구사항이 없다. | OPEN-12 결정 후 보호 테스트 대응표 작성 |
| 높음 | 에이전트 호환 경로에서 "도구 정상 종료 + 결과 파싱 실패"는 보고서 생성 오류가 된다(dev 계약과 같은 규칙). 독립 평가 경로는 해당 없음. | OPEN-13 결정 |
| 완료 | `confirmedFailureIds`·`unverifiedTestIds`에 선택 검사가 섞이던 문제 | 필수만 담고 선택은 `optionalConfirmedFailureIds`·`optionalUnverifiedTestIds`로 분리 |
| 완료 | 가상환경 확인 테스트가 기본 pytest 대상이라 컨테이너에서 실패하던 문제 | `pytest.ini` testpaths를 `tests`로 변경, 환경 확인은 `pytest checks` |
| 참고 | 민감값 치환은 진단 필드에만 적용된다(제목·suite·findings·toolEvidence 제외). | 호출자가 사전 정리 |
| 참고 | `created_at`은 Python 3.10 `fromisoformat` 기준이라 일부 ISO 표기를 못 읽는다. | `YYYY-MM-DDTHH:MM:SS+09:00` 사용 |

## 5. 팀원 정보 상태

| 담당 | 받은 정보 | 남은 정보 |
|---|---|---|
| 1번 | ID 규칙, Artifact·Manifest, 수정 3회·도구 재시도 2회, 보고서 수신·판정 코드(dev 확인) | 실행 종료 시 최종 Snapshot·작업공간·finalVerdict 전달 지점, Single 실행 진입점, Run Configuration 위치 |
| 2번 | — | 보호 테스트를 컨테이너에 넣어 실행하는 방법, testScope·scannerProfile 값, read_test_report/read_security_report 결과 JSON |
| 3번 | `POST /api/auth/signup`(필드 email·password), 응답 201/422/409/500, 실행 방법(127.0.0.1:8000), `backend/app.db`의 `users` 테이블, `schemas.py` 정규식 이메일 검증, pwdlib Argon2 (`3번필요사항중간정리본`, `sehwa-dev`) | 테스트 DB 경로 분리, 데이터 초기화, 서버 로그 수집 |

3번 구현과 개발정의서의 차이(이메일 공백, canonical_email 컬럼, Argon2 수치, 동시 가입 응답)는 OPEN-01·02 안건이다.

## 6. 검증 기록

| 날짜 | 결과 |
|---|---|
| 2026-10-06 | 수정 후 기본 pytest 285개 통과(평가 113 + dev 172), `pytest checks` 2개 별도. 주석 정리 전후 의도한 수정(필수·선택 목록 분리) 외 AST 동일. 공통 스키마 사본 4개와 dev 원본 동일. |
| 2026-10-06 | dev 실제 보고서 모델이 진단 정보를 포함한 합성 보고서 8개 수용 |
| 2026-10-05 | dev 공통 환경 통합 후 pytest 263개, 기존 unittest 172개 통과 |
| 2026-10-04 | 평가 wheel 빌드, 작업 폴더 밖에서 스키마 포함 import·보고서 생성 확인 |

모델 수용은 A2A 전송·실제 서비스 실행 검증을 뜻하지 않는다. 예시 출력은 모두 mock이다.

## 7. 다음 작업

1. 팀 회의: 독립 평가 구조(OPEN-11), SEC-004·006·007 연결(OPEN-12), 파싱 실패 처리(OPEN-13), 3번 구현 차이(OPEN-01·02).
2. 회원가입 보호 테스트 작성과 3번 서버 로컬 실행. 고장 낸 버전으로 FAIL 검출 확인.
3. pytest 결과 → `CheckResult` → 독립 평가 보고서 로컬 연결.
4. 1·2번 정보 수령 후 자동 채점 연결과 평가 기록 저장.
5. Single/Multi 비교 실험과 발표 근거.

## 8. 변경 이력

| 날짜 | 변경 |
|---|---|
| 2026-10-04 | 초기 집계·Manifest 비교, dev 호환 해시 형식 검사 |
| 2026-10-04 | 환경/명세서/개발진행사항 문서 구조 정리 |
| 2026-10-04 | 공통 보고서 어댑터·명시적 ID 대응·도구 근거 검증·합성 예시 추가 |
| 2026-10-04 | 평가 패키지·lock·스키마 동봉, dev 모델 호환 확인 |
| 2026-10-05 | 최신 dev `2b4fc60`에서 `seokmin`을 새로 만들고 평가 코드를 `src/evaluation/`로 이동. 루트 pyproject·uv.lock·.venv 하나로 통일(평가 전용 설정 제거). 기존 Orchestrator 코드·문서·테스트·의존성 버전 유지 |
| 2026-10-06 | 독립 평가 보고서(`independent.py`) 추가, 검사별 진단 정보와 알려진 민감값 치환 추가, 개인 PyCharm 소스 루트를 `src`로 수정 |
| 2026-10-06 | 평가 코드·테스트 주석 정리, 코드 검토 결과 기록, 문서를 독립 평가 구조와 명세 0.3에 맞게 재구성 |
| 2026-10-06 | 필수·선택 실패/미검증 ID 분리(`optional…` 필드 추가), 환경 확인 테스트를 기본 pytest 대상에서 분리, 주석 간결화 |
