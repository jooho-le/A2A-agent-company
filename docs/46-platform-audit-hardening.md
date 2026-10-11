# 46. 역할 1·2 개발정의서 점검 후 보완

작성 기준: 2026-10-11. 대상은 Orchestrator와 Agent/MCP 플랫폼이다. 새 제품 기능을 추가한 단계가 아니라, 45번 이후 점검에서 발견한 네 항목을 수정한 기록이다. **44번 실제 LLM·Docker 실행은 여전히 보류**하며 역할 3·4 구현·연결은 변경하지 않는다.

## 1. 해결한 항목

| 항목 | 기존 문제 | 보완 |
| --- | --- | --- |
| Source 비밀정보 반입 | 일반 파일의 Credential 본문이 시작 Source·Git Snapshot·BLOB·Sandbox로 들어갈 수 있었음 | 공통 검사로 거부. 허용된 원문 bytes·Commit·Snapshot Hash는 변경하지 않음 |
| Python 3.10 호환성 | `>=3.10` 선언과 달리 두 실행 경로가 3.11 전용 `asyncio.timeout()` 사용 | 같은 Task에서 동작하는 one-shot deadline 대체 경로 추가. 3.11+는 기존 native 사용 |
| RunConfiguration Credential | 검사 Suite/Scanner URL에 userinfo·Token을 넣으면 동결 설정에 남을 수 있었음 | 두 참조 필드는 저장 전 거부. 공통 로그·일반 JSON에는 URL Credential 마스킹 적용 |
| Agent DB 초기화 경쟁 | 동시 초기화의 WAL 전환이 생 SQLite 잠금 오류로 종료될 수 있었음 | BUSY/LOCKED만 최대 5초 내 재시도하고, 실패는 안전한 Store 오류로 반환 |

17번 당시 사용자 요청으로 제외됐던 URL 보호와 별도 WAL 제한도 이번 보완 요청에 포함했다. 제품의 비밀번호 Hash 알고리즘·정책을 바꾸거나 회원가입 서비스 코드를 수정한 것은 아니다.

## 2. Source 검사 경계

공통 구현은 [source_credentials.py](../src/orchestrator/core/source_credentials.py)다. 다음 접점에서 같은 거부 규칙을 사용한다.

- 시작 Repository의 승인 Commit Blob·Git Export: Workspace 복사 전에 검사.
- Developer Working Source: 새 Git 객체·Commit 작성 전에 검사.
- Source Artifact BLOB: 저장·Grant 발행 전 검사, 기존 BLOB도 조회할 때 재검사.
- Sandbox: Snapshot 및 추가 입력 파일을 실제 파일로 준비하기 전에 검사.
- LLM/MCP Source 필드: 기존 Source 원문 보존 정책을 공통 검사와 맞춤.

Credential 문자열 할당, 알려진 API Key/Bearer/JWT/Password Hash/Private Key 패턴, 이미 등록된 Host 자격증명, 정적 URL 인증정보를 검사한다. UTF-8/16/32, 일반적인 escape·문자 정규화·문자열 연결·최대 3회 percent encoding도 검사 뷰에서만 해석한다. 확장자를 `.bin`으로 바꿔도 검사하며 정상 바이너리는 보존한다.

일반 `password = request.password`, `password_hash = hash_password(password)`, 환경변수 참조, 동적으로 조립하는 URL 인증식은 비밀값 자체가 아니므로 원문을 유지한다. Source를 `[REDACTED]`로 치환하면 실행 내용과 승인 Hash가 달라지므로 **자동 정제·재동결 대신 거부**한다. 불변 Artifact·기존 Git 이력을 덮어쓰지 않는다.

검사 뷰 중복을 제거하고 불필요한 decoding을 줄였다. 같은 기존 15초 RunBudget에서 반복 Scanner 회귀가 다시 통과했으며 실행 예산이나 Tool/Fix 한도를 늘리지 않았다.

이 검사는 인식 가능한 패턴과 인메모리 등록 Key의 방어 경계다. 임의 암호화·난독화·분산 조립·실행 시 생성되는 모든 비밀정보를 탐지하는 taint 분석기는 아니다. 환경변수/Key 파일을 추가로 읽어 검사 목록을 만들지 않는다.

## 3. URL 참조 및 로그 보호

`RunConfiguration.protectedTestSuiteRef`와 `scannerProfileRef`에는 Credential을 넣지 않는다. 다음 입력은 API에서 422로 거부하며 Run·RunConfiguration을 저장하지 않는다.

```text
https://user:<credential>@host.example/tests
https://host.example/tests?api_key=<credential>
https://host.example/tests#access_token=<credential>
```

username-only userinfo, 인코딩된 Key·구분자·scheme, 전체 인코딩 URL, 중첩 redirect URL도 검출한다. 이미 `[REDACTED]`인 userinfo/비밀 파라미터 역시 동결 참조로 허용하지 않는다. 정상 `artifact://protected/tests`, Credential 없는 URL·경로·Hash는 그대로 보존한다.

API 검증 응답은 제출 값·입력 본문·예외 context를 반환하지 않는다. RunConfiguration 및 해당 Artifact의 검증 오류 문자열도 입력 값을 숨긴다. 일반 요청문·보고서·로그의 URL은 공통 [security.py](../src/orchestrator/core/security.py)에서 마스킹하며, 인코딩된 구조를 안전하게 부분 처리할 수 없으면 해당 URL 전체를 마스킹한다. 프로젝트 Hash와 Agent 발급 opaque ID는 기존 원문 보존 규칙을 유지한다.

**기존 DB 주의:** 과거 저장된 Credential-bearing RunConfiguration은 새 검증으로 조회·Repository 초기화/백필이 거부될 수 있다. 기존 레코드를 몰래 마스킹하거나 불변 Trigger를 해제해 고치지 않는다. 해당 데이터가 있는 운영 DB는 별도 복구 방향을 승인받아야 한다. 실제 자격증명이 노출됐다면 자격증명 교체도 별도로 필요하다. 이번 작업은 기존 운영 DB를 변경하지 않았다.

## 4. Deadline 및 DB 정책 유지

[task_deadline](../src/agents/core/deadlines.py)은 MCP stdio enter/body/exit를 같은 asyncio Task에서 처리한다. `wait_for()`로 별도 Task를 만들거나 반복 취소로 async cleanup/동기 worker 회수를 끊지 않는다. 각 단계에는 새 시간이 아닌 **기존 Run의 남은 시간**을 전달한다. 자체 만료와 일반 transport timeout·외부 취소를 구분한다.

실제 검증 환경은 Python 3.11.9다. 3.10 경로는 native timeout API 제거 및 cancel-count API가 없는 Task proxy로 검사했다. 실제 Python 3.10 인터프리터 실행을 확인했다고 주장하지 않는다. 3.10에는 취소 count API가 없으므로, 외부 취소 cleanup 중 deadline까지 도착하는 극단 경합은 native 3.11 수준의 count 보장을 하지 못한다.

[SQLiteAgentTaskStore](../src/agents/api/sqlite_task_store.py)는 역할 identity·DDL을 기존 원자적 트랜잭션으로 유지한다. 트랜잭션 밖 WAL 전환에서 SQLite BUSY/LOCKED만 짧게 기다리며 총 5초 한도를 둔다. 다른 오류/잘못된 journal mode/시간 소진은 안전한 Store 오류이며 경로·SQL·Credential을 노출하지 않는다.

이는 **DB 시작 경합 재시도**이며 모델·Tool 재시도가 아니다. Orchestrator + 네 역할의 DB 다섯 개를 분리하는 정책, 한 DB의 단일 live lease, 다른 역할 접근 거부, append-only 이력은 그대로다. 공유 DB나 multi-worker 운영을 허용하는 변경이 아니다.

## 5. 개발정의서 점검

| 기준 | 결과 |
| --- | --- |
| §0·§3·§6·§7 통신/공식 객체 | A2A 1.0 HTTP+JSON, wire `application/a2a+json`, MCP 2026-07-28 stdio 유지. SDK/lock 변경 없음 |
| §2 역할 경계 | 역할 1·2 코드만 보완. 제품 UI/API/DB 및 독립 평가 기준 변경 없음 |
| §4 수정/Retry | Fix 최대 3회·동일 논리 Tool Retry 최대 2회 및 공유 Run 예산 유지 |
| §5·§9 동일 검증 대상/판정 | Source 원문·불변 Snapshot/Artifact·동일 환경 유지. A2A COMPLETED를 제품 PASS로 바꾸지 않음 |
| §8-7·8-8·8-10 | Source 비밀 반입 차단 추가. Workspace 밖 파일 접근·Host 직접 실행·Container Key 주입 허용 안 함 |
| §11-5 Trace | URL Credential 공통 마스킹 보완. Source 전체를 일반 Trace에 넣지 않음 |

## 6. 검증 기록

- Credential/로그/RunConfiguration/API 검증 회귀: **29개 통과**.
- Deadline/Host 회귀: **38개 통과**. same-Task, 정리 완료, 외부 취소, worker 회수 포함.
- Agent Store/lifecycle/server 회귀: **106개 통과**. 실제 SQLite 읽기 잠금, 서로 다른 역할 24회·같은 역할 12회 동시 초기화 포함.
- Source 기존 관련 7모듈 회귀: **241개 통과**. 이후 추가한 인코딩·중첩 URL을 포함한 공통 검사 **13개 통과** 및 주요 접점 재검증 **18개 통과**.
- 전체 unittest 발견 **3,308개** 중 실제 소켓을 여는 4개를 제외한 **3,304개 통과**. 모듈 단위 세 그룹(1,042 + 993 + 1,269)으로 실행했으며 실패·오류·선택된 테스트의 skip은 모두 0이다. runtime 로그는 비활성화하지 않았다.
- 제외한 4개는 `test_owned_platform_tcp`의 두 TCP Host 테스트, `test_owned_controls_tcp`의 제어 테스트, `test_workspace_access`의 Unix socket/FIFO 접근 테스트다. 이 네 테스트의 성공을 주장하지 않는다.
- 위 focused 결과에는 중복 테스트가 있으므로 전체 결과와 합산하지 않는다.
- 문서의 focused 재검증 명령을 최종 코드에서 실행: **120개 통과**. Git Snapshot 크기 제한 우선 검사까지 재확인한 회귀 **23개 통과**.
- `pip check`: 의존성 충돌 없음. `git diff --check` 통과. 실제 LLM·Docker·TCP listener·팀원 서비스 호출은 실행하지 않았다. 테스트 안의 임시 Git/SQLite/파일·로컬 MCP stdio·ASGI·모의 Docker 검증은 실제 제품 실행과 구분한다.

focused 재검증 명령:

```bash
PYTHONPATH=src:tests .venv/bin/python -m unittest -q \
  test_credential_references test_security test_run_configuration \
  test_api_validation_errors test_source_credentials \
  test_task_deadlines test_host_services test_agent_task_store
git diff --check
```

## 7. 커밋 및 다음 작업

커밋 메시지: `Source 비밀정보 차단과 Python 호환성 및 Agent DB 초기화 보완`

**다음 작업은 44번 실제 실행 준비·확인**이다. 이번 46번은 추가 기능 로드맵이 아닌 점검 보완 기록이며, 45번 정리가 끝난 상태를 유지한다. 사용자가 Docker·승인 설정·자격증명을 준비하고 실제 실행을 요청할 때 진행한다. 프로젝트 Git commit/push는 하지 않았다.
