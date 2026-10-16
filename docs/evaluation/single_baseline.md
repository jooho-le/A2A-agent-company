# Single-Agent 비교 기준선 (D6, 팀 공유·논의용)

작성: 2026-10-14 · 4번(강석민) · 상태: **설계 초안. 실행기가 없어 실제 비교는 아직 불가**
근거: 개발정의서 11-A, 기업질의응답(Q75~80), 2차 기업미팅 11~12절, dev `run_configuration.py`·`telemetry_store.py`, [evaluation_spec.md](evaluation_spec.md) 13절.

## 1. 우선순위와 범위

- 2차 기업미팅(10월 6일): Multi-Agent 실구현이 최우선이고 Single 비교는 **후순위**다. 일정이 빠듯하면 축소하거나 뺄 수 있다(완전 폐기가 확정된 것은 아님).
- 기업 답변(Q79·80): Multi가 더 나쁘게 나와도 괜찮고, "무조건 좋다"가 아니라 어떤 상황에서 장단점이 있는지를 보이는 것이 중요하다.
- 따라서 이 문서는 **미팅 이후 실제 비교를 바로 시작할 수 있게 조건만 고정**하는 것이 목적이다. 실행 일정이나 반복 횟수를 확정하지 않는다.

## 2. Single-Agent의 정의

| 항목 | Single | Multi (현재 dev) |
|---|---|---|
| 개발·수정 주체 | 에이전트 1개가 기획·구현·검증·수정을 모두 수행 | Planner·Developer·QA·Security 4개 |
| 수정 기회 | 최대 3회 (Multi와 같음) | 최대 3회 |
| 도구 | Multi가 쓰는 도구 전체 집합(read/write/patch, build, unit, browser, security scan)을 같은 권한 범위에서 제공 | 역할별로 나눈 도구 |
| 모델·옵션 | 같은 provider·modelId·temperature·seed | 같음 |
| 판정 | Orchestrator가 같은 규칙으로 판정(판정은 1번 담당) | 같음 |

- "Single은 모델을 1번만 호출한다"가 아니다. 개발정의서 11-A: "Single만 1회 실행하고 Multi만 3회 수정하는 방식은 금지".
- 한쪽만 도구를 적게 주거나 더 좋은 모델을 쓰면 비교가 무효다(6절).

## 3. 통제 조건과 dev 지원 현황

| 통제 조건 | 같게 둘 값 | dev `RunConfiguration` 필드 | 현재 |
|---|---|---|---|
| 시작 코드 | 같은 commit·snapshot 해시 | `startingCommitHash`, `startingSnapshotSha256` | 있음 |
| 요구사항·완료 기준 | 같은 Scenario(SCN-001) | `scenarioId` | 있음 |
| 모델 | provider, modelId, revision, temperature, seed | `model.*` | 있음 |
| 수정·재시도 한도 | 3회 / 2회, 총 실행시간 | `limits.maxFixAttempts`(3 고정), `maxToolRetries`(2 고정), `runtimeBudgetMs` | 있음 |
| 환경 | 컨테이너 이미지 digest, 의존성 lock, 하드웨어, 네트워크 정책 | `environment.*` | 있음 |
| 보호 테스트 | 같은 suite 참조 | `protectedTestSuiteRef` | 있음 |
| 보안 스캐너 | 같은 버전·규칙 | `scannerProfileRef` | 있음 |
| 실행 순서·준비 상태 | 교대, warm-up 기록 | `executionOrder`, `warmupState` | 있음 |
| 구조 | `SINGLE_AGENT` | `architecture` | **`MULTI_AGENT`만 허용(`Literal`)** |

실험 Run은 `experimentId`를 주면 위 항목이 전부 있어야 생성된다(미완성 기준선 거부). 평가 보고서(`independent.py`)는 이미 `SINGLE_AGENT`를 받는다.

## 4. 보호 테스트(최종 시험지) 위치: A / B / B′

문제: 에이전트가 수정하면서 최종 시험 결과를 받으면 시험 문제를 연습하는 셈이 되어 비교가 공정하지 않다.

| 안 | 방식 | 장점 | 단점 |
|---|---|---|---|
| **A** | 순환에는 에이전트가 만든 테스트만. 보호 테스트는 최종 코드에서 한 번 채점, 결과 비공개 | 가장 공정, 구현 쉬움 | 에이전트가 시험과 다른 방향으로 고칠 수 있음 |
| **B** | 보호 테스트를 순환 안 QA 단계(PROTECTED)에 연결 | 개발정의서 11-A의 "수정 기회" 취지에 가깝고 수정 효과가 큼 | 같은 시험지로 채점하면 연습=시험. 별도 held-out 시험지 필요. QA 담당 REQ-001·002·003·004·007에만 연결 가능(dev `qa_services.py`), REQ-005·006은 불가 |
| **B′** | 순환에는 "어느 요구사항이 실패했는지"만 알리고 세부 값·입력은 숨김 | A와 B의 중간 | 알림 수준을 정해야 하고 dev 연결은 추가 작업 |
| held-out | 순환용 케이스와 최종 채점 케이스를 분리 | B의 오염 해소 | 테스트를 두 벌 만들어야 함 |

기록된 논의: 10월 12일 팀장 카톡에서 **B로 평가하고 A로도 실행해 비교**하기로 했다고 D0에 적어 두었다(카톡 원문은 확인하지 못했고 D0 기록 기준). B′는 4번이 추가로 제안한 안이다. B로 평가할 때는 순환용과 최종 채점용 시험지를 나누는 held-out이 필요하다. 어느 안이든 Single과 Multi에 같은 정책을 적용한다.
dev 제약: QA 보호 케이스는 Python unittest만, `/inputs/tests` 읽기 전용, 네트워크 차단, 보호 bytes 포함 설정 JSON 64KiB, 시험용 비밀번호는 실행 시 생성(literal 금지).

## 5. 지표와 데이터 출처

| 지표 | 계산(개발정의서 11-A) | dev에서 가져올 값 |
|---|---|---|
| 전체 성공률 | SUCCESS Run / **전체 계획 Run**. UNVERIFIED를 성공처럼 빼지 않고 Verified Rate도 같이 표시 | `GET /runs/{id}`의 `verdict` |
| Verified Rate | (SUCCESS+FAIL) / 전체 Run | 〃 |
| 요구사항 충족률 | PASS 필수 REQ / 전체 필수 REQ | 독립 평가 보고서 `requiredRequirementPassRate` |
| 공통 테스트 통과율 | 보호 테스트 PASS 비율 | 독립 평가 보고서 `requiredCheckPassRate` (Agent 자체 QA 통과율과 분리) |
| 수정 횟수 | 실제 코드 수정 사이클 수 | `fixAttempt` |
| 도구 재시도 | 인프라 재시도 수 | `/tool-attempts` |
| 시간 | wall clock(시작~판정 시각). 병렬 Step 합산 금지, 사람 대기는 `human_wait_ms`로 분리 | `/events`의 `RUN_STARTED`·`BUILD_PASSED`·`VERDICT_CREATED`·`RUN_FINISHED`/`RUN_ABORTED` 시각 차이(`run_metrics.py`). `createdAt`~`updatedAt`은 근사값 |
| LLM 호출·토큰 | 전체 호출 수, 입력·출력 토큰 | `/usage`의 `summary.modelCalls`, `knownTotalTokens`, `totalTokens`(불완전하면 null) |
| 비용 | 전체 비용. **누락은 0원이 아니라 unknown** | `summary.costUsd`(완전할 때만), `knownCostUsd`(부분 합), `costComplete` |
| Regression | 수정으로 새로 실패한 공통 테스트 수 | 수정 전후 독립 평가 보고서 비교(버전별 평가는 기록만 하고 순환에 전달하지 않음) |
| 협업 오버헤드 | Multi의 Agent 간 전달·대기 시간 | `/telemetry`(Single에는 해당 없음, 비교는 총시간으로) |

`/usage.summary`는 Run 전체 요약이므로 페이지의 record를 다시 더하지 않는다. `costUsd=null`을 0으로 쓰지 않는다. `HUMAN_REVIEW`는 재개될 수 있어 영구 종료로 일괄 처리하지 않는다. `sourceRun.missedDefect`(Orchestrator SUCCESS인데 보호 테스트 FAIL)는 두 구조의 "자체 성공" 신뢰도를 비교하는 보조 지표로 쓸 수 있다.

## 6. 비교가 무효가 되는 경우 (개발정의서 11-A)

| 상황 | 처리 |
|---|---|
| 시작 commit이 다름 | 유효하지 않음 |
| 한쪽만 더 좋은 모델 | 비교 무효 |
| 한쪽만 도구를 적게 제공 | 비교 무효 |
| 수정 횟수가 다름(Single 1회, Multi 3회 등) | 비교 무효 |
| 보호 테스트가 바뀜 | 비교 무효 |
| 환경 digest 불일치 | 해당 Run만 별도 표시·재실행 |
| 모델 API 장애 | UNVERIFIED로 기록, Run 삭제 금지 |
| 실행 순서·cache 상태 미기록 | 재현성 저하로 표시 |
| warm-up 상태(WARM/COLD)가 Run마다 다름 | 비교는 가능하되 주의로 표시(처리시간·비용 비교에 캐시·초기화 차이가 섞임). 실행 순서 숫자는 Run마다 달라야 정상이라 조건이 아니다 |

이 검사는 `run_metrics.comparison_validity()`로 구현했다. Run Configuration의 통제 항목과 평가 보고서의 `suite.sha256`·컨테이너 digest를 **모든 Run에 대해**(같은 구조의 반복 Run끼리도) 비교해 `INVALID_COMPARISON`(비교 무효)과 `RERUN_RUN`(해당 Run 재실행)으로 나눈다. 평가 보고서의 환경 지문(컨테이너 이미지·의존성 lock)은 보고서끼리뿐 아니라 **그 Run이 동결한 환경과도** 대조하고, 어긋나거나 보고서가 스스로 신분증 불일치를 밝힌 Run은 `RERUN_RUN`으로 표시한다. 구조별 요약(`experiment_summary`)은 평가 보고서가 채점한 코드 버전이 Run의 최종 코드 버전과 같을 때만 받는다(고치기 전 코드를 채점한 보고서는 거부). 문제 해결 수(`issuesFixed`)는 수정을 요청한 수(`issuesFixRequested`)와 따로, 다시 검증해서 PASS로 확인된 것만 센다. 합성 입력으로만 검증했고 실제 Run은 아직 없다.

## 7. 실행 순서·반복·비용

- 순서는 고정하지 않고 교대(Single→Multi, Multi→Single, …). 매 Run은 fresh workspace에서 시작하고 warm-up 상태를 기록한다(팀 승인 필요).
- 반복: 시나리오별·구조별 **5회를 제안**(미확정). 예: 3개 시나리오 × 2구조 × 5회 = 30 Run. 먼저 작은 파일럿 1~2 Run으로 시간·비용을 재고 규모를 정한다.
- 비용: 팀장님이 다음 달 10~20달러 정도의 API 사용을 기업과 이야기했다고 전해 들었다. **이는 승인된 Run 예산이나 단가가 아니다.** 단가표(price book)와 한도는 별도로 확정해야 하고, Run마다 `runtimeBudgetMs`와 비용 한도를 정한다.
- 기업 답변(Q78): 비교는 요구사항 3~5개 기준. 회원가입은 8개 REQ(평가 대상 7개)이고 이후 시나리오는 이후 확장이다.

## 8. 막힌 것과 요청

| 막힌 것 | 필요한 결정·작업 | 담당 논의 |
|---|---|---|
| Single 실행기가 없음. `architecture`가 `MULTI_AGENT`만 허용 | 누가 만들지, 기존 Multi 구현 일부를 단일 구조로 바꾸는 방법을 쓸지(기업 미팅의 제안) | 주호(팀장)·석민 |
| 시작 코드 미정(owned-source 미설정) | 승인 저장소·commit, 최소 실행 샘플 | 주호(팀장)·세화·석민 |
| 보호 테스트 위치 | A / B / B′ 선택, held-out 필요 여부 | 주호(팀장) |
| 실험 예산·모델·가격표 | 모델 lock, 단가, 한도, 반복 횟수 | 전원 |
| 최종 Snapshot 인계 | 소스 접근·Manifest·권한 | 주호(팀장) |

## 9. 4번이 먼저 해 둘 수 있는 것

- 완료: 통제 조건 비교, 지표 변환, 구조별 요약(`run_metrics.py`). 남음: 수정 전후 Regression 계산(버전별 평가 기록이 필요), 실제 Run으로 검증.
- 실제 Run이 없으므로 합성 데이터 결과는 실험 결과로 쓰지 않는다.
