# 16. 공통 A2A Agent 서버

> 범위: 1번+2번 담당 중 공통 Agent 서버 기반.
> 기준: 사용자 개발정의서의 A2A 1.0 HTTP+JSON 계약, 기존 01·14·15번 문서 및 Project Schema.
> 상태: Agent Card·Task 요청/조회/취소 HTTP 계약 구현. 역할별 Agent·LLM·MCP 실행과 3번·4번 작업 및 연동은 제외.

## 1. 개발한 내용

- 하나의 공통 서버를 `AGENT_ROLE`에 따라 Planner/Developer/QA/Security 프로세스로 각각 실행한다.
- 공식 SDK의 `AgentCard`, `SendMessageRequest`, `SendMessageResponse`, `Task`, `DefaultRequestHandler`, 메모리 Task Store와 이벤트 모델을 사용한다.
- Agent 서버가 SDK를 통해 Task/Context ID를 발급한다. Orchestrator의 Run/Step ID는 그대로 추적 metadata에 보존한다.
- 요청 검증, 선택적 Bearer 인증, 같은 Agent/Run/Scenario의 Context 소유권 검사, 입력·저장·응답·SDK protobuf 로그 마스킹을 추가했다.
- 기본 런타임은 Task를 받은 뒤 실행 미구현을 명시적으로 거절한다. Artifact·제품 성공·검사 결과를 만들지 않는다.
- 앱 종료 시 SDK handler의 `aclose()`를 호출한다. Orchestrator/웹/평가 코드와 의존성·Lock은 변경하지 않았다.

설치·Lock된 SDK는 `a2a-sdk 1.2.1`이며 Wire Protocol은 계속 `1.0`이다. 프로토콜 버전과 SDK 패키지 버전을 혼용하지 않는다. 공식 Task/Message/Artifact Schema를 자체 모델로 복제하지 않았다.

설치 SDK의 서버 route 패키지는 스트리밍 관련 선택 의존성도 import하므로, 이번 비스트리밍 단계는 FastAPI의 최소 라우트에서 SDK handler와 ProtoJSON 직렬화를 호출한다. 사용하지 않는 SSE 의존성을 새로 설치하지 않았다. Wire 형식과 호출 의미는 [A2A 공식 1.0 명세](https://a2a-protocol.org/v1.0.1/specification/)와 기존 프로젝트 계약을 참고했다. 범용 서버의 모든 A2A Operation을 구현했다는 뜻이 아니라 프로젝트 MVP의 진입점 구현이다.

## 2. 추가 파일

| 파일 | 역할 |
| --- | --- |
| `src/agents/main.py` | 설정을 받아 공통 FastAPI 앱 생성, SDK 종료 처리 |
| `src/agents/__main__.py` | `python -m agents` 실행, 역할별 Host/Port 적용 |
| `src/agents/api/card.py` | 실제 구현 상태를 알리는 공식 Agent Card |
| `src/agents/api/routes.py` | A2A HTTP+JSON API와 인증/버전/오류 경계 |
| `src/agents/api/validation.py` | 기존 Metadata 검증·ProtoJSON 별칭 우회 차단 |
| `src/agents/api/handler.py` | SDK handler 확장, Context/Task 소유권·취소 확인 |
| `src/agents/api/task_store.py` | SDK 메모리 Store의 저장/응답 마스킹 |
| `src/agents/core/logging.py` | 원본 protobuf가 문자열이 되기 전 SDK 로그 마스킹 |
| `src/agents/runtime/bootstrap.py` | SUBMITTED → REJECTED 기본 런타임, 취소 이벤트 |
| `tests/test_agent_server.py` | ASGI 기반 API·기존 A2A Client 호환 검증 |
| `tests/test_agent_server_safety.py` | 별칭·로그·JSON·응답 안전 경계 회귀 검증 |

## 3. 제공 API

Agent API는 Orchestrator의 `/api/v1` prefix를 사용하지 않는다.

| API | 동작 |
| --- | --- |
| `GET /health` | 서버 상태, 역할, `executionReady: false` 반환 |
| `GET /.well-known/agent-card.json` | HTTP+JSON / protocolVersion 1.0 / Agent 앱 version 0.1.0 / JSON 입출력 선언 |
| `POST /message:send` | 검증한 요청을 공식 SDK에 전달, `{"task": ...}` 반환 |
| `GET /tasks/{id}` | 저장된 공식 Task 조회. 선택 `historyLength` 지원 |
| `POST /tasks/{id}:cancel` | SDK에 취소 요청, 확인된 상태 반환. 이미 CANCELED이면 같은 결과 재확인 |
| `POST /message:stream` | 스트리밍 미지원 오류 |
| `GET 또는 POST /tasks/{id}:subscribe` | 스트리밍 미지원 오류 |
| `GET /docs`, `GET /openapi.json` | 공통 서버 API 확인, SendMessage 예시 제공 |

Task API 요청에는 `A2A-Version: 1.0`이 필수다. POST에는 `Content-Type: application/a2a+json`을 사용한다. SDK가 더 넓은 버전 비교를 지원하더라도 이 프로젝트는 정의서에 고정한 `1.0`만 받는다. Task 및 프로토콜 오류 응답도 A2A media type과 버전 Header를 사용한다.

## 4. 요청 검증과 상태 의미

새 요청은 `ROLE_USER`, UUIDv4 `messageId`, 비어 있지 않은 JSON Data Part, `returnImmediately: true`, JSON 출력 수용 선언, 유효한 기존 Workflow Metadata가 필요하다. 잘못된 요청은 SDK 실행 전에 거부한다.

`metadata`에는 `runId`, `workflowStepId`, `scenarioId`, `attempt`를 포함한다. 기존 `A2AWorkflowMetadata`를 재사용하여 UUIDv4·중복 ID·추가 필드 금지 규칙을 보존했다. Proto Struct가 정수도 `0.0`으로 전송하므로 유한한 정수값 float만 int로 정규화한다. boolean·소수·문자열·음수 attempt는 거부한다.

SDK의 `ParseDict`가 camelCase와 snake_case를 모두 받아들이는 점을 고려했다. 공식 descriptor에서 JSON 이름을 확인하여 `returnImmediately` 검사 후 `return_immediately`로 값을 덮는 등의 우회를 차단한다. Data Part 내부의 임의 JSON 필드 이름은 프로토콜 필드로 오인하지 않는다.

첫 Task 이벤트에는 SDK가 발급한 ID·Context와 프로젝트 metadata를 넣는다. 이 이벤트 이전에 status update만 보내면 SDK 계약 오류가 발생하므로 초기 Task → 상태 업데이트 순서를 지킨다.

기본 흐름:

```text
유효한 요청 → 서버 발급 Task → SUBMITTED → REJECTED
                                      사유: AGENT_RUNTIME_NOT_CONFIGURED
```

`returnImmediately: true`이므로 첫 응답이나 빠른 GET에서 SUBMITTED가 보일 수 있다. 이후 조회하면 REJECTED와 JSON 사유를 확인한다. 모델 설정값을 넣어도 아직 실제 런타임이 없으므로 거절한다. `executionReady: false`와 빈 Card skills는 이 상태를 알리기 위한 선언이다.

이 거절은 제품 결함이나 전체 Verdict FAIL을 뜻하지 않는다. 실제 LLM 업무·Build/Test/Scan·Artifact가 없는 단계이므로 성공 결과로 바꾸지 않는다.

## 5. 인증·소유권·비밀값

- `AGENT_BEARER_TOKEN`이 설정되면 Card·Send·Get·Cancel에 Bearer 인증을 강제한다. 해당 역할의 `ORCHESTRATOR_*_BEARER_TOKEN`과 운영자가 맞춘다.
- 잘못된 HTTP 인증은 401이다. 작업 도중의 공식 `TASK_STATE_AUTH_REQUIRED`와 다르다.
- Token 미설정은 loopback 개발 모드다. 사용자별 인증·Tenant·외부 서비스 공개 기능은 구현하지 않았다. `/health`와 API 안내 문서는 공개되어 있으나 요청/Task 데이터는 Token 설정 시 보호된다.
- Authorization 값을 SDK context, Task, metadata에 복사하지 않는다. Agent Card에는 인증 방식만 선언하며 실제 Token·LLM API Key는 넣지 않는다.
- 같은 서버·같은 Run/Scenario가 발급한 Context만 재사용한다. 새 Step은 같은 Context에서 새 Task를 생성할 수 있지만, 기존 Task를 이어가려면 원래 metadata와 Task/Context를 보존해야 한다.
- 모르는 Task ID는 404다. REJECTED/COMPLETED/FAILED 등 terminal Task를 재시작·취소하여 상태를 바꾸지 않는다. 취소 불가능하면 SDK의 `TASK_NOT_CANCELABLE` 오류를 반환한다.
- 실제 실행 취소 확인은 SDK 이벤트와 저장 Task로 수행한다. 테스트용 대기 Executor에서 WORKING → CANCELED와 반복 취소의 멱등성을 검증했다. 기본 미구현 런타임은 빠르게 REJECTED이므로 취소 전에 이미 terminal일 수 있다.
- 입력과 저장/응답은 기존 구조화 마스킹을 재사용한다. SDK 로그의 protobuf 인자는 먼저 JSON 구조로 변환해 비밀 필드를 마스킹한다. SDK 예외 로그에는 타입만 남기고 원문·Traceback 내용을 제외한다.
- Task/Context/Artifact의 opaque routing ID는 마스킹 과정에서 변경하지 않는다. 비밀값은 ID에 넣지 않는다. 기존 정책처럼 라벨 없는 임의 자연어를 비밀번호로 추론하는 것은 보장하지 않는다.
- 잘못된 JSON, 중복 JSON Key, NaN/Infinity/숫자 overflow, 1 MiB를 초과한 본문은 일반 오류로 거부하며 원문을 되돌려주지 않는다.

## 6. 실행과 눈으로 확인

프로젝트 루트에서, 기존 의존성이 설치된 `.venv`를 사용한다.

```bash
AGENT_ROLE=PLANNER PYTHONPATH=src .venv/bin/python -m agents
```

다른 터미널에서 `AGENT_ROLE=DEVELOPER`, `QA`, `SECURITY`로 각각 시작하면 기본 포트 8102, 8103, 8104를 사용한다. `AGENT_PORT`를 지정하면 그 값이 우선하므로 기존 `.env`의 공통 포트가 네 역할에 동일하게 적용되지 않도록 주의한다.

Planner 기준 브라우저 확인:

1. `http://127.0.0.1:8101/health` — 서버 실행, 역할 PLANNER, 실행 준비 false.
2. `http://127.0.0.1:8101/.well-known/agent-card.json` — Agent Card.
3. `http://127.0.0.1:8101/docs` — `POST /message:send`를 열고 예시 본문 사용, `A2A-Version`에 `1.0` 입력.
4. 반환된 `task.id`를 `GET /tasks/{id}`에 넣고 `A2A-Version: 1.0`으로 조회.
5. SUBMITTED가 먼저 보이면 다시 조회하여 REJECTED와 `AGENT_RUNTIME_NOT_CONFIGURED`를 확인.

Token을 설정했다면 Card·Task API에는 Authorization Header가 필요하다. 주소창 직접 GET에는 Header를 붙일 수 없으므로 인증된 HTTP Client를 사용한다. 아직 역할 업무가 미구현이므로 기존 Orchestrator의 Agent URL은 기본 미설정 상태로 유지한다.

## 7. 아직 하지 않은 것과 제한

- Task/Context는 앱 인스턴스별 메모리 저장이다. 프로세스를 종료하거나 앱을 새로 만들면 이전 ID는 조회되지 않는다.
- messageId 기반 영속 dedup이 없다. 같은 최초 요청을 다시 보내면 새 Task가 생성될 수 있으므로 자동 재전송하지 않는다.
- SDK의 기본 비동기 이벤트 실행만 사용한다. DB 저장·재시작 복구·내구성·장시간 실행과 취소 경쟁의 전체 생명주기는 17번이다.
- 종료 시 SDK EventQueue의 dispatcher 종료 경고가 관찰된다. 예외·테스트 실패는 아니지만, 이벤트 내구성이 확보되었다고 주장하지 않으며 후속 lifecycle 단계에서 종료/복구 정책을 점검한다.
- 실제 Planner/Developer/QA/Security Prompt와 LLM은 후속 구현이다. `create_app(executor=...)`는 신뢰 코드의 확장·테스트 주입 지점이지 HTTP 요청이나 모델이 실행기를 바꾸는 기능이 아니다.
- Workspace/Archive/ACL/Sandbox/MCP/실제 Build·검사·비교 실험 및 팀원 웹·평가 연동은 이번 작업에 없다.

## 8. 개발정의서 준수 점검

| 항목 | 결과 |
| --- | --- |
| A2A 1.0 / HTTP+JSON / returnImmediately / Task poll | 공식 SDK 객체·handler 재사용, HTTP 경계 검증 |
| Task ID/Context ID 발급 주체 | Agent SDK 발급, Orchestrator ID와 분리 |
| Metadata·Context 소유권 | 기존 Schema 모델 재사용, 같은 Agent/Run/Scenario만 재사용 |
| A2A 상태와 전체 제품 Verdict 분리 | Bootstrap 거절은 제품 FAIL/SUCCESS로 둔갑시키지 않음 |
| 미구현 상태·가짜 성공 금지 | REJECTED, 빈 skills, executionReady false, Artifact 없음 |
| Credential/Secret 정책 | Bearer Header 인증, 입력·저장·응답·protobuf 로그 마스킹 |
| 역할 경계 | Orchestrator는 MCP 직접 호출하지 않음. 역할 코드/검사 Tool은 후속 |
| 불변 Snapshot·실제 근거 | 이번에는 생성·검사 자체를 하지 않으며 충족했다고 주장하지 않음 |
| 3번·4번 및 해당 연결 제외 | backend/frontend/evaluation/평가 테스트 변경 없음 |

정의서 전체 구현 완료가 아니라 이번 공통 서버 단계의 계약 준수 점검이다.

## 9. 검증과 다음 작업

```bash
PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -v
git diff --check
```

검증 결과:

- 신규 Agent 서버·안전 경계 테스트 40개 통과.
- 전체 unittest 239개 통과: 기존 199개 + 신규 40개.
- 기존 A2A SDK Client의 Card/Send/Get/Cancel 호환 검사 통과.
- ProtoJSON alias 우회, protobuf 로그의 Secret 노출, 예외 원문 노출을 차단하는 회귀 검사 통과.
- OpenAPI의 Task API·버전 Header·SendMessage 본문 예시 확인.
- `git diff --check` 통과.

검증은 ASGITransport와 테스트용 Executor로 수행한다. 기존 Orchestrator의 실제 `A2AAgentClient` SDK가 Card 조회 → 요청 → Task polling → 취소 확인을 이 서버 API에 대해 수행하는 호환 검사도 포함했다. 외부 LLM 호출, MCP 실행, 실제 제품 기능 검증·팀원 평가 전체 완료를 의미하지 않는다.

커밋 메시지 제안: `공통 A2A Agent 서버와 요청 조회 취소 API 구현`

**다음 작업: 17번 — Agent Task 생명주기·영속화·Context·중복 방지.** 메모리 Task를 역할별 DB 기반으로 확장하고, 중복 Message·재시작 복구·상태 전이·실행/취소 경계를 관리한다. 실제 Prompt는 18번, LLM 연결은 19번에 이어간다. Git commit/push는 수행하지 않는다.
