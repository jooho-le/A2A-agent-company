# 88번 — 제품 서버 연결용 Host API 인계

기준일: 2026-11-15. 대상은 **제품 서버 8000 → 실제 Host 8002** 경계다. 83~87번의 구현 계약을 모았으며 프론트·제품 DB를 구현하거나 실제 OpenAI·Docker 서비스를 기동한 문서는 아니다.

근거: [Host·프론트엔드 연동 실행 기준](https://app.notion.com/p/414d062130a981cb9c52c76b89b4369e), [83번 인증](83-host-service-authentication.md), [84번 승인 설정](84-host-approved-configuration.md), [85번 멱등 접수](85-idempotent-run-submission.md), [86번 조회·복구](86-submission-lookup-recovery.md), [87번 ZIP](87-immutable-source-zip.md).

## 1. 연결·담당 경계

```text
브라우저 --제품 JWT--> 제품 API 8000 --Host 서비스 Bearer--> Host 8002
                                                       --> 역할별 Agent/MCP
```

- 제품 서버: JWT·소유권, 프로젝트명, 클라이언트 접수 키, Host 내부 키, Project→Run 연결 저장.
- Host: 승인 설정·생성 접수·Run/Step/Trace·불변 Source 제공. 제품 JWT나 사용자/프로젝트 DB를 관리하지 않음.
- `A2A_HOST_API_TOKEN`은 두 서버의 프로세스 환경에만 주입. 브라우저·Vite 변수·요청 본문·문서에 실제 값을 넣지 않음.
- Host와 Agent는 loopback·worker 1·reload 없이 실행. 제품 서버 목적지 URL은 서버 설정으로 고정.
- 기본 Bootstrap과 낮은 수준의 플랫폼 조립 함수를 실제 서비스 진입점으로 대신 사용하지 않음.

## 2. API 목록

모든 아래 경로는 Host 기준이며 서비스 Bearer가 필요하다.

| 순서 | API | 사용 |
| --- | --- | --- |
| 1 | `GET /api/v1/scenarios` | SCN-001 등록 UUID·지원 범위 확인 |
| 2 | `GET /api/v1/host/approved-configuration?scenarioId={uuid}` | 현재 공개 승인 설정과 식별자 |
| 3 | `POST /api/v1/runs` | 내부 `Idempotency-Key`로 Run 접수 |
| 복구 | `GET /api/v1/submissions/{internalKey}` | 응답 유실 시 기존 접수·현재 Run 확인 |
| 조회 | `GET /api/v1/runs/{runId}` | 현재 Workflow·Verdict |
| 조회 | `GET /api/v1/runs/{runId}/steps` | 역할·시도·Task·버전 |
| 조회 | `GET /api/v1/runs/{runId}/events?limit=100&offset={offset}` | 저장 순서의 Trace. total까지 페이지 처리 |
| 결과 | `GET /api/v1/runs/{runId}/artifacts` | Source와 각 보고서 목록 |
| 결과 | `GET /api/v1/runs/{runId}/issues` | 수정·재검증 Issue |
| 결과 | `GET /api/v1/runs/{runId}/artifacts/{artifactId}/download` | 선택한 등록 SOURCE의 ZIP |
| 제어 | `POST /api/v1/runs/{runId}/recover` | 기존 기록 기준 명시적 복구 |
| 제어 | `POST /api/v1/runs/{runId}/resume` | 검토/입력 후 기존 작업 재개 |
| 제어 | `POST /api/v1/runs/{runId}/cancel` | 기존 안전한 취소 규칙 |

실제 Host의 `/openapi.json`에는 서비스 Bearer, 필수 생성 키·Configuration, 생성 201/재사용 200, 접수 조회 모델, ZIP binary 응답·두 해시 헤더를 기술했다. `/docs`에서 확인할 수 있으며 토큰 값은 스키마에 들어가지 않는다. 이는 개발 진단용이고 브라우저 제품에 Host 토큰을 공급하는 경로가 아니다.

## 3. 요청 구성 예제

등록된 SCN-001 UUID는 `f7f9e5c3-ffc3-4b3f-918b-21e1b956ce76`이다. 실제 `/scenarios` 응답과 일치하는지 확인한다. 프로젝트명은 제품 DB, 자연어 원문은 `requestText`로 분리한다.

아래는 **제품 서버 구현 예시**다. 이번 작업에서 실행하지 않았다. POST를 실제 실행하면 Agent 작업과 유료 모델·Docker 처리가 시작될 수 있다.

```python
import hashlib
import json
import os
import httpx

scenario_id = "f7f9e5c3-ffc3-4b3f-918b-21e1b956ce76"
with httpx.Client(
    base_url="http://127.0.0.1:8002",
    headers={"Authorization": "Bearer " + os.environ["A2A_HOST_API_TOKEN"]},
    timeout=30, trust_env=False, follow_redirects=False,
) as host:
    approved_response = host.get("/api/v1/host/approved-configuration",
                                 params={"scenarioId": scenario_id})
    approved_response.raise_for_status()
    approved = approved_response.json()
    host_body = {
        "scenarioId": scenario_id,
        "requestText": "이메일과 비밀번호로 회원가입하는 API를 만들어줘.",
        "configuration": approved["configuration"],
    }
    request_sha256 = hashlib.sha256(json.dumps(
        host_body, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")).hexdigest()

    # 이 지점에서 제품 DB 트랜잭션을 먼저 커밋한다:
    # 소유자·projectId·clientKey -> 내부 UUIDv4 internal_key,
    # configurationId·원본 host_body·request_sha256를 동일 접수에 고정.
    # internal_key = 이미 저장된 접수의 내부 키

    # created = host.post("/api/v1/runs", json=host_body,
    #                     headers={"Idempotency-Key": internal_key})
    # 201과 200 모두 같은 Run 제출 응답 모델이다.
```

브라우저의 `projectId`, `configurationId`, `configuration`은 제품 서버가 소유권·최신 승인값과 모두 대조한다. 생성 시에는 서버가 확인한 Configuration을 사용한다. Host 본문에는 `scenarioId`, `requestText`, `configuration`만 전달한다. `projectId`, `configurationId`, `approved`, 사용자 ID, 클라이언트 키를 Host 본문에 추가하면 422다.

`configurationId`는 Host가 준 값을 사용한다. 브라우저의 숫자 직렬화로 식별자를 재발급하지 않는다. 최초 Host 요청을 저장한 뒤 설정을 바꾸거나 기본값을 다시 구성해 같은 내부 키에 다른 본문을 보내지 않는다.

## 4. 접수·중복·응답 유실 규칙

| 관측 | 제품 서버의 처리 |
| --- | --- |
| POST `201` | 새 Run 연결을 제품 DB에 저장 |
| POST `200` + `Idempotency-Replayed: true` | 기존 Run 연결 재사용. 새 Run으로 표시하지 않음 |
| POST `409 IDEMPOTENCY_KEY_CONFLICT` | 같은 키의 요청 변경 오류. 새 키 자동 발급 금지 |
| 응답 타임아웃·네트워크 오류·판단 불가한 5xx | 접수를 불확실 상태로 보존하고 같은 내부 키로 GET 조회 |
| 접수 GET `200`, `status=CREATED` | 내부 키·요청 해시·Run ID를 대조하고 기존 ProjectRun 연결 저장/복원 |
| 접수 GET `404` | POST 실패 확정 아님. 같은 키로 제한된 조회 후 미확인으로 보존 |
| 접수 GET `503`·연결 장애 | 미확인으로 보존. 제품의 프로젝트명·기존 Run 연결은 유지 |

Host가 영속화하는 접수 상태는 `CREATED` 하나다. 생성 전·실패한 POST의 접수를 만들어 놓지 않는다. 제품의 `PENDING/UNKNOWN` 등 상태는 별도의 제품 저장 규칙이며 Host Workflow나 A2A 상태로 보내지 않는다.

제품 서버는 원래 접수 매핑의 내부 키를 사용해 `GET /api/v1/submissions/{internalKey}`를 호출한다. `requestSha256`는 **Host에 실제 전달한 원본 JSON**의 해시다. 프로젝트 필드를 더한 브라우저 요청 전체의 해시와 비교하지 않는다.

조회 응답의 `originalResponse`는 최초 영수증, `run`은 현재 상태다. `CREATED`·`SCHEDULED`·A2A COMPLETED를 앱 개발 성공으로 표시하지 않는다. 화면 성공은 기존 Host의 `FINISHED + SUCCESS`를 기준으로 한다.

GET 연결 오류 복구는 기존 계약의 2·4·8초 총 3회 범위를 지키고 무제한 반복하지 않는다. 접수 없음 404도 새 생성의 근거로 쓰지 않는다. 생성 POST는 자동 재전송하지 않고 수동 재시도가 필요하면 **보관한 같은 내부 키·같은 본문**을 사용한다.

## 5. 복구·이벤트·다운로드 연결

`recovery.action=RECOVER`는 초기 미전송 상태의 안내다. 명시적으로 기존 `/runs/{runId}/recover`에 `{}`를 보내며 활성 작업·기존 예산 검사를 통과해야 한다. 알려진 Task는 재조회하고, `RECONCILE`이면 Task ID 없는 전송을 재실행하지 않는다. 입력 대기는 기존 `/resume`에 해당 Step과 입력을 지정한다. 사용자 확인 없이 새 Run을 만들거나 임의로 입력을 합성하지 않는다.

Run·Step·새 Trace는 2초 간격으로 겹치지 않게 조회한다. `runId + eventId`로 중복 표시를 막고 API 저장 순서를 유지한다. 요청 보냄·Task 접수·결과물 등록·최종 판정은 각각 다른 근거다. 재접속은 과거 이동 애니메이션을 자동 재연하지 않고 현재 상태를 복원한다. 실제 연결 실패를 목업 성공으로 바꾸지 않는다.

다운로드는 Artifact 목록의 `artifactType=SOURCE`와 요청 Run·버전을 확인한 뒤 같은 경로를 중계한다. 최종본 표시는 `run.codeVersion`과 해당 Source/실제 판정을 확인하고, 검증 중 후보와 구분한다. 더 잘 나온 과거 코드를 임의로 최종본으로 바꾸지 않는다.

제품 서버는 소유권 검사 후 ZIP을 스트리밍하고 `Content-Type`, 안전한 `Content-Disposition`, 원본/ZIP 해시, Artifact ID·코드 버전 헤더를 전달한다. ZIP SHA-256은 `X-Archive-Sha256`, 기존 Source Snapshot 확인은 `X-Snapshot-Sha256`을 사용한다. 404/409/413/503은 파일처럼 내려주지 않고 다운로드 불가 상태로 처리한다. `SOURCE_DOWNLOAD_REJECTED`는 차단 기록이며 자동 Verdict 변경이나 자동 코드 수정 이벤트가 아니다.

## 6. 남은 연결과 검증 범위

- 1·2번 산출물: 83~88번 HTTP 계약·구현·오프라인/ASGI 검사. 제품 서버·프론트 파일은 이번 작업에서 수정하지 않았다.
- 3번 연결: JWT·소유권, 승인 조회 중계, 접수 매핑 선저장, Run 연결·재접속, 상태/Trace, Source ZIP 중계. 준비되기 전 실제 POST 차단을 유지한다.
- 기존 81번: 승인 Runtime/Tool/시작 Source·이미지·자격증명을 준비하고 실제 Host/Agent/Docker 기동.
- 기존 82번: 실제 생성→Build→QA/Security→수정·재검증과 제품 화면·기록 대조.
- 4번의 평가용 생성 앱 연결 규격·공통 시작 코드·독립 시험은 이 Host HTTP 계약과 별개다.

관련 **unittest 382개가 통과**했다. 다음 명령으로 임시 Git·SQLite·ASGI·Fake A2A/Provider를 사용했다. 실제 외부 API·Docker·제품 서버·브라우저는 실행하지 않았으며, 이 결과를 실제 회원가입 앱 완성이나 독립 비교 실험 완료로 기록하지 않는다.

```bash
PYTHONPATH=src:tests .venv/bin/python -m unittest \
  test_submission_lookup test_source_download test_host_handoff_schema \
  test_host_service_api test_runs_api test_owned_host test_single_agent_host \
  test_owned_host_cli test_priced_host test_workflow_repository test_api_dependencies \
  test_runtime_configuration test_control_owner_lifecycle test_workflow_recovery \
  test_security_host test_host_policy_store test_artifact_service test_artifact_blob_store \
  test_sandbox_materialization test_source_credentials -q
```

## 7. 개발정의서 점검

A2A 1.0 상태/metadata/Task ID 생성, Scenario 요구사항, 불변 Source와 실행환경, MCP 역할 권한·Sandbox, 수정 최대 3회·Tool Retry 최대 2회·동결 예산·규칙 판정은 유지했다. 새 기능은 HTTP 접수·조회·변환 경계다. 실패·미검증을 성공으로 바꾸거나 기존 Snapshot·Artifact·Trace를 덮어쓰지 않는다. Git commit/push·브랜치 병합은 수행하지 않았다.
