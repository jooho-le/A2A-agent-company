# 48. Single-Agent 실행 설정과 프로젝트 JSON Schema

작성일: 2026-10-31. 선행 작업: [47번 실행 계약](47-single-agent-execution-contract.md).

## 1. 구현 내용

[RunConfiguration](../src/orchestrator/domain/run_configuration.py)의 `architecture`가 `MULTI_AGENT`와 `SINGLE_AGENT`를 받도록 확장했다. 기본값은 기존 `MULTI_AGENT`이며, 다른 문자열·추가 필드·변경 가능한 동결 설정을 허용하지 않는다.

[프로젝트 Schema](../schemas/project/run_configuration.schema.json)도 같은 두 값을 받는다. Python 직렬화·역직렬화, 불변 Artifact, SQLite 저장 후 다시 읽기에서 `SINGLE_AGENT`가 보존된다. 공식 A2A 객체·Role enum은 변경하지 않았다.

## 2. 실험 조건과 API 경계

`jooho`의 이전 코드는 실험 Run의 보호 테스트 참조를 필수로 요구했다. `dev`에 이미 반영된 석민의 Ver2 정책에 맞춰, `experimentId`가 있어도 `protectedTestSuiteRef`는 비워 둘 수 있게 했다. 시작 Commit/Snapshot, 모델, 환경, 실행시간, Scanner 참조, 실행 순서·준비 상태는 계속 필수다.

보호 테스트 참조 생략은 최종 공통 평가 생략이나 Single만 다른 시험지를 쓰는 허가가 아니다. 같은 방식의 Single/Multi는 평가 쪽에서 보호 테스트 조건과 최종 시험지를 대조한다. 수정 최대 3회·도구 재시도 최대 2회 및 Credential 참조 차단은 그대로다.

현재 `/api/v1/runs`의 실행 경로는 기존 Planner 기반이다. Single 요청을 여기로 보내 이름만 Single인 Multi 실행이 만들어지지 않도록 다음과 같이 처리한다.

| 요청 | 현재 동작 |
| --- | --- |
| 구조 생략 / `MULTI_AGENT` | 기존 생성·승인·Planner 전송 경로 |
| 유효한 `SINGLE_AGENT` 설정 | HTTP 503, `SINGLE_AGENT_RUNTIME_NOT_CONFIGURED`; Run/Step 저장과 Planner 전송 없음 |
| 잘못된 구조·불완전한 실험 기준 | HTTP 422 |

Single 실행 API 연결은 52번이다. 설정 지원을 실행 준비 완료로 표시하지 않는다. 기존 Owned Host도 승인된 Multi 설정과 다른 구조를 거절하는 검사를 유지한다.

## 3. 검증과 개발정의서 점검

- 두 구조의 공개 Artifact JSON을 실제 프로젝트 Schema로 검사하고, 불변 설정·직렬화·SQLite 재조회에서 구조를 확인했다.
- Ver1/Ver2 설정과 나머지 필수 실험 조건 누락 거부를 두 구조에 대해 확인했다.
- Single API 요청이 저장·Planner 전송 없이 503으로 끝나는지 확인했다.
- §11-A의 동일 조건, §4의 Fix/Retry 한도, §11의 동결 설정·기록 원칙을 유지했다.
- 48~50번 최종 회귀 결과와 실행 명령은 [50번 검증 기록](50-single-agent-prompts-context.md)에 함께 기록한다. 실제 Single Run·LLM·Docker는 실행하지 않았다.

커밋 메시지: `싱글에이전트 실행 설정과 JSON Schema 확장`

다음 작업: **49번 — 단일 Agent 정체성과 A2A 서버·저장소 기반**.
