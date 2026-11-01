# 49. 단일 Agent 정체성과 A2A 서버·저장소

작성일: 2026-10-31. 선행 작업: [48번 실행 설정](48-single-agent-run-configuration.md).

## 1. 구현 내용

[SingleAgentIdentity](../src/agents/core/identity.py)에 `SINGLE_AGENT` 서비스 식별자를 추가했다. 기존 네 업무 역할 `AgentRole`은 확장하지 않았다. 네 역할을 순회하는 기존 Multi Host의 의미와 역할별 MCP 권한도 유지한다.

[AgentSettings](../src/agents/core/config.py)는 네 역할 또는 Single 서비스 식별자를 받는다. 기존 네 역할 포트 매핑은 그대로이며, 별도 서비스 매핑에 Single 기본 포트 `8105`를 추가했다. 기본 DB는 `.data/agents/single_agent.sqlite3`이고 Host/Port/DB의 운영자 설정도 기존 검증을 따른다.

[Single 앱](../src/agents/single/app.py)은 기존 FastAPI·공식 A2A Handler·SQLite Store·인증·입력 검사·취소·종료 처리를 재사용한다. 다른 역할 설정을 이 앱에 전달하면 거절한다. 앱 생성만으로 DB를 만들거나 LLM/MCP/Docker를 시작하지 않는다.

## 2. 통신과 저장 경계

| 항목 | 구현 |
| --- | --- |
| Agent Card | `SINGLE_AGENT Agent`, A2A 1.0 HTTP+JSON, JSON 입출력 |
| HTTP | 기존 `/message:send`, `/tasks/{id}`, 취소 경로·Version/Content-Type 검사 |
| 인증 | 기존 선택적 loopback 개발 모드와 설정된 Bearer 검증을 그대로 재사용 |
| Task/Context ID | 서버 발급값 보존. 프로젝트 UUID나 단계 이름으로 대체하지 않음 |
| DB 정체성 | `SINGLE_AGENT`로 고정. 다른 역할 DB 재사용 거부 |
| 프로세스 소유권 | 기존 단일 live lease·CAS·append-only 기록·재시작 복구 유지 |
| 중복 Message | 기존 영속 receipt로 같은 Task 반환, 다시 실행하지 않음 |
| Single Context | Run당 원래 Context 재사용. 같은 Run의 새 Context 생성 거부 |
| 단계 실행 | 같은 Context에 실행 중/입력·인증 대기 Task가 있으면 새 병렬 Task 생성 거부. 입력·인증 재개는 기존 Task continuation 사용 |

다른 Run/Scenario의 Context를 가져올 수 없다. 완료된 단계 다음에는 같은 Context에서 새 Task를 발급받을 수 있으며, 새 Task와 새 Agent는 다른 개념이다.

## 3. 실행 명령과 현재 상태

```bash
PYTHONPATH=src .venv/bin/python -m agents.single
```

이 명령은 로컬 Single **통신 서버**를 시작한다. 현재 기본 실행기는 Bootstrap이므로 `/health`의 `executionReady`는 `false`, Card의 실행 Skill은 빈 목록이다. 작업 요청은 `AGENT_RUNTIME_NOT_CONFIGURED`로 REJECTED가 된다. 모델 설정이나 임의 주입 실행기가 있다고 실제 제품 개발 가능 상태로 광고하지 않는다.

50번은 Prompt·저장 기록 재구성, 51번은 MCP 권한, 52번은 실제 실행기·Host 연결이다. 이 단계의 테스트용 완료 실행기는 통신/저장 확인용이며 제품 개발 구현이 아니다.

## 4. 검증과 개발정의서 점검

- ASGI에서 Card·Health·요청·폴링·인증·버전 검사를 확인했다. 실제 TCP 서버를 띄운 검증은 아니다.
- 같은 Context의 다음 Task, Message 중복, 서버 재시작 후 Task/Context·기록 보존을 확인했다.
- 다른 Run의 Context, Run의 Context 분기, 병렬 단계 Task, 다른 역할 DB, 중복 live lease를 거절하는지 확인했다.
- CLI는 `uvicorn.run` 호출 인자만 검사했다. LLM·MCP·Docker를 실행하지 않았다.
- §0·§3·§6·§7의 공식 통신/식별자·수명주기, §11-A의 단일 주체를 유지했다. 기존 네 역할과 A2A 공식 Role enum은 변경하지 않았다.
- 최종 회귀 명령·결과는 [50번 검증 기록](50-single-agent-prompts-context.md)에 함께 기록한다.

커밋 메시지: `단일 Agent 정체성과 A2A 서버 및 영속 저장소 기반 추가`

다음 작업: **50번 — 통합 Prompt·단계별 Draft·동일 Context 기록**.
