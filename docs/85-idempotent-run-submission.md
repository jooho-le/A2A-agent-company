# 85번 — Run 접수 영속화와 중복 생성 방지

## 목적과 범위

응답 유실·중복 클릭·동시 요청으로 같은 업무가 여러 Run이 되는 것을 막는다. [Host·프론트엔드 연동 실행 기준 §4](https://app.notion.com/p/414d062130a981cb9c52c76b89b4369e)의 서버 내부 생성 키 계약을 구현했다.

제품 서버는 사용자·프로젝트·클라이언트 키를 자체 DB에 묶고, Host용 내부 UUIDv4를 별도로 발급·보관해야 한다. Host는 사용자 JWT나 제품 프로젝트 DB를 관리하지 않는다.

## POST 계약

```text
POST /api/v1/runs
Authorization: Bearer <제품 서버 전용 Host 토큰>
Idempotency-Key: <제품 서버가 발급한 내부 UUIDv4>
```

본문은 기존 `scenarioId`, `requestText`, `configuration`이다. 실제 `create_host()` 진입점에서는 키가 필수다. 기본 개발용 Bootstrap은 기존 키 없는 제출도 유지하며, 키를 명시하면 같은 멱등 규칙을 적용한다.

| 상황 | 응답과 동작 |
| --- | --- |
| 새 키·유효한 요청 | `201`, 기존 Run 제출 응답, `Idempotency-Replayed: false` |
| 같은 키·같은 원본 JSON | `200`, 저장한 최초 응답, `Idempotency-Replayed: true` |
| 같은 키·다른 요청 | `409 IDEMPOTENCY_KEY_CONFLICT` |
| 실제 Host에서 키 없음 | `422 IDEMPOTENCY_KEY_REQUIRED` |
| 잘못된 UUID·중복 키 헤더·중복 JSON 필드 | `422 IDEMPOTENCY_REQUEST_INVALID` |
| 승인되지 않은 Configuration·추가 제품 필드 | 기존 `422` 검증 유지 |

키가 있는 성공 응답은 `Location`과 `Cache-Control: no-store`를 제공한다. 재요청 응답의 Run 상태는 **최초 생성 당시 영수증**이며, 현재 작업 상태는 `GET /runs/{runId}`로 조회한다.

## 요청 비교와 저장

원본 JSON을 UTF-8·키 정렬·공백 없는 JSON으로 직렬화해 SHA-256을 계산한다. JSON 필드 순서·들여쓰기·유니코드 escape 표기는 같게 보지만, 문자열 내용이나 요청 필드가 다르면 별도 요청이다. 기본값을 생략한 요청과 명시한 요청도 원본 JSON이 다르면 같은 키로 교체할 수 없다.

`requestText`의 공백 정리·비밀정보 마스킹 **전** 원본으로 비교한다. 따라서 서로 다른 비밀정보가 모두 `[REDACTED]`가 되어 같은 요청으로 오인되는 문제가 없다. 원본 본문은 접수 기록에 저장하지 않고 해시만 저장한다. 기존 Run 본문의 마스킹 정책은 유지한다.

`run_submissions`는 내부 키, 요청 해시, Run/초기 Step ID, `CREATED`, 생성 시각, 최초 공개 응답을 보존한다. 업데이트·삭제는 DB trigger로 거절한다. `CREATED`는 **Run 생성이 저장됐다는 뜻**이며 작업 성공·완료가 아니다. 기존 Workflow·A2A 상태에 새 상태를 추가하지 않았다.

## 원자성과 재시작

Run, 초기 Planner Step, Trace, 고정 Configuration, Workspace 식별자, 접수 기록을 같은 SQLite `BEGIN IMMEDIATE` 트랜잭션으로 저장한다. 내부 키와 Run/Step 연결에 UNIQUE·외래키를 적용한다.

동시 요청의 승자만 Run을 만들고 작업을 예약한다. 다른 요청은 저장된 접수를 재사용하며 작업을 재전송하지 않는다. 저장 중 오류는 전체 트랜잭션을 되돌리므로 고아 Run·접수 기록이 남지 않는다. 기존 DB에는 새 테이블을 추가하며 기존 Run/Artifact/Trace를 다시 쓰지 않는다.

이미 저장된 접수는 현재 승인 설정을 다시 적용하기 전에 확인한다. 따라서 재시작 뒤 승인 설정이 바뀌어도 같은 원본 요청의 기존 영수증을 조회할 수 있고 새 작업을 예약하지 않는다.

단, DB 저장과 프로세스 내 BackgroundTasks 등록을 외부 실행까지 아우르는 하나의 트랜잭션으로 만들지는 않는다. 저장 직후 프로세스가 죽으면 최초 응답의 `SCHEDULED`만으로 실제 호출을 보장할 수 없다. 자동으로 새 Run이나 새 키를 발급하지 않는다. 서버 전용 Submission GET·응답 유실 확인·안전한 복구 연결은 **86번**에서 진행한다. 이 작업은 외부 Agent 실행의 exactly-once 보장을 주장하지 않는다.

## 검증

- 두 앱·별도 Repository 객체가 같은 SQLite에 동시에 12건 제출: `201` 1건, `200` 11건, Run/Step/접수/작업 예약 각 1건, 시작 Trace 3건.
- 재시작 뒤 같은 접수 재사용, 현재 승인 검증 실패 상황에서도 기존 영수증 반환, 추가 예약 없음.
- 다른 요청과 비밀정보 마스킹 후 같아지는 요청의 충돌 차단.
- 저장 중 강제 실패 시 전체 rollback, 같은 키로 재시도 가능.
- 잘못된 헤더·추가 필드·승인 설정 변경·불변 접수·기존 API 회귀 확인.

최종 관련 회귀는 아래 240개가 통과했다. 가상환경에 pytest가 없어 기존 unittest 실행 방식을 사용했다. 외부 API·실제 Docker·제품 서버·브라우저는 실행하지 않았다.

```bash
PYTHONPATH=src:tests .venv/bin/python -m unittest \
  test_host_service_api test_runs_api test_owned_host test_single_agent_host \
  test_owned_host_cli test_priced_host test_workflow_repository \
  test_api_dependencies test_runtime_configuration test_control_owner_lifecycle \
  test_workflow_recovery test_security_host test_host_policy_store -q
```

## 정의서 점검과 다음 묶음

요구사항·판정 규칙·동일 Snapshot 검증·수정 최대 3회·Tool Retry 최대 2회·MCP/Sandbox·모델 옵션은 변경하지 않았다. Run ID와 Step ID는 계속 Orchestrator가, A2A Task ID는 Agent가 생성한다. 인증과 멱등 키는 HTTP 경계에만 두며 Agent metadata에 전달하지 않는다.

다음 묶음은 **86번 Submission 조회·응답 유실/재시작 복구**, **87번 불변 Source ZIP**, **88번 제품 중계용 API 인계 문서·예제**다. 기존 **81번 실제 환경 기동**, **82번 실제 전체 Run 확인**은 해당 연결이 준비된 후 수행한다. Git commit/push·브랜치 병합은 하지 않았다.
