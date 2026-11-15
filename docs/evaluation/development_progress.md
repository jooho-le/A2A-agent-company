# 개발진행사항

최종 갱신: 2026-11-15 · 평가 명세 0.4 · 작업 브랜치 `seokmin` · dev `3147711` 반영, 직전 평가 커밋 `059e9eb` 이후 변경 포함
공통 계약 기준: dev `caa414432af52f838ef5b62c993246b48013178c` (dev `08542c6`까지 schemas·Orchestrator·Agent·MCP 코드 변경 없음, 2026-10-28 확인)

이 문서는 4번 평가 모듈의 **현재 상태**를 정리한다. 평가 기준·정책은 [명세서](evaluation_spec.md),
요구사항별 검사는 [기준표](scn001_criteria.md), 설치·실행은 [환경 설정](environment_setup.md)에서 관리한다. Single·Multi 비교용 최종 시험지는 [최종본 v4](final_exam.md)와 [핵심본](final_exam_core.md)이다.
팀장 전달용은 [dev 변경 공유 11.14~](dev_changes_2026-11-14.md)와 [이전 공유](dev_changes_2026-10-30.md), 실험 실행은 [시범 실험 실행 절차](experiment_runbook.md), Single 비교 설계는 [Single 비교 기준선](single_baseline.md),
시연 자료 정리는 [시연 자료](demo_materials.md)에 있다.

### 2026-11-15 커밋 전 최종 상태 — 이 절을 우선

- **순환 중 개발 피드백:** QA·Security의 현재 Snapshot 검사에서 얻은 세부 항목별 실제값·PASS/FAIL/NOT_RUN/UNVERIFIED·사유를 `fixRequest.validationFeedback`으로 전달한다. 같은 보고서의 PASS 항목도 포함하며 기대조건은 모델 전달 사본에서 제외한다. 불변 원본과 최종 독립 평가의 기대값은 유지한다. [계약·범위](loop_validation_feedback.md).
- **채점지 세 개:** 요구사항별 `summary.md`, 기대값을 포함한 세부 결과 `check_details.md`·`.json`, 다섯 축별 `axis_scorecard.md`·`.json`을 제공한다. 축별 채점지는 기존 측정 결과를 모으며 추가 모델 호출·채점 실행을 하지 않는다. [축별 채점지](axis_scorecard.md).
- **최종 검토 보완:** 세부 FAIL/상위 SKIP 모순을 미검증으로 표시하고, 단계 기록·토큰 구분값·Run 상태가 없으면 오류 없음·사용량·정상 종료를 추정하지 않게 했다.
- **전체 검증:** `tests`·`checks` **5,780 passed / 0 failed / 0 errors / 0 skipped**, 로컬 Docker 평가 통합 검사 9개 포함, 약 25분. 두 묶음은 중복 없는 파일 분할이며 이전 부분 실행을 합산하지 않았다. Python 코드·테스트 537개는 실행 전후 동일했다. [검토·증거](precommit_verification_2026-11-15.md).
- **실험은 미실행:** 실제 비교 대상은 **Single 하나 / Multi Ver1 / Multi Ver2**다. Single에 Ver1·Ver2를 붙이지 않는다. 기존 계획 두 파일·제출 절차·쌍별 집계에는 아직 2×2 가정이 남아 있다. 세 조건의 실행·분모·비교 정책을 일치시키기 전에는 기존 절차로 공식 실험을 시작하지 않는다. [실행 절차의 현재 경계](experiment_runbook.md).

아래 내용은 구현·검증의 시간순 이력이다. 과거의 검사 수, 네 조합·4/20 Run 안내, 당시 미완료 항목은 현재 실행 승인이나 최신 상태로 읽지 않는다. 이번 전체 테스트에서 실제 비교 실험·유료 모델 호출은 수행하지 않았다.

### 2026-11-15 세 번째 채점지(정확도·속도·비용·과정·안정성) 추가 — 구현 이력

테스트를 돌렸을 때 Run 결과 폴더에 나와야 할 채점지를 세 개로 정했다. ① 요구사항별 채점지(`summary.md`) ② 세부 항목별 채점지(`check_details.md`) ③ 정확도·속도·비용·과정·안정성 채점지. ①②는 이미 나왔고, ③은 그동안 Run마다 여러 파일(`outcome.json`·`timing_metrics.json`·`process_metrics.json`·`agent_tables.md` 등)에 흩어져 있었다. 그래서 `axis_scorecard.md`·`.json`을 추가해 지표 ID별로 한 파일에 모았다.

- **값을 모으는 방법:** 이미 계산한 결과만 모으고 새로 측정하지 않는다. 없는 값은 미확인과 사유 코드로 남긴다(0으로 채우지 않음).
- **채점하지 못한 Run:** 시간·비용·안정성 칸은 남고, 정확도는 미확인, 제품 통과는 "아님"이다.
- **비교 결과에서 계산하는 지표:** 계획 Run 전체가 분모인 비율·분포·변동은 `comparison.md`에서 계산한다고 표시한다.
- **정확도 주 지표 정정:** 정확도의 주 지표는 ACC-01(요구사항 완전 충족률, 검사가 모두 PASS인 요구사항 수 ÷ 7)이다. `summary.md`의 "최종 시험 점수"(요구사항별 부분점수 평균)는 보조 지표다. 둘은 REQ-003의 검사 3개 중 일부만 통과할 때 달라진다(예: SEC-003만 FAIL이면 ACC-01 6/7 = 0.857, 부분점수 0.952).
- **시험 지문 노출 검사:** 결과 파일 목록(`RESULT_FILES`)에 새 파일을 넣어, 생성 코드에 채점 결과가 섞이면 노출로 잡게 했다.
- **검증:** 최초 시험 5개에 누락된 단계 기록·토큰 값·Run 상태를 다루는 시험 3개를 보완해 해당 파일 8개가 통과했다. 최종 전체 5,780개 실행에도 포함된다. 결과 예시 Run을 실제 이미지로 다시 채점해 세 채점지가 모두 나오는 것을 확인했다(실제 모델 비교 Run은 아님).

### 2026-11-15 보호 시험 0.7.0 최종 점검 — 이전 검증 이력

커밋(`ad8c2da`) 뒤 보호 시험이 0.6.0(검사별 세부 관찰 기록 `check_details`)과 0.7.0(FAIL 뒤에도 독립 세부 항목을 계속 검사)으로 올라갔다. 9개 검사·7개 요구사항·점수 산식은 그대로다. 이 상태에서 4번 코드를 다시 점검했다.

| 항목 | 값 |
|---|---|
| 보호 시험 | **0.7.0**, 공식 최종 SHA-256 `cd7a246d2309ac7e3e281355be270a9ea8866e837da03058f8caaf10ef8f121f` |
| 공통 실행 이미지·시작 커밋 | `sha256:c712fdf445fe01217001440459070bf1e2b0a8d94ad3b9103bacfedddf7dae5a` · `e6e7158c356ff0be5ffa12ec59894d2217f61ca8` (변경 없음) |
| 사전점검 | Ver1·Ver2 모두 **ready=true**, 37개 항목(실제 브라우저 실행 포함) 전부 PASS. Ver1 보호 파일·도구 설정이 0.7.0 참조와 일치 |
| 시범 4 Run 제출 본문 | 실제 설정으로 만들어 계획과의 차이 0건(Single/Multi × Ver1/Ver2) |

**점검 내용과 수정**
- 0.7.0 검사 9개를 이전 판과 한 줄씩 대조했다. FAIL 조건·미검증 조건은 같고, 실패 뒤에도 독립 항목을 계속 관찰하는 점만 다르다(실패는 뒤의 PASS·미검증으로 덮이지 않는다: dev 실행기 순위 FAIL > SKIP > PASS).
- 세부 결과(`check_details`)는 판정·점수에 쓰이지 않는 보조 자료임을 확인했다. 전달 사본 압축(`compact_source`)은 단계마다 AST가 원본과 같을 때만 쓴다. Ver1 Unit 설정은 61,334바이트로 dev 한도까지 약 4.2KB 남았다(검사 파일을 더 늘리면 한도 시험이 먼저 막는다).
- **실제 dev Host 계약 시험 추가:** 측정 실행기를 가짜 Host가 아닌 실제 dev 앱에 붙여 토큰·중복 방지 키·200 재생·409·422·401, 접수 조회 지문 일치(실행기가 기록하는 지문 = dev 접수 기록 지문), 상태 조회, 응답 유실 복구(접수됨/접수 안 됨 모두 Run 1개)를 확인한다(`tests/evaluation/test_experiment_run_host_contract.py`, 4개).
- 수정 2건: ① 사전점검의 브라우저 보고서가 JSON이지만 객체가 아니면 예외 대신 `HOST_BROWSER_REPORT_INVALID`. ② 실행기에서 원장 키 변수와 이름이 겹치던 중복 방지 키 변수 이름 분리(동작 변화 없음).

**검증:** 저장소 전체(`tests`·`checks`, Docker 접근 차단 실행기) **5,732 passed / 0 failed / 13 skipped**, 2,768.89초(46분 9초). 생략 13개는 실제 이미지 시험이며 공통 이미지로 따로 실행해 **13 passed**. 전체 실행 뒤 넣은 계약 시험·수정 2건은 관련 7개 파일 **179 passed**로 확인했다(전체 재실행 수치에 합산하지 않음). 유료 모델 호출은 0회다.

**남은 것:** 키·토큰 주입(`A2A_PLATFORM_LLM_API_KEY`, `A2A_HOST_API_TOKEN`)과 Host 기동 → 시범 4 Run(유료) → 공식 한도 확정 → 공식 20 Run. 주호 확인(브라우저 `chromium_sandbox=False`, 병합 영향). SCN-002는 별도 과제.

### 2026-11-15 dev `3147711` 병합·실험 실행기 Host 인증 대응

주호의 새 dev(83~88번: Host 서비스 인증, 승인 설정 조회, Run 중복 생성 방지, 접수 조회·복구, Source ZIP)를 합쳤다. 겹친 파일은 `host.py`(같은 자리의 import 줄 충돌, 양쪽 유지)와 `routes/runs.py`(자동 병합) 두 개다. 새 계약 때문에 측정 실행기가 Run을 제출하지 못하게 되는 동작 충돌 두 가지를 고쳤다.

| 새 dev 계약 | 4번 대응 |
|---|---|
| 실제 Host API는 `/api/v1/...` 전체에 `Authorization: Bearer <A2A_HOST_API_TOKEN>` 필요 | `experiment_run`·`experiment fetch`가 환경 변수의 토큰을 헤더로 보낸다. 토큰은 기록·결과 파일에 남기지 않고, 없으면 제출 의도를 남기기 전에 `HOST_API_TOKEN_REQUIRED`로 멈춘다 |
| Run 생성에 `Idempotency-Key`(UUIDv4) 필수, 같은 키·본문은 같은 Run을 200으로 재생 | 제출 의도를 기록할 때 키와 Host 요청 지문을 함께 남기고(`runs.jsonl` 형식 3) 그 키로 보낸다. 200 재생도 성공으로 받는다 |
| 접수 조회 `GET /api/v1/submissions/{key}` | 응답을 잃은 제출의 명시적 복구(`--reconcile-submission`)를 Host DB 직접 조회에서 키 조회로 바꿨다. 접수 기록이 있으면 그 Run을 잇고(요청 지문 대조), `SUBMISSION_NOT_FOUND`가 확인될 때만 같은 키·같은 본문으로 다시 보낸다. 옛 Host의 일반 404·장애면 다시 보내지 않는다 |

**현재 상태**

| 항목 | 값 |
|---|---|
| 보호 시험 | **0.5.2**(앱 import 단계 로그까지 수집), 공식 최종 SHA-256 `dff9e3b5ee543b819c5326306529ae522db05d3b2380497f0809287fa557f624` |
| 공통 실행 이미지 | `capstone-sandbox:scn001`, `sha256:c712fdf445fe01217001440459070bf1e2b0a8d94ad3b9103bacfedddf7dae5a` |
| 시작 커밋 | `e6e7158c356ff0be5ffa12ec59894d2217f61ca8` |
| 사전점검 | Ver1·Ver2 모두 **ready=true**, 37개 항목(실제 브라우저 실행 포함) 전부 PASS |
| 실험 이미지 채점 | 실제 컨테이너 시험 10개 + 공식 시작 코드 통과 가능성 3개 = **13 passed**(0.5.2) |
| 병합 영향 시험 | 새 dev·Host 관련 145 passed, 측정 실행기 111 passed |

**남은 것:** ① 키·토큰 주입은 사용자가 한다(`A2A_PLATFORM_LLM_API_KEY`, `A2A_HOST_API_TOKEN`; Agent 토큰은 PyCharm 실행 구성에 등록됐지만 터미널 실행 때는 따로 넣는다). ② Host 기동과 접속 확인, 시범 4 Run(유료) → 공식 한도 확정 → 공식 20 Run. ③ 주호 확인: 브라우저 `chromium_sandbox=False` 결정(27·39번 문서와 다름). ④ SCN-002 전자문서 평가는 별도 과제.

**검증:** 병합 뒤 저장소 전체(`tests`·`checks`, Docker 접근 차단 실행기)를 한 번에 실행해 **5,689 passed / 3 failed / 13 skipped**, 2,407.21초(40분 7초). 실패 3개는 Codex가 만든 `test_experiment_selection.py`의 시험 준비 코드가 기록 형식 2로 제출 의도를 남긴 것이었다. 형식 3의 두 칸(중복 방지 키·Host 요청 지문)을 넣은 뒤 기록을 읽는 시험 4개 파일 **78 passed**. 생략 13개는 실제 Docker 이미지가 필요한 시험이며 공통 이미지로 따로 실행해 **13 passed**. 수정 뒤 전체 5,689개를 다시 실행한 것으로 합산하지 않는다. 유료 모델 호출은 0회다.

### 2026-11-15 최종 점검·로컬 인증 설정

Multi 내부 토큰 4개와 Single 토큰 1개를 로컬 PyCharm 구성에 등록했다. Single/Multi × Ver1/Ver2의 사전 검사와 임시 DB Host 기동·인증·종료를 확인했으며 실제 모델 전송은 차단했다. 앱 import 단계의 로그 노출 누락을 고쳐 시험 묶음을 **0.5.2**로 갱신했다. 네 조합의 결과를 한 폴더에서 반대 축을 고정해 비교하는 기능·pair-id 절차, 브라우저 준비 검사에서 정리 실패를 미검증으로 처리하는 기능도 보완했다.

저장소 전체 검사: **5,634 passed / 1 failed / 0 errors / 0 skipped**. 한 건은 검사 도중 시험지 버전을 수정하여 이전 입력 기록의 재생이 거부된 경우다. 버전 수정 후 해당 검사를 포함한 실제 Docker 10개와 시작 코드 이미지 3개, 변경 영향 범위의 회귀 검사는 모두 통과했다. 전체 검사를 수정 후 다시 실행한 것으로 합산하지 않는다. 자세한 수정·검사 수·증거·한계는 [최종 점검 기록](final_review_2026-11-15.md)에 있다. 유료 모델 호출과 실제 비교 실험은 아직 하지 않았다.

### 2026-11-15 앞선 점검 4건 수정·실험 환경 준비 도구

4번 코드 전체 점검에서 나온 4건을 고쳤다. 모두 커밋 전이다.

| 점검 | 수정 | 확인 |
|---|---|---|
| 저장(커밋)하지 않는 앱이 QA-SIGNUP-001에서 FAIL이 아니라 미검증(`DATABASE_URL_NOT_USED`), REQ-001 상한에 '충족 가능'으로 들어감 | 성공 뒤 행이 없을 때 그 요청 중 앱이 평가용 DB에 SQL을 실행했으면 FAIL, 전혀 쓰지 않았으면 미검증. 보호 시험 **0.5.1**, 공식 최종 SHA-256 `98878fe65e854da4c7e2d02864951693601ade8911df2722a3f47cc4b8399a64`, Ver1 보호 파일 재생성 | 합성 변형 `no_commit`이 시험지 세 벌 모두 QA-001 FAIL. 다른 DB를 쓰는 변형은 그대로 미검증 |
| 관찰 기록(로그·도구 출력·모델 입출력·A2A 출력) 저장 실패가 예외로 번져 측정 Run을 실패시킬 수 있음 | 제품 실행은 계속하고 구간을 `INCOMPLETE`·영수증 누락으로 남겨 평가가 미검증으로 봄([dev 변경 공유](dev_changes_2026-11-14.md)) | 모든 영수증 저장 실패 상태로 실제 파이프라인 실행: Run 결과 동일, 노출 검사 UNVERIFIED. 고치기 전 코드에서는 새 시험 3개 실패 |
| 시험 코드 속 해시 예시도 Run 기록 노출 FAIL | 4번 결정: 출처와 상관없이 완전한 해시 형식 값은 노출로 유지(기록만으로 실제 사용자 해시와 구분할 근거가 없음). 제품 점수·비교 지표 밖이며 [README](../../src/evaluation/README.md)에 기준을 적음 | 기준 문서화 |
| HUMAN_REVIEW 전환의 `PROCESS_GATE_REJECTED`를 다루는 시험 없음 | dev 저장소 기록과 평가 집계(`ORCHESTRATOR_OUTPUT` 단계 1건) 시험 추가 | 2개 시험 |

dev Unit 설정 크기 시험은 임의의 '순환 두 범위 합 56KiB' 대신 dev 실제 한도(Unit 설정 전체 64KiB, 직렬화 통과)로 확인하고 4KiB 이상 여유를 요구한다. 실제 Ver1 Unit 설정은 약 58.8KB다.

**실험 환경 준비:** `python -m evaluation.environment`(status·wheels·browsers·image·configure·all)를 추가했다. Host 샌드박스와 D5 채점 공통 이미지, 같은 lock의 시작 저장소, Ver1·Ver2 도구 설정, 계획의 이미지·하드웨어·시작 커밋, 사전점검을 한 번에 준비한다. 다운로드 단계는 `--allow-network`가 있어야 실행한다. 정한 값: Playwright 1.65.2, Bandit 1.9.4(규칙 B105~107·B303·B324), email-validator 포함, 도구 한도(빌드 60초, 단위·브라우저 90초, 보안 60초, 호출 120초). 실행 순서는 [시범 실험 실행 절차](experiment_runbook.md)에 있다.

**실행 환경 준비 완료(같은 날, 다운로드 승인 뒤):** 공통 실행 이미지 `capstone-sandbox:scn001`(`sha256:c712fdf4…dae5a`, 약 329MB), lock(wheel 33개, `sha256:d2767900…dcc0`), 시작 커밋 `e6e7158c…`, Ver1·Ver2 도구 설정, 두 계획의 이미지·하드웨어·시작 저장소 칸을 채웠고 사전점검이 두 계획 모두 **ready=true**(모든 항목 PASS)다. 같은 제한(권한 제거·읽기 전용·네트워크 없음·UID 10001)에서 패키지 버전과 브라우저 실행을 확인했다. 준비 중 드라이버 라이브러리(`libatomic1`) 누락, Playwright 1.65 의존성 목록 형식 변경, 로컬 베이스의 digest 참조 문제를 도구에 반영했다([실행 절차](experiment_runbook.md)).

**브라우저 실행 문제 해결:** dev 브라우저 실행기의 `chromium_sandbox=True`가 dev 샌드박스 제한(`--cap-drop ALL`·기본 seccomp)에서 `No usable sandbox!`로 실패했다(사용자 네임스페이스 차단이 유일한 원인, 실측). 사용자 승인으로 `chromium_sandbox=False`로 바꿨고 컨테이너 격리는 유지한다. 주호 문서 27·39번과 다른 결정이라 [dev 변경 공유](dev_changes_2026-11-14.md)에 근거와 확인 요청을 남겼다. 설정 대조만으로는 이 문제를 놓쳤으므로 사전점검에 실제 브라우저 실행 항목(`hostBrowser.<구조>`)을 더했다. 바꾼 뒤 두 계획 모두 ready=true, dev 브라우저 시험 242개·사전점검 시험 65개 통과.

**남은 것:** ① API 키(`A2A_PLATFORM_LLM_API_KEY`)·Agent 토큰 주입은 사용자가 직접 한다. ② Host 기동과 접속 확인, 시범 4 Run(유료 호출)은 사용자 지시 뒤다.

**검증:** 평가 전체(`tests/evaluation`)와 Host·Single/Multi 연동 33개 파일을 한 번에 실행해 **2,508 passed / 0 failed / 10 skipped**, 759.38초(12분 39초). 생략 10개는 `EVAL_DOCKER_IMAGE`가 필요한 실제 Docker 시험이다. 유료 모델 호출·다운로드·이미지 빌드는 하지 않았다.

### 2026-11-15 Docker 재허용·4번 전체 재점검

사용자 요청으로 로컬 Docker Desktop을 시작하고 Linux/arm64 평가 이미지를 준비했다. hash-locked wheel 22개의 오프라인 설치·의존성 검사·실제 설치 버전 대조를 통과했다. 이미지·lock·보호 시험 지문과 재실행 방법은 [실제 Docker 검증 기록](docker_verification_2026-11-15.md)에 보관한다. 아래 Docker 금지 기록은 **그 요청이 유효했던 이전 검사 당시**의 기록이다.

실제 컨테이너에서 정상 서비스의 최종·시범 채점, 평문/약한 해시 결함 탐지, DB 초기화 실패, 환경 lock 불일치 거부, umask 077 입력 재생, 두 버전 각 2회 회귀, 격리 제한, timeout 후 해당 컨테이너 삭제를 확인했다. 합성 서비스의 평가기 검증이며 실제 LLM 생성물·전체 Host 순환·Single/Multi 공식 실험은 별도다.

코드 재점검에서 D5 이미지 lock 관찰·입력 권한·부분 커버리지·timeout 정리, Agent 평가표의 다른 결과물/누락/중복 검사 판정, 수정 주기·검증 쌍의 잘못된 연결, 정적 보안 보조 판정의 저장값·이메일 정규화 연결을 보완했다. 보호 시험 0.5.0과 지문은 유지한다.

평가 전체(`tests/evaluation`)와 Host·Single/Multi 연동 39개 파일 **2,518개를 한 번에 검사하여 2,517 passed / 1 failed / 0 errors / 0 skipped**, 1228.12초를 확인했다. 실제 Docker 검사 10개는 모두 통과했다. 실패 1개는 새 이미지 lock 사전 검사를 반영하지 않은 기존 강제 종료 시험 대역이었다. 생산 코드는 변경하지 않고 해당 대역만 보완한 뒤 **관련 두 파일 89개가 모두 통과**했다(41.14초, 실제 Docker 정상 채점 재검사 포함). 현재 미해결 실패는 0개다. 전체 실행 이후 바뀐 점검 대상 파일은 해당 테스트 파일 하나뿐임을 SHA-256으로 대조했다. 수정 후 전체 2,518개를 다시 실행했다거나 중복된 검사 수를 합산했다고 표시하지 않는다.

**4번 전체 완료로 판정하지 않는다.** SCN-001 평가 기능 구현·합성 Docker 검증과 별개로 공식 Host/D5 환경·공통 시작 커밋 동결, 실제 모델 시범 및 공식 20 Run, Planner 사람 검토·발표 자료가 남는다. 독립적인 테스트 수정 경로는 Host 시작/완료·roundId 증거가 없으면 확정 계수하지 않는다. SCN-002는 회원가입 MVP 이후의 별도 확장 측정 과제로, 시나리오/API/저장/권한 계약과 시험지·시작 코드 확정 후 전용 평가 구현이 필요하다. 개발정의서의 확정 MVP는 SCN-001이므로 SCN-002를 현재 MVP 구현의 필수 누락으로 세지 않는다.

### 2026-11-15 잔여 기능 구현 — Docker 재허용 전 검증 기록

앞선 3회 점검 때 남아 있던 SCN-001의 Host 완료 패키지 생성, 과정·출력 자동 관찰, 실제 입력 저장·재생을 구현했다. 아래 날짜별 기록의 '미구현/남음'은 해당 점검 당시 상태이며, 당시 구현 여부는 이 절에 기록하며, 이후 Docker 재허용 검증은 문서 맨 위 최신 절을 우선한다. 합성 서비스·시험 대역 검증과 실제 비교 실험 완료는 구분한다. SCN-002 전자문서는 편집/제출·조회 범위와 연결 계약이 미정이므로 시나리오별 평가 시험 구현이 별도로 남아 있으며, 이 문서로 전체 시나리오 완료를 주장하지 않는다.

| 항목 | 이번 연결 | 판정 경계 |
|---|---|---|
| Host 최종 패키지 | 실제 보고서 저장 후 완료 시각을 읽어 보고서·완료 기록·사건을 원자 저장. 재시작 복구·중복 처리·자동 export. 상태 API와 최대 30초 저장 대기 | 누락·보류·보조 재개·저장 실패는 자동 완료가 아님. FAIL/ABORTED 패키지도 성공으로 세지 않음 |
| 과정 자동 계측 | 실행 전 정책 고정, 중앙 거부 사건, 실제 QA 입력·Snapshot 테스트 변경, 호출/출력 경계 영수증과 자동 집계 | 정책·자동 구간이 온전할 때만 비활성 A2A/LLM 자동 재시도·미지원 재계획 0건. 누락은 unknown |
| 출력 관찰 | Run에 연결된 공개 모델 출력·도구 인자, A2A 저장 전 자료, Python 출력/로그·도구 출력. 실제 등록 파일 바이트 자동 인계 | 런타임은 패턴·해시 영수증으로 제한. 사후 임의 knownValues 대조 불가는 UNVERIFIED, 잘림·미종료 구간도 미검증. OS 전체 관찰 아님 |
| 동일 입력 재생 | 실제 생성값·요청 순서를 비공개 파일로 저장. Run·Snapshot·시험·계약·이미지/lock·실행기 조건을 대조하여 재실행. 두 버전 각 2회 이상 회귀 확인 | 조건/입력 불일치·판정 변동·미검증은 회귀 확정 제외. 스케줄·해시 salt까지 고정하지 않음 |
| 재채점 보존 | `.private-inputs`와 `.evaluation-history`에 입력 및 원본 결과·계약 보존. 이전 버전 전체 결과는 `version_outputs`에 추가 | 현재 결과를 `--force`로 교체해도 원본 재생 기준은 삭제하지 않음. 공개 오류 상세에 시험 비공개 값을 노출하지 않음 |
| 준비 설정 | 승인된 모델/시범 한도로 로컬 runtime 생성, 공식 가격표 연결, Ver1 보호 케이스/범위/인계 설정 0.5.0 생성, `preflight --no-docker` 추가 | 실제 모델 호출이나 컨테이너 검사 없이 파일만 확인. Docker 검사는 명시적 미검증이며 ready=false |

보호 시험은 **0.5.0**, 최종 SHA-256 `b60ec485c5f9886c17f0f33fe449f801a0a5c6ad0b685bc8ed48066eedaeaefb`이다. 입력 기록·재생 연결로 변경됐으며 0.4.0 이하 결과와 혼합하지 않는다. 요구사항과 9개 검사 기준은 유지했다. 사용법·보존 경로·관찰 범위는 [평가 실행 설명](../../src/evaluation/README.md)을 따른다.

**실험 전제조건은 별도로 남는다.** 실제 고정 이미지와 그 환경에서 만든 의존성 lock, 동일 시작 커밋/Host source·tools 설정, 하드웨어 프로필, 로컬 API 인증, 시범 실행 및 공식 반복 실험이 필요하다. 이 점검 당시에는 Docker 금지 요청에 따라 상태 조회·이미지 조회·다운로드·빌드·실행 모두 하지 않았다. 준비 파일 검사는 `--no-docker`로 수행했고 Ver1·Ver2 모두 Docker 검사 비활성 및 실제 시작/이미지/도구 설정 미확인으로 준비 미완료다. 시범 모델·한도·반복 수가 다시 미정이라는 뜻은 아니다. 금액 상한을 새로 설정하지 않았다.

**최종 검증:** 저장소 전체 5,523개 항목을 Docker 접속 차단 상태에서 실행했다. 최초 전체 결과는 **5,520 passed / 2 failed / 1 skipped / 0 errors**, 2,336.43초(38분 56초)였다. 실패한 두 항목은 아래와 같이 수정했고, 수정 후 시간 검사 파일 **29개**와 재개 응답 관련 **124개**, 서로 다른 **153개가 모두 통과**했다. 현재 확인된 미해결 실패는 없다. 이는 최초 전체 실행과 수정 후 관련 재검증을 구분한 기록이며, 수정 후 전체 5,523개를 다시 실행했다고 표시하지 않는다. 생략한 1개는 실제 Docker 이미지·daemon이 필요한 `test_real_container_run`이다. 경고 942개는 SDK 시험의 cross-replica stream 미설정 및 도우미 클래스 수집 경고다. 최종 `git diff --check`도 통과했다.

전체 실행 JUnit: `/tmp/part4-completion-final-verified-20261115.xml`, 로그: `/tmp/part4-completion-final-verified-20261115.log`. 실제 Docker 명령·소켓 접속을 막는 별도 검사 실행기를 사용했고, Docker라는 이름이 필요한 기존 시험은 내용이 일치하는 연결 기능 없는 시험 프로그램만 허용했다. 요청 이후 Docker 상태/이미지 조회·다운로드·빌드·컨테이너 실행 및 유료 API 호출은 수행하지 않았다. 실제 Single/Multi 측정 결과로 이 수치를 사용하지 않는다.

전체 검사에서 재현된 두 항목을 별도로 보완했다. 첫째, 동기 검증기의 시간 시험은 100ms에 저장소 준비 시간까지 포함해 첫 호출 전 만료될 수 있었다. 시험에서 verifier의 실제 대기를 확인한 뒤 모듈 한정 논리시계로 마감 이후를 검증하도록 변경했다. 이벤트루프 비차단, 호출/시도 1회, 재시도 금지, 미검증 결과를 확인하며 생산 코드의 timeout은 바꾸지 않았다. 해당 파일 29개가 통과했다. 둘째, 이전 AUTH/INPUT_REQUIRED 응답이 늦게 도착해 정상 후속 요청이 HTTP 500으로 끝나는 경쟁을 수정했다. 같은 Task·Context의 직전 중단 attempt에 한해 현재 승인 상태를 다시 읽으며 재제출하지 않는다. continuation 14개와 Agent API/저장소/실행 계약/Host 연동 110개, 서로 다른 124개가 통과했고 독립 검토를 마쳤다.

### 2026-11-15 기존 5건 수정 및 3회 반복 점검 (이전 상태)

요청한 순서는 **기존 5건 수정 → 1차 완성 확인·전체 코드 점검·수정 → 2차 반복 → 3차 반복**이다. 점검 대상은 4번 평가 코드 전체와 이 기능을 연결하는 Host·Single/Multi 코드이며, 회차마다 담당을 바꿔 교차 검토한다. 아래 검증은 합성 서비스·시험 대역 및 로컬 통신 기준이다.

초기 수정:

| 문제 | 수정 |
|---|---|
| Agent 사용량 누락이 0원·PASS | 전체 영수증 개수·일관성·중복·Run/단계 배정 확인. 불완전한 확정값은 미확인, 확인된 부분값은 `known…`으로 분리 |
| 일부 요구사항만 재검증해도 수정 완료 PASS | Issue 대상 전체의 결과를 대조. 누락·미검증은 UNVERIFIED, 확인된 실패는 상세 근거로 보존 |
| FAIL+미검증 요구사항의 상태 변환 | 원본 PASS/FAIL/UNVERIFIED 개수를 보존. 미검증 중 확인된 실패를 별도 집계하되 충족률 상·하한은 기존 올바른 값을 유지 |
| TIMEOUT 후 다음 측정 Run 제출 | 배치 중단 및 다른 순서로의 새 제출도 차단. 기존 Run 종료 확인·인계 복구는 허용 |
| 결과 인계 실패 Run을 미시작으로 표시 | 비교에 `runs.jsonl` 연결. 수락·종료 기록이 있는 미수집 Run을 별도 행으로 보존하고, 근거 없는 부족분은 실행 여부 미확인으로 표시 |

초기 검증: Agent 평가표 80개, 실행기·복구 76개, 집계·원장·정확도 연결 197개 통과(묶음 간 중복 가능). 초기 5건의 수정과 별도로 아래 3회 점검을 진행한다.

| 회차 | 확인·추가 수정 | 검증 |
|---|---|---|
| 1차 | 완료. 부가 파일까지 원자 저장·재시도 복구, Agent/의미 검토 사용량 요약 불일치·음수 호출 방지, Security 보고서 도구 규격 정합화, 같은 실행 슬롯의 다른 Run 거부, 잘못된 JSON/대기 간격 처리, 잘못된 테스트 결과 자료형의 미검증 처리 | 담당 영역 검사 196·190·200개, 어댑터 12개 통과. 보조 요약·시연 목록 50개 통과/1개 생략(로컬 socket 제한). 초기 연결 233개 통과. 묶음 간 중복 있음 |
| 2차 | 완료. 이전 버전 회귀 집계의 최종 요약 혼입 차단, 실행 순서 건너뛰기 차단, 손상된 보조 커버리지 격리, 직접 score/record 경로의 자동 구간 덮어쓰기 차단, 조건이 다른 Single/Multi 쌍 제외 | 담당 영역 검사 232·195·269개, 요약 33개 통과. 초기 수정 연결 288개 통과. 묶음 간 중복 있음 |
| 3차 | 완료. 최초 직접 저장·옛 보류 기록의 자동 구간 보존, 중복 요구사항 ID 거부, 인계 원장 최신 상태 복구, 준비 검사의 보호 케이스·가격표 대조, 가상 0번 줄 커버리지 제외 | 담당 영역 검사 450·215·240개, 요약/어댑터/줄 기록 40개 통과. 묶음 간 중복 있음 |

**3회 수정 후 최종 통합 검사:** `tests/evaluation` 전체와 Host·Single/Multi 연동 21개 테스트 파일을 한 번에 실행하여 **2,038 passed / 1 skipped / 0 failed / 0 errors**, 848.52초(14분 8초)를 확인했다. 중복 합산이나 부분 재실행을 합친 숫자가 아니다. 생략한 1개는 `test_real_container_run`으로, `EVAL_DOCKER_IMAGE`와 실행 중인 Docker가 필요한 검사다. 경고 284개는 테스트 하네스의 cross-replica event stream 미설정 283개와 도우미 클래스 수집 경고 1개다. 실제 유료 모델·생성물 Docker 실험은 실행하지 않았다. JUnit 기록: `/tmp/part4-three-rounds-final-20261115.xml`, 실행 로그: `/tmp/part4-three-rounds-final-20261115.log`.

보호 시험은 **0.4.0**, 최종 SHA-256은 `a5b25379b33103057a9f80355e3d1bc9f5737f0316c9135c3fd4c904b7c6dd37`로 유지된다. 공통 보고서 스키마 사본 4개와 현재 dev 원본의 일치, 스키마 5개 문법, 변경 형식 검사를 통과했다. 과거 직접 채점 결과 중 bundle과 경계 표식이 모두 없는 자료는 당시 Run 상태가 저장되지 않았으므로 자동 구간을 소급 복원할 수 없다. 새 직접 저장은 이 경계를 보존한다.

완성 판정에서는 **구현한 기능의 결함 수정**, **아직 없는 Host/평가 기능**, **실제 실험 준비와 증거**를 구분한다. Host 최종 보고서·패키지 완료 사건 생성, 전체 과정·출력 계측, 동일 입력 저장·재생은 여전히 구현 잔여다. 이미지·lock·시작 커밋·운영 설정과 시범/공식 실험은 별도 준비·검증이다. 이 조건이 남아 있는 동안 4번 전체 완료로 표시하지 않는다.

현재 시범 Ver1·Ver2 계획의 준비 검사도 다시 실행했다. 모델·반복 수·호출/시간 한도·실행 순서는 PASS지만, 하드웨어 프로필·실제 시작 커밋·의존성 lock·Host 설정·고정 이미지와 Docker/평가 패키지는 미확인으로 `ready=false`다. 모델이나 시범 한도가 다시 미정이 된 것은 아니며, 준비 검사 자체는 실제 모델·컨테이너 비교 실험을 수행하지 않는다.

### 2026-11-14 재점검 후 추가 6건 수정

앞선 8건 수정 뒤 재현된 경계 조건을 추가로 보완했다. 아래 내용이 같은 날 앞선 기록보다 최신이며, 실제 모델·Docker 비교 실험 완료와는 구분한다.

| 문제 | 보완 |
|---|---|
| 원래 DB 엔진의 SQL 로그에 저장 해시가 노출돼도 보안 검사 PASS | 모든 가입 요청이 생성물의 원래 `get_db`·엔진·로그 설정을 사용하도록 변경. `echo=True` 노출 변형을 보안 검사로 검출 |
| 평가기가 테이블을 만들어 DB 초기화가 없는 앱도 PASS | 관찰용 DB 연결은 읽기 전용이며 DB·테이블을 만들지 않음. 생성물의 원래 초기화로 가입해야 통과. 첫 요청에서 정상 초기화하는 앱도 허용 |
| Host가 Run을 수락했으나 응답을 못 받으면 재시도 때 중복 제출 | 전송 전 `SUBMITTING` 기록, 프로세스 간 잠금, 기록 파일 원자 교체. 응답 불명확 시 자동 재제출 금지, 명시적 확인으로 기존 Run만 복구 |
| 계획을 바꿔도 같은 순서의 이전 Run 재사용 | 전체 계획·요청문·실행 순서 지문 고정. 완료된 Run 재사용과 다음 순서 제출에도 변경된 계획 거부 |
| 인계 폴더 생성 후 기록 저장 실패 시 복구 불가 | 기존 인계의 파일 해시·Run·설정·완전성을 검증해 재사용. 불완전한 인계는 보존 후 같은 Run에서 다시 생성 |
| 사람 보조 후 처음 내보낸 성공 결과가 자동 정확도·속도·비용에 혼입 | 원본 재개 사건을 기준으로 `QUARANTINED_ASSISTED` 분리. 최종 자동 채점 생략, 자동 구간 값은 미확인, 계획·실행 슬롯 유지. 과거 결과도 비교 시 다시 확인 |

보호 시험 묶음은 0.4.0으로 변경했다. 최종 SHA-256은 `a5b25379b33103057a9f80355e3d1bc9f5737f0316c9135c3fd4c904b7c6dd37`이다. 기존 0.3.0과 같은 시험 지문으로 취급하지 않으며, 새 실험 준비 파일과 등록표에는 현재 지문을 사용해야 한다.

**검증:** 평가 전체와 연동 검사 4개 파일을 실행해 **1,640 passed / 2 failed / 1 skipped**, 379.53초였다. 두 실패는 합성 앱에 `lifespan`을 도입하면서 기존 `startup` 이벤트 시험 준비 코드가 실행되지 않은 문제였다. 시작 성공·실패 검사를 `lifespan`·기존 startup 이벤트 각각으로 보완한 뒤 해당 파일 **34 passed**를 확인했다. 전체 결과에서 그 파일의 이전 32개를 새 34개 결과로 대체하면 **1,644개 통과 / 1개 생략 / 남은 실패 0개**다(전체 재실행 수치가 아닌 전체 검사+수정 후 해당 파일 재검사). 생략은 실제 Docker 이미지·daemon이 필요한 검사다. 유료 모델·실제 생성물 Docker 비교 실험은 실행하지 않았다. `git diff --check`와 시험지 문서의 지문 일치도 확인했다.

아직 남은 구현은 Host의 최종 보고서·패키지 완료 기록 생성(TIME-02·자동 완료 PASS), 모든 관문 거부·A2A/LLM 재시도·테스트 변경·출력 자동 수집이다. 실제 이미지·시작 커밋·운영 설정 준비와 시범·공식 실험도 별도로 남아 있다.

### 2026-11-14 4번 파트 마무리 구현

11.14 합의: 4번 파트를 구현하는 데 필요한 결정은 4번이 정한다. Ver1·Ver2는 둘 다 쓴다. 이에 따라 개발정의서 D0~D6에 남은 연결을 구현했다. dev 쪽 변경은 [dev 변경 공유](dev_changes_2026-11-14.md)에 있다.

| 항목 | 구현 | 확인 |
|---|---|---|
| D0 연결 규격 | dev 시나리오 계약에 `externalContract`(앱·DB·User 모델·가입 API·시작 파일·패키지)를 넣고 Planner·Developer·QA·Security·Single 입력과 의미 검토 기준에 전달. 평가는 동결 계약의 규격이 시험지 연결부(`CONNECTOR`)와 다르면 설정·채점을 만들지 않음 | dev 시험 8개, 평가 시험(연결부·CONNECTOR·dev 계약 일치) |
| D0 시작 저장소 | `evaluation.starting_source`: 골격의 시작 파일 + 실행 이미지 lock으로 별도 git 저장소(고정 작성자·시각 커밋)를 만들고 Host 시작 코드 설정과 lock 해시를 냄 | dev 시작 코드 검증 통과, 같은 입력이면 같은 커밋 |
| D2 Ver1 보안 보호 케이스 | 케이스 파일 2판(케이스별 역할). Host는 역할이 보호 범위의 역할과 같을 때만 Security 보호 범위를 받음. 평가 쪽 Ver1 설정이 2판을 만듦 | 평가 쪽 실제 Ver1 설정(QA 6 + 보안 3)이 Host 검사 통과 |
| D6 측정 Run 실행(11-A) | `evaluation.experiment_run`: 계획 값으로 Host 실행 설정 생성, 교차 실행 순서, 실험 Run 선언 제출, 멈춤 무재개 대기, 인계 폴더 생성·`runs.jsonl` 기록 | 실제 dev Host 설정 검사로 만든 제출 본문이 Host 제출 검사 통과 |
| 결정 값 | 모델 `gpt-4.1-mini-2025-04-14`·temperature 0. 시범 한도: 모델·도구 호출 200, 출력 8,192, 응답 120초, 60분. 반복: 시범 조합마다 1회 → 공식 조합마다 5회(공식 한도는 시범 사용량으로). 측정 Run 질문·보류는 종료로 집계 | 시범 계획 [Ver1](../../examples/evaluation/scn001-pilot.ver1.plan.json)·[Ver2](../../examples/evaluation/scn001-pilot.ver2.plan.json) |
| dev 시험 정리 | 의미 검토 뒤 갱신 안 된 시험 4개 패치 적용 | 두 파일 13개 통과 |

**검증(2026-11-14):** 저장소 전체 `python -m pytest -q` **5,141 passed / 1 failed / 1 skipped**, 35분 54초.
- 실패 1개(`test_mcp_execution_runtime`의 느린 검증기 시험)는 부하에 민감한 시간 시험이다. 따로 세 번 돌리면 모두 통과하고, 그 시험과 MCP 코드는 `ea06d12`에서 바뀌지 않았다.
- 건너뛴 1개는 실제 Docker·평가 이미지가 필요한 시험이다.
- 공유 dev에서 원래 실패하던 시험 4개는 패치로 통과한다. 실제 유료 모델이나 생성물 Docker 평가를 돌린 결과는 아니다.

**같은 날 외부 점검 8건 수정:** 기존 시험이 놓친 입력·실패 경로를 재현하고, 고친 뒤 변형까지 회귀 시험으로 고정했다.

| 점검 | 문제 | 수정 | 회귀 시험 |
|---|---|---|---|
| 1 (P1) | 시험기가 `get_db`를 시험용 세션으로 바꿔 끼워, 원래 DB 연결이 고장 난 앱도 9개 PASS | QA-SIGNUP-001에 생성물의 원래 `get_db`로 격리 DB에 가입하는 단계 추가. 실패 응답이면 FAIL, 다른 DB에 쓰면 미검증(`DATABASE_URL_NOT_USED`). 시험 묶음 0.3.0 | 합성 변형 `broken_get_db`(수정 전 9 PASS 재현 → 수정 후 QA-001 FAIL)·`get_db_other_database`, 시험지 세 벌 |
| 2 (P1) | Host 비용이 십진 문자열(`"0.001"`)이라 Agent 평가표 합산이 TypeError | 비용을 Host와 같은 형식으로 읽어 `Decimal`로 더함. 형식이 틀리면 0이 아니라 모름 | 문자열 비용 합산·표 생성, 잘못된 값 7종 |
| 3 (P1) | 인계가 실패하면 Run이 기록에서 빠지고 다시 실행하면 Run이 중복 | `runs.jsonl`을 사건 기록(SUBMITTED→ENDED→EXPORTED/EXPORT_FAILED)으로 바꿔 수락 즉시 남김. 다시 실행하면 같은 Run의 대기·인계를 이어 함 | 인계 실패 후 재실행(제출 1회), TIMEOUT 후 재대기, 인계 끝난 순서는 Host에 다시 묻지 않음 |
| 4 (P2) | 계획과 다른 모델·커밋·한도로 제출 가능, 사전점검도 일부 한도만 대조 | 제출 직전 계획 전체(모델 5칸, LLM 한도 6칸, 시간 예산, 수정·재시도 한도, 시작 커밋, 이미지, 하드웨어)를 실제 설정과 대조. 사전점검도 LLM 한도 전체 대조 | 칸마다 차이 발견, 다르면 제출 전 중단, 실제 dev Host 설정으로 일치 확인 |
| 5 (P2) | Ver1·Ver2 비교는 한도 확인 없이도 준비 PASS | `parity --group-by loopPolicy`(같은 구조의 두 설정이 보호 범위·보호 케이스만 다른지)를 추가하고, 두 비교 모두 동일 확인·Run별 고정 정책·비교 기준이 맞아야 준비 PASS | 평가 쪽이 만든 실제 Ver1·Ver2 설정 통과, 한도·도구·보호 범위·시작 코드 차이 실패, 다른 비교용 보고서 거부 |
| 6 (P2) | 질문·보류로 멈춘 Run의 전체 시간이 None | 멈춘 상태로 바뀐 사건 시각을 끝으로 씀(`endedBy: PAUSED`). 그 뒤 다른 상태가 섞이면 정하지 않음 | 실제 하네스 표본 7개(질문·의미 검토 보류·모델 오류·한도 소진) |
| 7 (P2) | 요구사항·완료 조건이 바뀌어도 기존 시험지로 통과, 추가 요구사항은 조용히 빠짐 | 시험지에 SCN-001 요구사항 기준(설명·완료 조건·검증 담당)을 고정하고 지문에 포함. 다르거나 늘거나 줄면 설정·채점 거부 | 완료 조건(8→12자)·설명·검증 담당 변경, 추가·삭제, 중복 키 |
| 8 (P2) | `runtime --force`가 검사 실패 시 기존 정상 파일까지 삭제 | 같은 폴더 임시 파일에서 검사한 뒤 바꿔 끼움 | 실패해도 기존 파일·폴더 그대로 |

- 이 8건 수정 당시 최종 시험 묶음은 0.3.0, SHA-256 `2c75ee3368e7d32af03e032705b8679bf4cf8eb3571456596b988365f180d9fb`이었다. 이후 재점검 6건 보완으로 0.4.0이 되었으며 현재 지문은 위 최신 항목을 따른다.
- 검증: 평가 시험 전체 + 평가 코드를 쓰는 dev 시험 3개 파일 **1,600 passed / 1 skipped**(실제 Docker 필요), 5분 58초.
- 아직 남은 구현: Host의 최종 보고서·패키지 완료 기록 생성(TIME-02·자동 완료 PASS), 모든 관문 거부·A2A/LLM 재시도·테스트 변경·출력 자동 수집. 실제 모델·MCP·Docker로 수정→재검증→Trace 복원을 보이는 증거와 공식 실험은 환경 준비 뒤다.

**남은 것(환경):**
- 실행·평가 이미지: `BASE_IMAGE` digest, 같은 lock·wheelhouse·ms-playwright 준비, Docker 빌드
- 시작 저장소 생성: lock이 있어야 만들 수 있다
- Host 설정 파일 채우기: 이미지 digest·하드웨어·도구 한도
- OpenAI 키·가격표
- 실행 순서: 시범 Run → 공식 한도 확정 → 공식 20 Run

### 2026-11-13 추가 구현과 4번 범위 재검토

- 완료한 평가 코드: 자동 완료의 증거 누락 PASS 방지, 공식 구조 비교 준비 판정(도구·한도 및 모든 Run의 고정 정책 확인), TIME-05 최초 통과 Snapshot 시간, 수정/재시도/관문 거부/충돌 추가 기록 집계, Planner 독립 사람 검토 입력, 동일 생성물 재채점 결과 비교.
- D0·D2·D5: SCN-001 요구사항·필수 시험 9개와 점수 기준을 유지한다. 새 시간 지표는 기존 버전별 채점 결과를 사용하고, Planner 검토나 과정 횟수를 제품 정확도에 가산하지 않는다. D1 정적 분석은 계속 보조 점검이다.
- D4: 생성 서비스와 플랫폼 UI를 구분한다. Host 예산 장부 대조, 구현 수정·도구 재시도·보고서 충돌 자동 추출, 업무 모델 호출 직전 입력 해시 기록 및 Agent 표 대조를 연결했다. 모델 입력 대조는 metadata 전달 증거이며 의미 이해나 모든 파일 읽기를 증명하지 않는다.
- TIME-02: 완료 사건·최종 보고서·파일 해시를 받는 수집기/검증기/비교 집계를 구현했다. Host의 실제 최종 보고서 생성·저장 후 완료 사건 발행은 아직 없다. 완료 계약 v1은 준비안이며 기존 Run의 export 시간을 완료 시각으로 쓰지 않는다. 재계획·테스트 변경·전체 관문 거부·A2A/LLM 재시도 및 미수집 출력은 추가 원본 계측이 필요하다. 현재 실제 Host Run의 자동 완료 PASS는 여전히 확정할 수 없다.
- 남은 실험 준비: 공통 시작 코드·앱/DB/로그 규격, 구체 모델·예산·반복 수, 평가 이미지·의존성, 실제 생성물 두 번 채점과 Single/Multi 시범·공식 실험. 전자문서는 범위·시나리오 등록·보호 시험이 별도다. 로그인·프로젝트 DB·화면 중계는 3번, Host 기동·공급은 1·2번의 책임이다.
- 이번 후속 변경은 평가 코드와 필요한 Host 내보내기·업무 모델 호출 계측, 테스트·문서다. 유료 모델 실행이나 생성물 Docker 평가 성공을 기록하지 않는다. 사용 방법과 추가 기록 계약은 `src/evaluation/README.md` 마지막 절에 있다.
- 1차 검증(2026-11-13): `python -m pytest -q tests/evaluation --tb=short` **1,398 passed / 2 skipped / 12 warnings**, 243.19초. 생략은 실행 환경의 Unix socket 제한과 실제 Docker·평가 이미지 미준비다. 경고는 dev 하네스의 cross-replica event stream 미설정이다. 추가 회귀 시험 48개를 포함하며 실제 유료 모델/생성물 Docker 실험을 뜻하지 않는다. 신규 CLI 도움말과 `git diff --check` 확인. 공식 시험 묶음 SHA-256은 `d2c2d59124b6b6ed04859a33cc6cc32158f504aa1ff38330ba0c8f00d96c28e7`로 유지된다.


- Host 증거 연결 후 전체 평가 검사: **1,436 passed / 2 skipped / 36 warnings**, 296.81초. 생략 사유는 Unix socket 제한과 실제 Docker·평가 이미지 미준비다. 이후 패키지 파일 재읽기 제거·Source/Manifest 전체 대조·최종 보고서 노출 검사와 잘못된 입력 처리를 추가해 관련 검사로 확인했다(최종 패키지 회귀 검사 **22 passed**, 17.61초).
- Single 내보내기 검사 **13 passed**, 81.12초. 최신 dev의 업무 호출 7회 + 의미 검토 4회를 실제 공급자 요청 목록·영수증과 대조하도록 오래된 기대값 2곳을 고쳤다. Single 모델 입력 대조와 예산 장부 PASS도 실제 로컬 하네스 기록으로 확인했다. 전체 Single 정상/수정 파이프라인과 모델 계측·기존 영수증 검사도 수행했다. 실제 LLM·Docker 대신 결정적 시험 대역을 사용했다.

## 1. 평가 구조 요약

4번의 최종 독립 평가는 개발 순환(Planner → Developer → QA/Security Agent → 수정) **밖에서** Agent 생성물을 채점한다.
순환 안 QA·Security Agent는 2번이 만들었고, 10.29 회의 뒤 4번이 정한 평가 방식(QA·Security 같은 실행 방식)에 맞춰 Security도 QA처럼 자기 테스트·순환용 보호 테스트를 실행해 측정 결과로 판정하도록 4번이 dev를 고쳤다(회의록의 "석민: 테스트에 필요한 사항은 dev 코드에서 수정" 범위, 2절 dev 행). 4번의 D1 `assess()`는 최종 평가의 보조 점검이다.
10월 29일 회의록(기업 미팅 사전 논의)은 Single vs Multi 비교 실험 방법으로 **방안 1 = 순환 테스트 + 최종 테스트(같은 검증 기준을 충족한 결과에 도달하는 시간이 주 지표)**, **방안 2 = 최종 테스트 중심(정확도·소요 시간)**을 "방식 검토 중"으로 적고 기업 의견을 듣기로 했다. 이후 4번의 개발 방침으로 두 방안을 먼저 멀티에이전트에 적용해 **Ver1 = 순환 테스트 + 최종 테스트**, **Ver2 = 최종 테스트만(순환용 보호 테스트 없음, 에이전트 자체 검사는 있음)**으로 나눴다. 여기서 순환 테스트는 평가 쪽이 순환 안에 넣는 보호 시험지다. Ver1·Ver2 비교는 멀티에이전트 안에서 순환용 보호 테스트를 준 효과를 보여 주며, Single 대비 Multi의 효과는 아니다. Single vs Multi는 같은 방안 안에서 구조별로 비교한다(`experiment compare --group-by architecture`, 명세 4.1절).
채점용 시험지(pilot·final)는 에이전트에게 주지 않고, 시작 코드·채점 Snapshot에 시험지·평가 코드·결과 파일이 섞이면 설정을 만들거나 채점하지 않는다(`exam_exposure`). 그 밖의 비공개(저장소 접근 권한 등)는 운영으로 지킨다. 3번(세화)의 backend는 서비스 웹페이지 자체의 백엔드이며 평가 대상(에이전트가 만든 생성물)이 아니다.

팀장 카톡(2026-10-14) 기준 4번의 일: ① QA·Security 평가 기준과 검사 항목 정리(미팅에서 논의), ② 평가 모듈에 실행 정보·결과물 정보 연결 준비,
③ 시연 실행 로그·생성 파일·화면 정리(12월 발표용, 팀장에게 전달), ④ 실제 테스트와 Single/Multi 비교는 미팅 이후 진행할 수 있게 준비.
10월 22일 텔로스 자문에서 "설계 문서는 줄이고 실제 동작하는 파이프라인을 우선"하라는 의견을 받았고, 다음 회의는 11월 2일(텔로스)이다. 19일 주 팀 회의의 결정 기록은 남아 있지 않다.
10.29 회의록에 적힌 것: UI/UX 개편(3번 쪽), 비교 실험 방안 1·2(방식 검토 중), 생성 코드 규격과 규격 정의 Agent 도입 여부(논의 중·기업 의견 수렴 예정), 기타 개발로 **석민 = Snapshot 인계 방식 등 남은 테스트에 필요한 사항을 dev 코드에서 수정**, **주호 = 싱글 에이전트 개발**. 회의 뒤 정한 4번 방침: 멀티에이전트 평가를 먼저 완성(Single 실행기는 주호가 개발하고 4번은 평가 기준·비교 집계를 맡음), 위 Ver1·Ver2, QA·Security 같은 실행 방식, 시작 코드는 "시작 코드 규격을 정의하는 에이전트" 여부를 기업 팀장님과 논의한 뒤 정함.
11.2 텔로스 회의록(세화 작성)과 자문(11.4 결과보고서): 순환 테스트에 기업이 문제를 제기했고(QA Agent와 책임 중복, 중간 피드백과 최종 평가 분리, 정확도를 맞춘 뒤 속도만 비교하는 방식 재검토, 평가 기준의 신뢰성) 두 비교 방안은 유지하되 최종 평가 구조는 **미확정**이다. 기업은 Agent별 평가와 Agent 사이 정보 전달 추적을 강조했고, 후속 작업으로 Evaluation = **Agent별 평가 항목·로그 구성(금주 우선)**, **실제 API 테스트와 결과 기록(11월 4일경)**이 적혔다(개인 배정표는 아님). 팀장 카톡: 석민 = 에이전트 평가표 세 개 볼 수 있게 준비 + 파이프라인 생각. 규격 정의 Agent 도입은 미확정(팀·기업 추가 검토). Single 실행기는 주호가 구현해 dev에 병합했다.

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
| 최종 평가 시험지 | 최종본 v4 (2026-11-09) | final_exam.md, final_exam_core.md | 검사 9개(기능 6·보안 3)의 절차·PASS 조건, 판정 규칙, 다섯 축 측정식, 비교 조건. 현재 시험 코드(`exam_final.py`, 시험 묶음 0.5.0, SHA-256 `b60ec485…aefb`)와 대응한다. 옛 초안 final_exam_v1.md·final_exam_final.md(코덱스)를 대체. 외부 계약 연결부와 합성 서비스의 실제 Docker 재실행 일치는 검증했다. 공식 Host/D5 환경·시작 커밋 동결과 시범·공식 실험은 남아 있다 |
| 공유 문서 | 작성 | dev_changes_2026-10-30.md, single_baseline.md | 팀장 전달용 dev 변경 정리, Single 비교 기준선(Single 실행기 구현됨, 실제 비교 Run 전). 예전 보안 판정 기준 한 장(share_security_criteria.md)은 내용이 기준표·이 문서와 겹쳐 2026-10-30 삭제 |
| 회원가입 실행 검사(D2) | 구현(합성 서비스로 검증) | protected_tests/ | 실행 검사 9개(QA 6·Security 3)와 시험지 세 벌(순환용 practice·시범 최종용 pilot·공식 최종용 final, 같은 검사·같은 입력 종류, 다른 값·모양·순서). 검사 내용은 `signup_qa_checks.py`·`signup_security_checks.py`, 연결부는 `signup_link.py`. 정적 판정이 보던 곳을 보완: 앱 코드의 info·debug 로그 수집, DB 오류 주입, 형식이 틀린 본문, base64·16진수 노출 검색, 거부 뒤 정상 가입 순서, DB 직접 쓰기로 UNIQUE 확인. 순환 안 범위는 역할별 두 개(dev 설정 크기 약 20KB씩). 합성 서비스와 변형 47개를 읽기 전용 Snapshot으로 세 벌 모두 돌려 같은 검사만 FAIL(합성 결함 기준의 난이도 확인). 실제 생성물로는 아직 안 돌림 |
| 결과 어댑터 | 구현 | check_adapters.py | unittest-v1 보고서 → CheckResult. 보고서 없음·형식 오류·대응표 밖 줄은 모두 미검증. `protected_tests.final_check_results`로 독립 평가 보고서까지 연결(정적 판정 변환 함수는 남겨 둠) |
| Trace 검사·세부 지표(D4) | 구현(실제 연결 남음) | trace_checks.py, run_metrics.py | Run 기록의 비밀번호·해시 노출(REQ-006 Orchestrator 부분, 검사 범위 `coverage` 표시), 요구→개발→검증→Issue→수정→재검증→판정 연결(REQ-008), Regression 계산(버전별 보고서 연결 `regression_from_reports`), 협업 시간(역할별 LLM·도구 시간, 전달 지연, 시도·재개 구간별 에이전트 활동·병렬 겹침·에이전트 밖 시간, 사람 대기). dev 파이프라인 테스트의 실제 Run 6개로 확인. A2A Message 본문은 dev `a2a_export`로 꺼내 넘기면 함께 봄(2026-10-30, dev 파이프라인 Run에서 오탐 없음 확인). 남음: 알려 준 비밀값을 넘길 경로, 버전별 평가에서 Regression 자동 산출 |
| 최종 독립 채점기(D5) | 구현(실제 컨테이너 실행 전) | final_evaluation.py, coverage_runner.py, scoring.py, summary.py | Snapshot tar 해시·lock·이미지 해시 확인 → 안전하게 풀기 → 채점 시험지(pilot·final 중 선택) 9개를 컨테이너(`DockerRunner`)에서 실행 + 순환용 시험지 참고 채점(차이의 차이 보조 지표용) + 보조 점검(지나간 줄 기록, 정적 판정) → 독립 평가 보고서·점수(요구사항별 같은 비중·검사 완료율·확인된 보안 위반), Run 기록 검사, 회의·발표용 요약. 가짜 docker·합성 서비스로 검증. 최종 Snapshot·Manifest는 dev `snapshot_export`로 꺼내 넘긴다. 결과는 `experiment score`가 저장소 `experiments/`에 보관. 남음: 평가 이미지, 실제 컨테이너 실행 |
| Ver1·Ver2 비교(10.29 회의 뒤 방침) | 구현(합성 입력) | run_metrics.py, scoring.py | `comparison_validity(..., group_by='loopPolicy')`·`experiment_summary(..., group_by='loopPolicy')`: 같은 Multi 구조에서 보호 테스트 참조(`protectedTestSuiteRef`)만 달라야 하고(이름표는 동결 설정과 대조), 쌍(`pairId`)마다 Ver1·Ver2가 하나씩, 같은 최종 시험지. 정책별 평균 점수·완료율·보안 위반 Run 수, 같은 품질 기준(최종 독립 평가 PASS) 달성 시간·비율(`qualifiedVerdictMs`·`qualifiedRate`), `loop_policy_effect`로 차이의 차이(보조 지표). Single/Multi 구조별 비교는 같은 방안 안에서(`compare --group-by architecture`) |
| dev: Security 실행 테스트(10.30, 4번이 dev 수정) | 구현(dev 시험으로 검증) | mcp_tools, agents/runtime, agents/platform, orchestrator | 테스트 범위에 역할(`role: "SECURITY"`) 표시, Security에 `write_test_file`(`outputs/security/tests`)·`run_unit_tests`, Security 단위 테스트 실행 기록·Snapshot 읽기 권한, Security 출력 계약의 `testCases`, Host가 Security 자체·보호 테스트를 QA와 같은 방식으로 실행하고 측정 결과로 요구사항 판정(정적 증명과 함께 쓰지 않음), 보호 케이스 파일 하나를 범위의 역할대로 QA·Security에 나눔, Security 보고서·Orchestrator가 `run_unit_tests` 증거를 받음. Security 범위가 없으면 예전 정적 증명 방식 그대로. dev 파이프라인 시험에서 Security 테스트 통과로 Run SUCCESS, 실패로 Security Issue 확인 |
| 실험 도구·시험지 노출 확인(10.30) | 구현(합성 입력) | experiment.py, protected_tests(`exam_exposure`), final_evaluation.py | `configure`(Ver1·Ver2 Host 설정 쌍, 시작 코드 커밋에 시험지·평가 코드가 섞였으면 거부), `fetch`(Run 기록), `score`(내보낸 Snapshot을 D5로 채점해 Run별 보관, `--loop-policy`로 계획상 버전 기록), `record`(채점 못 한 Run의 기록만 보관), `compare`(채점 여부와 상관없이 모든 Run으로 Ver1·Ver2 비교·차이의 차이·Regression 합계), `score --all-versions`로 버전별 채점과 Regression 계산. 결과는 저장소 `experiments/scn001/<시험지>/<날짜-이름>/`에 커밋(내보낸 tar 제외, 결과 파일이 시작 코드·Snapshot에 섞이면 `RESULTS_PATH`로 거부). D5는 채점할 Snapshot에 시험지와 같은 내용의 파일이나 평가 코드 경로가 있으면 실행·점수를 만들지 않음(`EXAM_EXPOSED`). 일부만 베낀 경우는 찾지 못함 |
| Agent별 평가표(11.2 회의, 11.5) | 구현(dev 시험 하네스 Run) | agent_eval.py | dev `evaluation_export` 폴더로 한 Run의 ① 작업 정확성(Planner 요구사항 추출, Developer 빌드·최종 결과물·수정 해결, QA·Security 판정과 독립 평가 대조·Issue 근거) ② 정보 전달(단계마다 받은 입력이 규칙상 입력과 같은지, 받은 입력에 맞는 결과를 냈는지) ③ 처리시간·오류(작업 수, A2A 처리 시간, LLM·도구 호출·시간·토큰·비용, 단계·A2A·LLM 오류)와 오류 발생 지점을 만들고 Markdown 표로 낸다(`python -m evaluation.agent_eval`). Single은 단계 역할로 같은 표. 독립 평가 보고서가 없으면 그 칸은 미검증. 시나리오 정책은 Run이 고정한 것을 써서 다른 시나리오도 되고, 평가 기준이 없는 phase(팀장 보조 에이전트 검토 중)는 미검증으로 두되 시간·오류는 잰다. 여러 Run은 `--summarize`로 (시나리오, 구조)별로 모은다(발표의 Single·Multi 비교) |
| Single 비교 실험(D6) | 평가 쪽 준비됨, 실제 Run 전 | single_baseline.md, experiment.py, trace_checks.py | Single 실행기는 주호가 구현해 dev에 병합(`architecture` SINGLE_AGENT 허용, `evaluation_export`). 4번은 평가 기준·비교 집계(`experiment compare --group-by architecture`, 보호 테스트 조건이 같아야 유효)와 Single 기록 수용(A2A 문서 스키마 2·저장소 하나, 2026-11-05) |

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
| 2026-11-14 (상의 없이 할 수 있는 준비) | 평가 전체 검사 1,450개 통과·1개 건너뜀(Docker 미준비), 278.02초. 사전점검 Host 대조를 더한 뒤 1,467개 통과·1개 건너뜀, 290.77초. 작업 폴더의 커밋 전 dev 수정과 평가 모듈 추가분을 포함한 수치다. 저장소 전체 검사는 5,054개 통과·5개 실패·1개 건너뜀(37분 58초)이었다. 실패 4개는 공유 dev `ea06d12` 원본 복사본에서도 같게 실패했고, 1개는 타이밍 검사라 따로 3번 돌려 모두 통과했다. 오래된 시험 4개 패치는 원본 복사본에서 두 파일 13개 통과를 확인했다. 시작 골격·전자문서 초안 검사 8개는 이 수치에 포함된다. |
| 2026-11-13 (최신 dev 대응·남은 지표) | dev `ea06d12`에서 평가 전체 검사 1,351개 통과·1개 건너뜀(실제 Docker 이미지/daemon 미준비), 237.95초. 받기 직후 기존 1,221개 통과를 먼저 확인했다. 새 검사 130개 중 dev 하네스(가짜 LLM·가짜 docker, 생성 코드 실행 없음)로 만든 실제 기록 11개 사용, 현재 dev 하네스로 Run 3개를 매번 새로 만드는 호환 검사 5개 포함. `finalCodeVersion: null` 회귀 검사 7개는 고치기 전 코드에서 모두 실패함을 확인했다. Python 3.10.2와 3.13에서 실제 기록 11개의 원인·수신 기록·검토 호출 결과가 같다. 추적·노출 검사는 최신 dev 성공 Run 둘 다 PASS(의미 검토 해시 오탐 없음). 전체 dev 검사는 재실행하지 않음. 실제 모델·Docker 비교 실험 결과는 아님. |
| 2026-11-11 (평가 연결 보완) | 평가 전체 검사 1,218개 통과·2개 건너뜀(실제 Docker 이미지/daemon 미준비, 실행 환경의 Unix 소켓 생성 제한), 217.31초. 이후 HUMAN_REVIEW 자동 구간 보존·인계 상태 일치 확인을 보완하고 관련 workflow·agent_eval 검사 48개를 다시 실행해 모두 통과(새 검사 2개 포함, 앞 수치와 중복). 전체 dev 검사는 재실행하지 않음. 준비 양식으로 preflight를 실행해 미확정 설정·Docker 미준비를 확인. 합성 서비스/실행기 대역 검증이며 실제 모델·Docker 비교 실험 결과는 아님. |
| 2026-11-07 (Agent별 평가 요약·시나리오 정책) | 11.5 중간점검 반영: `agent_eval`이 Run이 고정한 시나리오 정책을 쓰게 함(없으면 Run의 scenarioId로 등록 시나리오, 다른 시나리오 정책은 거부), 평가 기준이 없는 phase를 다른 역할로 채점하던 것을 미검증으로(시간·오류는 따로 남김), 여러 Run 모으기(`--summarize`) 추가. 시험 6개 추가(28개), 고정 파일에 Run의 scenarioId를 남김. 평가 시험 1,168개 통과·1개 건너뜀(Docker) |
| 2026-11-05 (Agent별 평가표) | `agent_eval.py` 추가: dev 시험 하네스(가짜 LLM·가짜 docker, 생성 코드 실행 없음)로 Multi(QA 실패 → 수정 → 재검증)와 Single Run을 만들고 dev `evaluation_export`로 꺼내 세 표가 채워짐을 확인. 시험 22개(전달 누락·다른 버전·A2A 기록 없음·요구사항 누락·다른 Snapshot 검사·Planner 누락·놓친 결함·잘못된 FAIL·독립 평가 미검증·다른 Run 보고서·수정 실패·근거 없는 Issue·빌드 실패·에이전트 오류·모르는 값·부분 비용·Markdown·CLI). 시험이 찾아 고친 것: 끝 상태 기록이 없는 A2A Task를 에이전트 오류로 세던 것 → 기록 부족(시간 None). Single A2A 문서(스키마 2·저장소 하나)를 노출 검사가 "모양이 틀린 문서"로 거부하던 것 → 받도록 고침(재현 시험 추가). 평가 시험 1,162개 통과·1개 건너뜀(Docker) |
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

## 8. 다음 작업과 준비 이력

**2026-11-15 현재 남은 실행 준비:** 실제 고정 이미지·해당 환경의 lock 및 도구 설정 → 공통 시작 저장소/커밋 생성·연결 → 하드웨어/인증 확인 → Single·Multi 시범 → 시범 사용량을 근거로 공식 한도 동결 → 공식 반복 실험. 모델과 시범 한도·반복 수는 이미 계획에 반영했고, 가격표·로컬 runtime·Ver1 보호 케이스도 준비했다. Docker 금지 요청을 유지하므로 실제 컨테이너 검사는 진행하지 않는다. SCN-002 전자문서의 범위·연결 계약·문제지는 별도 확장 과제다. 아래는 당시 준비·결정 이력이며 미정이었던 값을 다시 미정으로 되돌리지 않는다.

**2026-11-14 상의 없이 할 수 있는 준비:** dev 파일은 고치지 않고 다음을 만들었다.
- **시작 코드 골격 초안:** [`examples/starting-source/scn001-signup`](../../examples/starting-source/scn001-signup/README.md). 지금 가정한 연결 규격을 따른 빈 골격이다. 평가 시험(`test_starting_skeleton.py`)으로 다음을 확인했다.
  - 골격만 채점하면 9개 검사가 모두 FAIL이 아니라 미검증(User 모델 없음)이다.
  - 최소 구현을 더하면 공식 최종 시험지 9개가 모두 PASS다.
  - 시험지·평가 코드가 섞이지 않았다.
- **전자문서 등록 초안:** [`scn002_edocument_draft.md`](scn002_edocument_draft.md)와 계약 JSON. dev 계약 복원·승인 원장이 받는 것을 시험으로 확인했다. 범위(편집 vs 제출·조회)와 연결 규격은 미정이다.
- **실험 사전점검 강화:** `preflight`가 실험 계획과 구조별 Host 실행 설정 파일을 대조한다.
  - 대조 항목: 모델, 호출·시간 한도, 도구 시간 한도, 이미지, 하드웨어, 시작 커밋, lock 해시, 두 구조 동일 여부
  - 호출 한도 하한: 수정 없는 한 바퀴 10회에 못 미치면 미준비, 수정 1회 20회에 못 미치면 경고(시험 하네스 기준)
- **주호님 전달 사항:** [`dev_changes_2026-11-14.md`](dev_changes_2026-11-14.md). 담은 내용은 다음과 같다.
  - 작업 폴더의 커밋 전 dev 수정 요약(관측 실패 시 호출 중단 여부 결정 필요)
  - 공유 dev에서 원래 실패하는 시험 4개의 원인과 패치. 공유 dev 복사본에서 확인했고, 작업 폴더에는 적용하지 않았다.
  - 상의가 필요한 dev 작업 6가지
  - (같은 날 갱신) 11.14 합의 뒤 이 문서는 4번이 반영한 dev 변경 공유로 바뀌었다(연결 규격, Ver1 보안 보호 케이스 2판, 시험 패치 적용). 맨 위 "2026-11-14 4번 파트 마무리 구현" 참조.

**2026-11-13 최신 dev(`ea06d12`) 대응·남은 지표 구현:** dev를 빨리 감기로 받았다(평가 파일과 겹치는 변경 없음). 평가가 기대는 dev 부분(인계·Snapshot 내보내기, Host 설정 검사, 시나리오 계약, Run 조회 API, 상태 종류, 도구 권한, OpenAI 어댑터)은 바뀌지 않았다. 새 dev는 에이전트 결과마다 의미 검토 호출을 같은 Run 예산으로 붙이고, 검토 보류·Planner 질문이면 입력 대기, 개발·검증 보류·모델 오류면 사람 검토로 멈춘다. dev 시험 하네스(가짜 LLM·가짜 docker, 생성 코드 실행 없음)로 성공·보류·질문·API/인증 오류·한도 초과·중단 Run 11개를 만들어 확인하고 다음을 구현했다.
- 끝 상태 원인 분류(`outcomes.py`): 에이전트 상태 메시지 code → 의미 검토 기록 → 호출 영수증 → Host 사건 순서로 읽는다. 모델 API·인증·설정 문제는 개발정의서 11-A대로 미검증 Run으로 표시한다. 호출 시간 초과·도구 실행 실패는 검토 대상으로 둔다.
- `workflow`가 입력 대기 Run을 받고 `outcome.json`을 남긴다. 결함 수정: 코드 전에 멈춘 Run(`finalCodeVersion: null`)을 형식 오류로 거부하던 것을 고쳤다. 고치기 전 코드에서는 새 회귀 시험 7개가 모두 실패했다.
- 의미 검토 호출을 작업 호출과 나눠 Run·역할별로 센다(하네스 기준 같은 흐름에서 Multi 13→20회, Single 7→11회).
- 정확도: ACC-01 완전 충족률 하한·상한, ACC-04 판정 가능률, QA 통과율, 보안 차단 결함 수.
- 안정성·비교: STAB-02~06, 자동 완료 판정(다섯 조건, 확인 못 한 조건은 미확인), Single/Multi 짝 비교.
- 도구·LLM 한도 동일 확인(`parity.py`): 정책 합집합, 실행 설정 파일, Run별 고정 설정 해시 대조.
- Agent별 평가표에 수신 기록 대조와 역할별 의미 검토 호출을 추가했다.

당시 구현한 평가 기능에 이어 같은 날 Host 증거 연결을 추가했다(문서 맨 위 현황 참조). 실험 전제조건과 아직 없는 원본 계측은 구분한다. 실제 비교 실험에 남은 조건은 아래와 같다.
- 예산은 의미 검토 호출을 포함해 정한다. 예시 설정의 모델 호출 상한 8회로는 하네스의 수정 1회 흐름(Multi 20회)도 끝나지 않는다.
- 시작 규격(연결 규격)은 요청문이 아니라 동결 시나리오 계약으로 넘겨야 한다. 새 dev는 요청문을 승인 기준으로 보지 않아, 요청문에만 적으면 Planner가 승인 안 된 조건으로 질문하며 멈출 수 있다. (11.14 반영: `externalContract`)
- Ver1 보안 연습 범위는 지금 Host가 거부한다(dev 수정 또는 QA 연습만으로 진행). (11.14 반영: 케이스 파일 2판)
- 모델 확정(팀장 실측은 gpt-4.1-mini, temperature 0), Docker·평가 이미지, 시범 실행. (11.14 결정: gpt-4.1-mini-2025-04-14·temperature 0, 시범 계획 작성)
- 전자문서는 개발정의서에 개발 부분이 없다. 요구사항은 시나리오 문서(F-01~07, S-01~05)에만 있고, 범위(편집 vs 제출·조회)부터 정해야 한다.

**2026-11-11 구현 보완:** Host 인계 폴더의 파일 크기·해시·Run·버전 대조 후 최종/이전 버전 채점과 Agent별 표를 함께 만드는 `evaluation.workflow`, 실험 준비 점검 `evaluation.preflight`, 지정 출력 파일과 시험용 비밀값 입력, 입력/출력 토큰 완전성 집계, Markdown 비교표와 통과 1회당 비용을 연결했다. Agent 평가표의 오래된 검사 기대값을 현행 항목에 맞추고, 외부 PASS/Agent FAIL을 오탐으로 단정하던 표현 및 다른 산출물·과거 버전 평가를 최종 결과에 쓰는 문제를 보완했다. 평가 이미지 레시피와 미확정 값을 비워 둔 실험 준비 양식을 추가했다. 실행 방법과 공개 연결 조건은 `src/evaluation/README.md`의 통합 평가 절에 있다.

**2026-11-11 사용자 확인:** 모델 제공자는 OpenAI를 사용한다. 구체적인 모델은 아직 지정되지 않았고, API 호출 예산과 공통 시작 코드는 미정이다. `examples/evaluation/experiment-plan.example.json`에는 `model.provider: "openai"`만 반영했다. 양식의 다른 예시값을 공식 실험 결정으로 보지 않는다.

**11.11 당시 남아 있던 조건:** 공통 시작 코드·모델·호출/시간/비용 한도·반복 수 확정, Docker 실행 및 lock/wheelhouse·이미지 digest 준비, Provider 인증·가격표 연결, 실제 생성물 시범 평가(수정 전후 포함), 동일 조건 Single/Multi 공식 반복 실험이다. 당시 Docker daemon 연결이 불가능했고 저장소 `.env`와 `.data`는 없었다. 환경 변수나 별도 인증 경로의 존재까지 부정하는 것은 아니다. 진단용 API/A2A 기록과 생성 서비스 전체 평가를 혼동하지 않는다. 전자문서는 계약·범위 확정 및 시나리오별 보호 시험이 별도로 필요하다.

11.5 중간점검(교수님 회의, 세화 작성): 예선 2026-11-27(발표 10분 + 질의 5분)에 에이전트 성능 비교 결과를 낸다. 기준은 정확도와 수행 속도, 측정 시나리오는 회원가입과 전자문서 두 개(예선 전 목표), 라이브 시연은 측정에 안 쓴 시나리오 3이다. 텔로스 방문(예선 5~7일 전, 날짜 미정)이 실질 마감이다. 4번에 생기는 일: 전자문서 범위 확인(회의록 "편집" vs 명세 9.1·backend "제출·조회"), dev 시나리오 등록(주호)과 전자문서 계약·보호 테스트(4번), 팀장 보조 에이전트가 생기면 평가표 역할 확장(지금은 미검증으로 표시).

11.2 회의 뒤 우선순위: ① Agent별 평가표(구현, 2026-11-05) ② 실제 API 테스트 기록(Run마다 `evaluation_export` → `agent_eval`, 최종 결과물은 D5) ③ 순환·최종 테스트 구조는 팀·기업 결정 전까지 지금 도구를 유지. 실제 테스트 전에 주호와 정할 것: 실험 모델(추론 모델이면 temperature를 보내지 않게 dev 수정 필요할 수 있음), 초기 비용(약 10달러)으로 돌릴 Run 수, 시작 코드.

0. 2026-10-30 외부 점검에서 나온 결정 두 가지: ① (결정·수정) Ver2는 보호 테스트 참조가 없어 dev가 실험 Run(`experimentId`)으로 받지 않던 것을 dev 규칙에서 고쳤다(실험 Run이라도 보호 테스트 참조는 비워 둘 수 있음, Single/Multi 비교는 평가 쪽 비교 검사가 두 구조의 보호 테스트 조건이 같은지 확인). 두 버전 모두 같은 `experimentId`로 선언하고 `executionOrder`·`warmupState`를 적는다. ② (결정, 2026-10-30) 측정 테스트 모드에서 스캐너 경고가 확정되지 않으면 실행 테스트가 모두 PASS여도 Run이 HUMAN_REVIEW로 끝난다. 일단 HUMAN_REVIEW를 하나의 결과로 받아들이고 두 버전에 같은 규칙으로 다룬다(성공률에는 넣지 않고 `humanReview`로 따로 세며, 최종 시험지 채점은 그대로 한다). 시범 실행에서 실제 스캐너 규칙으로 얼마나 자주 생기는지 본 뒤, 경고 확인 경로(dev)나 규칙 범위 조정을 다시 정한다. 경고를 무조건 무시하게 바꾸지 않는다.
1. 생성 코드 규격: 시나리오마다 달라질 수 있어 "시작 코드 규격을 정의하는 에이전트"를 둘지 기업 팀장님과 논의한다(11월 2일 텔로스 회의). 시작 코드의 위치·형태(캡스톤 안 일반 파일에서 만든 별도 git 저장소 등)는 그 논의 뒤에 정한다. 어느 방식이든 연결 조건(DB 접근은 `get_db`로, DB 위치는 `DATABASE_URL` 환경 변수로)과 "시작 코드에 평가 코드(`src/evaluation`, 시험지) 미포함"이 들어가야 한다(Host는 시작 저장소 커밋의 파일 트리 전체를 에이전트 작업 폴더로 복사한다).
2. dev 수정(10.29 회의록의 석민 담당: 테스트에 필요한 사항)은 2026-10-30에 마쳤다: Security 실행 테스트 경로, 최종 Snapshot·Manifest 내보내기(`python -m agents.platform.snapshot_export --database <Host DB> --run-id <Run> --out <폴더> [--all-versions]` → `v<N>/source.tar`·`v<N>/manifest.json`·`run.json`, 저장된 tar 바이트 그대로이고 내용 해시·tar 해시·Manifest의 snapshotSha256이 같을 때만 씀), 실행 의존성, wheel 포함, 3.10 호환. 남은 것은 평가 컨테이너 이미지(fastapi·httpx·sqlalchemy·argon2-cffi 포함)를 만들어 해시로 고정하는 일이다.
3. Ver1·Ver2 실행: `python -m evaluation.experiment configure`로 설정 쌍을 만들어 Host에 넣는다(Ver1은 보호 범위·보호 케이스 포함, Ver2는 제외, 나머지 조건은 같게). 시범 실행은 pilot, 공식 비교는 final 시험지로, 쌍 번호·순서를 기록하며 `snapshot_export` → `experiment fetch` → `experiment score` → `experiment compare` 순서로 처리한다.
4. D5 실제 연결: Docker를 켜고 실제 컨테이너로 한 번 돌린다. 보고서·요약은 `experiments/scn001/<시험지>/<날짜-이름>/`에 커밋하고, git에서 빠지는 내보낸 Snapshot tar(정적 판정 근거가 가리키는 `source.tar`)는 따로 보관할 곳을 정한다. 버전별 Snapshot은 `snapshot_export --all-versions`·`experiment score --all-versions`로 받아 Regression을 실제 결과에서 산출한다.
5. 실제 Run으로 D4(`run_metrics`·`trace_checks`)를 검증하고 `summary`로 요약, 시연 자료를 `demo_index`로 정리(12월 발표). 시험 비밀값 파일·지정 산출물/프로세스 출력 목록을 받는 경로는 11.11 연결했다(`--secrets-file`, `--output-root`, `--output-manifest`). 실제 운영 시 어떤 프로세스 출력을 수집할지와 마스킹 전 A2A 경계 수집은 별도 결정이다. 지정 파일 검사 성공을 전체 출력 검사 완료로 확대하지 않고 REQ-006 부분 검사로 표시한다.
6. Single vs Multi(D6): Single 실행기는 주호가 구현했다. 실제 Run이 쌓이면 같은 방안 안에서 `experiment compare --group-by architecture`로 비교하고 Agent별 평가표를 함께 낸다. 최종 평가 구조(순환·최종 테스트)는 11.2 회의에서 미확정이다.

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
| 2026-11-05 | 11.2 회의·자문 반영: Agent별 평가표 세 개(`agent_eval.py`: 작업 정확성·정보 전달·처리시간과 오류, 오류 발생 지점), Single 실행기 병합(주호)에 맞춰 Single A2A 문서 수용, 문서의 Single 상태·다음 작업 갱신 |
| 2026-11-07 | 11.5 중간점검 반영: Agent별 평가표의 시나리오 정책·새 phase 처리, 여러 Run 모으기, 예선 일정과 전자문서 측정 준비 기록 |
| 2026-11-13 | 최신 dev `ea06d12` 대응: 입력 대기 Run 수용과 코드 전 멈춤 Run(`finalCodeVersion: null`) 거부 결함 수정, 끝 상태 원인·의미 검토 호출 분리·자동 완료(`outcomes.py`), ACC-01 하한·상한·ACC-04·QA 통과율·보안 차단 결함 수, STAB-02~06·Single/Multi 짝 비교, 도구·LLM 한도 동일 확인(`parity.py`), 수신 기록 대조, dev 하네스 기록 고정 파일과 현재 dev 호환 시험 |
| 2026-11-14 | 시작 코드 골격 초안(`examples/starting-source/scn001-signup`)과 시험지 연결 확인 시험, 전자문서 등록 초안(`scn002_edocument_draft.md`·계약 JSON·dev 형식 시험), 주호님 전달 사항(`dev_changes_2026-11-14.md`)과 공유 dev의 오래된 시험 4개 패치(`patches/`, 적용하지 않음) |
| 2026-11-14 | 실험 사전점검에 Host 실행 설정 대조와 호출 한도 하한 추가(`preflight`, 계획 양식에 `hostConfigurations` 칸) |
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
