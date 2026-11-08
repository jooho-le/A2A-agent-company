# 프론트엔드·백엔드 연동 안내

확인 기준: 2026-11-08의 현재 로컬 코드입니다. 역할 3의 React 화면, FastAPI 제품 백엔드, Orchestrator를 연결할 때 필요한 정보를 정리합니다. 아래 명령은 실행 안내이며 이번 문서 작업에서 서버·LLM·Docker를 실행했다는 뜻은 아닙니다.

## 1. 세 시스템의 역할과 주소

| 시스템 | 코드 | 현재 프론트엔드에서 사용하는 주소 | 담당 기능 |
| --- | --- | --- | --- |
| React 개발 서버 | `frontend/` | `http://localhost:5173` | 화면, 입력, 토큰 보관, API 요청 |
| 제품 FastAPI 백엔드 | `backend/` | `http://127.0.0.1:8000` | 회원가입·로그인·JWT·전자문서 |
| Orchestrator | `src/orchestrator/` | Vite 프록시 대상 `http://127.0.0.1:8001` | Agent Run·Step·Event·산출물 관리 |

```text
브라우저의 React
  ├─ http://127.0.0.1:8000/api/auth/*     → 제품 백엔드 → 사용자 DB
  ├─ http://127.0.0.1:8000/api/documents  → 제품 백엔드 → 문서 DB·로컬 PDF
  └─ /api/v1/* → Vite 프록시 → Orchestrator → A2A Agent 실행기 → LLM·도구
```

제품 백엔드와 Orchestrator는 별도 서버·DB 계약입니다. 제품 서버를 실행해도 Agent 서버는 시작되지 않습니다. 제품 JWT를 Orchestrator·A2A 공통 인증으로 전달하는 기능도 현재 UI에는 연결되어 있지 않습니다.

실제 Owned Host의 포트는 승인된 실행 설정을 따릅니다. 기본 `8000`을 사용하면 제품 서버와 충돌할 수 있으므로 현재 프록시 대상인 `8001`과 맞는지 담당자와 확인해야 합니다.

## 2. 설치·실행 순서

### 제품 백엔드: 첫 번째 터미널

저장소 루트에서 시작하는 macOS·Linux 기준 명령입니다. 기존 가상환경이 있으면 생성 단계를 생략합니다.

```bash
cd backend
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
export JWT_SECRET="$(.venv/bin/python -c 'import secrets; print(secrets.token_urlsafe(32))')"
.venv/bin/python -m uvicorn main:app --host 127.0.0.1 --port 8000 --reload
```

`JWT_SECRET`은 서버 터미널의 환경변수입니다. 생성 명령은 값을 화면에 출력하지 않고 환경변수에 넣습니다. 이미 사용하는 안전한 키가 있다면 이를 유지합니다. 키를 바꾸면 기존 토큰이 더 이상 검증되지 않습니다. 현재 코드가 `.env` 파일을 자동으로 읽는 구조는 아닙니다.

| 확인 대상 | 주소 |
| --- | --- |
| 제품 서버 실행 확인 | `http://127.0.0.1:8000/` |
| Swagger UI | `http://127.0.0.1:8000/docs` |
| OpenAPI JSON | `http://127.0.0.1:8000/openapi.json` |

`GET /`의 정상 응답은 `{"message":"A2A backend is running"}`입니다. 이 응답만으로 JWT 설정이나 Agent 실행까지 확인되는 것은 아닙니다. 테이블은 시작 시 `create_all()`로 생성하며 기존 테이블을 자동 변경하는 마이그레이션은 없습니다.

### 프론트엔드: 두 번째 터미널

별도 터미널의 저장소 루트에서 시작합니다. 설치된 Vite의 Node.js 지원 범위는 `^20.19.0 || >=22.12.0`입니다.

```bash
cd frontend
npm ci
npm run dev
```

터미널에 표시된 주소로 접속합니다. 기본 포트가 사용 중이면 `5174` 등으로 바뀔 수 있습니다. 오피스 목업 확인에는 제품 서버나 LLM이 필요하지 않습니다. 3D 장면은 WebGL 지원 브라우저가 필요합니다.

Orchestrator·Agent를 실제 실행하려면 별도의 승인된 Host 설정·소스·도구·모델·환경변수가 필요합니다. 제품 서버 실행 명령을 그대로 재사용하거나 임의의 설정으로 유료 모델을 호출하지 않습니다.

## 3. 화면과 API 연결 위치

| 파일 | 역할과 실제 연결 여부 |
| --- | --- |
| [App.jsx](frontend/src/App.jsx) | 상태 기반 페이지 전환, 로그인 진입 설정 |
| [LoginPage.jsx](frontend/src/pages/LoginPage.jsx) | 실제 로그인 API, `access_token` 저장 |
| [SignupPage.jsx](frontend/src/pages/SignupPage.jsx) | 실제 회원가입 API, 성공 후 로그인 이동 |
| [DocumentPage.jsx](frontend/src/pages/DocumentPage.jsx) | 실제 문서 업로드·제출/배정 목록·다운로드 |
| [AtelierDashboard.jsx](frontend/src/pages/AtelierDashboard.jsx) | 오피스·프로젝트·직원·업무 흐름·결과물 목업 |
| [PipelinePage.jsx](frontend/src/pages/PipelinePage.jsx) | 실제 Run 생성 및 Run·Step·Event 조회 |
| [agentRuns.js](frontend/src/api/agentRuns.js) | 실행 모드 인자를 지원하는 Run 생성 함수. 현재 위 화면에서는 미사용 |
| [vite.config.js](frontend/vite.config.js) | `/api/v1`, `/health`를 Orchestrator로 전달 |

현재 기본 설정은 `App.jsx`의 `REQUIRE_PLATFORM_LOGIN = false`입니다. 첫 화면은 로그인 없이 열리는 atelier 오피스이며, 사이드바 하단 대표님 계정 메뉴에서 `Agent Pipeline 열기`와 `전자문서 관리`를 선택합니다. 로그인 화면으로 직접 이동하는 메뉴는 현재 기본 흐름에 없습니다.

기존 로그인 흐름으로 계정 연동을 검증하려면 해당 설정을 `true`로 사용하는 절차를 팀과 정합니다. 이 경우 회원가입 → 로그인 → 오피스로 이어집니다. 이 문서 작업에서는 설정을 변경하지 않았습니다. 공개 오피스 진입은 서버의 문서 인증·권한 검사를 해제하지 않습니다.

오피스의 업무 시작·승인·수정 요청·진행률·예시 결과물·3D 시연은 목업입니다. 3D 완료 연출이나 예시 파일 다운로드는 실제 Run 성공이나 실제 Artifact 생성의 증거가 아닙니다. 실제 Pipeline 응답도 현재 3D 캐릭터에 자동 반영되지 않습니다.

## 4. 제품 인증 계약

### 회원가입과 로그인

두 API는 `Content-Type: application/json`으로 `email`, `password`를 받습니다. 아래는 형식 설명용 가상 입력입니다.

```json
{
  "email": "user@example.com",
  "password": "Password123"
}
```

| 요청 | 정상 응답 | 주요 실패 |
| --- | --- | --- |
| `POST /api/auth/signup` | 201, `{"success":true,"message":"회원가입에 성공했습니다."}` | 409 이메일 중복, 422 입력 검증, 500 저장 실패 |
| `POST /api/auth/login` | 200, `{"access_token":"<access_token>","token_type":"bearer"}` | 401 이메일/비밀번호 불일치, 422 입력 검증, 500 JWT 설정 오류 |
| `GET /api/auth/me` | 200, `id`, `email`, `role` | 401 토큰 없음·무효·만료·사용자 없음, 500 JWT 설정 오류 |

회원가입에서 이메일 형식·최소 8자 비밀번호를 검증하고 Argon2 해시만 저장합니다. 추가 필드가 금지되어 있으므로 요청으로 `role`을 지정할 수 없습니다. 로그인 실패는 사용자 존재 여부를 구분하지 않고 `이메일 또는 비밀번호가 올바르지 않습니다.`를 반환합니다.

JWT는 `HS256`, 유효기간 30분이며 `sub`에 문자열 사용자 ID, `email`, `exp`를 넣습니다. 비밀번호·해시·역할은 토큰에 넣지 않습니다. `JWT_SECRET`이 없을 때 안전하지 않은 기본값을 사용하지 않고 토큰 발급·검증 시 설정 오류를 알립니다.

### 브라우저 토큰 사용

로그인 성공 시 기존 화면은 `sessionStorage.setItem('access_token', data.access_token)`으로 저장합니다. 보호된 문서 API에는 다음 헤더를 사용합니다.

```http
Authorization: Bearer <access_token>
```

`sessionStorage`는 같은 출처·탭에서 새로고침 후 유지됩니다. `localhost:5173`과 `localhost:5174`는 다른 출처이므로 토큰이 공유되지 않습니다. 로그아웃은 토큰을 제거하는 프론트 처리이며 서버의 토큰 폐기 API는 없습니다.

현재 앱은 저장된 토큰의 존재로 초기 화면을 판단하며 `/api/auth/me`를 통한 초기 검증·자동 갱신·만료 시 자동 로그아웃은 구현하지 않았습니다. 서버가 최종 인증을 수행합니다. 토큰이 없으면 DocumentPage는 요청을 보내지 않고 로그인 필요를 안내합니다.

실제 토큰·비밀번호·JWT 비밀키를 문서·Git·로그에 남기지 않습니다. `JWT_SECRET`이나 LLM API Key를 프론트엔드 또는 `VITE_*` 변수에 넣지 않습니다.

## 5. 전자문서 계약

모든 문서 API는 Bearer JWT가 필요합니다. 사용자 이름·담당자 목록 API는 없으므로 담당자는 기존 가입 사용자의 숫자 ID로 입력합니다. 해당 계정의 `GET /api/auth/me`로 자신의 ID를 확인할 수 있습니다.

### 업로드

`POST /api/documents`는 `multipart/form-data`의 `file` 1개와 `assignee_id`를 받습니다. 아래는 기존 화면과 같은 전송 방식의 예시입니다. `file`, `assigneeId`는 사용자가 입력한 값입니다.

```js
const token = sessionStorage.getItem('access_token')
const formData = new FormData()
formData.append('file', file)
formData.append('assignee_id', assigneeId)

const response = await fetch('http://127.0.0.1:8000/api/documents', {
  method: 'POST',
  headers: { Authorization: `Bearer ${token}` },
  body: formData,
})
```

`Content-Type`은 직접 설정하지 않습니다. 브라우저가 multipart 경계값을 포함해 설정합니다. 위 코드는 전송 부분의 예이며 실제 화면은 토큰·입력·응답·통신 실패도 처리합니다.

- 제출자는 JWT의 사용자입니다. 클라이언트의 `submitter_id`로 변경하지 않습니다.
- 담당자는 DB에 존재하는 사용자이며 제출자와 같은 ID여도 지정할 수 있습니다.
- 10MiB = 10,485,760바이트 이하를 허용하고 초과는 413입니다.
- 서버는 시작 바이트 `%PDF-`를 확인하며 불일치는 415입니다. 확장자나 신고된 MIME 유형만으로 판단하지 않습니다.
- 전체 PDF 구조 분석·악성코드 검사는 없습니다.

성공 시 201로 다음 메타데이터를 반환합니다. 값은 설명용 예시이며 실제 레코드를 나타내지 않습니다.

```json
{
  "id": 1,
  "original_filename": "증빙자료.pdf",
  "file_size": 2048,
  "content_type": "application/pdf",
  "submitter_id": 1,
  "assignee_id": 2,
  "created_at": "2026-11-08T10:00:00"
}
```

현재 화면은 성공 정보를 표시하고 선택 중인 목록을 다시 조회합니다. 서버 내부의 `stored_filename`과 `file_path`는 공개 응답에 없습니다.

### 목록

| API | JWT 사용자와의 관계 |
| --- | --- |
| `GET /api/documents?view=submitted` | 자신이 제출한 문서. `view` 생략 시에도 동일 |
| `GET /api/documents?view=assigned` | 자신이 담당자인 문서 |

응답은 `{"documents":[...]}`이며 각 항목은 업로드 성공 시와 같은 공개 메타데이터입니다. 비어 있으면 200으로 `{"documents":[]}`를 반환합니다. `created_at` 내림차순·동일 시각이면 `id` 내림차순으로 정렬합니다. 다른 사용자 ID를 쿼리에 전달해 검색하는 계약이 아닙니다.

화면의 기본 탭은 `submitted`이며 `assigned`로 전환하면 해당 목록을 조회합니다. 원본 파일명, ID, 제출 일시, 파일 크기, 제출자 ID, 담당자 ID를 표시합니다. 사용자 이름을 추측하지 않고, 탭 변경 시 이전 요청을 취소해 오래된 결과가 섞이지 않게 합니다.

### 다운로드

`GET /api/documents/{document_id}/download`는 성공 시 200과 `application/pdf` 바이너리를 반환합니다. 제출자 또는 담당자만 받을 수 있습니다. 무관한 사용자는 403, 문서나 저장 파일이 없을 때 등은 404입니다.

현재 화면은 Bearer를 붙인 `fetch`로 요청하고 `response.ok`와 PDF 형식을 확인한 뒤 `Blob`으로 변환합니다. 오류 JSON을 PDF로 저장하지 않습니다. 목록의 `original_filename`을 안전한 이름으로 정리해 임시 링크로 저장하고 Object URL·DOM 요소를 사용 후 정리합니다. 각 문서의 로딩·오류는 독립적입니다.

서버는 `Content-Disposition`을 설정하지만 현재 CORS에는 해당 헤더의 공개 설정이 없습니다. 화면은 이를 읽는 데 의존하지 않고 목록의 원본 파일명을 사용합니다. 일반 URL 링크만으로는 JWT가 붙지 않으므로 보호 API를 단순한 `<a href>`로 바꾸지 않습니다.

## 6. 저장 위치·스키마

| 대상 | 원본·저장 위치 |
| --- | --- |
| DB 모델 | [backend/models.py](backend/models.py) |
| API 모델 | [backend/schemas.py](backend/schemas.py) |
| DB 연결 | [backend/database.py](backend/database.py) |
| SQLite 파일 | `backend/app.db` |
| PDF 실체 | `backend/uploads/`의 UUID v4 기반 `.pdf` 파일 |

`User`에는 `id`, `email`, `password_hash`, `role`, `created_at`이 있습니다. `Document`에는 `id`, `submitter_id`, `assignee_id`, `original_filename`, `stored_filename`, `file_path`, `file_size`, `content_type`, `created_at`이 있습니다.

`submitter_id`와 `assignee_id`는 모두 `users.id`를 참조합니다. 이메일과 저장 파일명은 각각 고유합니다. DB에는 PDF 바이너리를 넣지 않습니다. 원본 이름을 저장 경로로 사용하지 않으며 다운로드도 DB·허용된 저장 폴더를 기준으로 검증합니다.

SQLite의 외래키 선언과 실행 시 강제 검사는 별개입니다. 현재 `PRAGMA foreign_keys=ON`은 설정되어 있지 않고 API에서 제출자·담당자를 확인합니다. DB 날짜 열에는 시간대 지정이 없으므로 API 날짜를 임의로 UTC 확정값으로 취급하지 않습니다. 상세 컬럼 제약은 [backend/README.md](backend/README.md)를 참고합니다.

## 7. CORS·Vite 프록시

| 브라우저 요청 | 현재 경로 |
| --- | --- |
| `http://127.0.0.1:8000/api/auth/*` | 제품 서버로 직접 요청. CORS 적용 |
| `http://127.0.0.1:8000/api/documents...` | 제품 서버로 직접 요청. CORS 적용 |
| `/api/v1/...` | Vite에서 `http://127.0.0.1:8001`로 경로를 유지해 전달 |
| `/health` | 같은 Orchestrator로 전달. 제품 서버 실행 확인 URL이 아님 |

제품 서버의 `allow_origins`는 `http://localhost:5173`과 `http://localhost:5174`입니다. `allow_credentials=True`, 메서드·헤더는 모두 허용하지만 모든 출처를 허용하지는 않습니다. 이 설정은 Cookie 로그인을 구현했다는 뜻이 아니며 현재는 Authorization 헤더를 사용합니다.

`http://127.0.0.1:5173`, `localhost:5175`, HTTPS 주소는 다른 출처입니다. 브라우저 접속 주소와 서버 허용값을 맞춥니다. `/api/auth/*`와 `/api/documents`는 현재 프록시 대상이 아니므로 제품 API만 상대 경로로 변경하면 연결되지 않습니다.

`npm run build`로 만든 정적 파일만으로는 API 전달 서버가 제공되지 않습니다. 배포 시 API 공개 주소, 리버스 프록시, CORS를 별도로 정합니다. 현재는 운영용 공통 API URL 환경변수를 추가한 구성이 아닙니다.

## 8. Orchestrator와 현재 Pipeline UI

Pipeline의 실행 버튼이 `POST /api/v1/runs`를 호출합니다. 현재 전송하는 고정 시나리오는 다음과 같습니다.

```json
{
  "scenarioId": "f7f9e5c3-ffc3-4b3f-918b-21e1b956ce76",
  "requestText": "회원가입 기능을 구현해주세요."
}
```

성공 응답의 `run.runId`, `firstStep.agentRole`, `firstStep.status`, `dispatchStatus`를 표시합니다. 이후 같은 ID로 아래 API를 병렬 조회하고 수동 통합 새로고침에서도 같은 세 API를 호출합니다. 새로고침 시 Run을 다시 만들지 않고 자동 폴링도 하지 않습니다.

| API | 응답 처리 |
| --- | --- |
| `GET /api/v1/runs/{runId}` | Run 객체를 직접 반환. `runId`, `status`, `verdict`, `codeVersion`, `fixAttempt` |
| `GET /api/v1/runs/{runId}/steps` | `steps` 배열. `agentRole`, `status`, `attempt`, `a2aTaskState` |
| `GET /api/v1/runs/{runId}/events` | `events` 배열과 `runId`, `total`, `limit`, `offset`. 현재 UI에 페이지 이동은 없음 |

Run의 `FINISHED`는 종료이며 성공 보장이 아닙니다. `verdict=null`은 미판정입니다. `NOT_CONFIGURED`는 실행 전달 미설정이며 `SCHEDULED`도 실행 성공은 아닙니다. Step 미생성이나 적은 Event 수를 그 자체로 오류라고 해석하지 않습니다. QA·Security의 동시 `RUNNING`도 표시할 수 있습니다.

현재 Pipeline에는 Artifact·Issue·Usage/Cost·실제 수정 조작 UI가 연결되어 있지 않습니다. 실행 결과·오류·수정 요청 카드는 미연동을 명시합니다. 오피스의 가상 결과나 3D 연출과 혼동하지 않습니다.

`agentRuns.js`의 `createAgentRun()`은 `configuration: { architecture }`를 보낼 수 있지만 현재 Pipeline은 이 함수를 쓰지 않고 위 JSON만 전송합니다. 기본 아키텍처는 `MULTI_AGENT`입니다. Owned Host에서는 승인 구성 검사도 있으므로 현재 UI 입력만으로 실제 환경에서 실행이 성립한다고 보장할 수 없습니다.

`SINGLE_AGENT_RUNTIME_NOT_CONFIGURED`는 503, Host와 요청의 모드 불일치는 `RUNTIME_ARCHITECTURE_MISMATCH`로 422, 승인 구성 불일치는 `OWNED_AGENT_CONFIGURATION_INVALID`로 422입니다. 모델, 실행 제한, 소스, 도구, API Key, 인증 방식은 담당자가 승인한 설정과 대조합니다.

## 9. 싱글·멀티의 LLM 연동 상태와 근거

**두 모드 모두 LLM 호출 구현 경로는 있습니다. 실제 외부 LLM API·Docker를 사용한 전체 실행 성공이 검증되었다는 뜻은 아닙니다.** Orchestrator가 A2A로 작업을 전달하고 Agent의 공통 엔진이 Provider를 호출하는 구조입니다.

| 확인 대상 | 구현 위치·내용 |
| --- | --- |
| Provider 선택 | [src/agents/llm/configuration.py:31](src/agents/llm/configuration.py#L31). 현재 팩토리는 `openai` 지원 |
| 외부 API 호출 | [src/agents/llm/openai_responses.py:175](src/agents/llm/openai_responses.py#L175). `httpx.AsyncClient.post`로 `https://api.openai.com/v1/responses`에 전송 |
| 공통 LLM 실행 | [src/agents/llm/engine.py:109](src/agents/llm/engine.py#L109). 제한을 적용해 `provider.complete()` 대기 |
| 멀티 Planner | [src/agents/runtime/planner.py:269](src/agents/runtime/planner.py#L269). LLM 엔진 실행 |
| 멀티 Developer | [src/agents/runtime/developer.py:189](src/agents/runtime/developer.py#L189). LLM 엔진 실행 |
| 멀티 QA | [src/agents/runtime/qa.py:215](src/agents/runtime/qa.py#L215). LLM 엔진 실행 |
| 멀티 Security | [src/agents/runtime/security.py:252](src/agents/runtime/security.py#L252). LLM 엔진 실행 |
| 싱글 실행 | [src/agents/single/runtime.py:123](src/agents/single/runtime.py#L123). 같은 `SINGLE_AGENT`와 현재 단계로 엔진을 만들고 125행에서 실행 |
| Host 실행계 선택 | [src/agents/platform/host.py:344](src/agents/platform/host.py#L344). Provider 구성 후 352행에서 싱글/멀티 플랫폼 선택 |
| 모델·한도·키 참조 | [src/agents/platform/configuration.py:146](src/agents/platform/configuration.py#L146). `model`, `apiKeyEnv`, `llmLimits`, `runtimeBudgetMs` 설정 및 369행에서 지정 환경변수의 키 로딩 |

Provider에는 모델, 출력 토큰 한도, 시간제한, 구조화 응답·도구 처리가 구현되어 있습니다. 싱글은 네 직원 중 한 명을 선택하는 방식이 아니라 한 실행 주체가 단계를 바꿔 수행하는 방식입니다. 설정 예제가 있다는 사실은 실제 키·승인 모델·Host가 현재 준비되었다는 증거가 아닙니다.

검증 기록도 구분합니다. [Single 최종 감사 기록:221](docs/56-single-agent-final-audit-runbook.md#L221)은 완성·수정 흐름 검사에 결정적 LLM·Docker 어댑터를 사용했으며 실제 키·Docker daemon·외부 모델 호출·브라우저는 실행하지 않았다고 명시합니다. [최신 통신 검증:164](docs/58-strict-agent-communication-validation.md#L164)도 실제 모델 응답·코드 생성·MCP·빌드 등의 미수행을 명시합니다. 코드 연결과 실제 환경의 성공 검증은 별도로 판단합니다.

## 10. 오류 발생 시 확인 순서

| 증상·상태 | 확인할 내용 |
| --- | --- |
| `Address already in use` | 해당 포트의 기존 서버 확인. 제품·Orchestrator에서 8000 동시 사용 금지 |
| 브라우저 연결 실패 | 대상 서버·포트·URL 확인. 네트워크 오류와 HTTP 응답 구분 |
| CORS 오류 | 실제 origin이 5173/5174의 `localhost`인지 확인. Swagger 성공만으로 CORS는 검증되지 않음 |
| 401 | 같은 출처·탭의 토큰 확인. 만료·키 변경·사용자 삭제 시 재로그인 |
| 403 | 문서 제출자 또는 담당자인지 확인. 요청 사용자 ID로 권한 변경 불가 |
| 문서 404 | ID뿐 아니라 파일 누락·저장 경로 거부도 고려. 내부 경로는 화면에 표시하지 않음 |
| 409 | 등록된 이메일의 재가입 여부 확인 |
| 413 / 415 | 10MiB 초과 / PDF 서명 불일치 확인 |
| 422 | 자료형, 필수 입력, `view`, 등록된 `scenarioId`, 승인 구성 등의 `detail` 확인 |
| 제품 API 500 | `JWT_SECRET`, 저장 폴더, DB를 서버에서 확인. 내부 예외를 사용자에게 그대로 전달하지 않음 |
| Orchestrator 503 | 단일 실행계 미설정 등 `detail` 확인. API 존재와 실행계 구동을 구분 |
| 갱신해도 PENDING | 실행 전달 미설정·실행계 미구동 확인. 3D 재생으로 실제 Step이 진행되지는 않음 |

명시적 실패는 보통 `{"detail":"설명"}`, Pydantic 입력 검증은 `detail` 배열입니다. 제품 서버는 배열 항목을 `loc`, `msg`, `type`으로 제한합니다. 화면은 문자열·배열·JSON이 아닌 프록시 장애를 구분하고 `response.ok`로 성공을 판정합니다.

## 11. 검증·인계

프론트엔드 빌드·정적 검사는 `frontend/`에서 실행합니다.

```bash
npm run build
npm run lint
```

제품 백엔드 자동 테스트는 `backend/`에서 실행합니다. 루트의 `tests/`와 구분합니다.

```bash
.venv/bin/python -B -m unittest discover -s tests -v
```

이 테스트는 임시 DB·임시 저장 폴더를 사용하고 실제 `app.db`와 `uploads/`를 사용하지 않습니다. 2026-11-08의 앞선 README 갱신 작업에서는 50건이 통과했습니다. 이번 연동 문서 작성에서 다시 실행한 기록은 아닙니다. 빌드·ASGI 테스트와 실제 브라우저의 CORS·파일 저장은 따로 확인합니다.

실제 연동은 다음 순서로 확인합니다.

1. 제품 서버 `/`·`/docs`와 프론트엔드의 실제 접속 주소를 확인합니다.
2. 기존 인증 흐름에서 A·B·C 계정을 준비하고 `/api/auth/me`로 각 ID를 확인합니다.
3. A가 B에게 PDF를 제출하고 A의 `submitted`, B의 `assigned`에 표시되는지 확인합니다.
4. A·B로 원본을 받고 C의 직접 다운로드가 403으로 거절되는지 확인합니다.
5. 미로그인·만료, 빈 목록, 10MiB 경계, 형식 오류, 존재하지 않는 담당자를 확인합니다.
6. Orchestrator는 승인 구성·포트·A2A 인증·모델/도구 제한을 담당자와 합의한 뒤 별도 검증합니다. Run 접수, 실행, 최종 판정, 산출물을 차례로 확인합니다.

과거 수동 검증에서는 A의 제출·다운로드, B의 배정 목록·다운로드, C의 403, 문서 ID 2의 원본과 다운로드 파일의 SHA-256 일치가 확인되었습니다. 상세 기록은 [backend/README.md](backend/README.md)에 있습니다. 이번에 새로 실행한 결과가 아니며 최신 UI의 모든 동작이나 실제 LLM·Docker 검증을 보장하지 않습니다.

인계 시 제품·Orchestrator 주소, 사용 origin, 로그인 설정·만료, 담당자 ID 확인 방법, 승인된 Run 구성, 싱글/멀티 Host 선택, 실제 API와 시연 목업의 구분을 공유합니다. 실제 키·토큰 대신 필요한 환경변수명과 설정 파일 위치를 공유합니다.

상세한 시스템 설명은 [frontend/README.md](frontend/README.md), [backend/README.md](backend/README.md), [Owned Platform 인계](docs/45-owned-platform-handoff.md), [Single 최종 감사](docs/56-single-agent-final-audit-runbook.md)를 참고합니다.
