# 프론트엔드·백엔드 연동 안내

최근 갱신: 2026-11-10. 파란색 업무 요청창 통합, 프로젝트·결과물 보관함의 실제 Run 연결, 3D 역할 표시 번호 1~5와 승인 Configuration 공급 차단 상태를 반영했습니다. 아래 명령은 실행 안내입니다. 이번 문서 갱신에서 테스트·서버·LLM·Docker를 실행했다는 의미는 아닙니다.

## 1. 세 시스템의 역할과 주소

| 시스템 | 코드 | 현재 프론트엔드에서 사용하는 주소 | 담당 기능 |
| --- | --- | --- | --- |
| React 개발 서버 | `frontend/` | `http://localhost:5173` | 화면, 입력, 토큰 보관, API 요청 |
| 제품 FastAPI 백엔드 | `backend/` | `http://127.0.0.1:8000` | 회원가입·로그인·JWT·전자문서 |
| 기존 Orchestrator | `src/orchestrator/` | 기본 프록시 대상 `http://127.0.0.1:8001` | 기존 Run 저장·조회 및 구성된 실행계 연결 |
| 실제 Multi Agent Host | `src/agents/platform/` | 선택 대상 `http://127.0.0.1:8002` (아직 미실행) | Host 자체 Orchestrator와 Agent `8101~8104`, 승인 설정 검증 |

```text
브라우저의 React
  ├─ http://127.0.0.1:8000/api/auth/*     → 제품 백엔드 → 사용자 DB
  ├─ http://127.0.0.1:8000/api/documents  → 제품 백엔드 → 문서 DB·로컬 PDF
  └─ /api/v1/* → Vite 프록시 → Orchestrator → A2A Agent 실행기 → LLM·도구
```

제품 백엔드와 Orchestrator는 별도 서버·DB 계약입니다. 제품 서버를 실행해도 Agent 서버는 시작되지 않습니다. 제품 JWT를 Orchestrator·A2A 공통 인증으로 전달하는 기능도 현재 UI에는 연결되어 있지 않습니다.

8001과 Host 8002는 별도 앱·설정된 저장소를 사용하며 자동으로 데이터를 공유하지 않습니다. 실제 Host에서 생성한 Run·Step·Trace·Issue·Artifact/Report는 같은 Host 8002에서 조회해야 합니다. 두 서버 통합 없이 프록시 대상으로 선택할 수 있으며, 대상 전환 자체는 Run/DB를 복사하지 않습니다.

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
| [AtelierDashboard.jsx](frontend/src/pages/AtelierDashboard.jsx) | 실제 Run 자동 선택·공유 Hook·3D·프로젝트·보관함 연결 |
| [useAgentRunMonitor.js](frontend/src/hooks/useAgentRunMonitor.js) | Run·Step·Event·실행 방식 조회, 취소·부분 오류·3초 폴링 |
| [useOfficeRunSubmission.js](frontend/src/hooks/useOfficeRunSubmission.js) | 기존 파란색 요청창의 승인 기반 제출·오류 상태. 별도 생성 폼은 미렌더링 |
| [agentRunSubmission.js](frontend/src/api/agentRunSubmission.js) | 공개 설정 검사·등록 시나리오 확인·중복 제출 방지 |
| [agentRunEventFeed.js](frontend/src/hooks/agentRunEventFeed.js) | 신규 Run 초기 및 증분 Trace 수집, 기존 Run 과거 재생 방지 |
| [agentRunProject.js](frontend/src/components/atelier/agentRunProject.js) | 현재 요청의 이름·Run ID·Run/Step 상태를 프로젝트에 연결 |
| [agentArtifactCatalog.js](frontend/src/api/agentArtifactCatalog.js) | 기존 Artifact 응답에서 보관함용 공개 등록 정보·요약 추출 |
| [AtelierViews.jsx](frontend/src/components/atelier/AtelierViews.jsx) | 실제 프로젝트·결과물 카드, 빈 상태·오류·메타데이터 펼침 |
| [agentRunDisplayState.js](frontend/src/components/atelier/agentRunDisplayState.js) | 네 역할 표시 상태와 확인된 전달 Event 변환 |
| [AgentOffice3D.jsx](frontend/src/components/atelier/AgentOffice3D.jsx) | 실제/목업 표시 모드와 3D 런타임 수명 주기 |
| [PipelinePage.jsx](frontend/src/pages/PipelinePage.jsx) | 보존된 개발용 화면. 현재 사용자 메뉴에서 진입하지 않음 |
| [agentRuns.js](frontend/src/api/agentRuns.js) | 생성·Run·Step·Event·실행 방식 조회. GET 함수는 Dashboard Hook에서 사용 |
| [vite.config.js](frontend/vite.config.js) | `/api/v1`, `/health`를 Orchestrator로 전달 |

현재 기본 설정은 `App.jsx`의 `REQUIRE_PLATFORM_LOGIN = false`입니다. 첫 화면은 파란색 업무 요청창과 5인 3D 오피스이며 기존 헤더·사이드바를 유지합니다. 헤더와 계정 메뉴의 `새 업무`는 같은 요청창으로 이동합니다. 계정 메뉴의 전자문서 관리는 유지하며, 별도 Run 생성·UUID 조회·개발용 상세 패널과 Pipeline 진입은 사용자 화면에서 제거했습니다.

기존 로그인 흐름으로 계정 연동을 검증하려면 해당 설정을 `true`로 사용하는 절차를 팀과 정합니다. 이 경우 회원가입 → 로그인 → 오피스로 이어집니다. 이 문서 작업에서는 설정을 변경하지 않았습니다. 공개 오피스 진입은 서버의 문서 인증·권한 검사를 해제하지 않습니다.

업무 시작은 승인 설정을 확인하는 실제 생성 경로이며 실패를 목업 성공으로 표시하지 않습니다. 프로젝트·보관함은 현재 화면에서 접수한 Run만 표시하고 없으면 빈 상태를 보여줍니다. 105초 시연·역할 소개의 예시는 실제 업무 결과와 별개입니다. 3D 역할 버튼은 1 오케스트레이터, 2 계획, 3 개발, 4 QA, 5 보안이며 번호만 변경했고 내부 인덱스·캐릭터·모션은 유지했습니다.

우리 직원과 3D에는 리더인 오케스트레이터와 계획·개발·QA·보안의 다섯 캐릭터를 표시합니다. 실제 `AgentRole`과 Step 역할은 `PLANNER`, `DEVELOPER`, `QA`, `SECURITY` 네 가지입니다. 리더는 관리 서버를 시각화한 것이며 별도 LLM Agent·Step·제품 사용자를 추가하지 않습니다. 직원 카드는 현재 모델의 정면 렌더링 `office-*.png`를 사용하며, 모델 외형 변경 시 카드 이미지도 갱신해야 합니다.

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
| `/api/v1/...` | Vite의 `ORCHESTRATOR_PROXY_TARGET`으로 전달. 기본 `http://127.0.0.1:8001` |
| `/health` | 같은 Orchestrator로 전달. 제품 서버 실행 확인 URL이 아님 |

제품 서버의 `allow_origins`는 `http://localhost:5173`과 `http://localhost:5174`입니다. `allow_credentials=True`, 메서드·헤더는 모두 허용하지만 모든 출처를 허용하지는 않습니다. 이 설정은 Cookie 로그인을 구현했다는 뜻이 아니며 현재는 Authorization 헤더를 사용합니다.

`http://127.0.0.1:5173`, `localhost:5175`, HTTPS 주소는 다른 출처입니다. 브라우저 접속 주소와 서버 허용값을 맞춥니다. `/api/auth/*`와 `/api/documents`는 현재 프록시 대상이 아니므로 제품 API만 상대 경로로 변경하면 연결되지 않습니다.

`npm run build`로 만든 정적 파일만으로는 API 전달 서버가 제공되지 않습니다. 배포 시 API 공개 주소, 리버스 프록시, CORS를 별도로 정합니다. 현재는 운영용 공통 API URL 환경변수를 추가한 구성이 아닙니다.

실제 Host 준비 시 프론트엔드 디렉터리에서 다음과 같이 Vite 대상을 선택합니다. 환경변수를 바꾼 뒤에는 Vite만 재시작해야 하며, 이 명령이 Host를 시작하지는 않습니다.

```bash
ORCHESTRATOR_PROXY_TARGET=http://127.0.0.1:8002 npm run dev
```

변수를 지정하지 않고 `npm run dev`를 실행하면 기본 `http://127.0.0.1:8001`로 돌아갑니다. `/api/v1`과 `/health`는 항상 같은 대상으로 연결됩니다. 이 변수는 Vite 서버 프로세스에서만 읽으며 브라우저용 `VITE_*` 설정이 아닙니다. 8001·8002 서버는 종료하거나 재시작하지 않습니다.

## 8. Orchestrator·실제 Run 조회와 3D 연결

### API 함수와 요청·응답

실제 계약은 [runs.py 라우트](src/orchestrator/api/routes/runs.py), [runs.py 스키마](src/orchestrator/api/schemas/runs.py), [상태 정의](src/orchestrator/domain/states.py), [TraceEvent](src/orchestrator/domain/trace.py)를 기준으로 합니다. 모든 경로는 Vite의 `/api/v1` 프록시를 사용합니다.

| 함수 | HTTP 요청 | 응답과 주요 필드 |
| --- | --- | --- |
| `createAgentRun(scenarioId, requestText, architecture = 'MULTI_AGENT', { signal, configuration, approved = false } = {})` | `POST /api/v1/runs` | `run`, `firstStep`, `dispatchStatus` |
| `getAgentRun(runId, { signal } = {})` | `GET /api/v1/runs/{runId}` | Run 객체 자체. `runId`, `scenarioId`, `status`, `verdict`, `codeVersion`, `fixAttempt` 등 |
| `getAgentRunSteps(runId, { signal } = {})` | `GET /api/v1/runs/{runId}/steps` | `runId`, `steps`. 각 항목의 `workflowStepId`, `agentRole`, `status`, `attempt`, `codeVersion`, `a2aTaskState` 등 |
| `getAgentRunEvents(runId, { limit = 100, offset = 0, signal } = {})` | `GET /api/v1/runs/{runId}/events?limit=100&offset=0` | `runId`, `events`, `total`, `limit`, `offset`. `limit` 1~500, `offset` 0 이상 |
| `getAgentRunArtifacts(runId, { signal } = {})` | `GET /api/v1/runs/{runId}/artifacts` | 팝업용 `artifacts`, 보관함용 공개 `catalog`를 함께 반환 |
| `getAgentRunIssues(runId, { signal } = {})` | `GET /api/v1/runs/{runId}/issues` | 팝업용 안전한 Issue 요약 |
| `getAgentRunArchitecture(runId, { signal } = {})` | `GET /api/v1/runs/{runId}/configuration` | 서버의 `RUN_CONFIGURATION` Artifact에서 `configuration.architecture`만 추출해 `{ runId, architecture }` 반환 |

생성 함수는 승인된 공개 Configuration을 전달한 `MULTI_AGENT` 요청만 허용합니다. 옵션이 없는 기존 두 인자 호출은 POST 전에 차단됩니다. `getAgentRunScenarios()`로 `GET /api/v1/scenarios`의 등록 여부를 확인한 뒤 본문에 `scenarioId`, `requestText`, 공개 `configuration` 전체를 보냅니다. `{ architecture }`만 보내는 예제는 실제 Host 실행 계약을 충족하지 않습니다.

실제 승인값이 확정되지 않았으므로 이 문서에는 실행 가능한 모델·예산·이미지·Source 예시를 만들지 않습니다. `approved`는 클라이언트 제출 조건이며 POST 본문이나 서버 승인 증명에 포함되지 않습니다.

기존 Run 조회 응답의 주요 필드 예시는 다음과 같습니다. 자리표시자와 값은 계약 설명용이며 실제 실행 기록이 아닙니다. 실제 응답에는 `workspaceId`, `resumeState`, `terminationReason`, 생성·갱신 시각 등도 포함됩니다.

```json
{
  "runId": "<runId>",
  "scenarioId": "<scenarioId>",
  "status": "RECEIVED",
  "verdict": null,
  "codeVersion": null,
  "fixAttempt": 0
}
```

Step·Event 조회는 각각 `{"runId":"<runId>","steps":[]}`, `{"runId":"<runId>","events":[],"total":0,"limit":20,"offset":0}` 형태입니다. Event 항목은 `eventId`, `workflowStepId`, `eventType`, `occurredAt`, `actor`, `attempt` 및 값이 있는 `workflowState`, `a2aTaskState`, `codeVersion` 등의 실제 스키마 필드를 사용합니다. 없는 메시지·전달 대상 필드를 만들어 읽지 않습니다.

공통 API 계층은 오류의 `status`, `code`, `detail`을 보존하고 `NETWORK_ERROR`·응답 형식 오류·`AbortError`를 구분합니다. 취소 신호는 `signal`로 전달합니다. 주요 HTTP 오류는 404 Run 없음, 422 UUID·입력·승인 구성 오류, 503 실행계 미설정입니다. `dispatchStatus=NOT_CONFIGURED`는 미설정이고 `SCHEDULED`도 성공 판정이 아닙니다.

### Dashboard의 공유 데이터와 사용자 화면

```text
파란색 요청창: 프로젝트 이름 + 요구사항 + 업무 시작
  → useOfficeRunSubmission → agentRunSubmission: 승인 설정·등록 시나리오 확인
  → POST /api/v1/runs → run.runId 자동 선택 + traceFromStart: true
  → useAgentRunMonitor: Run·Step·Event·Configuration·Issue·Artifact 조회
      → AgentOffice3D: 모션·문서 전달·업무 팝업·세리머니
      → submittedRunProject: 프로젝트 이름과 Run·Step 상태 연결
      → artifactCatalog: 보관함의 공개 산출물 등록 정보·요약
```

`AtelierDashboard`가 기존 모니터링 Hook을 한 번 호출합니다. 사용자는 UUID를 입력하거나 별도 조회 버튼을 누르지 않습니다. `AgentRunCreate`와 `AgentRunMonitor` 파일은 보존되어 있지만 Dashboard에서 렌더링하지 않습니다. 모니터 내부의 GET·취소·오류 처리·페이지/커서 관리는 유지됩니다.

Run의 `FINISHED`는 종료이며 성공 여부는 `verdict`로 판단합니다. Step 실패와 QA Report의 FAIL은 구분합니다. 생성 실패 시 입력과 기존 화면을 보존하고, 최종 상태가 확인되면 이전 실행 예약 안내를 숨깁니다. Run ID와 프로젝트명은 현재 Dashboard 메모리에만 있으므로 새로고침 후 자동 복원되지 않습니다.

### 네 작업 상태와 리더의 동작

| 데이터 | 캐릭터 표시 |
| --- | --- |
| Step `PENDING` 또는 미생성 | 기본 대기 |
| Step `RUNNING` | 해당 역할의 작업 모션 반복 |
| Step `SUCCEEDED` | 단계 종료 후 안정된 자세, 전체 성공 아님 |
| Step `FAILED` | 작업 중단·오류 표정 |
| Step `WAITING_INPUT` | 작업 중지·입력 대기 |
| Step `CANCELED` | 작업 중단·취소 |
| 조회 실패·미정의·판별 불가 | 상태 확인 불가, Agent 실패로 단정하지 않음 |
| 리더의 Run `RECEIVED` | 요청 접수 |
| 리더의 Run `WAITING_INPUT`·`HUMAN_REVIEW` | 입력 대기 |
| 리더의 Run `FINISHED`·`ABORTED` | 실행 관리 종료, 성공 여부는 verdict로 별도 확인 |
| 그 밖의 정상 활성 Run | 실행 관리 중 |

역할별 모션 시간을 독립적으로 관리하므로 QA·Security가 동시에 `RUNNING`일 수 있습니다. 같은 Run·Step·상태의 재조회는 모션을 재시작하지 않습니다. 복수 Step은 코드 버전·재시도 등의 근거로 선택하며 충돌하면 임의로 정하지 않습니다. Event로 Step 상태나 실제 진행률을 추측하지 않습니다. 같은 Run의 갱신 중에는 마지막 상태를 유지하고 조회 실패 시 마지막 자세를 멈춰 오류를 표시합니다. 새 Run에는 이전 Run 상태를 적용하지 않습니다.

### 확인된 Event의 문서 전달

실제 전달은 `officeTraceEvents.js`와 `agentRunEventFeed.js`를 사용합니다. `A2A_MESSAGE_SENT`가 같은 Run의 유일한 Step과 연결되고 `actor=Orchestrator` 및 코드 버전 조건을 만족할 때만 리더 → 해당 역할의 문서 전달을 만듭니다. 대상이 불명확하면 임의 전달하지 않으며 Task 상태 변경만으로 반대 방향 전달을 생성하지 않습니다. QA·Security의 조건이 맞는 병렬 전달은 유지합니다.

화면의 20건 Event 페이지와 별개인 Trace 커서가 최대 500건씩 조회합니다. 서버의 `sequence` 기준 응답 배열 순서를 그대로 보존하고 `eventId`로 중복을 제거합니다. 클라이언트가 발생 시각이나 역할로 재정렬하지 않습니다.

- 방금 생성한 Run은 `traceFromStart: true`로 최초 `offset=0`부터 수집해 생성 응답 이전에 저장된 이벤트도 포함합니다.
- 기존 Run 수동 조회의 과거 이벤트 생략 정책은 내부 코드·테스트에 유지합니다. 현재 사용자 화면에는 UUID 입력·수동 선택 UI가 없습니다.
- 페이지 묶음 전체가 성공한 뒤에만 커서·중복 처리 상태를 반영합니다. 실패·취소 시 기존 상태를 보존하여 재시도하고, Run 변경 시 이전 큐를 정리합니다.
- 내부 Event 페이지 처리와 Trace 커서는 독립적이며 현재 사용자 화면에는 페이지 조작 UI가 없습니다. 3초 폴링과 연출 큐 때문에 실제 이벤트 시각과 애니메이션 시작 시각은 다를 수 있습니다.

문서 아이콘은 확인된 호출을 시각화하며 실제 파일 바이트 전송의 증거는 아닙니다. 기존 105초 목업의 전달 시간표는 실제 모드에 사용하지 않습니다.

백엔드는 `a2a_tasks.py`에서 실제 호출·응답 Trace를 기록하고 같은 Run의 저장소에서 `/events`로 조회합니다. SQLite 조회는 `ORDER BY sequence`를 사용하며 프론트엔드는 응답 배열 순서를 유지합니다. 이 연결의 Mock 검증과 실제 LLM E2E 검증은 구분합니다.

### 3초 자동 갱신

실제 오피스와 프로젝트·결과물 보관함에서 선택된 Run을 자동 조회합니다. 완료된 조회 묶음 후 3초 뒤 다음 조회를 예약하며 진행 중인 같은 요청은 공유합니다.

- 홈 실제 모드에서는 기존 `collectEvents`로 Trace를 수집하고 결과를 함께 조회합니다.
- 프로젝트·보관함에서는 `includeResults`로 Artifact·Issue 조회를 활성화합니다. 3D Trace 수집과 결과 목록 조회를 분리하여 같은 Hook·API를 재사용합니다.
- `FINISHED`·`ABORTED` 또는 `SINGLE_AGENT` 확인 시 반복 조회를 중지합니다. `FINISHED` 자체가 성공은 아닙니다.
- 핵심 조회 실패 시 마지막 데이터를 유지하고 재시도 간격을 6·12·24·최대 30초로 늘립니다. Artifact·Issue 오류는 독립적으로 처리합니다.
- 프로젝트의 Run 조회 오류와 보관함의 Artifact 조회 오류에는 해당 화면의 `다시 불러오기`를 제공합니다. 이 동작은 GET 재조회이며 새 Run을 만들지 않습니다.
- 기존 Run 조회·일시정지·페이지 조작 버튼은 사용자 화면에서 제거했습니다. 내부 취소·상태 정리 로직은 유지하며, 화면별 활성 조건·Run 변경·언마운트에 맞춰 요청과 타이머를 정리합니다.

### 실제 성공과 5인 세리머니

실제 모드에서 조회 오류 없이 Run·Step·Event·Configuration 조회가 완료되고 `architecture=MULTI_AGENT`, `status=FINISHED`, `verdict=SUCCESS`여야 합니다. 3D 준비와 모션 허용도 필요합니다. `FAIL`, `UNVERIFIED`, `HUMAN_REVIEW`, `null` 또는 Step 하나의 `SUCCEEDED`로 재생하지 않습니다.

관측 중 미종료 Run이 최종 성공으로 바뀌면 자동 후보가 됩니다. 처음 조회한 과거 성공 Run은 사용자의 명시적인 재생을 기다립니다. 리더와 네 캐릭터가 참여하며 서로 다른 성공 Run마다 `team → cheer → hug` 중 하나만 배정합니다. 네 번째부터 다시 `team`으로 순환합니다.

| 기록 | `localStorage` 키 |
| --- | --- |
| 105초 목업 시연의 순서 | `atelier.office3d.ceremony-order.v1` |
| 실제 성공 Run UUID·배정 포즈·다음 순서 | `ai-agent-company.office3d.success-ceremonies.v1` |

두 기록과 초기화는 독립적입니다. 재생 직전에 실제 이력을 저장하여 같은 Run의 폴링·StrictMode·재진입·새로고침으로 중복 소비하지 않습니다. 저장 실패 시 재생하지 않고 중간에 화면을 나가도 같은 Run을 자동 재생하지 않습니다. 한 탭 기준이며 여러 탭 동시 실행의 잠금은 없습니다.

### 프로젝트·결과물 보관함의 실제 데이터

프로젝트는 `useOfficeRunSubmission`에 저장된 이름·요구사항과 생성 응답의 `run.runId`를 `submittedRunProject()`로 연결합니다. 기존 카드·검색 디자인을 재사용하며 Run 상태와 역할별 Step 상태를 표시합니다. QA·Security 병렬 상태도 각각 유지합니다. 실제 근거가 없는 진행률 숫자·진행 막대는 표시하지 않습니다. 실제 Run이 없으면 목업 카드 대신 빈 상태를 보여줍니다.

현재 조회 가능한 범위는 **이 화면에서 생성한 Run**입니다. 전체 Run 목록과 프로젝트명 영구 저장 API가 없으므로 이전 세션의 프로젝트 이력·새로고침 후 복원은 지원하지 않습니다. 프로젝트 카드를 누르면 같은 Run의 실제 3D 오피스로 돌아가며 새 Run을 생성하지 않습니다.

| Host API | 제공 범위와 현재 UI 사용 |
| --- | --- |
| `GET /api/v1/runs/{runId}/artifacts` | `runId`, `artifacts`. 산출물 JSON과 실행 설정 Artifact를 반환. 기존 조회 한 번으로 팝업 요약과 보관함용 공개 목록을 생성 |
| `GET /api/v1/runs/{runId}/artifacts/{artifactId}` | 해당 Artifact JSON. 개별 조회 계약은 있으나 목록 응답에 필요한 정보가 있어 현재 UI는 별도 요청하지 않음 |
| 소스 파일 원문·바이너리 다운로드 | 현재 HTTP API 미제공. UI에 파일 원문 보기·다운로드 버튼을 만들지 않음 |

`agentArtifactCatalog.js`는 등록 URI의 마지막 경로 부분을 등록명으로 사용하고, 실제 응답의 종류·생성 주체·등록 시각·산출물/코드 버전·보고서 요약을 표시합니다. 이름이 없으면 파일 이름 미제공으로 안내합니다. 목록에 있다는 사실은 등록됨을 뜻하며 검증 성공이나 다운로드 가능을 뜻하지 않습니다.

`SOURCE`는 소스 Snapshot 등록 정보이며 내부 소스 파일 목록·원문이 아닙니다. `CHANGE_REPORT.fileChanges`의 경로와 작업 종류는 변경 보고서 내용으로 표시하며 현재 파일의 존재를 보장하지 않습니다. QA/보안 보고서도 확인된 결과만 요약합니다. `artifact://` URI를 HTTP 링크로 바꾸거나 임의의 소스 파일·다운로드를 생성하지 않습니다.

기존 `getAgentRunArtifacts()`의 팝업용 `artifacts`는 유지하고 공개 `catalog`를 추가해 같은 응답을 재사용합니다. 원본 Configuration·manifest·Host 내부 경로·민감 로그는 목록 상태에 복사하지 않습니다. 조회 실패는 빈 결과와 구분하고 같은 Run의 마지막 정상 목록이 있으면 유지합니다. 재조회는 GET만 수행하며 Run이 바뀌면 이전 목록을 섞지 않습니다.

보관함의 등록 정보 보기는 안전한 메타데이터·요약을 펼치는 기능입니다. 제품 백엔드의 PDF 다운로드 API와 Agent의 코드 산출물은 별개의 계약입니다. 사용자 화면에 예시 파일을 실제 결과물처럼 표시하지 않습니다.


### 보존된 Pipeline과 승인 설정

`PipelinePage.jsx`는 보존된 개발용 코드이며 현재 사용자 메뉴에서 열지 않습니다. 이 코드의 고정 scenario·request 직접 POST는 승인 Configuration을 공급하지 않으므로 실제 Host 실행 진입점으로 사용하지 않습니다. 현재 사용자 진입점은 파란색 요청창 → `useOfficeRunSubmission` → 기존 `agentRunSubmission`입니다.

Owned Host는 [host.py](src/agents/platform/host.py)의 `HostConfiguration.approved_configuration`과 `validate_configuration()`을 기준으로 요청을 검증합니다. 승인값은 서버 측 runtime·tools·source 및 관련 검증 설정으로 구성됩니다. 모델·실행 한도·환경·시작 commit/snapshot·scanner 등의 승인값과 요청을 대조하므로 최소 `{ architecture }`만으로 실제 Host 실행이 보장되지 않습니다.

프론트엔드의 승인 입력 경계는 구현되어 있습니다. `AtelierDashboard`는 `approvedRunSubmission = { approved: true, scenarioId, configuration }`을 받아 `useOfficeRunSubmission`에 전달합니다. 다만 `App.jsx`는 이를 공급하지 않아 현재 기본 `null`이며 실제 POST를 차단합니다. `agentRunSubmission.js`의 공개 필드 검사와 민감정보 패턴 차단은 서버 승인을 대신하지 않습니다.

| 공급·검증 경로 | 현재 지원 범위 |
| --- | --- |
| `approved_run_configuration(platform)` | 서버 내부에서 공개 템플릿 반환. HTTP API 아님 |
| `app.state.approved_host_configuration_json` | Host 내부 보관 정보. 브라우저 조회 계약 아님 |
| `GET /api/v1/runs/{runId}/configuration` | 기존 Run의 고정 설정. 현재 Host 승인 템플릿의 대체 수단 아님 |
| Host `validate_configuration()` + 제출 검증기 | 서버 승인 모델·한도·환경·Source·검증 참조 대조와 민감정보 검사 |
| 일반 Orchestrator의 기본 `submission_validator=None` | Host와 같은 승인 검증을 보장하지 않음 |

공개 Configuration은 모델·제한·이미지 digest·dependency lock hash·네트워크 정책·Source commit/snapshot·검증 참조입니다. OpenAI API 키·A2A 토큰·비밀번호·Host 내부 경로·원본 runtime/tools/source 파일은 서버 전용입니다. 기존 제품 JWT 인증도 Host 승인 검증을 대체하지 않습니다.

**안전한 승인 설정 공급 HTTP 계약은 아직 없습니다.** 팀장과 공개 설정 조회 또는 서버 측 설정 참조 방식, 접근 권한, 설정 변경 시 재검증을 확정해야 합니다. 모델·예산·도구 이미지·Source Snapshot도 승인받아야 합니다. 새 endpoint나 승인값을 임의로 만들지 않았습니다. 프론트엔드의 요청창·결과 표시 연결이 구현되어 있어도 승인 설정 공급 차단은 그대로 유지됩니다.

미설정 싱글 실행은 `503 SINGLE_AGENT_RUNTIME_NOT_CONFIGURED`, 실행 방식 불일치는 `422 RUNTIME_ARCHITECTURE_MISMATCH`, 승인 구성 불일치는 `422 OWNED_AGENT_CONFIGURATION_INVALID`입니다. 제품 JWT와 Agent Run 접근 권한의 공통 연결은 별도 작업이며 `리더` UI가 제품 관리 권한을 의미하지 않습니다.

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

검증 기록도 구분합니다. [Single 최종 감사 기록:221](docs/56-single-agent-final-audit-runbook.md#L221)은 완성·수정 흐름 검사에 결정적 LLM·Docker 어댑터를 사용했으며 실제 키·Docker daemon·외부 모델 호출·브라우저는 실행하지 않았다고 명시합니다. [실제 OpenAI 통신 진단](docs/59-live-openai-communication.md)은 별도 진단 실행기의 실제 API·A2A 송수신 결과를 기록하며, 제품 실행기·MCP·Docker·코드 생성·빌드·제품 테스트 실행은 범위에서 제외합니다. 진단에서 사용한 모델값은 현재 Host의 승인값을 대신하지 않습니다. 코드 연결과 실제 환경의 성공 검증은 별도로 판단합니다.

제품용 실제 실행 경로는 `agents.platform.run_host → create_host → 역할별 실행기 → OpenAI Responses API`입니다. `diagnostics.communication`의 실제 통신 진단 경로는 제품 Host·Run 저장/조회와 별개입니다. 통신 진단의 목업 제거는 프론트엔드 105초 시연 제거를 의미하지 않습니다.

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

프론트엔드 자동 테스트는 같은 디렉터리에서 다음과 같이 실행합니다. 실제 API 없이 상태 변환·폴링·전달·세리머니·좌석 경로·근접샷·머리·관절을 검증합니다.

```bash
node --test tests/*.test.mjs
```

기존 문서의 2026-11-09 기록에서는 118개 테스트와 build·lint·공백 검사가 통과했습니다. 앞선 3D·직원 카드 작업의 격리된 브라우저 Mock 검증 48개는 과거 기록입니다. 당시 보안 좌석 화분 제거는 코드로 확인했으며 브라우저 실행은 승인되지 않아 새 브라우저 검증을 수행하지 않았습니다. 실제 서버·LLM 호출도 수행하지 않았습니다. 기존 105초 목업과 `team → cheer → hug` 기록은 유지합니다.

최근 신규 Run 연결 작업에서는 `frontend/tests/newAgentRunFlow.test.mjs`의 Mock 테스트 **5개 PASS**가 보고되었습니다. 범위는 생성 응답의 Run ID 자동 연결, 최초 이벤트의 페이지 수집·순서·중복 제거, 기존 Run 과거 재생 방지, 중복 제출·실패 처리입니다. 기존 브라우저 64항목·단위 테스트 103개 PASS는 앞선 작업의 기록이며 현재 전체 테스트 수로 합산하지 않습니다.

승인 Configuration 공급 경로 점검에서는 안전한 HTTP 계약이 없어 코드 변경이나 새 Mock 실행을 하지 않았습니다. 이번 문서 갱신도 문서 정적 확인만 수행하며 기존 테스트·Build/Lint를 재실행하지 않습니다. 실제 Run POST·OpenAI 호출·Host/Docker 실행은 수행하지 않았습니다.

최근 UI·프로젝트/보관함 작업의 검증 기록은 다음과 같습니다. 전체 회귀 결과나 실제 Host E2E 성공으로 해석하지 않습니다.

| 작업 | 제한된 검증 결과 |
| --- | --- |
| 파란색 요청창 통합 | `officeRequestUi.test.mjs` 1개 PASS(4개 검사 묶음), 관련 기존 `newAgentRunFlow.test.mjs` 5개 PASS. 단일 입력창·승인 차단·자동 관측·중복/실패 처리 확인 |
| 프로젝트·보관함 | `projectOutputs.test.mjs` 3개 PASS. 격리 UI의 4개 검사 묶음과 공개 Artifact 변환·Run/Step 매핑 확인 |
| 변경 파일 정적 확인 | 해당 작업의 ESLint·공백 검사 통과. 전체 Build/Lint는 재실행하지 않음 |
| 3D 역할 번호 1~5 | 표시식만 변경하고 공백 검사 확인. 내부 인덱스·동작 변경 없음 |

격리 UI 검사는 실제 Dashboard·요청·모니터 Hook과 Mock HTTP를 사용하고 3D 렌더러를 대체했습니다. 따라서 캐릭터 동작·레이아웃 전체를 다시 검증한 것은 아닙니다. 기존 64항목 브라우저·103개 단위 테스트·105초 시연은 재실행하지 않았고, 과거 118개/48개 기록과도 합산하지 않습니다. 이번 문서 갱신에서는 문서 형식·링크만 정적으로 확인합니다. 실제 Run POST·OpenAI·Host·Docker 실행은 하지 않았습니다.


제품 백엔드 자동 테스트는 `backend/`에서 실행합니다. 루트의 `tests/`와 구분합니다.

```bash
.venv/bin/python -B -m unittest discover -s tests -v
```

이 테스트는 임시 DB·임시 저장 폴더를 사용하고 실제 `app.db`와 `uploads/`를 사용하지 않습니다. 2026-11-08의 앞선 README 갱신 작업에서는 50건이 통과했습니다. 이번 갱신에서 백엔드 테스트를 다시 실행한 기록은 아닙니다. 빌드·ASGI 테스트와 실제 브라우저의 CORS·파일 저장은 따로 확인합니다.

실제 연동은 다음 순서로 확인합니다.

1. 제품 서버 `/`·`/docs`와 프론트엔드의 실제 접속 주소를 확인합니다.
2. 기존 인증 흐름에서 A·B·C 계정을 준비하고 `/api/auth/me`로 각 ID를 확인합니다.
3. A가 B에게 PDF를 제출하고 A의 `submitted`, B의 `assigned`에 표시되는지 확인합니다.
4. A·B로 원본을 받고 C의 직접 다운로드가 403으로 거절되는지 확인합니다.
5. 미로그인·만료, 빈 목록, 10MiB 경계, 형식 오류, 존재하지 않는 담당자를 확인합니다.
6. Orchestrator는 승인 구성·포트·실제 DB 경로·A2A 인증·모델/도구 제한을 담당자와 합의한 뒤 별도 검증합니다. 승인된 공급 계약과 Host 준비 후 파란색 입력창에서 실제 Run 1건을 생성하여 자동 관측·병렬 검토·수정·재검증·최종 판정과 프로젝트/보관함의 실제 응답을 확인합니다. 현재 사용자 화면은 기존 Run UUID 입력을 제공하지 않습니다. Mock으로 확인한 초기·증분 Trace 순서와 실제 전달 대상을 실제 Run 1건에서 대조합니다. 이벤트 시각과 애니메이션 시작 시각이 동일하다고 가정하지 않습니다.

과거 수동 검증에서는 A의 제출·다운로드, B의 배정 목록·다운로드, C의 403, 문서 ID 2의 원본과 다운로드 파일의 SHA-256 일치가 확인되었습니다. 상세 기록은 [backend/README.md](backend/README.md)에 있습니다. 이번에 새로 실행한 결과가 아니며 최신 UI의 모든 동작이나 실제 LLM·Docker 검증을 보장하지 않습니다.

인계 시 제품·Orchestrator 주소, 사용 origin, 로그인 설정·만료, 담당자 ID 확인 방법, 승인된 Run 구성, 싱글/멀티 Host 선택, 실제 API와 시연 목업의 구분을 공유합니다. 실제 키·토큰 대신 필요한 환경변수명과 설정 파일 위치를 공유합니다.

상세한 시스템 설명은 [frontend/README.md](frontend/README.md), [backend/README.md](backend/README.md), [Owned Platform 인계](docs/45-owned-platform-handoff.md), [Single 최종 감사](docs/56-single-agent-final-audit-runbook.md)를 참고합니다.
