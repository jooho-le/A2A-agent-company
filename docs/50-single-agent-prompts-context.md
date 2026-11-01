# 50. Single-Agent 통합 Prompt와 동일 Context 기록

작성일: 2026-10-31. 선행 작업: [49번 서버·저장소](49-single-agent-a2a-server.md).

## 1. 구현 내용

[통합 Prompt](../src/agents/single/prompts.py)는 Single 정체성과 공통 규칙을 한 번 정의한다. `PLANNING`·`IMPLEMENTING`·`QA`·`SECURITY`·`FIXING`은 업무 단계이며, 단계를 바꿔도 System Prompt와 Agent 정체성이 바뀌지 않는다. 기존 네 Agent를 실행하거나 그 역할 Prompt로 교체하지 않는다.

신뢰된 Host 입력(단계·고정 Scenario·Metadata)과 작업 데이터(요청·이전 기록)를 JSON에서 구분한다. 요청이나 Source에 System/역할 변경 문구가 있어도 System Prompt에 삽입하지 않는다. 보호된 요구사항·기준을 낮추거나 도구·최종 판정을 위조할 수 없다는 규칙을 명시했다.

## 2. 단계별 출력 계약

[Single Draft 계약](../src/agents/single/contracts.py)은 기존의 제한된 모델 출력·검증기를 재사용한다. 네 Agent 실행기를 호출하는 것이 아니라 JSON Draft 형식과 검증 함수만 재사용한다.

| 업무 단계 | 출력·검증 |
| --- | --- |
| PLANNING | 고정 Requirement에 연결한 Task 계획. 필수 항목 누락·기준 변경 거부 |
| IMPLEMENTING / FIXING | 작업 요약·READY / INPUT_REQUIRED / REJECTED. 실제 Source·Build·최종 Verdict를 모델이 발급하지 않음 |
| QA | Host가 승인한 테스트 scope와 해당 Requirement의 Case 연결. 결과 PASS/FAIL·실행 증거는 Host 책임 |
| SECURITY | 해당 Requirement·Scanner Finding·허용 Source 위치에 대한 검토 제안. 제안과 검증된 판정을 구분 |

Draft 계약은 Scenario 전체의 SHA-256과 연결한다. 같은 Scenario ID라도 내용이 바뀐 계약을 Prompt와 섞으면 거절한다. 승인된 selector·Requirement 범위를 벗어난 Draft나 임의 결과 필드를 거절한다.

`dev`의 측정 테스트용 Security 계약은 `test_selectors` 옵션을 제공한다. Single 계약도 Host의 `security_test_selectors`를 같은 옵션으로 전달할 수 있도록 했다. 현재 `jooho`의 예전 Security 계약에 없는 옵션을 요청하면 정적 방식으로 몰래 대체하지 않고 거절한다. `dev` Security 실행 테스트 코드와 실제 MCP/Host 연결은 후속 작업에서 함께 대조해야 하며, 이번 단계가 실행 테스트 연결 완료를 뜻하지 않는다.

## 3. 영속 대화·작업 기록

[SQLiteAgentTaskStore.context_tasks](../src/agents/api/sqlite_task_store.py)는 다음을 제공하는 **Host 전용 읽기 API**다. 새 HTTP 조회 경로나 별도 기록 DB를 만들지 않는다.

1. 현재 프로세스의 DB lease와 Context의 소유자·Run·Scenario를 확인한다.
2. 하나의 SQLite 읽기 트랜잭션에서 Context의 저장된 Task를 최초 접수 순서로 읽는다.
3. 각 Task의 메시지, 상태 메시지, Artifact, Metadata를 복사해 돌려준다. 수정 이력과 영수증은 기존 저장소에 유지한다.
4. Prompt 준비 시 현재 Step이 실제 접수된 작업인지, Scenario/Draft 계약이 일치하는지 다시 확인한다.

재시작 후 같은 DB·Context에서 이전 계획·단계 결과와 현재 요청을 함께 Prompt에 넣는다. 이는 저장된 메시지/산출물의 재구성이며 모델의 숨겨진 추론이나 Provider 내부 상태 저장이 아니다. 모델·도구를 자동 재실행하거나 새 예산을 지급하지 않는다.

최대 128개 Task, Prompt 전체 최대 1 MiB의 준비 경계를 둔다. 호출자는 이보다 낮은 한도를 지정할 수 있다. 이전 기록을 조용히 잘라내지 않고 초과하면 거절한다. Prompt 크기는 System과 데이터의 UTF-8 바이트를 함께 계산한다. 운영 Runtime의 실제 Run/모델 한도 연결은 후속 단계다.

알려진 비밀값은 기존 저장·입력 정제 경계를 거치고 Prompt 객체의 repr에 본문을 표시하지 않는다. 임의 비밀정보를 모두 탐지한다고 주장하지 않으며, Source/파일 접근 통제는 51번 이후 실제 도구 경계에서도 필요하다.

## 4. 검증 결과

48~50번의 관련 회귀를 아래 명령으로 실행해 **381개가 통과**했다. 실패·오류·건너뜀은 없다. 앞선 기본 검사와 중간 회귀는 이 수에 다시 합산하지 않았다.

```bash
PYTHONPATH=src:tests .venv/bin/python -W error::RuntimeWarning -m unittest -q \
  test_run_configuration test_runs_api test_agent_mcp_settings \
  test_runtime_configuration test_owned_host test_agent_bootstrap \
  test_agent_server test_agent_server_safety test_agent_task_store \
  test_agent_lifecycle test_agent_lifecycle_safety test_agent_role_contracts \
  test_agent_role_outputs test_llm_runtime \
  test_single_agent_server test_single_agent_prompts
```

검사 대상은 두 구조 설정·동결/저장, 잘못된 Single API 전송 차단, 기존 Multi 설정/Host·A2A/Task 회귀, Single 저장소·재시작·Context 보호, 단계별 Draft와 Prompt 분리·기록 보존·한도다. 실제 LLM·Docker·TCP 서버·제품 코드 개발·Single/Multi 비교 실험은 실행하지 않았다.

Python 모델과 프로젝트 Schema의 `architecture` 정의 일치, JSON Schema 자체의 유효성, 47~50번 문서의 로컬 링크 18개(README 추가 링크 포함 21개)와 Markdown 형식도 확인했다. `git diff --check`가 통과했다. 검증에는 기존 가상환경의 unittest를 사용했으며 의존성·개인 설정·브랜치·Git 이력은 변경하지 않았다.

## 5. 개발정의서 점검

| 기준 | 이번 구현·경계 |
| --- | --- |
| §2·§11-A 단일 주체 | 하나의 서비스 정체성·Context·System Prompt. 네 역할 실행기 호출 없음 |
| §0·§3·§6·§7 통신·식별·수명주기 | 기존 A2A Handler·서버 발급 ID·Store와 보호 규칙 재사용 |
| §4 수정·Retry | 기존 Fix 3회·Retry 2회와 공통 Prompt 규칙 유지. 실제 Single 수정 루프는 53번 |
| §5·§8·§9 보호 기준/동일 Snapshot | Prompt/Draft에 요구사항·증거 위조 금지, 실제 권한·도구·Snapshot 연결은 51~53번 |
| §10 판정 | Draft와 실제 보고서/증거·최종 Verdict 분리. READY/Task COMPLETED를 제품 PASS로 승격하지 않음 |
| §11 추적·§11-A 동일 조건 | 저장된 Context의 이력 보존, Scenario 계약 Hash 확인, 설정 동결·Ver2 규칙 보존 |

## 6. 다음 작업과 커밋

커밋 메시지: `싱글에이전트 통합 Prompt와 단계별 출력 및 대화 기록 연결`

48~50번을 한 커밋으로 올릴 때: `싱글에이전트 실행 설정과 A2A 서버 및 통합 Prompt 기반 구현`

다음 작업: **51번 — Single-Agent MCP 기능 합집합과 단계·경로 권한 연결**. 이후 52번에서 실제 LLM 실행기와 Orchestrator를 연결한다. 현재 Single 서버는 계속 `executionReady=false`이며 Run API의 Single 실행 요청은 503으로 거절한다.
