# 개발진행사항

최종 갱신: 2026-10-31 · 평가 명세 0.4 · 작업 브랜치 `seokmin` (dev `08542c6` 병합 + 평가 변경)
공통 계약 기준: dev `caa414432af52f838ef5b62c993246b48013178c` (dev `08542c6`까지 schemas·Orchestrator·Agent·MCP 코드 변경 없음, 2026-10-28 확인)

이 문서는 4번 평가 모듈의 **현재 상태**를 정리한다. 평가 기준·정책은 [명세서](evaluation_spec.md),
요구사항별 검사는 [기준표](scn001_criteria.md), 설치·실행은 [환경 설정](environment_setup.md)에서 관리한다.
팀장 전달용은 [dev 변경 공유](dev_changes_2026-10-30.md), Single 실행기(주호 개발)를 기다리는 Single 비교 설계는 [Single 비교 기준선](single_baseline.md),
시연 자료 정리는 [시연 자료](demo_materials.md)에 있다.

## 1. 평가 구조 요약

4번의 최종 독립 평가는 개발 순환(Planner → Developer → QA/Security Agent → 수정) **밖에서** Agent 생성물을 채점한다.
순환 안 QA·Security Agent는 2번이 만들었고, 10.29 회의 뒤 4번이 정한 평가 방식(QA·Security 같은 실행 방식)에 맞춰 Security도 QA처럼 자기 테스트·순환용 보호 테스트를 실행해 측정 결과로 판정하도록 4번이 dev를 고쳤다(회의록의 "석민: 테스트에 필요한 사항은 dev 코드에서 수정" 범위, 2절 dev 행). 4번의 D1 `assess()`는 최종 평가의 보조 점검이다.
10월 29일 회의록(기업 미팅 사전 논의)은 Single vs Multi 비교 실험 방법으로 **방안 1 = 순환 테스트 + 최종 테스트(같은 검증 기준을 충족한 결과에 도달하는 시간이 주 지표)**, **방안 2 = 최종 테스트 중심(정확도·소요 시간)**을 "방식 검토 중"으로 적고 기업 의견을 듣기로 했다. 이후 4번의 개발 방침으로 두 방안을 먼저 멀티에이전트에 적용해 **Ver1 = 순환 테스트 + 최종 테스트**, **Ver2 = 최종 테스트만(순환용 보호 테스트 없음, 에이전트 자체 검사는 있음)**으로 나눴다. 여기서 순환 테스트는 평가 쪽이 순환 안에 넣는 보호 시험지다. Ver1·Ver2 비교는 멀티에이전트 안에서 순환용 보호 테스트를 준 효과를 보여 주며, Single 대비 Multi의 효과는 아니다. Single vs Multi는 같은 방안 안에서 구조별로 비교한다(`experiment compare --group-by architecture`, 명세 4.1절).
채점용 시험지(pilot·final)는 에이전트에게 주지 않고, 시작 코드·채점 Snapshot에 시험지·평가 코드·결과 파일이 섞이면 설정을 만들거나 채점하지 않는다(`exam_exposure`). 그 밖의 비공개(저장소 접근 권한 등)는 운영으로 지킨다. 3번(세화)의 backend는 서비스 웹페이지 자체의 백엔드이며 평가 대상(에이전트가 만든 생성물)이 아니다.

팀장 카톡(2026-10-14) 기준 4번의 일: ① QA·Security 평가 기준과 검사 항목 정리(미팅에서 논의), ② 평가 모듈에 실행 정보·결과물 정보 연결 준비,
③ 시연 실행 로그·생성 파일·화면 정리(12월 발표용, 팀장에게 전달), ④ 실제 테스트와 Single/Multi 비교는 미팅 이후 진행할 수 있게 준비.
10월 22일 텔로스 자문에서 "설계 문서는 줄이고 실제 동작하는 파이프라인을 우선"하라는 의견을 받았고, 다음 회의는 11월 2일(텔로스)이다. 19일 주 팀 회의의 결정 기록은 남아 있지 않다.
10.29 회의록에 적힌 것: UI/UX 개편(3번 쪽), 비교 실험 방안 1·2(방식 검토 중), 생성 코드 규격과 규격 정의 Agent 도입 여부(논의 중·기업 의견 수렴 예정), 기타 개발로 **석민 = Snapshot 인계 방식 등 남은 테스트에 필요한 사항을 dev 코드에서 수정**, **주호 = 싱글 에이전트 개발**. 회의 뒤 정한 4번 방침: 멀티에이전트 평가를 먼저 완성(Single 실행기는 주호가 개발하고 4번은 평가 기준·비교 집계를 맡음), 위 Ver1·Ver2, QA·Security 같은 실행 방식, 시작 코드는 "시작 코드 규격을 정의하는 에이전트" 여부를 기업 팀장님과 논의한 뒤 정함.

| 관점 | 요구사항 | 검사 (기준표) |
|---|---|---|
| QA | REQ-001·002·003·004·007 | QA-SIGNUP-001~006 (실행) |
| SECURITY | REQ-003·005·006 | SEC-SIGNUP-002·003·005 (실행, QA와 같은 방식). 정적 판정 `STATIC-REQ-003·005·006`은 최종 보조 점검(점수 제외) |
| Orchestrator | REQ-008 | 독립 평가 보고서 밖. D4 `trace_checks`가 Run 기록으로 요구→개발→검증→수정→재검증 연결을 확인 |

관점별 요구사항은 dev `scenario_registry.py`의 SCN-001 등록 정보와 같다.

## 2. 현재 상태

| 기능 | 상태 | 파일 | 범위 |
|---|---|---|---|
| 검사 집계 | 구현 | aggregation.py | 필수·선택 분리, 누락·중복·증거 없는 판정·승인 없는 제외 처리, 실패와 미검증 동시 보존, 진단 정보·민감값 치환 |
| Manifest | 구현 | manifest.py | 형식 검사, 기준·관측 불일치 시 결과 미사용·UNVERIFIED·통과율 null |
| 독립 평가 보고서 | 구현 | independent.py | 평가 ID·대상 Run 분리, suite 해시, 요구사항 범위 검사, 요구사항별·관점별 요약, 검사·요구사항 통과율 분리, 대상 Run 기록(`sourceRun`), 스키마 검증 |
| 에이전트 호환 보고서 | 구현(보조) | reporting.py | dev QA_REPORT/SECURITY_REPORT 변환, ID 대응표·도구 실행 근거·재시도 규칙·스키마 검사 |
| 보안 판정(D1) | 구현, 보조 점검으로 전환(10.30) | security/ | 기준 JSON + `assess()`, REQ-003·005·006 정적 판정. 점수·판정에는 넣지 않고 D5 최종 평가의 보조 점검(`STATIC-REQ-*`, 실행 검사가 지나가지 않은 코드 경로)으로만 쓴다. 순환 안 Security는 실행 테스트로 판정(dev 수정, 아래 dev 행). 작업량·재귀 한도 초과는 `ANALYSIS_TOO_COMPLEX` UNVERIFIED |
| 정상·결함 예제(D3) | 구현 | tests/evaluation/signup_examples.py | 정상 1 + 결함 18 + 정상 변형 9. 합성 소스이며 실제 생성물이 아님 |
| 기준표(D0) | 결정 반영 | scn001_criteria.md | 요구사항별 검사·증거·판정 조건. 2026-10-28 정의서·dev 정책·회의 기록을 대조해 결정 사항(Argon2 "이상", 정규화, 정적 PASS 범위 등)과 D2 판정 세부를 기록. 생성물 규격 채택·MEDIUM 추가 차단만 팀 결정 대기 |
| 결과 수집·지표(D4) | 구현(합성 입력) | run_metrics.py | dev `/runs/{id}`·events·issues·tool-attempts·usage·configuration JSON → 지표(사건 시각 차이로 시간, 비용·토큰 미확인은 None), 구조별 요약(성공률 분모=전체 계획 Run), 비교 가능 여부 검사(모든 Run 대조, 환경 digest 불일치는 해당 Run 재실행, 평가 보고서의 환경 지문도 그 Run의 동결 환경과 대조, warm-up 차이는 주의). 문제 해결 수는 재검증 PASS만 세고, 평가 보고서의 코드 버전이 Run의 최종 코드 버전과 다르면 요약에서 거부. 실제 Run으로 검증하지 않음 |
| 시연 자료 정리(D4) | 구현 | demo_index.py | 로그·생성 파일·화면·보고서 목록과 파일별 SHA-256(청크 단위), 빈 분류 표시, 심볼릭 링크·특수 파일(FIFO·소켓)·알 수 없는 폴더 거부. 256MiB 이하 텍스트의 알려진 비밀값을 끝까지 검색하고 `sensitiveScan`에 완료·생략 및 이유를 기록 |
| 공유 문서 | 작성 | dev_changes_2026-10-30.md, single_baseline.md | 팀장 전달용 dev 변경 정리, Single 비교 기준선(Single 실행기 대기). 예전 보안 판정 기준 한 장(share_security_criteria.md)은 내용이 기준표·이 문서와 겹쳐 2026-10-30 삭제 |
| 회원가입 실행 검사(D2) | 구현(합성 서비스로 검증) | protected_tests/ | 실행 검사 9개(QA 6·Security 3)와 시험지 세 벌(순환용 practice·시범 최종용 pilot·공식 최종용 final, 같은 검사·같은 입력 종류, 다른 값·모양·순서). 검사 내용은 `signup_qa_checks.py`·`signup_security_checks.py`, 연결부는 `signup_link.py`. 정적 판정이 보던 곳을 보완: 앱 코드의 info·debug 로그 수집, DB 오류 주입, 형식이 틀린 본문, base64·16진수 노출 검색, 거부 뒤 정상 가입 순서, DB 직접 쓰기로 UNIQUE 확인. 순환 안 범위는 역할별 두 개(dev 설정 크기 약 20KB씩). 합성 서비스와 변형 47개를 읽기 전용 Snapshot으로 세 벌 모두 돌려 같은 검사만 FAIL(합성 결함 기준의 난이도 확인). 실제 생성물로는 아직 안 돌림 |
| 결과 어댑터 | 구현 | check_adapters.py | unittest-v1 보고서 → CheckResult. 보고서 없음·형식 오류·대응표 밖 줄은 모두 미검증. `protected_tests.final_check_results`로 독립 평가 보고서까지 연결(정적 판정 변환 함수는 남겨 둠) |
| Trace 검사·세부 지표(D4) | 구현(실제 연결 남음) | trace_checks.py, run_metrics.py | Run 기록의 비밀번호·해시 노출(REQ-006 Orchestrator 부분, 검사 범위 `coverage` 표시), 요구→개발→검증→Issue→수정→재검증→판정 연결(REQ-008), Regression 계산(버전별 보고서 연결 `regression_from_reports`), 협업 시간(역할별 LLM·도구 시간, 전달 지연, 시도·재개 구간별 에이전트 활동·병렬 겹침·에이전트 밖 시간, 사람 대기). dev 파이프라인 테스트의 실제 Run 6개로 확인. A2A Message 본문은 dev `a2a_export`로 꺼내 넘기면 함께 봄(2026-10-30, dev 파이프라인 Run에서 오탐 없음 확인). 남음: 알려 준 비밀값을 넘길 경로, 버전별 평가에서 Regression 자동 산출 |
| 최종 독립 채점기(D5) | 구현(실제 컨테이너 실행 전) | final_evaluation.py, coverage_runner.py, scoring.py, summary.py | Snapshot tar 해시·lock·이미지 해시 확인 → 안전하게 풀기 → 채점 시험지(pilot·final 중 선택) 9개를 컨테이너(`DockerRunner`)에서 실행 + 순환용 시험지 참고 채점(차이의 차이 보조 지표용) + 보조 점검(지나간 줄 기록, 정적 판정) → 독립 평가 보고서·점수(요구사항별 같은 비중·검사 완료율·확인된 보안 위반), Run 기록 검사, 회의·발표용 요약. 가짜 docker·합성 서비스로 검증. 최종 Snapshot·Manifest는 dev `snapshot_export`로 꺼내 넘긴다. 결과는 `experiment score`가 저장소 `experiments/`에 보관. 남음: 평가 이미지, 실제 컨테이너 실행 |
| Ver1·Ver2 비교(10.29 회의 뒤 방침) | 구현(합성 입력) | run_metrics.py, scoring.py | `comparison_validity(..., group_by='loopPolicy')`·`experiment_summary(..., group_by='loopPolicy')`: 같은 Multi 구조에서 보호 테스트 참조(`protectedTestSuiteRef`)만 달라야 하고(이름표는 동결 설정과 대조), 쌍(`pairId`)마다 Ver1·Ver2가 하나씩, 같은 최종 시험지. 정책별 평균 점수·완료율·보안 위반 Run 수, 같은 품질 기준(최종 독립 평가 PASS) 달성 시간·비율(`qualifiedVerdictMs`·`qualifiedRate`), `loop_policy_effect`로 차이의 차이(보조 지표). Single/Multi 구조별 비교는 같은 방안 안에서(`compare --group-by architecture`) |
| dev: Security 실행 테스트(10.30, 4번이 dev 수정) | 구현(dev 시험으로 검증) | mcp_tools, agents/runtime, agents/platform, orchestrator | 테스트 범위에 역할(`role: "SECURITY"`) 표시, Security에 `write_test_file`(`outputs/security/tests`)·`run_unit_tests`, Security 단위 테스트 실행 기록·Snapshot 읽기 권한, Security 출력 계약의 `testCases`, Host가 Security 자체·보호 테스트를 QA와 같은 방식으로 실행하고 측정 결과로 요구사항 판정(정적 증명과 함께 쓰지 않음), 보호 케이스 파일 하나를 범위의 역할대로 QA·Security에 나눔, Security 보고서·Orchestrator가 `run_unit_tests` 증거를 받음. Security 범위가 없으면 예전 정적 증명 방식 그대로. dev 파이프라인 시험에서 Security 테스트 통과로 Run SUCCESS, 실패로 Security Issue 확인 |
| 실험 도구·시험지 노출 확인(10.30) | 구현(합성 입력) | experiment.py, protected_tests(`exam_exposure`), final_evaluation.py | `configure`(Ver1·Ver2 Host 설정 쌍, 시작 코드 커밋에 시험지·평가 코드가 섞였으면 거부), `fetch`(Run 기록), `score`(내보낸 Snapshot을 D5로 채점해 Run별 보관, `--loop-policy`로 계획상 버전 기록), `record`(채점 못 한 Run의 기록만 보관), `compare`(채점 여부와 상관없이 모든 Run으로 Ver1·Ver2 비교·차이의 차이·Regression 합계), `score --all-versions`로 버전별 채점과 Regression 계산. 결과는 저장소 `experiments/scn001/<시험지>/<날짜-이름>/`에 커밋(내보낸 tar 제외, 결과 파일이 시작 코드·Snapshot에 섞이면 `RESULTS_PATH`로 거부). D5는 채점할 Snapshot에 시험지와 같은 내용의 파일이나 평가 코드 경로가 있으면 실행·점수를 만들지 않음(`EXAM_EXPOSED`). 일부만 베낀 경우는 찾지 못함 |
| Single 비교 실험(D6) | 평가 쪽 준비됨, Single 실행기 대기 | single_baseline.md, experiment.py | Single 실행기 개발은 주호(10.29 회의록). 4번은 평가 기준·비교 집계: `experiment compare --group-by architecture`로 같은 방안 안에서 Single·Multi를 비교(보호 테스트 조건이 같아야 유효). dev `RunConfiguration`은 아직 `MULTI_AGENT`만 받으므로 Single 실행 경로는 주호의 dev 작업 |

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
HUMAN_REVIEW는 재개될 수 있어 종료 시각을 기록하지 않는다. `sourceRun.startedAt`은 Run 응답의 `createdAt`이므로 소요 시간은 생성~종료 기준이다. 연결 필수 검사가 모두 승인 N/A인 요구사항은 충족률 분모에서 제외하며 적용 요구사항이 없으면 null이다.

**`build_report(...)`** (에이전트 호환, 보조 경로)
실제 A2A Task·Artifact ID가 담긴 context, 필수 검사만의 계획, `TestBinding` 대응표, 도구 실행 근거를 받는다.
ERROR·NOT_RUN을 UNVERIFIED로 바꾸고 원래 상태·사유는 `details`에 보존한다.
SECURITY는 요구사항당 결과 1개만, NOT_APPLICABLE은 거부한다.

**`security.assess(request)` / `security.evaluate(request)`**
dev 42번 `SecurityAssessmentRequest`를 받아 REQ-003·005·006을 소스 정적 분석으로 판정한다. 생성 코드는 import·실행하지 않는다.
모르는 구조는 UNVERIFIED, 허용 증거 참조가 없으면 UNVERIFIED로 내린다. `assess`는 내부 오류를 `None`으로 돌려주므로
원인을 보려면 `evaluate`를 직접 호출한다. 작업량·재귀 한도 초과는 해당 요구사항의 UNVERIFIED로 처리한다. 증거 JSON은 UTF-8 64KiB 한도이며 경고 판정과 근거를 함께 제한하고 `omittedFindings`를 기록한다. 요구사항 근거 자체가 한도를 넘으면 모든 요구사항을 UNVERIFIED로 내리고 `EVIDENCE_LIMIT_EXCEEDED`를 남긴다.

**`run_metrics.fetch_run_bundle(get, run_id)` / `run_metrics(bundle)` / `experiment_summary(runs, planned_runs, evaluations)` / `comparison_validity(entries)`**
Run 조회 JSON 수집(페이지 이어 받기), 한 Run의 지표 계산, 구조별 요약, 두 구조 비교 가능 여부 검사.
측정하지 못한 값은 0이 아니라 None이고, 성공률 분모는 시작하지 못한 Run을 포함한 전체 계획 Run이다.
모델 호출 수는 전체 측정이 있을 때만 `modelCalls` 합계로 표시하며, `knownModelCalls`는 확인된 부분 합, `modelCallsComplete`는 완전성이다. 놓친 결함 수는 연결된 Run의 SUCCESS와 독립 평가의 필수 실패로 계산하므로 보고서의 선택 `sourceRun` 생략에 영향받지 않는다.
API 오류는 즉시 수집 중단, 선언된 사건 수 불일치는 `EVENTS_INCOMPLETE`, 전체 수 미확인은 `EVENTS_COMPLETENESS_UNKNOWN`이다. 뒤의 두 경우 사건 기반 시간은 None이다. 실행 시간은 RUN_STARTED 사건부터 계산하며 Run 생성 시각 기준과 구분한다.

**`protected_tests` (D2)**
`suite.json`(검사 9개·시험지 세 벌·범위 다섯 개)과 보호 테스트 파일을 읽어 dev 설정(역할별 `unit_test_scope`, `owned_cases(contract, role)`)과 최종 평가 입력(`evaluation_inputs(contract, exam)`, `final_check_results`)을 만든다. `python -m evaluation.protected_tests export`가 Ver1용 파일 세 개(보호 케이스 하나, 보호 범위 두 개, 인계 정보)를 만든다.

**`check_adapters.check_results_from_unit_report(report, cases, *, evidence_id)` / `check_results_from_assessment(...)`**
unittest-v1 보고서·정적 판정 결과를 CheckResult로 바꾼다. SKIP("UNVERIFIED:코드")·누락·형식 오류·대응표 밖 줄은 미검증이다.

**`trace_checks.check_trace_exposure(documents, criteria, known_values=...)` / `check_traceability(bundle, contract)` / `fetch_trace_documents(get, run_id)`**
Run 기록의 비밀번호·해시 노출(REQ-006 Orchestrator 부분)과 요구→개발→검증→Issue→수정→재검증→판정 연결(REQ-008). 결과에는 위치·근거 코드만 남긴다. 노출 결과의 `coverage`는 읽은 기록·받지 못한 기록·보지 못하는 출력(A2A Message 본문·Artifact 파일 내용·에이전트 프로세스 출력)·알려 준 값 개수다. PASS는 부분 검사 결과이지 REQ-006 전체 충족이 아니다.

**`run_metrics.collaboration_metrics(events)` / `regression_count(version_results)` / `regression_from_reports(reports)`**
역할별 LLM·도구 시간, 전달 지연, 에이전트 활동(병렬 구간은 한 번, 시도·재개 구간별로 끊어 사람 대기는 제외)·병렬 겹침·에이전트 밖 시간, 사람 대기. 버전별 결과의 PASS→FAIL Regression(같은 Run·같은 보호 묶음의 버전별 보고서에서 바로 계산).

**`final_evaluation.evaluate_final_snapshot(*, archive, expected, scenario_contract, source_run_id, runner, exam, ...)`** (D5)
Snapshot tar 해시·lock·이미지 해시 확인 → 안전하게 풀기 → 채점 시험지(`exam`: pilot·final 중 반드시 선택) 실행 + 순환용 시험지 참고 실행 + 줄 기록 실행(`DockerRunner`, dev 샌드박스와 같은 제한) + 정적 판정(보조) → 독립 평가 보고서·점수(`score`)·참고 보고서(`referenceReport`)·보조 점검(`supplementary`)·증거·Trace 검사. 신분을 하나라도 확인하지 못하면 결과를 판정·점수에 쓰지 않는다. `LocalRunner(trusted=True)`는 합성 표본 시험용이다. `known_values`는 노출 검사로 넘기고, 결과의 `trace`에는 Run 번호, `runner`에는 평가 번호를 붙인다.

**`summary.render_summary(report, ...)`**
독립 평가 결과·실패→수정→재검증 흐름·지표·Regression·노출 검사 범위를 회의·발표용 Markdown으로 만든다. 에이전트가 쓴 자유 글은 넣지 않는다. 함께 넣는 자료가 보고서와 다른 Run·코드 버전·구조·평가의 것이거나 출처가 없으면 만들지 않는다.

**`demo_index.build_demo_index(root, *, run_id, created_at, ...)`**
`logs/source/screens/reports` 폴더의 파일을 해시와 함께 목록으로 만든다.

테스트를 실행하는 곳은 D5 실행기뿐이고 실제 생성 코드는 컨테이너에서만 실행한다. 나머지 함수는 이미 나온 결과를 다루며, 네트워크로 무언가를 보내거나 증거 URI를 열거나 ID 등록 여부를 조회하지 않는다.

## 4. 코드 검토 결과와 알려진 제약

2026-10-06~2026-10-28에 검토·보완한 항목이다. 상세는 명세 15.2절.

| 우선 | 항목 | 상태 |
|---|---|---|
| 결정 필요 | 에이전트 호환 경로에서 "도구 정상 종료 + 결과 파싱 실패"는 보고서 생성 오류가 된다(dev 계약과 같은 규칙). 독립 평가 경로는 해당 없음. | OPEN-13 |
| 완료 | 전체 점검의 보안 오판 3건: 정규화 후 원문 복원·무관한 함수, logging 로거의 임의 변수 이름, `extra="allow"` 응답 필터 신뢰. 등록된 이메일 검증기의 반환 경로와 로거 생성·별칭, 응답 모델의 설정·직렬화를 확인하도록 보완 | 2026-10-17 |
| 완료 | 승인 N/A 요구사항을 충족률 분모에서 제외. 놓친 결함은 선택 `sourceRun` 대신 실제 Run과 확인된 필수 실패로 계산. 전체 모델 호출 수는 계획된 모든 Run 측정 시에만 합계, `knownModelCalls`·`modelCallsComplete`로 부분 합 구분. 사건 수 미확인 시 모든 사건 기반 시간은 None | 2026-10-17 |
| 완료 | 증거 JSON UTF-8 64KiB 한도에서 경고 판정·근거를 함께 제한하고 `omittedFindings` 기록. 요구사항 근거만으로 한도를 넘으면 `EVIDENCE_LIMIT_EXCEEDED`·모두 UNVERIFIED. 시연 텍스트는 256MiB 이하 전체 검색, 검사 생략은 `sensitiveScan`으로 구분 | 2026-10-17 |
| 완료 | D1이 흔한 해시 코드를 잘못 판정하던 문제: FastAPI 튜토리얼 방식(`password_hash = PasswordHash.recommended()`)이 모델의 같은 이름 칸 때문에 미검증, passlib Argon2가 승인 안 된 해시로 FAIL. 클래스 속성 정의를 이름 덮어쓰기로 보지 않게 하고 passlib Argon2를 승인 목록에 추가(설정·type·salt 판정), bcrypt 기본 `CryptContext`·`scheme="bcrypt"`는 FAIL, 프로젝트 안 가짜 라이브러리 파일은 승인 안 함 | 2026-10-28 |
| 완료 | 외부 점검(2026-10-29) 4건과 같은 종류: ① 대문자·공백 이메일 재가입이 500이어도 QA-SIGNUP-003 PASS(최종 PASS) → 4xx 거부를 요구, 같은 종류로 동시 가입의 나머지 응답(QA-SIGNUP-005)도 4xx 요구 ② 첫 응답이 이미 끝 상태인 A2A Step의 활동 시간 0초 ③ 입력·인증 대기 뒤 재개한 Step에서 사람 대기 시간이 에이전트 활동에 포함 → 시도·재개 구간별로 끊음(다른 에이전트의 동시 작업은 유지) ④ 요약에 다른 Run의 흐름·비용이 섞임 → Run·코드 버전·구조·평가 번호 대조. 연결되지 않던 기능도 연결: 최종 채점기의 `known_values`, 노출 검사 범위 표시, 버전별 보고서 → Regression. 새 시험은 수정 전 코드에서 실패함을 확인. 실제 Run 표본의 협업 시간은 수정 전후 같음 | 2026-10-29 |
| 완료 | D2 자체 점검에서 막은 구멍: 가입 경로가 다르면(404·405) 거부 검사가 거저 PASS, 모든 입력을 422로 거부하는 서비스에서 형식 오류 검사가 거저 PASS(정상 가입 전제 조건 추가), 자체 handler를 단 로거(`propagate=False`) 누락, 보고서 설명 글의 해시 형식 이름을 노출로 오인, 실패 메시지에 salt 값이 찍힘, 동시 가입 스레드의 원인 코드 손실 | 2026-10-28 |
| 완료 | 필수·선택 실패/미검증 ID 분리, 가상환경 확인 테스트를 기본 pytest에서 분리 | 2026-10-06 |
| 완료 | Manifest 불일치 시 통과율 0.0 → null, 검사별 errorCode를 Manifest 오류로 기록 | 2026-10-14 |
| 완료 | D1 오탐(`passed`·`MIN_PASSWORD_LENGTH`·`exc.errors()` 변수 처리·pydantic 정규화·`CREATE UNIQUE INDEX`)과 미탐(요청 모델 통째 로깅·`model_dump()`·`sys.stderr.write`·`__dict__` 응답·해시 호출 후 원문 반환·무관한 컬럼 UNIQUE) | 2026-10-14 |
| 완료 | Bandit CONFIRMED는 코드 근거가 있을 때만(필드 이름 문자열 `"password"`는 오탐), 파싱 중 `RecursionError`는 해당 파일만 분석 불가로 기록, `await` 해시 호출 인정 | 2026-10-14 |
| 완료 | `missedDefect`가 평가 UNVERIFIED일 때 확인된 FAIL을 놓치던 문제, HUMAN_REVIEW를 종료로 처리하던 문제 | 2026-10-14 |
| 완료 | `comparison_validity`가 구조당 보고서 하나만 비교해 입력 순서에 결과가 달라지던 문제(모든 Run 대조) | 2026-10-14 |
| 완료 | 외부 점검에서 찾은 "결함인데 PASS" 4건(해시 변수 재대입·가짜 `.hash()`, 다른 키로 나가는 입력값, 복합 UNIQUE, 하드코딩 자격증명 오탐 해제) | 2026-10-14 |
| 완료 | 비교 유효성 검사가 구조 이름표·평가 보고서의 Run 번호·필수 조건 누락을 허용하던 문제, 동결 구조를 인자로 덮어쓰던 문제, 계획에 없는 구조의 Run이 요약에서 사라지던 문제 | 2026-10-14 |
| 완료 | 외부 점검(2026-10-15) "결함인데 PASS" 4건과 같은 종류의 변형: 같은 이름의 매개변수·재정의·덮어쓰기로 가짜 해시 도구를 승인 도구로 오인, `**설정`·`*값`으로 숨긴 약한 해시 설정을 기본값으로 대체, 글자와 칸 객체를 섞은 복합 UNIQUE, 일부 항목에서만 입력값을 지우는 반복문. 이제 "모든 경우에 성립한다고 증명할 수 있을 때"만 PASS이고 나머지는 UNVERIFIED(또는 FAIL) | 2026-10-15 |
| 완료 | 같은 점검의 연장으로 직접 찾아 막은 구멍: 해시 함수·검증 오류 처리기의 **함수 이름**을 람다·데코레이터·재대입·같은 이름의 매개변수로 바꿔치기, 검증 오류 처리기를 람다·`exception_handlers`·FastAPI 생성자로 등록해 분석을 건너뛰기, 예외 객체(`exc`) 자체를 응답·로그에 쓰기(`str(exc)` 등), 비밀번호가 변수·하위 클래스·별칭·요청 본문(`request.json()`)·해시 변수를 거쳐 로그로 새기. 정상 코드를 FAIL로 만들지 않는 확인 8개를 함께 두었다 | 2026-10-15 |
| 완료 | 외부 점검(2026-10-16, 6차) 2건: 정상 코드에 `unused = payload`, `unused = db`를 연속으로 넣으면 판정기가 끝나지 않음, 비밀번호 해시를 계산만 하고 버린 뒤 이메일 해시를 돌려주거나 저장해도 통과. 별칭이 가리키는 곳을 늘리기만 하는 방식(한도 있음)으로 바꿔 반드시 끝나게 하고, 함수가 실제로 돌려주는 해시만 증거로 삼음. 같은 종류로 직접 찾아 막은 구멍: 별칭을 만드는 방식 19가지 × 입력 필드를 쓰는 방식 20가지(`__dict__`·`vars()`·`object.__setattr__`·`update`·`pop` 등)의 조합, 메서드가 `self`를 덮어쓰는 경우, 도우미 함수 3단 이상, 승인한 해시 도구를 `__dict__`·`__class__`·`globals()`로 바꿔치기. 시간 문제 두 가지도 고침: 이메일 `strip().lower()`가 많을 때 시간이 제곱으로 늘던 것(500개에 2.9초 → 0.2초, 2,000개는 12초 안에 끝나지 않던 것이 0.6초), 3,000항 덧셈에서 재귀 한도에 걸리던 것 | 2026-10-16 |
| 완료 | 외부 점검(2026-10-16, 5차) 2건: 이름만 password인 변수에 이메일을 담아 해시, 이메일만 돌려주는 함수의 결과를 해시해도 통과. 해시하는 값은 변수 이름·인자 이름이 아니라 실제 출처((입력 이름, 필드 경로))로 확인하고, 입력 필드를 덮어쓴 뒤의 해시·`self` 필드·튜플 교환도 막음 | 2026-10-16 |
| 완료 | 외부 점검(2026-10-16, 4차) 11건: 입력을 버리는 함수·`*= 0`·잘라내기를 거친 값을 입력의 해시로 인정, 이름만 같은 가짜 `os.urandom`, 비밀번호 대신 이메일 해시, SQL 문자열 속 `DEFAULT 'unique'`, 다른 계정 표(보관용)의 UNIQUE, `response_model=None`·응답 객체 직접 반환, 이전 코드 버전의 평가를 최종으로 집계, 수정 요청을 해결로 셈, WARM/COLD 차이 누락, FIFO에서 수집이 멈춤. 해시 입력은 허용 목록 방식(입력의 내용을 통째로 간직하는 변환만)으로 바꾸고 SQL은 실제로 실행하는 글자만 칸별로 읽음. 증명하지 못하면 UNVERIFIED | 2026-10-16 |
| 완료 | 외부 점검(2026-10-16) "결함인데 PASS" 4건: `payload.password or ""`처럼 and·or를 거친 비밀번호 노출, 입력이 아니라 변수에 담은 상수를 해시, 고정 salt 지정, `CREATE TABLE other (id INTEGER UNIQUE, email TEXT)`처럼 같은 줄의 다른 칸·다른 표의 UNIQUE를 이메일 중복 방지로 인정. 이제 해시하는 값은 입력(매개변수)에서 나온 것이어야 하고, 고정 salt는 FAIL, 이메일 UNIQUE는 비밀번호 칸이 있는 사용자 표의 이메일 칸 자신의 것만 인정 | 2026-10-16 |
| 완료 | 위 점검의 연장으로 직접 찾아 막은 구멍: 결과를 버리는 이메일 정리, 평문이 저장되는 다른 길(`.values()`·`setattr`·사전·SQL `execute`), 비밀번호가 나가는 다른 출구(`pprint`·`file.write`·헤더·쿠키·`locals()`·람다/안쪽 함수) | 2026-10-16 |
| 완료 | 평가 보고서의 환경 지문(컨테이너 이미지·의존성 lock)을 그 Run의 동결 환경과 대조하지 않던 문제. 어긋나거나 보고서가 신분증 불일치를 밝힌 Run은 `RERUN_RUN`으로 표시 | 2026-10-15 |
| 완료 | wheel에 `security/scn001_security_criteria.json`과 보호 테스트 묶음이 빠지던 문제: `pyproject.toml` package-data에 추가하고 저장소 밖에서 wheel만 설치해 묶음·기준을 읽는 것을 확인 | 2026-10-30(10.29 회의: 4번이 dev 수정) |
| 완료 | 팀 Python 버전 선언(`>=3.10`)과 어긋나던 dev 승인 함수(`security_approval.py`)의 3.11 전용 `co_qualname`·`co_exceptiontable`: 3.10에서는 없는 값을 기본값으로 두고, 안쪽 함수의 이름 위조는 컴파일러의 중첩 함수 표시(`CO_NESTED`)로 막음. 3.11 이상의 해시 값은 그대로. 승인 시험 85개가 3.10·3.13 모두 통과 | 2026-10-30 |
| 참고 | 민감값 치환은 호출자가 알려 준 문자열만, 진단 필드에서만 수행한다. 제목·suite·findings·toolEvidence 제외. | 호출자가 사전 정리 |
| 참고 | `created_at`은 3.10에서도 읽히는 `fromisoformat` 기준이라 일부 ISO 표기를 못 읽는다. | `YYYY-MM-DDTHH:MM:SS+09:00` 사용 |
| 참고 | `assess()`는 내부 오류를 `None`으로만 알린다(dev 승인 계약상 로그·전역 상태 금지). | `evaluate()`로 디버깅 |
| 참고 | D1 이름 판정은 설명 단어(text, name, key 등)가 낀 이름을 비밀번호 값이 아닌 것으로 본다. 정적 분석은 호출 흐름·동시성·런타임을 보지 않는다. | 실행 검사로 보완 |
| 참고 | REQ-006의 PASS는 "지원하는 노출 경로를 찾지 못했다"는 뜻이지 없다는 증명이 아니다. 변수 따라가기는 한 함수 안에서만 하고, 모르는 함수를 거친 값은 비밀번호로 보지 않는다(오탐을 줄이려는 선택). 다른 함수로 값을 넘겨 그 안에서 출력하거나 바깥 함수의 변수를 안쪽 함수가 쓰는 경우는 놓칠 수 있다 | 실행 검사(SEC-SIGNUP-005)로 보완 |
| 참고 | D1이 지원하는 구조는 FastAPI + pydantic + SQLAlchemy(SQLModel)이다. 그 밖의 구조(Flask의 전역 `request` 등)나 증명하지 못하는 코드는 UNVERIFIED로 남기고, 실제 동작은 실행 검사(D2·D5)로 확인한다. 어떤 정적 분석도 모든 우회를 막지 못하므로 분석기를 더 넓히기보다 증명할 수 있는 패턴을 명시하는 방향을 택했다 | 실행 검사로 보완 |
| 참고 | SQL 글자를 f-string으로 조립해 비밀번호를 끼워 넣는 코드처럼 ORM 밖의 저장 방식은 일부만 본다(`db.execute("... password_hash ...", (값,))` 형태는 본다). 비밀번호가 다른 표에 있는 구조나 이메일 칸을 믹스인으로 물려받는 구조는 이메일 UNIQUE를 사용자 표와 연결할 수 없어 UNVERIFIED다 | 실제 생성물을 본 뒤 조정 |
| 참고 | D1은 해시 결과를 담은 변수(`hashed = hash_password(...)` 다음에 `password_hash=hashed`)를 그 변수에 값이 묶이는 곳이 모두 `이름 = 해시 호출(...)`일 때만 따라간다. 분기에서 평문을 대입하거나 매개변수로 받은 변수, 이메일을 해시한 변수는 UNVERIFIED다 | 실제 생성물을 보고 조정 |
| 참고 | 입력 필드 덮어쓰기 추적의 한계: 알 수 없는 라이브러리 함수가 입력을 감싸 돌려주는 경우(`Wrapper(form)`), 나중에 컨테이너에 담는 경우(`h = []; h.append(form)`), 람다를 바로 호출하는 경우, 프레임워크가 암묵적으로 부르는 검증기가 필드를 바꾸는 경우, 도우미 함수 5단계보다 깊은 경우는 따라가지 않는다. 대신 분석이 끝나지 않으면 PASS를 만들지 않는다 | 실행 검사(SEC-SIGNUP-002)가 보완 |
| 참고 | `security/*.py`를 고치면 dev 승인 해시가 달라진다. | 코드 동결 뒤 승인 |
| 참고 | dev 테스트 `test_auth_after_source_write_preserves_bytes_and_blocks_model_replay`가 간헐적으로 실패한다(수정 전 커밋에서도 재현). 평가 코드와 무관한 1·2번 담당 영역이라 고치지 않았다 | 1·2번 담당에게 전달 |
| 참고 | `tests/evaluation/fixtures/reference_backend`는 서비스 웹페이지(세화) backend 복사본으로 정적 판정 오탐 확인용이다(생성물 정답·규격 아님). `dev_pipeline_runs.json`·`dev_pipeline_timing.json`은 dev 파이프라인 테스트가 만든 실제 Run에서 추적·시간 계산에 쓰는 사건만 남긴 표본이다. | 필요하면 표본 갱신 |
| 참고 | D2 한계: 생성물이 `get_db`를 거치지 않고 다른 DB에 쓰면 임시 DB에서 사용자를 볼 수 없어 FAIL처럼 보일 수 있다(시작 코드 규격에 "DB 접근은 `get_db`로"를 넣어야 함). 컨테이너의 `/snapshot`은 읽기 전용이라, 시작할 때(lifespan) DB 파일을 소스 폴더 옆에 만드는 앱(세화 backend 방식)은 시작 실패로 9개 모두 미검증이다(시작 코드 규격에 "DB 위치는 `DATABASE_URL`로"를 넣어야 함). 동시 가입 시험은 확률적이라 PASS는 "이번 시도에서 중복 없음"이다. 실행 노출 검사(SEC-SIGNUP-005)는 그대로·base64·16진수·해시 모양을 찾고, 앱 시작부터 종료까지 표준 logging 기록 전부와 stdout·stderr를 모은다. 암호화 등 다른 변환, 파일에 직접 쓴 글, 다른 로깅 라이브러리가 먼저 잡아 둔 출력 통로, 종료 뒤 남는 스레드 출력은 찾지 못한다. (Run 기록 노출 검사 D4는 그대로 적힌 값·해시 모양·비밀번호 이름 칸만 찾는다.) pepper를 섞은 해시는 가입 비밀번호로 검증되지 않아 FAIL이다. | 시작 코드 규격에 반영 |
| 완료 | 실행 시험에 필요한 sqlalchemy·argon2-cffi를 `pyproject.toml` dev 그룹에 추가(`uv.lock` 갱신). 기본 `uv run`에서도 실행 시험이 돈다. 평가 컨테이너 이미지에도 같은 라이브러리가 있어야 한다 | 2026-10-30 |
| 참고 | D5는 가짜 docker와 합성 서비스로만 검증했고 실제 컨테이너로는 아직 돌리지 않았다(Docker Desktop 꺼짐). 평가 이미지에 fastapi·httpx·sqlalchemy·argon2-cffi가 있어야 한다. `LocalRunner` 결과는 이미지를 확인할 수 없어 판정에 쓰지 않는다. | Docker를 켜고 `EVAL_DOCKER_IMAGE`로 시험 |

## 5. 팀원 정보 상태

| 담당 | 받은 정보 | 남은 정보 |
|---|---|---|
| 1번 | ID 규칙, Artifact·Manifest, 수정 3회·도구 재시도 2회, Run 조회 API와 Run Configuration, 보고서 수신·판정 코드(dev 45번) | 최종 Snapshot·Manifest·A2A 본문은 4번이 dev에 추가한 `snapshot_export`·`a2a_export`로 받음(10.30). dev 변경 확인(특히 실험 Run 규칙)과 팀 문서 반영은 [dev 변경 공유](dev_changes_2026-10-30.md) 참고. Single 실행기는 보류 |
| 2번 | QA 보호 케이스 연결 규격(unittest·읽기 전용 `/inputs/tests`·6필드, REQ-001·002·003·004·007만), Security 승인 함수 계약(42번) | 없음. 최종 채점은 D5가 같은 제한의 컨테이너를 직접 띄우고, 순환 안 Security 실행 테스트 경로는 4번이 dev에 추가(10.30) |
| 3번 | 플랫폼 backend의 `POST /api/auth/signup`(email·password), 응답 201/422/409/500, 실행 방법, `users` 테이블, pwdlib Argon2, 서버 로그는 파일 없이 Uvicorn 콘솔뿐(수집 절차는 테스트 쪽에서 정함), 초기화 스크립트 없음(`3번필요사항중간정리본`), 테스트의 임시 DB·`get_db` 교체 방식(sehwa-dev) | 없음. 시작 코드(생성물 규격)는 규격 에이전트 여부를 기업 팀장님과 논의한 뒤 정함 |

3번의 플랫폼 구현을 시작 코드·참고 예제로 채택하더라도 이메일 정규화(앞뒤 공백 제거·소문자)와 Argon2 수치는 정의서·dev 정책으로 이미 정해져 있다. 플랫폼 표본은 공백 이메일을 정리하지 않고 거부하므로 정적 SEC-SIGNUP-003은 FAIL이고 실행 검사는 통과한다. 플랫폼 코드를 생성물 규격으로 자동 채택하지 않는다.

## 6. 지켜야 할 dev 제약

- 순환 안 PROTECTED 케이스는 그 역할의 요구사항에만 연결할 수 있다: QA는 REQ-001·002·003·004·007(`qa_services.py`), Security는 REQ-003·005·006(`security_services.py`, Security 역할 범위는 2026-10-30 4번이 추가). Ver1은 순환용 시험지의 QA 6개·Security 3개를 각 역할이 실행한다.
- 보호 테스트: Python unittest만, `/inputs/tests` 읽기 전용, import 기준 `/snapshot`, 네트워크 차단, 보호 bytes 포함 Unit 설정 64KiB(그래서 보내는 사본은 주석을 뺀다, 순환 안 두 범위 합 약 50KB, 실제 suite 참조 기준), 테스트 0개·import 오류는 TEST_RUNNER_ERROR, 시험용 비밀번호는 실행 시 생성(literal 금지, dev 19번).
- assess: 인자 하나인 최상위 함수, 생성 코드 import/exec 금지, PASS/FAIL/CONFIRMED/FALSE_POSITIVE에는 허용 참조와 비어 있지 않은 관측 JSON, 기준 64KiB, 구현 32파일·4MiB, 논리 이름은 영문자 시작.
- 지표: `costUsd=null`을 0으로 쓰지 않음, 병렬 Step 시간 합산 금지, 성공률 분모 = 전체 계획 Run, UNVERIFIED Run 삭제 금지.
- 범위 밖(2차 미팅): SQL Injection·XSS 정밀 검사, 깊은 보안 분석, 다수 화면 테스트.

## 7. 검증 기록

아래 수치는 각 날짜의 해당 작업 시점 기록이며 현재 작업 폴더 전체의 통과 보장이 아니다. 특히 2026-10-16 5차의 평가 494개와 2026-10-17 수정 전의 평가 739개는 서로 다른 코드·시험 범위다. 2026-10-17 수정 후 결과는 별도 행으로 기록한다. 전체 저장소 검사와 평가·관련 계약 검사도 구분한다.

| 날짜 | 결과 |
|---|---|
| 2026-10-31 (주석·문서 점검·전체 시험) | 주석·문서를 고치기 전 상태(실행 코드는 지금과 같음) 기준 저장소 전체 pytest: 4,459개 통과·1개 실패·1개 건너뜀(실제 Docker, 24분 39초). 실패는 dev `test_planner_developer_continuation.py`의 `test_auth_after_source_write_preserves_bytes_and_blocks_model_replay`(10월 15일부터 기록된 간헐 실패: 그 시험만 세 번 돌리면 2번 통과·1번 실패, 그 파일 11개는 통과). 시험이 도는 동안 문서·주석을 점검해 고침: 예전 상태 표현(Single "보류", 회의록에 없는 "회의 결정" 인용, Regression 자동 산출 전, 결과 보관 위치 미정, 설정 크기 약 44KB, "연습 효과"), 명세서 13.3 기준 달성 시간 지표·15.1 구현 범위, 모듈 머리말(지문 정의·정책 확인·시험지 섞임 확인·실험 도구 옵션)과 시험 파일 설명. 코드 파일은 주석·설명만 바뀜(AST 대조), 평가 시험 1,139개 통과·1개 건너뜀 |
| 2026-10-30 (10.29 회의록 대조) | 회의록 원문(2쪽)과 코드·문서를 대조(외부 점검 + 자체 점검). 고친 것: ① 방안 1의 지표(같은 검증 기준을 충족한 결과에 도달하는 시간)에 맞춰 최종 독립 평가 PASS인 Run만 세는 기준 달성 시간·비율(`qualifiedVerdictMs`·`qualifiedRate`)을 추가(Orchestrator SUCCESS·최종 FAIL인 Run이 성공 시간에만 들어가는 것을 시험으로 고정) ② 회의록의 본래 비교(Single vs Multi)를 같은 방안 안에서 할 수 있게 `experiment compare --group-by architecture` 추가, 비교 검사가 보호 테스트 참조 없음(방안 2)을 "필수 조건 누락"으로 막던 것을 "Run들 사이에서 같으면 유효"로 바꿈 ③ 문서가 회의 뒤 방침(Ver1·Ver2, QA·Security 같은 실행 방식, 멀티 먼저)을 "회의 결정"으로 적던 것을 원문과 방침으로 나눔 ④ Single 실행기 담당을 4번에서 주호로 바로잡음(회의록 원문, 사용자 확인) ⑤ 후속: `score`가 보고서 구조를 늘 MULTI_AGENT로 적어 Single Run을 채점하면 구조별 비교가 무효가 되던 것 → Run 기록의 동결 구조를 따르고 다른 구조를 주면 채점 전에 멈춤(수정 전 동작에서 실패하는 시험 추가), "Single/Multi 비교는 보호 테스트 참조를 요구"라는 예전 규칙 문장 세 곳 정정. 평가 시험 1,139개 통과 |
| 2026-10-30 (정책 칸 대응·집계 불변식) | 정책 칸·요구사항 완료 조건마다 어기는 구현을 잡는 검사·합성 변형·정상 대조를 표로 만들어(기준표 5절) 빈칸을 채움: Argon2 반복 미달·argon2i·일반 SHA 저장, 로컬 대소문자를 그대로 두는 정리, 중복 가입에 성공 응답(새 결함 변형 8개 포함 47개). 시험지 세 벌의 입력 종류를 검사마다 자동 대조하는 시험을 추가하고, 이 시험이 찾은 차이를 고침: SEC-003·QA-003·QA-006·SEC-005의 공백 방향(시범은 앞 공백만, 공식은 뒤 공백만), 2글자 최상위 도메인(공식에만), 순환용 기본 이메일 모양, 형식 오류 이메일·형식이 틀린 본문 종류, 숫자로 시작하는 공식 이메일에서 대소문자가 바뀌지 않던 변형(10/16 확률로 검사 약화). 예전 시험지에서 대조 시험·공백 변형·최상위 도메인 변형이 실패함을 확인. 집계 불변식 무작위 시험(점수·차이의 차이 300개, 실험 요약 200개)을 추가하고, 3차에서 고친 범위 버그를 되살리면 이 시험이 실패함을 확인 |
| 2026-10-30 (마지막 자체 점검) | 저장소 전체 시험 없이 코드를 다시 읽고 고침: ① Python 3.12부터 handler 필터가 가린 기록 사본을 돌려주면 그 사본이 출력되는데 원본으로 글을 만들던 것 → 사본으로(3.12 이상 시험 추가) ② `fetch`로 결과 폴더에 받아 둔 Run 기록이 meta가 없으면 채점 때 사라지던 것 → 채점 전 Run 기록이면 이어받음 ③ 보관 결과끼리 맞지 않을 때 명령줄 traceback → `RESULTS_INCONSISTENT` ④ 결과 폴더를 처음 만들다 실패할 때 traceback → `RESULT_REPLACE_FAILED` ⑤ 인코딩 후보를 상한까지만 꺼냄. 사건의 `workflowState` 이름이 dev API와 같음을 확인. 새 시험 3개는 수정 전 코드에서 실패 확인. 평가 시험은 3.13 전체, 3.10에서 보호 테스트·실험 도구·지표 190개 통과(3.12 이상 시험 1개 건너뜀) |
| 2026-10-30 (외부 점검 3차·전체 시험) | 커밋 7개로 정리한 뒤 저장소 전체 pytest: 첫 실행 22개 실패(시간에 민감한 dev 시험, 실패한 파일을 따로·함께 다시 돌리면 모두 통과), 다시 돌린 전체 실행 4,437개 통과·1개 건너뜀(실제 Docker, 24분). 외부 점검 3차 5건을 재현한 뒤 고침: ① 정책상 다른 주소(Gmail 점·+태그만 다름)를 합치는 서비스가 만점 → SEC-SIGNUP-003에 별칭 하위 사례(세 시험지, 다른 값) ② 비밀번호를 필터로 가린 뒤 내보내는 정상 앱을 SEC-SIGNUP-005 FAIL로 오판 → 필터·형식 적용 뒤 실제로 나가는 글을 증거로(수준 때문에 지금 안 나가는 앱 코드 기록은 같은 필터·형식으로), NullHandler로만 가는 기록은 출력 아님 ③ 차이의 차이 범위가 두 버전에서 다른 Run 묶음 → 같은 쌍 기준 ④ 결과 교체 중 실패하면 원래 결과가 비교에서 빠짐 → 되돌리기, 못 되돌리면 RESULT_RESTORE_NEEDED ⑤ 다른 정책 파일을 받아도 고정 기준으로 판정 → CONTRACT_NOT_SUPPORTED. 새 시험은 모두 수정 전 코드에서 실패 확인 |
| 2026-10-30 (외부 점검 추가: 최신 재확인) | 6건을 재현한 뒤 고침. ① Ver2 실험 Run 등록: 외부 점검의 재확인은 dev 규칙 수정 전에 돈 것이었고, 실제 API(ASGI·임시 SQLite)로 같은 실험의 Ver1·Ver2 요청이 수정 전 422·수정 후 둘 다 201, Host도 두 버전을 모두 승인(Ver2는 보호 범위·참조 없음)함을 시험으로 고정. 실험 계획상 버전(`--loop-policy`)을 결과에 적어 동결 설정과 대조. ② 채점하지 못한 Run이 비교에서 사라짐 → `record`로 Run 기록만 남기고 `compare`가 모든 Run을 집계(정책마다 성공 1·실패 1이면 기록 2·시작 안 함 0·실패 1, 비용·토큰 합산), 채점 못 한 Run의 짝은 차이의 차이 확정 계산에서 뺌. ③ 받는 도중 Run이 바뀌면 예전 판정 시간을 씀 → Run 상태를 처음·끝에 받아 같을 때만 쓰고(최대 3번), 최종 상태와 판정·종료 사건이 이어지지 않으면 시간 없음. ④ DB 실패 요청에서 새로 만든 해시를 base64로 남기면 놓침(세 시험지 9/9 PASS로 재현) → base64·16진수 후보를 풀어 해시 모양 확인. ⑤ 승인되지 않은 비밀번호 복잡성 조건(재현) → QA-SIGNUP-004에 8자 이상 소문자만·숫자만 추가. ⑥ 단가표가 달라도 비용 비교에 표시 없음 → 단가표를 지표에 남기고 다르면 주의. 새 시험은 모두 수정 전 코드에서 실패 확인. 평가 1,115개 통과·1개 건너뜀(실제 Docker), dev 관련(실행 설정·Run API·Host·Host 명령·Security Host·파이프라인·검증 재검증) 93개 통과. dev 전체 시험은 돌리지 않음 |
| 2026-10-30 (외부 점검: 테스트 초안 전체 검토) | 지적 R1~R10과 추가 2건을 재현한 뒤 고침. 보호 테스트: 시작할 때 만든 독립 로거·종료할 때 남긴 로그의 비밀번호(9/9 PASS로 재현), lifespan 상태를 쓰는 정상 앱의 동시 가입 FAIL, 종료 때 엔진을 정리하는 정상 앱의 8개 미검증, 중복 가입을 거부하면서 기존 비밀번호를 바꾸는 서비스의 PASS → 새 변형 6개가 수정 전 코드에서 5개 실패(정상 대조 1개는 통과)를 확인하고 세 시험지 모두 기대대로 판정. 시험지 지문에 요구사항 연결 포함(수정 전 같은 지문 재현). 실험·점수: 강제 재채점 잔여 파일(수정 전 재현), 저장 결과 재확인(바꿔 넣은 보고서·다른 순환용 시험지·손상 보고서·다른 Run·남은 회귀 파일), 잠정 하한으로 만든 차이의 차이. Run 기록: 이름에 든 비밀값이 위치에 복사됨·빈 A2A 저장소를 본 것으로 셈(둘 다 수정 전 재현). dev: Security 측정 테스트 오류 코드 보존. Ver2 실험 선언 불가(dev `RunConfiguration` 규칙)를 재현해 팀 결정으로 올림. 관련 시험만 돌림: 평가 1,107개 통과·1개 건너뜀(실제 Docker), dev 파이프라인·Security Host·Host·Host 명령·검증 재검증 80개 통과. dev 전체 시험은 돌리지 않음 |
| 2026-10-30 (전체 점검 후 수정) | 전체 점검(dev 전체 시험은 돌리지 않음)에서 찾은 것을 고침: ① `experiment configure`가 설정을 `ver1/`·`ver2/`에 쓰면 상대 경로 `dependencyLockFile`이 다른 파일을 가리킴 → 원래 설정 폴더 기준 절대 경로로 바꾸고, 기준 폴더를 모르면 거부 ② `compare`의 성공률 분모가 결과 폴더 수(채점된 Run만)였음 → `--planned`로 계획 Run 수를 받고, 안 주면 `plannedRunsKnown: false` 표시, 계획보다 결과가 많거나 계획에 없는 정책이면 거부 ③ `score`가 이미 결과가 있어도 컨테이너 채점을 다 돌린 뒤에야 거부 → 채점 전에 `OUTPUT_EXISTS` ④ dev `a2a_export`가 폴더 이름의 `?`·`#`·`%` 때문에 저장소를 못 엶(수정 전 코드로 재현) → 경로 인코딩 ⑤ Host 정책 해시가 Security 측정 방식을 `UNVERIFIED`로 적음 → `MEASURED_TESTS` ⑥ 예시 설정에 Security 테스트 범위 없음 → 추가. 문서의 옛 범위 이름·정적 판정 위치·일정 설명도 고침. 관련 시험만 돌림: 실험 도구 9개, dev 파이프라인·Security Host·Host·Host 명령 68개 통과. 이 전 dev 전체 시험은 Snapshot 내보내기·Security 역할 계약까지 넣은 시점 3316개 통과·1개 실패(아래 간헐 실패) |
| 2026-10-30 (dev 남은 수정) | 최종 Snapshot 내보내기(`agents.platform.snapshot_export`)를 dev 파이프라인 Run으로 확인: 꺼낸 tar의 SHA-256이 Manifest와 같고 D5 `read_snapshot`·`ExecutionManifest.from_payload`가 그대로 읽음, 덮어쓰기 거부, 없는 Run 거부(파이프라인 시험 9개 통과). dev 승인 함수 3.10 호환(승인 시험 85개 3.10·3.13 통과), sqlalchemy·argon2-cffi dev 그룹 추가, wheel에 평가 기준·보호 테스트 포함(저장소 밖 설치 확인). 평가·환경 검사가 기본 환경·3.10·3.13 모두 1077개 통과·1개 건너뜀(실제 Docker). Security·Host 관련 dev 시험 175개 통과 |
| 2026-10-30 (QA·Security 실행 방식 통일, Ver1·Ver2) | 평가: 검사 9개 × 시험지 세 벌을 합성 서비스와 변형 27개로 세 벌 모두 돌려 결함마다 같은 검사만 FAIL(난이도 확인). info·debug 비밀번호 로그(수정 전 SEC-SIGNUP-005 PASS로 재현)·DB 오류 미처리(정상 합성 서비스도 해시가 traceback에 남던 것을 오류 주입으로 찾음) 변형을 추가. 오류 주입은 처음 방식이 SQLAlchemy 버전에 따라 감싸지지 않아(3.10 환경에서 재현) SQLite 권한 함수로 바꿈. 평가·환경 검사 3.13 `uv run --with sqlalchemy --with argon2-cffi`로 1077개 통과·1개 건너뜀(실제 Docker), 3.10에서 1076개 통과·1개 실패(dev 승인 함수의 3.11 전용 속성), 기본 환경 1040개 통과·38개 건너뜀. dev 수정: Security 실행 테스트 경로를 dev 파이프라인 시험(Security 테스트 통과로 Run SUCCESS, 실패로 Security Issue)과 생성 규칙 시험으로 확인, dev 전체 시험(평가 제외) 3316개 통과(21분). 순환 안 설정 크기는 QA 약 20KB·Security 약 19KB(dev 한도 64KiB, 둘 합쳐 56KiB 미만 시험). 실제 LLM·Docker 실행은 하지 않음 |
| 2026-10-29 (외부 점검 2차 뒤 자체 점검) | 미커밋 변경을 다시 읽고 문서·코드를 대조. 고친 것: ① 요약 문서가 검사 범위(`coverage`)가 빠진 노출 결과를 "보지 못한 출력·받지 못한 기록 없음"으로 적던 것 → "미완료(검사 범위 정보 없음)"(시험 1개, 수정 전 코드로 재현) ② 기준표 QA-SIGNUP-005 행의 FAIL·미검증 조건을 2차 규칙에 맞춤 ③ 4절의 실행 시험 변형 수(19 → 23). 순환 안 보호 테스트 지문은 커밋 `33e163f`와 같고 최종 범위 지문만 바뀜. 보호 설정 크기는 순환 안 22,076바이트·최종 31,867바이트(시험 한도 32KiB). `origin/dev`(`81623d6`)의 트리는 `33e163f`와 같음. 문서 로컬 링크 24개 정상, diff 공백 검사 통과. 평가·환경 검사 3.13 `uv run --with sqlalchemy --with argon2-cffi`로 1044개 통과·1개 건너뜀(실제 Docker, Docker 꺼짐), 기본 환경 1012개 통과·33개 건너뜀, 3.10에서 1043개 통과·1개 실패(dev 승인 함수의 3.11 전용 속성) |
| 2026-10-29 (외부 점검 2차) | 평가 문서(PDF) 점검에서 나온 판정 2건·보고 1건을 고침. ① 동시 가입(QA-SIGNUP-005)에서 아무 계정도 생기지 않으면 응답과 상관없이 미검증이던 것 → 5xx 응답이 있으면 FAIL, 모두 4xx면 단독 가입을 다시 해 보고 되면 FAIL·안 되면 미검증(합성 변형 3개 추가: 동시 요청만 모두 500, 동시 요청만 모두 409, 모든 가입 500. 앞의 둘은 수정 전 코드에서 미검증으로 재현) ② 요약 문서가 검사 11개 PASS 아래에 Run 기록 노출 FAIL을 맨 끝 절에만 적던 것 → 판정 줄에 검사 수를 적고, 바로 아래에 판정에 들어가지 않는 Run 기록 검사와 REQ-006 전체 노출 검증 상태(충족하지 않음·미완료)를 적고 REQ-006 줄에도 덧붙임(시험 5개, 수정 전 코드에서 실패 확인) ③ QA-006은 정상 가입 거부를 이미 FAIL로 처리하고 있어 코드 문제가 아니라 PDF 설명 오류였고, 정적 판정이 컨테이너 밖에서 돈다는 점·실제 Docker 미실행을 문서에 분명히 함. 평가·환경 검사 3.13 `uv run --with sqlalchemy --with argon2-cffi`로 1043개 통과·1개 건너뜀(실제 Docker), 기본 환경 1011개 통과·33개 건너뜀, 3.10.2에서 1042개 통과·1개 실패(dev 승인 함수의 3.11 전용 속성). 최종 범위 보호 설정 31,827바이트 |
| 2026-10-29 (푸시 전 전체 점검 + 외부 점검) | 담당 코드 전체를 dev API·실행기와 다시 대조. 사건·Issue·Telemetry·Artifact·도구 시도 응답 모양과 runId 필드, 보호 테스트 실행 인자, 기록의 가린 값(`[REDACTED]`)이 평가 코드와 맞음을 확인. 찾아서 고친 것: ① 로컬 실행 시험이 쓰기 가능한 Snapshot에서 돌아, 컨테이너(읽기 전용)에서 DB 파일을 소스 폴더 옆에 만드는 앱이 시작 실패로 모두 미검증이 되는 것을 못 잡음 → 시험도 읽기 전용으로 돌리고 변형 2개 추가(기존 변형 18개는 결과 같음) ② D5가 Docker에 넘기는 파일 권한이 umask를 따라 077이면 컨테이너 사용자가 못 읽음(수정 전 코드로 재현) → 폴더 755·파일 644로 고정 ③ Python 3.10에서 아주 긴 식 파일 시험의 전제가 틀림(3.10은 그 파일을 읽을 수 있음) → 버전별 기대값으로 고침. 평가·환경 검사 3.13 `uv run --with sqlalchemy --with argon2-cffi`로 1002개 통과·1개 건너뜀(실제 Docker), 기본 환경 975개 통과·28개 건너뜀, 3.10.2에서 1001개 통과·1개 실패(dev 승인 함수의 3.11 전용 속성, 4절 참고). dev 보고서 모델이 합성 보고서 8개 수용, 문서 로컬 링크 정상 |
| 2026-10-28 (외부 점검 후속) | 위 D2·D4·D5 추가 뒤 외부 점검에서 재현한 오판을 고침: 보호 테스트가 앱 startup/lifespan을 실행하지 않아 시작 실패 앱도 통과하던 문제, passlib category별 설정·펼친 hash 인자를 기본 정책으로 승인하던 문제(UNVERIFIED로), 실행기 오류·비정상 종료·종료 코드와 보고서 불일치를 근거로 쓰던 문제, D5에 다른 Run·다른 최종 버전의 기록을 넣어도 받던 문제, Trace 사건·Issue의 Run 식별자 미대조, SUCCESS인데 최종 버전 검증 사건이 없어도 추적 PASS, Issue 요약만으로 재검증 인정, 요약에서 검증 종료 사건만으로 "문제 없음" 표시. 회귀 표본의 사건·Issue runId는 부모 Run 식별자로 복원(새 실행 증거 아님). 평가·환경 검사 `uv run --with sqlalchemy --with argon2-cffi`로 999개 통과·1개 건너뜀(실제 Docker). 보호 테스트 bytes와 Security 구현이 바뀌어 suite 지문·Host 승인 해시는 최종 코드로 다시 만들어야 한다 |
| 2026-10-28 | 최신 dev(`08542c6`) 병합 후 D1 보강, D2 보호 테스트·결과 어댑터, D4 Trace 검사·Regression·협업 시간, D5 최종 채점기·요약·설정 내보내기 추가. 평가·기본 설정 검사 947개 통과·24개 건너뜀(실행 시험은 sqlalchemy·argon2 필요), `uv run --with sqlalchemy --with argon2-cffi`로 970개 통과·1개 건너뜀(실제 Docker 시험). D1은 새 사례 22개와 저장소 `.py` 폴더 36묶음·파일 366묶음에서 이전 판정과 차이 0. 보호 테스트는 최소 회원가입 서비스와 변형 17개를 dev unittest 실행기로 돌려 확인했고 Python 3.10.2에서도 같은 결과. Trace·시간 계산은 dev 파이프라인 테스트가 만든 실제 Run 6개로 확인(판정이 끝난 5개 추적 PASS, 멈춘 1개 미검증, 노출 오탐 0). 저장소 전체 pytest는 이번에 돌리지 않음(dev 코드 변경 없음) |
| 2026-10-17 (전체 점검 2차) | 평가 코드 전체를 다시 읽고 고침: ① 비밀번호 필드가 `SecretStr`이고 검증 오류 처리기가 없을 때 REQ-006 PASS(실제 FastAPI 422 응답에 원문 `input`이 실림을 확인) ② 이메일 필드에 등록됐지만 이름에 email이 없는 검증기(`def clean(cls, v)`)를 REQ-003 FAIL ③ `password_digest` 이름의 값 저장을 REQ-005 FAIL(원문 저장으로 오인) ④ 사용량을 하나도 모를 때 `knownTotalTokens`가 None이 아니라 0. 결정 A에 따라 REQ-003은 저장되는 이메일 값의 정리를 따라가 확인(경로 함수·도우미 함수에서 정리한 정상 코드 PASS, 정리가 전혀 없거나 경로 함수가 입력을 그대로 저장하면 FAIL). 시연 자료 목록은 `.DS_Store`를 무시하고 JSON·URL·HTML로 바뀌어 적힌 비밀값도 찾음. BOM이 붙은 소스도 분석, 스키마는 UTF-8로 읽음, 쓰이지 않는 `_result_is_used`·`MAX_SCAN_BYTES` 제거. 팀 승인 전인 `pyproject.toml` 변경(wheel에 `security/*.json` 추가)은 되돌림. 평가 테스트 846개 통과(전체 pytest는 사용자 요청으로 이번에 돌리지 않음), 새 시험 41개 중 24개는 직전 커밋 코드에서 실패함을 확인. 분석기를 Python 3.10에서 따로 돌려 이전 입력 1,549개의 판정이 3.13과 같음(오늘 바뀐 응답 모델 규칙 1건 제외), 저장소 `.py` 363묶음 재판정에서 예외 없고 판정이 바뀐 묶음은 `backend/models.py` 1개(정리 코드가 없는 파일 하나만 넣으면 REQ-003 FAIL) |
| 2026-10-17 (전체 점검 수정 후) | 현재 평가·기본 설정 검사 806개 통과, 샌드박스에서 생략된 소켓 검사 1개는 별도 실행으로 통과(총 807개). 이전·이번 외부 회귀 24개 통과, 보안 승인·검증·정책 고정 관련 dev 검사 113개 통과. dev 원본 모델이 합성 보고서 8개 수용. (이 행의 wheel 포함 확인은 팀 공유 파일 `pyproject.toml` 변경이 승인 전이라 되돌렸다.) 문서 로컬 링크 27개 정상, diff 공백 검사 통과. 실제 LLM·Docker·최종 생성물 실행 검증은 포함하지 않음. 보안 구현이 바뀌었으므로 Host 연결 때 승인 해시를 다시 생성해야 함 |
| 2026-10-16 (5차) | 5차 외부 점검 반영 후 평가 테스트 494개 통과(보안 판정 사례 35개 추가, 그중 20개는 직전 커밋의 분석기에서 실패함을 확인. 입력 필드를 덮어쓰는 시험은 변경 감지를 끄면 실제로 실패함을 확인), 점검자의 재현 파일(4차 11개 + 이전 17개)과 직전 미해결 3건 재현도 통과, `pytest checks` 2개, `scripts/check_dev_reports.py` 합성 보고서 8개 수용, 보고서 합성 예시 실행 확인. 저장소 전체 pytest 3802개 모두 통과(20분 55초). 저장소 `.py` 359묶음 재판정에서 예외가 없고 판정이 달라진 묶음은 4차와 같은 2개뿐이다 |
| 2026-10-16 (4차) | 4차 외부 점검 반영 후 평가 테스트 459개 통과(보안 판정 사례 64개, 비교·지표·시연 자료 시험 추가), 점검자가 만든 재현 11개와 이전 재현 17개(1·2차) 통과, 직전 점검의 미해결 3건 재현 파일도 모두 FAIL 또는 UNVERIFIED, `pytest checks` 2개, `scripts/check_dev_reports.py` 합성 보고서 8개 수용, 보고서 합성 예시 실행 확인. 저장소 전체 pytest 3767개 모두 통과(21분 23초. 앞서 적은 dev 간헐 실패 테스트도 이번에는 통과). 새 보안 판정 사례 64개 중 48개는 직전 커밋의 분석기에서 실패함을 확인. 저장소 `.py` 359묶음 재판정에서 예외가 없고 직전 커밋과 판정이 달라진 묶음은 2개뿐이다(`backend/routers/auth.py`·`documents.py`를 파일 하나만 넣으면 응답 모델이 다른 파일에 있어 REQ-006이 PASS에서 UNVERIFIED로 바뀜. backend 폴더 전체로 넣으면 그대로). 보안 구현이 또 바뀌어 승인 해시를 코드 동결 뒤 다시 만들어야 함 |
| 2026-10-16 | 3차 외부 점검 반영 후 평가 테스트 386개 통과(보안 판정 사례 60개 추가, 그중 39개는 수정 전 분석기에서 실패함을 확인), 점검자가 만든 재현 17개(1·2차) 통과, `pytest checks` 2개, `scripts/check_dev_reports.py` 합성 보고서 8개 수용, 보고서 합성 예시 실행 확인. 저장소 전체 pytest는 3694개 중 3693개 통과(21분 45초)이고 실패 1개는 앞서 적은 같은 dev 간헐 실패 테스트다. 저장소 `.py` 359묶음 재판정에서 예외·2초 초과가 없고 직전 커밋과 판정이 달라진 묶음도 없음. 같은 종류의 우회 변형 약 80개(이번 점검 4건의 변형과 정상 코드 오탐 확인 포함)를 직접 만들어 보고 PASS가 나오는 결함 형태가 없음을 확인(원시 SQL 조립 등 문서에 적은 한계 제외). 보안 구현이 또 바뀌어 승인 해시를 코드 동결 뒤 다시 만들어야 함 |
| 2026-10-15 | 2차 외부 점검 반영 후 평가 테스트 326개 통과(보안 판정 사례 63개와 비교 검사 4개 추가), 점검자가 만든 재현 17개(1차 12 + 2차 5) 모두 통과, `pytest checks` 2개, `scripts/check_dev_reports.py` 합성 보고서 8개 수용, 보고서 합성 예시 실행 확인. 새로 추가한 보안 판정 사례 63개 중 40개와 비교 검사 4개는 수정 전 코드에서 실패함을 확인(나머지는 이미 맞게 판정하던 경우와 정상 코드가 FAIL이 되지 않는지 보는 확인). 저장소 `.py` 359묶음을 수정 전후로 비교하면 예외·2초 초과가 없고 판정이 달라진 것은 1건뿐이다(`src/orchestrator/main.py`만 따로 넣으면 처리기가 다른 파일에 있어 REQ-006이 PASS에서 UNVERIFIED로 바뀜. 폴더 전체로 넣으면 그대로). 같은 종류의 우회 변형 약 60개를 직접 만들어 보고 PASS가 나오는 결함 형태가 없음을 확인. 저장소 전체 pytest는 3634개 중 3633개 통과(21분 43초). 실패 1개는 dev의 `tests/test_planner_developer_continuation.py::…::test_auth_after_source_write_preserves_bytes_and_blocks_model_replay`(500 `INVALID_AGENT_RESPONSE`)로 평가 코드와 무관하다. 단독으로 3번 돌리면 1번, 수정 전 커밋(`ca0f262`)을 따로 받아 8번 돌려도 1번 실패하는 간헐 실패 테스트다. 보안 구현이 또 바뀌어 승인 해시를 코드 동결 뒤 다시 만들어야 함 |
| 2026-10-14 | 외부 점검 반영 후 평가 테스트 259개 통과(재현 사례 12개 + 추가 시험 16개 포함), 점검자가 만든 재현 12개 모두 통과. 이전 점검 사례 14개와 저장소 `.py` 359묶음 재판정에서 오탐 증가·예외 없음. 보안 구현을 고쳐 승인 해시를 다시 만들어야 함 |
| 2026-10-14 | 점검·수정 후 평가 테스트 231개 통과(합성 입력), `pytest checks` 2개, `scripts/check_dev_reports.py`로 dev 원본 모델이 합성 보고서 8개 수용. 저장소 `.py` 358묶음을 D1에 넣어 예외·2초 초과 없음. 저장소 전체 pytest는 수정 전(`-x`) 2461개 통과·dev 테스트 1개 실패(`test_planner_developer_continuation`, 원인 미확인), 수정·주석 작업을 마친 뒤 전체 3539개 모두 통과(22분 32초, 실패 없음. 앞의 1개 실패는 재현되지 않았고 원인은 확인하지 못했다) |
| 2026-10-14 | Python 3.13, dev `caa4144` 기준 평가 테스트 146개 통과. D1이 dev 승인 로더(`load_approved_assessor`) 통과 |
| 2026-10-09 | 평가 테스트 126개, 전체 298개 통과. dev `WorkflowRun` 응답 5가지 상태로 호환 확인 |
| 2026-10-06 | 기본 pytest 285개 통과(평가 113 + dev 172). 공통 스키마 사본 4개와 dev 원본 동일. dev 실제 보고서 모델이 합성 보고서 8개 수용 |
| 2026-10-05 | dev 공통 환경 통합 후 pytest 263개 통과 |
| 2026-10-04 | 평가 wheel 빌드, 작업 폴더 밖에서 스키마 포함 import·보고서 생성 확인 |

모델 수용·합성 입력 통과는 A2A 전송·실제 서비스 실행 검증을 뜻하지 않는다. 예시 출력은 모두 mock이다.

## 8. 다음 작업

0. 2026-10-30 외부 점검에서 나온 결정 두 가지: ① (결정·수정) Ver2는 보호 테스트 참조가 없어 dev가 실험 Run(`experimentId`)으로 받지 않던 것을 dev 규칙에서 고쳤다(실험 Run이라도 보호 테스트 참조는 비워 둘 수 있음, Single/Multi 비교는 평가 쪽 비교 검사가 두 구조의 보호 테스트 조건이 같은지 확인). 두 버전 모두 같은 `experimentId`로 선언하고 `executionOrder`·`warmupState`를 적는다. ② (결정, 2026-10-30) 측정 테스트 모드에서 스캐너 경고가 확정되지 않으면 실행 테스트가 모두 PASS여도 Run이 HUMAN_REVIEW로 끝난다. 일단 HUMAN_REVIEW를 하나의 결과로 받아들이고 두 버전에 같은 규칙으로 다룬다(성공률에는 넣지 않고 `humanReview`로 따로 세며, 최종 시험지 채점은 그대로 한다). 시범 실행에서 실제 스캐너 규칙으로 얼마나 자주 생기는지 본 뒤, 경고 확인 경로(dev)나 규칙 범위 조정을 다시 정한다. 경고를 무조건 무시하게 바꾸지 않는다.
1. 생성 코드 규격: 시나리오마다 달라질 수 있어 "시작 코드 규격을 정의하는 에이전트"를 둘지 기업 팀장님과 논의한다(11월 2일 텔로스 회의). 시작 코드의 위치·형태(캡스톤 안 일반 파일에서 만든 별도 git 저장소 등)는 그 논의 뒤에 정한다. 어느 방식이든 연결 조건(DB 접근은 `get_db`로, DB 위치는 `DATABASE_URL` 환경 변수로)과 "시작 코드에 평가 코드(`src/evaluation`, 시험지) 미포함"이 들어가야 한다(Host는 시작 저장소 커밋의 파일 트리 전체를 에이전트 작업 폴더로 복사한다).
2. dev 수정(10.29 회의록의 석민 담당: 테스트에 필요한 사항)은 2026-10-30에 마쳤다: Security 실행 테스트 경로, 최종 Snapshot·Manifest 내보내기(`python -m agents.platform.snapshot_export --database <Host DB> --run-id <Run> --out <폴더> [--all-versions]` → `v<N>/source.tar`·`v<N>/manifest.json`·`run.json`, 저장된 tar 바이트 그대로이고 내용 해시·tar 해시·Manifest의 snapshotSha256이 같을 때만 씀), 실행 의존성, wheel 포함, 3.10 호환. 남은 것은 평가 컨테이너 이미지(fastapi·httpx·sqlalchemy·argon2-cffi 포함)를 만들어 해시로 고정하는 일이다.
3. Ver1·Ver2 실행: `python -m evaluation.experiment configure`로 설정 쌍을 만들어 Host에 넣는다(Ver1은 보호 범위·보호 케이스 포함, Ver2는 제외, 나머지 조건은 같게). 시범 실행은 pilot, 공식 비교는 final 시험지로, 쌍 번호·순서를 기록하며 `snapshot_export` → `experiment fetch` → `experiment score` → `experiment compare` 순서로 처리한다.
4. D5 실제 연결: Docker를 켜고 실제 컨테이너로 한 번 돌린다. 보고서·요약은 `experiments/scn001/<시험지>/<날짜-이름>/`에 커밋하고, git에서 빠지는 내보낸 Snapshot tar(정적 판정 근거가 가리키는 `source.tar`)는 따로 보관할 곳을 정한다. 버전별 Snapshot은 `snapshot_export --all-versions`·`experiment score --all-versions`로 받아 Regression을 실제 결과에서 산출한다.
5. 실제 Run으로 D4(`run_metrics`·`trace_checks`)를 검증하고 `summary`로 요약, 시연 자료를 `demo_index`로 정리(12월 발표). 노출 검사에 쓸 시험 비밀값(`known_values`)을 Host가 넘길 경로와 에이전트 프로세스 출력 수집 범위를 정한다(A2A Message 본문은 `a2a_export`로 수집). 못 정한 출력은 REQ-006 부분 검사로 표시한다.
6. Single vs Multi(D6): Single 실행기는 주호가 개발한다(10.29 회의록). 준비되면 같은 방안(Ver1 조건 또는 Ver2 조건) 안에서 `experiment compare --group-by architecture`로 비교한다. 11월 2일 기업 미팅 의견에 따라 방안을 정한다.

일정은 팀장 카톡·회의비 신청서 기준(11월 2일 텔로스 회의, 12월 최종 발표)이며 납기 확정이 아니다. 10월 22일 자문(텔로스)에 따라 설계 문서 추가는 줄이고 실제 동작하는 연결을 우선한다.

## 9. 변경 이력

| 날짜 | 변경 |
|---|---|
| 2026-10-17 | 전체 점검의 보안 오판 3건·집계 오류 3건·증거 크기·큰 로그 검색을 수정하고 회귀 검사를 추가. 보안 기준 JSON 배포 포함은 팀 공유 파일 변경이라 되돌리고 승인 대기로 둠. 해시 verify 검사 조건, 담당 3번과 생성물의 구분, D1 순환 연결과 최종 독립 평가, D4 미구현 범위, 시간·페이지·시험 공개·과거 검증 설명을 정리. 실제 생성물 실행·최종 Snapshot 연결·비교 실험은 아직 미완료 |
| 2026-10-28 | 개발정의서·기업 미팅·dev 정책을 대조해 4번이 정할 평가 기준을 확정(Argon2 "이상", canonical 컬럼 불필요, 테스트 연결·로그 수집 방법, 4xx 거부, 동시 가입 기준, 정적 PASS 범위, 실행 보안 검사는 최종 전용)하고 이미 정해진 항목(이메일 정규화, B안, Python 3.10+, Single 담당 4번 — 10.29 회의록에서 주호로 바뀜)을 문서에 반영. D2 회원가입 보호 테스트(`protected_tests/`)와 결과 어댑터(`check_adapters.py`), D4 Trace 노출·추적 검사(`trace_checks.py`)와 Regression 계산을 추가. dev 파일·`pyproject.toml`은 바꾸지 않음 |
| 2026-10-28 | D1 해시 도구 인식 보강: FastAPI 튜토리얼 방식(`password_hash = PasswordHash.recommended()`)이 모델의 `password_hash` 칸과 이름이 겹쳐 미검증이던 문제(클래스 속성을 같은 이름의 덮어쓰기로 보던 것)를 고치고, passlib Argon2를 승인 목록에 추가(passlib `argon2.hash`가 승인 안 된 해시로 FAIL이던 오판 해소). bcrypt가 기본인 `CryptContext`·`scheme="bcrypt"` 지정은 FAIL, 프로젝트 안 가짜 라이브러리 파일은 승인하지 않음. 새 사례 22개, 저장소 `.py` 폴더 36묶음·파일 366묶음에서 이전 판정과 차이 0 |
| 2026-10-28 | D5 최종 채점기(`final_evaluation.py`: Snapshot 확인·안전한 풀기·컨테이너 실행·정적 판정·보고서), 회의·발표용 요약(`summary.py`), 팀장 전달 설정 내보내기(`python -m evaluation.protected_tests export`), 협업 시간 지표(`collaboration_metrics`, `humanWaitMs`) 추가. 기준표의 생성물 연결 규격을 "서비스 웹페이지 backend 구조를 임시 가정"으로 고침 |
| 2026-10-29 | 푸시 전 전체 점검: 보호 실행 시험을 읽기 전용 Snapshot으로(시작할 때 DB를 만드는 앱 변형 2개 추가), D5 파일 권한 고정, 3.10 시험 전제 수정, 쓰지 않는 import 정리, 평가 코드·시험에 한글 주석·설명 보강. 시작 코드 규격에 "DB 위치는 `DATABASE_URL`로"를 팀 결정 항목에 추가. 외부 점검 4건 수정(중복 4xx 판정, A2A 즉시 완료·재개 구간, 요약 출처 대조)과 `known_values`·노출 검사 범위·버전별 Regression 연결, 문서의 "구현"과 "실제 연결 완료" 구분 |
| 2026-10-29 | 외부 점검 2차: 동시 가입에서 계정이 하나도 생기지 않을 때 5xx·단독 가입 가능 여부로 FAIL/미검증을 나누고, 요약 문서가 검사 11개 판정과 Run 기록 노출·REQ-006 전체 검증 상태를 구분해 적도록 고침. 기준표·공유 문서·README에 정적 판정 위치(컨테이너 밖)와 실제 Docker 미실행을 분명히 적음 |
| 2026-10-29 | 10.29 회의록(비교 방안 1·2는 검토 중, 테스트에 필요한 dev 수정(Snapshot 인계 등)은 4번, Single 에이전트 개발은 주호, 생성 코드 규격 정의 에이전트는 기업 의견 수렴)과 회의 뒤 방침(멀티에이전트 먼저 완성, Ver1(순환 안 보호 테스트 + 최종, 속도 중심)·Ver2(최종만, 정확성 중심)) 반영. 다음 작업과 승인 대기 항목을 이에 맞게 바꾸고, 현재 비교 코드가 Ver1·Ver2를 받지 못한다는 점을 기록 |
| 2026-10-30 | QA·Security를 같은 실행 방식으로 통일: 검사 9개 × 시험지 세 벌(순환용·시범 최종용·공식 최종용, 난이도를 변형 27개로 확인), 정적 판정은 최종 보조 점검으로, 정적 판정이 보던 곳은 실행 검사 보완(로그 수준·오류 주입·형식이 틀린 본문·인코딩 검색·DB 직접 쓰기). D5에 시험지 선택·순환용 참고 채점·줄 기록·점수(요구사항별 같은 비중)·연습 효과, run_metrics에 Ver1·Ver2 비교. dev 수정(4번 담당): Security도 QA처럼 자기 테스트를 쓰고 Host가 자체·보호 테스트를 실행해 측정 결과로 판정(역할 표시 범위, MCP 도구·실행 기록, Security 계약 `testCases`, Host의 보호 케이스 분배, Orchestrator의 `run_unit_tests` 증거 허용). Security 범위가 없으면 예전 정적 증명 방식 그대로 |
| 2026-10-30 | 시험지 노출 안전장치(시작 코드 확인·D5 채점 전 확인)와 Ver1·Ver2 실험 도구(`evaluation.experiment`), Security 역할 계약에 테스트 방식 안내 추가. 시작 코드 위치·형태는 시작 코드 규격 에이전트 논의 뒤 결정 |
| 2026-10-30 | REQ-006 노출 검사를 A2A Message 본문까지 넓힘: dev `agents.platform.a2a_export`(역할별 에이전트 작업 저장소를 읽기 전용으로 열어 한 Run의 받은 요청·대화 기록·Task·Artifact·상태 사건을 꺼냄), `trace_checks`가 이 문서(`a2aMessages`)를 받으면 본문까지 찾고 "보지 못한 출력"에서 뺌(역할이 빠지면 미검증), `experiment fetch`가 노출 검사 기록도 받고 `score --trace --a2a`로 넘김. dev 파이프라인 Run의 실제 A2A 본문에서 오탐 없음, 요청 문장을 알려 준 값으로 주면 본문에서 찾음 |
| 2026-10-30 | 전체 점검 후 수정: 실험 도구의 lock 경로·계획 Run 수(성공률 분모)·결과 덮어쓰기 전 거부, dev A2A 내보내기 경로 인코딩·Host 정책의 Security 측정 방식 표시·예시 설정. README·명세·공유 문서의 옛 범위 이름·정적 판정 위치·일정 설명 정리 |
| 2026-10-30 | 외부 점검(테스트 초안 전체 검토) 반영: 보호 테스트의 로그 수집(모든 로거·종료 뒤 판정)·동시 요청(같은 lifespan)·검사별 앱 재적재, 거부된 요청의 기존 계정 변경 FAIL, 시험지 지문에 요구사항 연결 포함, 보내는 보호 파일은 주석 뺀 사본, 강제 재채점 결과 세트 교체, 비교 때 저장 결과 재확인, 차이의 차이를 완료 Run으로만 계산, 노출 결과 위치의 비밀값 치환, 빈 A2A 저장소 미검증, dev Security 실행 오류 코드 보존. Ver1·Ver2 정의 기록(Ver2 = 순환 테스트가 빠진 구조, 에이전트 자체 검사는 협업의 일부), dev 실험 Run 규칙 수정(Ver2를 보호 테스트 참조 없이 실험 Run으로), 스캐너 경고 HUMAN_REVIEW는 결과로 받아들이기로 결정(시범 뒤 재검토) |
| 2026-10-30 | 외부 점검 추가 반영: 채점 못 한 Run 보관(`record`)·집계, 실험 계획상 버전 대조, Run 수집 시점 일관성, DB 실패 요청의 인코딩된 해시 노출, 비밀번호 문자 구성 입력, 비용 단가표 표시. Ver2 실험 Run 등록을 실제 API·Host로 확인 |
| 2026-10-30 | 외부 점검 3차 반영: 이메일 별칭(점·+태그) 검사, 로그는 필터·형식 적용 뒤 실제 출력으로 판정, 차이의 차이 범위를 같은 쌍 기준으로, 결과 교체 실패 복구, 지원하지 않는 정책 거부 |
| 2026-10-30 | 정책 칸·완료 조건 → 검사·변형 대응표(기준표 5절), 시험지 세 벌 입력 종류 자동 대조와 그 결과로 찾은 차이 수정, 집계 불변식 무작위 시험 |
| 2026-10-30 | 10.29 회의록 대조: 기준 달성 시간 지표, 같은 방안 안의 Single·Multi 비교, 회의록 원문과 회의 뒤 방침 구분, Single 실행기 담당 주호로 정정 |
| 2026-10-31 | 주석·문서 점검: 회의록 인용과 Single 담당 표현을 원문 기준으로 맞추고, 예전 상태 설명(Regression 자동 산출 전·결과 보관 위치 미정·설정 크기)과 용어(연습 효과 → 차이의 차이, 보조 지표)를 고침. 명세서에 기준 달성 시간 지표 추가. 코드 동작 변경 없음 |
| 2026-10-04 | 초기 집계·Manifest 비교, dev 호환 해시 형식 검사, 문서 구조 정리, 공통 보고서 어댑터·합성 예시, 패키지·lock·스키마 동봉 |
| 2026-10-05 | 최신 dev `2b4fc60`에서 `seokmin`을 새로 만들고 평가 코드를 `src/evaluation/`로 이동. 루트 pyproject·uv.lock·.venv 하나로 통일 |
| 2026-10-06 | 독립 평가 보고서(`independent.py`), 검사별 진단 정보·민감값 치환, 필수·선택 목록 분리, 주석 정리, 문서를 독립 평가 구조와 명세 0.3에 맞게 재구성 |
| 2026-10-09 | 2차 기업미팅·팀장 오더 반영: 독립 평가 보고서에 `sourceRun` 추가 |
| 2026-10-14 | 외부 점검(Codex) 지적 8건 중 7건 반영: 보안 판정 오판 4건, 비교·지표 검사 허점 3건. wheel 보안 기준 JSON 누락은 팀 공유 파일(`pyproject.toml`)이라 보류. README·기준표·공유 문서의 낡은 표기도 정리 |
| 2026-10-14 | 평가 코드(`src/evaluation`, `tests/evaluation`, `scripts/check_dev_reports.py`)의 기존 주석을 쉬운 말로 다시 쓰고, 파일 머리말(역할·용어)과 함수·복잡한 줄 주석을 초보자 기준으로 추가. 코드 구조(AST)는 29개 파일 모두 이전과 같고 테스트 231개 통과 |
| 2026-10-14 | Python 3.13·dev `caa4144` 기준으로 정리. 기준표(D0)·Security 정적 판정(D1)·정상/결함 예제(D3)·결과 지표와 시연 자료 목록(D4)·공유 문서 3종 추가. 점검 수정(D1 오탐·미탐, 통과율 null, `missedDefect`, 비교 가능 검사 순서 의존, `HUMAN_REVIEW` 처리, 파싱 오류 처리). 명세 0.4. 인계용 문서(`claude_code_handoff.md`)는 내용을 이 문서로 옮기고 삭제 |
| 2026-10-15 | 2차 외부 점검(Codex) 7건 중 5건 반영: 보안 판정이 이름·인자·칸 종류·조건부 삭제에 속던 4건과 평가 보고서 환경 미대조 1건. 같은 종류의 구멍(함수 이름 바꿔치기, 처리기 등록 방법, 예외 객체 사용, 변수를 거친 비밀번호 노출)을 추가로 막고 회귀 시험 63개를 추가. wheel 보안 기준 JSON 누락(팀 공유 파일 `pyproject.toml`)과 `.python-version`(팀 Python 통일)은 팀장 승인·팀 회의로 넘기고 문서 설명을 바로잡음. 푸시 전 정리 |
| 2026-10-16 | 3차 외부 점검(Codex) 4건 반영: and·or를 거친 노출, 입력이 아닌 상수의 해시, 고정 salt, 다른 칸·다른 표 UNIQUE. 같은 종류의 구멍(결과를 버리는 이메일 정리, 평문 저장의 다른 길, 비밀번호가 나가는 다른 출구)을 추가로 막고 회귀 시험 60개를 추가. 이메일 UNIQUE는 사용자 표(비밀번호 칸이 있는 표)의 것만 인정하도록 바뀜 |
| 2026-10-16 | 4차 외부 점검(Codex) 11건 반영: 보안 판정 7건(입력을 버리는 함수·가짜 난수·이메일 해시·`*= 0`·SQL 문자열 오인·다른 계정 표의 UNIQUE·응답 필터링), 지표·비교·시연 자료 4건(이전 코드 평가 집계·수정 요청을 해결로 셈·WARM/COLD 차이·FIFO 멈춤). 해시 입력 판정을 허용 목록 방식으로 바꾸고, SQL은 실제로 실행하는 글자만 칸별로 읽고, 이메일 UNIQUE는 가입에 쓰는 표별로 판정. 회귀 시험을 추가하고 지원하는 구조(FastAPI + pydantic + SQLAlchemy)를 명시 |
| 2026-10-16 | 5차 외부 점검(Codex) 반영: 해시하는 값의 판정을 이름이 아니라 실제 출처로 바꿈(이름만 password인 이메일 변수, 이메일만 돌려주는 함수의 결과). 입력 필드를 덮어쓰는 우회와 `self` 필드도 막고 회귀 시험 35개를 추가 |
| 2026-10-16 | 6차 외부 점검(Codex) 반영: 변수 재대입 때문에 판정기가 멈추던 문제와, 버리는 해시까지 증거로 합치던 문제. 별칭·필드 쓰기 방식을 넓혀 막고(조합 시험 추가), 대표적인 큰 입력이 제한 시간 안에 끝나는지와 분석 한도를 넘으면 UNVERIFIED가 되는지를 별도 프로세스로 확인하는 시험을 추가(모든 입력의 선형 시간 복잡도를 증명하는 것은 아님). 작업량 한도는 저장소 코드 389묶음 측정값을 보고 정함 |
