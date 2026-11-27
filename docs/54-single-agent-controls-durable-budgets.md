# 54. Single-Agent 입력·취소·복구와 Run 전체 예산 보존

작성일: 2026-11-01. 선행 작업: [53번 수정·재검증](53-single-agent-fix-revalidation.md). 담당: 주호(역할 1·2).

## 1. 구현 결과

명시적 `create_single_platform()` Host에 기존 제어 서비스와 영속 예산을 연결했다. 입력·인증 대기 뒤에도 같은 WorkflowStep·A2A Task·Context를 사용하고, 수정 횟수·후보·동결 요구사항·남은 예산을 유지한다.

| 동작 | Single 처리 |
| --- | --- |
| 입력 재개 | 원래 INPUT_REQUIRED Task에 답변, 같은 Task/Context에서 attempt만 증가 |
| 인증 재개 | Host가 인증을 별도로 구성한 뒤 원래 AUTH_REQUIRED Task를 재개. 비밀값을 답변 데이터에 전달하지 않음 |
| QA/Security 대기 | QA → Security 순서 유지. 앞 작업이 다시 대기/실패하면 미발송 작업을 보내지 않음 |
| 취소 | 실제 Agent 작업 종료·취소를 확인한 뒤 ABORTED. 확인 불가 시 성공/취소 완료로 꾸미지 않음 |
| 재시작 | 저장된 Task·Context·동결 설정·예산·모델 영수증을 확인해 복구 |
| 완료 작업 | GET으로 확인. 판정 전 중단됐다면 실제 저장된 보고서를 검증해 수집, 모델·도구 중복 실행 없음 |

기본 통신 서버의 Bootstrap, 미구성 Orchestrator의 Single 등록 거절은 유지한다. Single 재개 API도 명시적 Host 구성이 없으면 기존 503 거절을 유지한다. 이 작업으로 실제 LLM·Docker 환경 시연 완료를 선언하지 않는다.

## 2. 제어 경로와 보호 기준

[WorkflowControlService](../src/orchestrator/application/workflow_controls.py)의 기존 입력·인증·관측·취소 검사를 재사용한다. Single의 Agent 매핑은 제어 시에도 `single_agent` 하나다. 업무별 Developer/QA/Security 라벨을 별도 Agent 서버나 Context로 만들지 않는다.

- Single 재개는 해당 Single Dispatcher와 기존 예산 preflight가 구성돼야 한다.
- 취소도 Single Dispatcher임을 확인해 잘못된 네 역할 엔드포인트로 취소하지 않는다. 취소 자체에는 새 모델 호출 예산을 요구하지 않는다.
- 이미 보낸 Message의 Task ID가 불명확하면 임의로 다시 보내거나 취소 완료로 표시하지 않는다.
- FAILED/REJECTED/CANCELED Task를 새 작업인 것처럼 재시작하지 않는다.
- 기존 보고서가 등록된 사람 검토 상태를 입력 한 번으로 SUCCESS로 바꾸지 않는다.
- 동결 요구사항·실행환경·후보·권한·예산을 바꾸는 제어 입력은 기존 검사를 거친다.

Single의 검증 단계에서는 QA를 먼저 관측/재개한다. 아직 INPUT_REQUIRED/AUTH_REQUIRED이거나 완료되지 않았다면 같은 Context로 Security를 보내지 않는다. Security를 재개할 때 완료된 QA는 GET으로 확인하며 새 QA Task나 추가 모델 호출을 만들지 않는다. 두 보고서의 기존 등록·판정 경계도 유지한다.

`attempt`는 같은 Task의 재개 횟수다. `fixAttempt`와 `codeVersion`은 바뀌지 않으며 별도 수정 주기로 계산하지 않는다.

## 3. 영속 예산과 재시작

[Single Host](../src/agents/single/platform.py)가 기존 `AgentTelemetryStore`와 `RunBudgetRegistry`를 함께 구성한다. 별도의 임의 예산 DB나 새로운 복구 규칙을 만들지 않았다.

보존·검증하는 항목:

1. 동결 Run Configuration 지문과 같은 Host 한도.
2. Run 시작 UTC와 원래 절대 deadline. 대기·재개·재시작으로 시간을 다시 지급하지 않음.
3. Run 전체 모델 호출·도구 호출·확인된 토큰 수.
4. 효과 발생 전 예약된 호출 sequence와 완료 후 사용량 영수증.
5. 미완료 예약·미확인 사용량·원장 누락·시간 역행 여부.

| 복구 상태 | 결과 |
| --- | --- |
| 같은 설정·한도, 모든 예약/사용량 확인 | 기존 예산을 복구 |
| 미완료 모델/도구 예약 | 실행 차단. 적용 여부 불명의 작업을 자동 반복하지 않음 |
| 토큰은 계상했지만 영수증 누락 | 실행 차단 |
| 사용량 미확인 | `null`/불완전 상태 유지, 재시작 실행 차단 |
| 한도·모델 등 동결 조건 변경 | 기존 Run에 새 조건/예산 적용 거절 |
| 시간 역행·기한 만료 | 실행 차단 |
| 원장이 없거나 이미 종료/취소된 Run | 임의 예산 생성/재실행 거절 |

실행 중 남은 예산의 참조를 공유하는 것과 재시작 후 복원은 구분한다. 원래 live 정책이 허용하는 인증 재시도에서도 미확인 사용량을 0으로 만들지 않으며, 재시작 뒤에는 불완전 영수증으로 실행을 허가하지 않는다. 완료된 Task/보고서를 GET으로 수집하는 관측은 새 모델 실행과 구분한다.

## 4. Single 실행 영수증 연결

영속 복구 감사에 필요한 기본 LLM/MCP 기록을 [공통 Telemetry](../src/agents/platform/telemetry.py)에 연결했다.

- LLM 호출 예약은 실행 전에, 사용량은 외부 observer보다 먼저 원장에 기록한다.
- 실제 호출 주체는 `SINGLE_AGENT`다. Step의 기존 업무 라벨은 영수증·Source 검사의 범주로 유지한다.
- Telemetry 저장 시 동결 architecture와 Step/Task/Context/attempt를 대조한다. Single을 네 독립 Agent로 기록하거나 Multi에 Single 주체를 붙이는 입력을 거절한다.
- 내부 도구 영수증의 업무 역할은 실제 Step과 대조하며 Host 실행·모델 도구 양쪽 기록을 연결한다.
- 기존 Multi의 기록 형식은 보존한다. Prompt·Source·도구 본문·인증 비밀값을 새 로그 필드로 추가하지 않는다.

이 원장은 **안전한 복구를 위한 기본 계상**이다. 승인 가격표 연결과 전체 사용량/비용 인계, Single 대화·Snapshot 내보내기는 55번에서 마무리한다. 승인되지 않은 비용이나 미확인 토큰을 0으로 표시하지 않는다.

## 5. 검증 범위

신규 검사:

- Planner 입력 재개, QA 반복 입력, Security 입력, 수정 중 Developer 입력.
- 인증을 별도로 구성한 재개와 비밀값 비전달·미확인 토큰 보존.
- Host/Agent 앱·SQLite Task Store·Registry를 닫고 새로 구성한 뒤 Planner/QA 대기를 같은 Task/Context로 재개.
- 모델 한도를 모두 쓴 상태의 완료 보고서를 GET으로 복구·판정하고 추가 실행하지 않음.
- 대기 중 취소, 실행 중 모델 종료를 확인한 취소, 잘못된 Host의 Single 취소 거절.
- 미구성 API 거절, 예산 소진 시 attempt/전송 변경 전 차단.
- 절대 deadline·호출 수·토큰 보존, 미완료 예약·누락/미확인 사용량·시계·한도 불일치 차단.
- Single/Multi 영수증 주체의 교차 위조 거절.

```bash
PYTHONPATH=src:tests .venv/bin/python -W error::RuntimeWarning -m unittest -q \
  test_single_agent_controls test_single_agent_durable_budgets
```

Provider/Docker CLI는 결정적인 테스트 어댑터다. 실제 A2A SDK/HTTP-ASGI·임시 Git·SQLite·Snapshot·MCP Dispatcher·영수증·제어/판정 코드를 사용한다. 재시작 검사는 실제 운영 프로세스를 강제 종료하는 실험이 아니라 구성 요소를 닫고 동일한 저장소로 다시 구성하는 시험이며, 미확정 크래시 상태는 예약 원장·SDK Store의 기존 복구 검사로 제한한다.

최종 신규 **22개**, 관련 회귀 **161개**, 기존 가격계상 호환 검사 **12개**가 모두 통과했다. `RuntimeWarning`·미대기 coroutine 오류는 없었다. 변경/신규 Python **10개**의 Python 3.10 문법, 변경 Markdown의 로컬 링크 **65개**, 신규 파일의 공백·줄 끝, `git diff --check`도 통과했다.

관련 회귀 명령:

```bash
PYTHONPATH=src:tests .venv/bin/python -W error::RuntimeWarning -m unittest -q \
  test_agent_durable_budgets test_agent_platform_budgets \
  test_owned_agent_telemetry test_llm_runtime_telemetry \
  test_workflow_controls test_workflow_control_guards test_validation_interrupted_controls \
  test_workflow_recovery test_owned_agent_controls test_control_owner_lifecycle \
  test_owned_dispatch_controls test_single_agent_pipeline test_single_agent_fix_pipeline test_single_agent_server

PYTHONPATH=src:tests .venv/bin/python -W error::RuntimeWarning -m unittest -q test_priced_telemetry
```

이전 단계의 검사 수치를 이번 결과에 합산하지 않는다. 실제 LLM API·Docker daemon·운영 TCP 서버·Single/Multi 비교 실험은 실행하지 않았다. 개인 설정·인증 파일·프로젝트 브랜치·Git 이력은 변경하지 않았다.

## 6. 개발정의서 대조

| 기준 | 이번 결과 |
| --- | --- |
| §0·§3·§6·§7 A2A 식별/상태 | 공식 SDK·원래 Task/Context·같은 Step의 attempt로 재개 |
| §4 수정·Retry·제어 | 수정 횟수/후보 보존, 미확정 실행 재전송 금지, 실제 종료 후 취소 |
| §5·§8·§9 보호 기준/증거 | 입력으로 요구사항·Source/보고서·검증 기준을 바꾸지 않음 |
| §10 도구/격리 | 기존 단계·경로·Snapshot·Sandbox·비밀값 경계 유지 |
| §11·§11-A 예산·기록 | 전 단계·재개·재시작의 같은 Run 예산/절대 deadline, 실제 Single 계상 |
| 전체 평가/비용 | 승인 가격표·전체 기록/평가 인계는 55번, 실환경·비교 결과는 별도 확인 |

커밋 메시지: `싱글에이전트 입력·취소·복구와 영속 실행 예산 연결`

다음 작업: **55번 — Single-Agent 실행 기록·사용량·비용과 평가 인계 연결**.
