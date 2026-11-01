# 53. Single-Agent 수정·재검증 루프와 판정 연결

작성일: 2026-11-01. 선행 작업: [52번 최초 구현·검증](52-single-agent-initial-pipeline.md). 담당: 주호(역할 1·2).

이 문서는 53번 당시 범위·검증 기록이다. 이후 [54번 제어·복구](54-single-agent-controls-durable-budgets.md)에서 Single 입력/인증 재개·취소·영속 예산 복구를 연결했다. 아래의 제어 미구현 설명은 53번 당시 상태다.

## 1. 구현 결과

명시적 `create_single_platform()` Host에서 최초 후보의 확인된 Build·QA·Security 실패를 기존 Issue/Fix Request에 연결하고, **같은 Single Agent·Provider·Context로 최대 3회 수정**하도록 연결했다.

```text
계획 → 최초 구현·Build → QA → Security → 기존 판정
                 ↑                         │
                 └── FIXING: Issue 수정 ───┘
                     새 Source·Build → QA → Security → 재판정
```

Build가 제품 오류로 실패하면 그 후보의 QA·Security를 시작하지 않고 수정으로 이동한다. 생성 코드의 실행·테스트는 기존 Host의 Sandbox 경계를 사용한다. 모델의 READY나 A2A COMPLETED를 제품 성공으로 바꾸지 않는다.

## 2. 변경한 실행 경로

| 위치 | 변경 |
| --- | --- |
| [공통 Dispatcher](../src/orchestrator/application/dispatch.py) | Single의 Fix 발송을 허용하고 수정 Task도 `agent_id=single_agent`와 기존 Context로 전송 |
| [Single 실행기](../src/agents/single/runtime.py) | 최초 구현과 수정 업무를 저장된 실행 Context의 `fix_attempt`로 구분, 수정은 `SinglePhase.FIXING` 사용 |
| [수정 입력 Loader](../src/agents/runtime/developer_context.py) | 동결 설정이 Single이면 모든 업무 단계의 Context/Task 계보를 검사. 기존 Multi의 Developer 전용 매핑 검사는 보존 |
| [Single Host](../src/agents/single/platform.py) | 기존 수정 루프를 활성화. 서비스·Provider·Run 예산 재생성 없음 |
| [Agent Card](../src/agents/api/card.py) | 단일 주체의 최대 3회 수정 기능을 설명. 기존 Skill ID는 호환성을 위해 유지 |

네 Agent 실행기를 생성하거나 다른 역할 Prompt로 전환하지 않는다. WorkflowStep과 도구 영수증의 Developer/QA/Security는 기존 검사·산출물 연결용 업무 범주다. 실제 A2A Agent 매핑과 MCP 모델 주체는 Single을 유지한다.

수정 입력의 `fixRequest.attempt`는 기존 계약을 사용한다. 원래 요구사항·Acceptance Criteria·시작 Commit/Snapshot·모델·환경·스캐너·보호 테스트 정책은 변경하지 않는다.

## 3. 수정 Task와 Context 검사

수정 전에 기존 Loader가 다음 내용을 확인한다.

1. `FIX_REQUESTED` Issue 이벤트와 새 WorkflowStep, 이전 Source·Build·QA·Security Artifact의 실제 등록 계보.
2. 이전 후보의 내용 저장소·최신 Source·완료 Task·Artifact ID·요구사항·코드 버전 일치.
3. Single의 모든 이전 업무가 같은 Context를 사용했고 이전 Task ID가 중복되지 않았는지.
4. 현재 매핑이 `single_agent`의 같은 Run/Context이며, 최신 이전 Task 또는 현재 Task를 가리키는지.

QA/Security 이후 수정하는 Single에서는 마지막 Task가 Developer가 아닐 수 있다. 이 경우 이전 Developer만 최신 Task로 간주하지 않고 저장된 전체 Single 계보를 확인한다. 오래된 Planner Task를 최신 매핑으로 내밀거나 다른 Context로 변경하면 모델·Source 쓰기 전에 거절한다.

새 Task가 발송됐지만 Agent Task ID가 아직 관측되지 않은 정상 구간의 `latestTask=None` 허용은 기존 방식으로 유지한다. 이를 과거 Task 재사용이나 새 Context 생성 권한으로 확대하지 않는다.

## 4. 후보·보고서·판정

| 항목 | 유지하는 정책 |
| --- | --- |
| 최초 후보 | `fixAttempt=0`, `codeVersion=1` |
| 수정 한도 | 최대 3회, 후보는 최대 4개. 한도 뒤 추가 수정 Task 없음 |
| Source | 기존 후보를 덮어쓰지 않고 새 Artifact·Commit·Snapshot과 이전 후보 연결 생성 |
| 검증 | 새 후보의 Build와 QA → Security를 다시 실행. 보고서는 해당 후보의 같은 Execution Manifest 사용 |
| 결과 | 기존 SUCCESS·FAIL·UNVERIFIED·HUMAN_REVIEW 의미와 상태 전이 재사용 |
| 같은 Issue 반복 | 기존 fingerprint·연속 반복 횟수와 사람 검토 중단 정책 재사용 |
| 근거 부족 | 승인 proof/측정 근거 없는 보안 결과를 성공으로 만들지 않음 |
| 인프라/불확실한 쓰기 실패 | 제품 수정으로 감추지 않고 기존 검토·안전 Retry 경계 유지 |

Security 측정 테스트도 제품 실패를 Issue로 전달한다. 테스트 PASS만으로 미확정 Scanner 경고를 없애는 규칙은 추가하지 않았다. 보호 테스트와 최종 독립 채점 시험지를 수정 모델에 노출하는 새 기능도 추가하지 않았다.

## 5. 예산·미완료 경계

기획·최초 구현·검증·수정·재검증은 같은 `RunBudgetRegistry`의 예산 객체와 deadline을 사용한다. Source가 바뀌어도 동결 Run Configuration이나 누적 모델 호출 한도를 다시 만들지 않는다. 최초 흐름에서 호출 한도를 모두 썼으면 수정 요청이 생겨도 추가 모델 호출·Source 수정은 진행하지 않는다.

입력/인증·사람 검토 뒤 재개, 전체 취소 제어, 재시작 후 영속 예산 복구는 **54번**이다. `/resume`, `/recover`의 미구성 Single 503 거절을 해제하지 않았다. SDK의 저장된 Task/Context를 보존하지만 이 단계만으로 복구 실행을 지원한다고 선언하지 않는다.

전체 사용량/비용·대화 기록·Snapshot 평가 인계는 **55번**, 최종 실행 안내·종합 점검은 **56번**이다. 기본 Orchestrator의 Single 미구성 거절과 `python -m agents.single`의 Bootstrap은 유지한다. 실제 키·Docker 실행과 비교 실험 완료도 선언하지 않는다.

## 6. 검증

신규 `test_single_agent_fix_pipeline`은 실제 A2A ASGI, 임시 Git 후보, SQLite, MCP Dispatcher, 도구 영수증과 기존 판정기를 사용한다. Provider와 Docker CLI는 결정적인 테스트 어댑터이며 생성 Source·테스트를 Host에서 실행하지 않는다.

| 검사 | 확인 대상 |
| --- | --- |
| QA 실패 → 수정 성공 | 한 Context·System Prompt 유지, 이전 QA/Security 기록 전달, 새 Source·보고서·Manifest |
| 최대 3회 수정 | 서로 다른 실패로 후보 4개까지 생성하고 FAIL 종료, 네 번째 수정 미발송 |
| 같은 Issue 반복 | 기존 연속 반복 중단 시점에 HUMAN_REVIEW |
| Security 실패 | 측정 보안 테스트 실패로 수정 후 재검증 |
| Build 실패 | 실패 후보의 QA/Security 미실행, 새 후보 Build 통과 후 검증 |
| 공통 호출 한도 | 초기 7회 소진 뒤 수정 모델·Source 쓰기 미실행 |
| 보안 근거 부족 | 성공이나 불필요한 수정으로 바꾸지 않고 검토 상태 유지 |
| 다른 Context | 수정 모델 호출 전 거절 |
| 오래된 Task 매핑 | 최신 Single Task인 것처럼 과거 Planner Task 재사용 거절 |

```bash
PYTHONPATH=src:tests .venv/bin/python -W error::RuntimeWarning \
  -m unittest -q test_single_agent_fix_pipeline
```

관련 회귀는 다음 명령으로 **107개가 통과**했다. 기존 Multi 수정·Context·Issue/재검증·Dispatcher와 Single 최초 흐름·서버·Prompt·MCP 검사를 포함한다.

```bash
PYTHONPATH=src:tests .venv/bin/python -W error::RuntimeWarning -m unittest -q \
  test_developer_fix_agent test_owned_agent_fix_pipeline \
  test_issue_revalidation_evidence test_dispatch test_workflow_contract_regressions \
  test_single_agent_pipeline test_single_agent_server test_single_agent_prompts test_single_agent_mcp
```

신규 최종 **9개와 관련 회귀 107개가 모두 통과**했다. `RuntimeWarning`·미대기 coroutine 오류는 없었다. 변경/신규 Python 7개 파일의 Python 3.10 문법, README·47번·52번·53번 문서의 로컬 링크 63개, 신규 파일 공백·줄 끝과 `git diff --check`도 통과했다.

이전 52번의 전체 검사 수치를 이번 결과로 다시 합산하지 않았다. 이번 검증에는 실제 LLM API·Docker daemon·TCP 서버·Single/Multi 비교 실험이 포함되지 않는다. 개인 설정·인증 파일·프로젝트 브랜치·Git 이력도 변경하지 않았다.

## 7. 개발정의서 대조

| 기준 | 이번 결과 |
| --- | --- |
| §2·§11-A 단일 주체 | 같은 Agent·Provider·Context·System Prompt로 수정까지 연결 |
| §4·§11-A 수정 기회 | 기존 Multi와 같은 최대 Fix 3회·반복 Issue 중단·Retry 상한 유지 |
| §5·§8·§9 동일 후보/증거 | 새 Source와 해당 후보 Build·QA·Security Manifest 및 계보 재검증 |
| §10 MCP·보호 기준 | FIXING 단계와 기존 Source 쓰기 범위, 검증/보호 테스트 쓰기 차단 유지 |
| §11-A 공통 조건 | 동결 설정·요구사항·환경·스캐너·Run 예산 유지. 실제 비교 실험은 미수행 |
| 제어/기록 | 재개·복구·영속 예산은 54번, 전체 기록/평가 인계는 55번으로 구분 |

커밋 메시지: `싱글에이전트 수정·재검증 루프와 최종 판정 연결`

다음 작업: **54번 — Single-Agent 입력·취소·복구와 Run 전체 예산 보존**.
