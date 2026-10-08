# 30. Planner Agent

작성 기준: 2026-10-08. 범위는 1번+2번의 Planner 실행과 A2A 계획 출력이다. 3번 제품 웹서비스·4번 독립 평가·팀원 연결은 변경하지 않는다.

## 1. 구현 내용

- `PlannerAgentExecutor`: 기존 LLMEngine을 사용하여 모델의 계획 Draft를 검증하고 공식 A2A Task에 결과를 게시한다.
- `PlannerExecutionContext`: 신뢰된 Host의 동결 Run 설정·모델·요구사항·예산·Artifact 식별자를 전달한다.
- `SQLitePlannerContextLoader`: 기존 Orchestrator DB를 읽어 현재 Planner Step과 동결 설정을 확인한다. Workflow 상태·Registry·Source를 수정하지 않는다.
- `planner_contract.py`: 모델용 구조화 Decision Schema와 기존 `requirements.json` 계약으로의 Host 조립을 구현한다.
- 실제 Planner를 명시적으로 주입했을 때만 Planner Skill과 `executionReady=True`를 광고한다. 다른 역할 설정에 Planner를 주입하면 거절한다.

```text
기존 A2A admission → SUBMITTED
→ Host의 Run/Step/모델/예산 확인
→ 최초 승인 입력·현재 Task/Context·추가 답변 검증
→ WORKING
→ LLM의 PLAN / INPUT_REQUIRED / REJECTED Draft
→ Host가 보호된 requirements + 검증한 implementationPlan 조립
→ requirements.json Artifact
→ COMPLETED
```

Planner에는 MCP Tool이 없다. 제품 Source·DB·Test를 작성하지 않고 Build/QA/Security 결과나 프로젝트 최종 Verdict도 생성하지 않는다. Planner COMPLETED는 계획 완료일 뿐 제품 SUCCESS가 아니다.

## 2. 모델 제안과 보호된 요구사항 분리

SCN-001의 REQ-001~008 UUID·표시 Key·설명·Acceptance Criteria는 Run 생성 때 동결한 값에서 가져온다. 로그인·OAuth 등의 제외 범위와 이메일/비밀번호 정책도 모델이 바꿀 수 있는 설정이 아니다. 최신 Registry를 다시 읽어 기존 Run의 기준을 바꾸지 않는다.

모델의 출력은 다음 세 필드만 허용한다.

| 필드 | 의미 |
| --- | --- |
| `kind` | `PLAN`, `INPUT_REQUIRED`, `REJECTED` 중 하나 |
| `implementationPlan` | taskId/title/description/requirementIds/dependsOn |
| `questions` | 설명이 필요한 경우의 제한된 질문 목록 |

모델은 requirements·runId·projectArtifactId·artifactVersion·모델 설정·Tool 결과를 출력할 수 없다. Requirement 참조는 동결된 UUID의 정확한 enum으로 제한한다. Task는 최대 128개, 질문은 최대 8개다.

기존 제품 Artifact Schema는 `$id`·`uniqueItems`·선택적 `dependsOn`을 사용하므로 LLM strict 출력 Schema와 직접 동일하게 사용하지 않는다. 별도의 닫힌 Decision Schema에서 모든 필드를 required로 만들고, 최종 결과는 **기존 Artifact Schema를 그대로 유지**하여 재검증한다. 새로운 A2A 공식 Schema나 제품 Artifact 규격을 만들지 않는다.

PLAN 검증 순서:

1. 모델 응답의 bounded JSON·Decision Schema·비밀정보·문자열을 확인한다.
2. Host가 동결한 canonical requirements를 복사한다.
3. 기존 `PlannerPlan`으로 중복 Task/Requirement·미지 참조·자기 의존·순환·요구사항 누락을 거절한다.
4. `ScenarioDefinition.validate_planner_requirements()`로 보호된 값과 정확히 대조한다.
5. 저장소의 고정 `planner_output.schema.json`을 offline 검증한다. 외부 `$ref`는 허용하지 않는다.
6. 모든 검증이 끝난 후에만 A2A Artifact를 게시한다.

Task의 자연어 내용이 실제 구현 품질·요구사항 의미를 충족하는지까지 증명하는 검사는 아니다. 이후 Developer·독립 QA/Security가 담당한다.

## 3. Host 입력과 예산

Host Context에는 다음 값이 필요하다.

- 현재 A2A Workflow Metadata와 동결 `RunConfigurationArtifact`.
- 실제 선택한 `configuration.model`과 `limits.runtimeBudgetMs`. 누락 시 가짜 값으로 채우지 않는다.
- 이미 발급된 Workspace UUID와 원래 사용자 요청.
- Host가 관리하는 기존 공유 `ExecutionBudget`.
- Host 발급 Project Artifact UUIDv4, 최초 Artifact Version 1.

입력 body 자체를 Host의 신뢰 근거로 삼지 않는다. 최초 요청은 기존 Orchestrator의 `{request, workspaceId, runConfiguration, scenarioContract}` 형태여야 하고 보호된 Host 값과 대조한다. Proto Struct의 정수형 double 표현은 허용하지만 `true`를 숫자 `1`과 같다고 인정하지 않는다.

SQLite Loader는 단일 읽기 snapshot에서 Run·Step·Workspace·동결 설정을 읽고 다음을 확인한다.

- 동일 Run/Scenario/Workspace와 Planner 역할.
- Run PLANNING, Step RUNNING, 승인된 현재 Step attempt.
- Source/codeVersion/제품 출력 Artifact가 없는 계획 단계.
- 이미 저장된 A2A Task/Context ID는 원값 그대로 일치. 최초 dispatch가 관측보다 빠른 경우 아직 미저장 ID는 허용.
- 동결 모델·예산·Scenario의 의미/ID/권한·알려진 비밀정보 검증.

저장된 동결 Scenario 문자열이 누락·빈 값·잘못된 타입이면 모델 재검증 전에 거절한다. 기존 `RunConfigurationArtifact`의 신규 Run 생성용 Registry fallback을 저장된 Planner 입력 복구에 사용하지 않는다.

Context/Executor 생성 자체는 DB·모델·네트워크·MCP·Workspace를 실행하지 않는다. SQLite Loader는 호출 시 읽고, 동기 Host reader는 Worker Thread에서 실행하여 SDK 이벤트 루프를 막지 않는다. 신뢰된 reader는 읽기 전용이어야 하며 이미 시작된 Thread를 취소로 강제 종료하지는 못한다.

Host factory 자체에 별도 강제 timeout을 추가한 것은 아니다. 운영자는 조회 시간을 제한해야 하며, 조회가 지연되면 SUBMITTED로 남아 수동 취소가 필요할 수 있다. 조회 이후의 예산 검사로 만료된 LLM 호출은 차단하지만 조회·안전 취소 정리·DB 저장까지의 hard wall-clock 상한을 보증하지 않는다.

`budget_resolver(configuration)`은 기존 Run 예산을 반환하는 Host capability다. **호출마다 새 예산을 만들거나 Run 재개 시 deadline·사용량을 초기화하면 안 된다.** Planner 내부는 모델·Tool Retry나 예산 재생성을 하지 않는다. 공유 예산 객체의 기존 사용량을 보존하고 동결 시간 한도를 초과하는 객체는 거절한다. 분산/재시작 전체 Run 예산 영속화는 34/37번 후속이다.

Host factory와 기존 Repository 자체는 신뢰 경계다. 이번 입력 대조는 원격 모델/Message의 기준 변경을 거절하는 기능이며, DB 파일·Schema를 직접 교체할 수 있는 권한에 대한 암호학적 변조 방어나 기존 공통 DB의 SQL INSERT/REPLACE 정책 전체를 새로 강화한 것은 아니다.

## 4. 결과 상태와 추가 입력

| 상황 | A2A 상태 / 결과 |
| --- | --- |
| 유효한 PLAN | requirements.json 한 개, JSON data Part 한 개 → COMPLETED |
| 설명 필요 | 질문만 게시 → INPUT_REQUIRED, Artifact 없음 |
| Capability 밖 | PLANNER_OUT_OF_SCOPE → REJECTED, Artifact 없음 |
| Host 입력/역할/동결 설정 불일치 | REJECTED, LLM 실행 없음 |
| Provider 인증 오류 | LLM_AUTH_REQUIRED → AUTH_REQUIRED, Credential 요청/출력 없음 |
| 모델 거부 | REJECTED |
| 응답·Schema·의존성·예산·모델 실행 오류 | FAILED, 완성 Artifact 없음 |
| 취소 | SDK의 Worker 종료 후 CANCELED. 재호출/가짜 완료 없음 |

INPUT_REQUIRED/AUTH_REQUIRED은 질문/상태를 게시한 실행이 반환한다. 승인된 후속 Message는 기존 Task/Context에서 이어가며, 초기 전체 payload가 재전송되지 않아도 **durable Task history의 최초 입력**을 복구한다. 추가 답변은 별도 clarification data로 전달한다. 보호된 `scenarioContract`, `runConfiguration`, Workspace/Run 식별자 등의 덮어쓰기를 거절한다.

후속 attempt는 A2A 호출 attempt이며 `fix_attempt`를 증가시키는 코드 수정 Cycle이 아니다. 기존 Store의 interrupted continuation·metadata identity·Message 중복·terminal 재실행 차단을 재사용한다. 실제 Orchestrator의 전체 입력 재개/취소/Human Review 제어 시연은 36번에서 이어간다.

## 5. Artifact와 관측 경계

| 값 | 생성/관리 |
| --- | --- |
| Task.id / contextId | 기존 Agent SDK admission, opaque 원값 보존 |
| A2A Artifact ID / 응답 Message ID | SDK TaskUpdater의 신뢰된 생성기 |
| projectArtifactId / artifactVersion | Host Context, 모델 창작 금지 |
| Artifact metadata | runId/workflowStepId/projectArtifactId/artifactVersion |
| Artifact payload | 기존 schemaVersion=1 / requirements / implementationPlan |
| Registry 등록·다음 Developer 호출·최종 Verdict | 기존 Orchestrator, 이번 Executor가 직접 변경하지 않음 |

Provider 오류 원문·Prompt·모델 JSON·Source를 로그/Trace에 추가하지 않는다. 기존 입력/출력 redaction과 안전한 상태 코드를 사용한다. 질문·계획 문구의 알려진 Credential은 검증/마스킹 경계를 거치며, 임의 자연어의 모든 비밀을 탐지한다고 주장하지 않는다. `usage_sink`는 기존 정제된 `UsageRecord`를 받는 선택적 Host callback이며, 토큰 비용/영속 통합 Trace를 새로 계산하지 않는다.

## 6. 실행 설정과 기본 서버의 차이

```python
from agents.main import create_app
from agents.runtime.planner import PlannerAgentExecutor
from agents.runtime.planner_context import SQLitePlannerContextLoader

# settings.role=PLANNER. 아래 객체들은 운영자가 먼저 구성한다.
# provider: 기존 19번 LLMProvider; request마다 동결 model로 호출됨.
# repository: 기존 Orchestrator repository.
# resolve_shared_run_budget: 새로 만들지 않고 기존 예산을 반환하는 Host 함수.
planner = PlannerAgentExecutor(
    provider=provider,
    context_factory=SQLitePlannerContextLoader(repository, resolve_shared_run_budget),
)
app = create_app(settings, executor=planner)
```

위 방식은 명시적 Host 구성 예시이며 그대로 실행할 독립 스크립트가 아니다. 동결 모델·예산·실행 중 Step·실제 인증/Provider 설정이 먼저 필요하다. 다른 역할에 Planner를 붙이면 `AGENT_EXECUTOR_ROLE_MISMATCH`로 실패한다.

- `create_app(settings)` 및 기존 `python -m agents`: Bootstrap 유지, executionReady=False, 실행 Skill 없음.
- `create_app(settings, executor=planner)`: Planner Skill 광고, executionReady=True. Planner 실행 경로가 구성되었다는 뜻이며 Credential 유효성·모든 Run의 실행 가능·제품 성공을 보증하지 않는다.
- 모델 설정 환경변수만 넣어도 자동 실행하는 구조는 만들지 않았다. 기본 Provider/Context/서버/Orchestrator 자동 연결은 34번이다.

## 7. 개발정의서 준수 점검

| 기준 | 이번 구현 |
| --- | --- |
| §1 회원가입 기준 | 동결 REQ-001~008 및 제외 범위 보존, 기준 완화 금지 |
| §2 책임 분리 | Planner는 계획만. Source/Test/Build/제품 Verdict 접근 없음 |
| §3/§6~7 A2A 1.0 | 기존 공식 SDK·HTTP+JSON·returnImmediately·send/get/cancel·Task/Context 분리 유지 |
| §4 재시도·수정 | 모델 자동 Retry 없음, 기존 Tool Retry/수정 상수 및 fix_attempt 미변경 |
| §8 MCP 권한 | Planner 허용 Tool 없음, MCP subprocess/Tool 접근 없음 |
| §9 Artifact | 기존 requirements.json 계약·Host 발급 metadata·Version 1·Registry 책임 유지 |
| §10 판정 | 계획 COMPLETED를 전체 SUCCESS로 바꾸지 않음 |
| §11 개인정보/Trace | 정제 코드/Usage만, 원문 오류·Prompt·Source 로그 추가 없음 |
| 1번+2번 범위 | 웹서비스·Evaluation·development-log·Lock/의존성 미변경 |

기존 비밀번호 보호 정책 보완·Agent DB 초기 WAL 경쟁은 해결 범위가 아니다. 이번으로 기업 시연·실제 LLM 생성 품질·회원가입 제품 완성·Single/Multi 비교가 완료된 것은 아니다.

## 8. 검증과 인계

```bash
PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -p 'test_planner*.py' -q
PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -p 'test_agent*.py' -q
PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -q
PYTHONPATH=src .venv/bin/python -m compileall -q src/agents
.venv/bin/pip check
git diff --check
```

신규 테스트는 101개(출력 계약 34·실행/A2A 49·Host Context 18)다. 기존 Planner 출력 테스트까지 포함한 `test_planner*.py` 106개가 통과했다.

- 최종 전체 회귀: **1,969개 / 219.272초 / OK**. 마지막 동결 기준 fallback 차단까지 반영한 코드로 실행했다.
- `compileall`, `pip check`, `git diff --check`: 통과. 의존성 추가·Lock 변경은 없다.
- 기존 SDK의 `event_stream` 미구성/복제 Streaming 비활성화 경고와 pip의 캐시 권한 경고는 관측했지만 테스트 실패·의존성 충돌은 없다. 복제 Streaming을 지원한다고 광고하지 않는다.

테스트는 실제 LLMEngine·A2A Handler·SQLite Task Store·Orchestrator 출력 parser와 Fake Provider를 사용한다. 외부 LLM API·실제 Docker·제품 코드 실행은 하지 않는다. 전체 회귀의 로컬 통신/subprocess 검증은 실행 권한 승인을 받아 수행한다.

Git commit/push는 수행하지 않는다. 개별 번호의 쉬운 한 문장 설명은 사용자 요청대로 생략한다.

커밋 메시지: `보호된 요구사항 기반 Planner Agent와 A2A 계획 출력 구현`

다음 작업: **31번 — Developer Agent 구현**.
