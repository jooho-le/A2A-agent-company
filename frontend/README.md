# A2A Agent Company 프론트엔드

## 1. 프로젝트 소개와 담당 역할

A2A Agent Company는 여러 인공지능 에이전트(Agent)의 협업 흐름을 보여주는 프로젝트입니다. 이 디렉터리는 역할 3인 웹 애플리케이션 및 시연 화면(Web Application / Demo)을 담당합니다.

프론트엔드는 회원가입·로그인, 시나리오 선택, 전자문서 제출·조회·다운로드, 에이전트 실행 현황을 표시합니다. 인증과 전자문서 처리는 FastAPI 백엔드에 요청하고, 에이전트 실행 요청과 상태 조회는 오케스트레이터(Orchestrator)에 요청합니다. 프론트엔드에서 직접 LLM을 호출하거나 Docker를 실행하지 않습니다.

화면에 API 연결이 구현되어 있다는 사실과 실제 에이전트의 전체 실행이 검증되었다는 사실은 구분해야 합니다. 실제 LLM·Docker를 포함한 전체 흐름(End-to-End) 실행은 아직 검증 전입니다.

## 2. 사용 기술

- React 19와 React DOM으로 화면을 구성합니다.
- Vite 8과 `@vitejs/plugin-react`로 개발 서버와 빌드를 구성합니다.
- 자바스크립트(JavaScript), JSX와 CSS를 사용합니다.
- React의 `useState`, `useEffect`, `useRef`로 화면 상태와 요청을 관리합니다.
- 브라우저 기본 `fetch`, `FormData`, `Blob`, `AbortController`를 사용합니다.
- `sessionStorage`에 로그인 토큰을 저장합니다.
- ESLint로 코드의 정적 검사를 수행합니다.

React Router나 별도의 UI 라이브러리는 사용하지 않습니다. `App.jsx`의 상태를 기준으로 페이지 컴포넌트를 전환합니다.

## 3. 폴더 구조

```text
frontend/
├── README.md
├── package.json                 # 의존성과 실행 명령
├── package-lock.json            # 의존성 버전 고정 정보
├── vite.config.js               # React 설정과 개발용 프록시
├── eslint.config.js             # 정적 검사 설정
├── index.html                   # 애플리케이션 진입 HTML
├── public/                      # 정적 파일
└── src/
    ├── main.jsx                 # React 진입점과 StrictMode 설정
    ├── App.jsx                  # 인증 상태와 페이지 전환
    ├── App.css                  # 인증·대시보드 공통 스타일
    ├── index.css                # 전역 스타일과 테마 변수
    ├── assets/                  # 배경 이미지와 아이콘
    └── pages/
        ├── LoginPage.jsx        # 로그인 화면과 API 요청
        ├── LoginPage.css        # 로그인 배경과 글래스모피즘 스타일
        ├── SignupPage.jsx       # 회원가입 화면과 API 요청
        ├── DashboardPage.jsx    # 시나리오 선택과 로그아웃
        ├── DocumentPage.jsx     # PDF 업로드·목록·다운로드
        ├── DocumentPage.css     # 전자문서 화면 스타일
        ├── PipelinePage.jsx     # Run·Step·Trace 조회와 표시
        └── PipelinePage.css     # 파이프라인 화면 스타일
```

전체 화면은 어두운 업무용 서비스 테마를 사용하며, 로그인 화면에는 반투명 카드와 배경 흐림 효과를 적용했습니다. 작은 화면에 대응하는 CSS도 포함되어 있습니다.

## 4. 설치와 실행

### 준비 사항

Node.js와 npm이 필요합니다. 설치된 Vite의 Node.js 지원 범위는 `^20.19.0 || >=22.12.0`입니다. `package.json`과 `package-lock.json`에 정의된 의존성을 사용합니다.

아래 명령은 저장소 최상위 디렉터리에서 시작합니다.

```bash
cd frontend
npm ci
npm run dev
```

개발 서버가 출력하는 주소를 브라우저에서 엽니다. 기본 주소는 `http://localhost:5173`이며, 해당 포트가 사용 중이면 Vite가 다른 포트를 선택할 수 있습니다.

화면만 확인할 때와 달리 실제 API 기능을 사용하려면 해당 서버도 별도로 실행되어 있어야 합니다.

- 인증·전자문서 백엔드는 `http://127.0.0.1:8000`을 사용합니다.
- Orchestrator는 현재 Vite 프록시 기준 `http://127.0.0.1:8001`을 사용합니다.
- 로그인 토큰을 발급·검증하는 백엔드에는 `JWT_SECRET` 환경변수 설정이 필요합니다. 프론트엔드에 이 비밀키를 넣지 않습니다.
- 백엔드의 교차 출처 요청 허용(CORS) 설정은 `http://localhost:5173`과 `http://localhost:5174`를 허용합니다. 다른 주소로 접속하면 인증·전자문서 요청이 차단될 수 있습니다.

### 빌드와 정적 검사

다음 명령은 `frontend/`에서 실행합니다.

```bash
npm run build
npm run lint
```

빌드 결과는 `dist/`에 생성됩니다. 빌드 결과를 로컬에서 확인하려면 다음 명령을 사용합니다.

```bash
npm run preview
```

현재 `package.json`에는 별도의 프론트엔드 자동 테스트 명령이 정의되어 있지 않습니다. 빌드·정적 검사 통과만으로 실제 서버 연동이나 브라우저 다운로드까지 검증되는 것은 아닙니다.

## 5. 회원가입·로그인과 화면 흐름

### 기본 화면 흐름

```text
최초 접속
  → access_token이 없으면 로그인 화면
  → 회원가입 성공 후 로그인 화면
  → 로그인 성공 후 시나리오 선택 화면
      → 시나리오 1: Agent Pipeline 화면
      → 시나리오 2: 전자문서 화면
```

`App.jsx`는 `sessionStorage`의 `access_token` 존재 여부를 초기 인증 상태로 사용합니다. 토큰이 있으면 새로고침 후 시나리오 선택 화면을 표시합니다. 선택했던 하위 화면이나 Run ID를 복원하는 기능은 없습니다.

이 화면 전환은 토큰의 유효성 검증을 대신하지 않습니다. 프론트엔드에서 `/api/auth/me`를 호출하는 초기 인증 확인, 토큰 자동 갱신, 만료 시 자동 로그아웃은 구현되어 있지 않습니다. 보호된 전자문서 요청의 최종 인증·권한 검사는 백엔드에서 수행합니다.

### 회원가입

이메일, 비밀번호, 비밀번호 확인을 입력합니다. 입력값은 `useState`로 관리하며, 비밀번호 확인이 일치하지 않으면 화면에서 제출을 막습니다.

`POST /api/auth/signup`에 `email`과 `password`를 JSON으로 전송합니다. 이메일 형식, 비밀번호 최소 8자, 중복 이메일의 최종 검사는 백엔드에서 수행합니다. 성공하면 로그인 화면으로 이동하고 가입 안내를 표시합니다. 실패하면 백엔드 메시지를 표시합니다.

### 로그인과 로그아웃

`POST /api/auth/login`에 `email`과 `password`를 JSON으로 전송합니다. 성공 응답의 `access_token`을 같은 이름의 `sessionStorage` 키에 저장하고 시나리오 선택 화면으로 이동합니다. `token_type`은 별도로 저장하지 않습니다.

비밀번호 표시·숨김, 요청 중 버튼 비활성화, 실패 메시지와 서버 연결 오류 안내를 제공합니다. 로그아웃하면 `access_token`을 제거하고 로그인 화면으로 이동합니다. 별도의 로그아웃 API는 호출하지 않습니다.

로그인 상태 유지, 비밀번호 찾기, Google·GitHub 로그인은 비활성화된 준비 중 UI이며 실제 기능은 연결되어 있지 않습니다.

## 6. 전자문서 업로드·목록·다운로드

시나리오 선택 화면의 전자문서 버튼으로 `DocumentPage`에 진입합니다. 이 화면은 FastAPI 문서 API를 직접 사용하며 Orchestrator나 에이전트를 호출하지 않습니다.

### PDF 업로드

PDF 파일 1개와 기존 사용자의 담당자 ID를 입력하고 제출합니다. 담당자 목록이나 이름 조회 기능은 없으며, 사용자가 숫자 ID를 직접 입력합니다.

`FormData`에 `file`과 `assignee_id`를 넣어 `POST /api/documents`로 전송합니다. 브라우저가 전송 경계값을 생성하도록 `Content-Type` 헤더를 직접 설정하지 않습니다.

화면에서 파일 선택과 담당자 ID 입력 여부를 확인합니다. 파일 크기 최대 10MiB, PDF 서명(`%PDF-`), 담당자 존재 여부의 최종 검사는 서버에서 수행합니다. 실제 파일은 백엔드의 `backend/uploads/`에 UUID 기반 이름으로 저장되고, DB에는 메타데이터만 저장됩니다.

성공하면 문서 ID, 원본 파일명, 파일 크기, 담당자 ID를 표시하고 현재 선택한 목록을 갱신합니다. 파일 형식·크기·입력 오류, 인증 오류, 서버 오류와 연결 실패를 안내합니다.

### 제출·배정 목록

첫 화면에서는 내가 제출한 문서를 조회합니다. 탭을 바꾸면 실제 API로 해당 목록을 조회합니다.

- 내가 제출한 문서: `GET /api/documents?view=submitted`
- 나에게 배정된 문서: `GET /api/documents?view=assigned`

응답의 `documents` 배열에서 원본 파일명, 문서 ID, 제출 일시, 파일 크기, 제출자 ID, 담당자 ID를 표시합니다. 서버가 반환한 최신순 정렬을 사용하며 사용자 이름을 임의로 만들지 않습니다.

목록 새로고침, 조회 중 표시, 탭별 빈 목록 안내, 인증·서버·통신 오류 안내를 제공합니다. 탭 변경 시 이전 요청을 취소하고, 이전 탭의 결과가 현재 목록에 나타나지 않도록 처리합니다.

### PDF 다운로드

두 목록의 각 문서에 다운로드 버튼이 있습니다. 실제 문서 ID로 `GET /api/documents/{document_id}/download`를 호출합니다.

성공 여부와 PDF 응답 형식을 확인한 뒤 바이너리를 `Blob`으로 받아 임시 객체 주소(Object URL)를 만듭니다. 목록의 `original_filename`에서 경로·제어 문자·안전하지 않은 파일명 문자를 정리하여 다운로드 이름으로 사용합니다. 임시 링크와 객체 주소는 사용 후 정리합니다.

다운로드 상태와 오류는 문서별로 관리하므로 다른 문서는 독립적으로 요청할 수 있습니다. 인증 오류(401), 권한 없음(403), 문서·파일 없음(404), 서버 오류와 연결 실패를 안내합니다. 제출자 또는 담당자만 다운로드할 수 있는 권한 검사는 백엔드에서 수행합니다.

## 7. Agent Pipeline 화면

시나리오 1의 버튼은 `PipelinePage`로 화면만 전환합니다. 화면 진입만으로 Run을 생성하지 않으며, 사용자가 실제 시나리오 실행 버튼을 눌렀을 때만 요청합니다.

### Run 생성과 조회

`POST /api/v1/runs`에 다음 고정 시나리오 요청을 전송합니다.

```json
{
  "scenarioId": "f7f9e5c3-ffc3-4b3f-918b-21e1b956ce76",
  "requestText": "회원가입 기능을 구현해주세요."
}
```

생성 응답의 `run.runId`, `firstStep.agentRole`, `firstStep.status`, `dispatchStatus`를 표시합니다. 이때 `firstStep.status`는 생성 당시 상태입니다.

생성 성공 후 받은 `run.runId`로 다음 세 조회를 병렬 호출합니다.

```text
GET /api/v1/runs/{runId}
GET /api/v1/runs/{runId}/steps
GET /api/v1/runs/{runId}/events
```

Run 조회 응답은 `run`으로 감싼 형태가 아닌 Run 객체 자체입니다. 화면에 `runId`, `status`, `verdict`, `codeVersion`, `fixAttempt`를 표시합니다. `verdict=null`은 아직 최종 판정 없음으로, `codeVersion=null`은 아직 코드 버전 없음으로 표시합니다. `FINISHED`는 실행 종료를 뜻하며 성공 여부는 `verdict`와 별도로 확인합니다.

### 에이전트 단계와 현재 작업

Step 응답의 `steps` 배열을 사용하여 Planner·Developer·QA·Security 카드에 `agentRole`, `status`, `attempt`, `a2aTaskState`를 표시합니다. 역할 설명은 고정 예시 문구이며 실제 실행 결과가 아닙니다.

현재 에이전트는 `RUNNING`인 모든 Step에서 구합니다. QA와 Security가 동시에 실행 중이면 둘 다 표시합니다. 실행 중인 Step이 없으면 `PENDING`, `WAITING_INPUT`, `SUCCEEDED`, `FAILED`, `CANCELED` 등 실제 조회된 상태를 요약합니다. 아직 생성되지 않은 Step을 실행 중으로 표시하지 않습니다.

### 실행 이벤트와 통합 새로고침

실행 기록(Trace)은 응답의 `events` 배열을 사용합니다. `eventType`, `occurredAt`, `actor`, `workflowState`, `a2aTaskState`, `attempt`와 값이 있는 Step·Task ID를 표시합니다. 이벤트가 없으면 빈 상태 안내를 표시합니다.

현재는 페이지 나누기 없이 기본 조회 범위인 처음 최대 100건을 표시합니다. 전체 건수는 `total`로 표시하며, 그 이후 이벤트를 추가로 가져오는 UI는 없습니다.

Run·Step·Trace 새로고침 버튼은 현재 Run ID로 세 조회만 다시 수행합니다. 새 Run을 생성하지 않으며 자동 반복 조회(Polling)는 없습니다. 각 조회의 로딩·오류를 독립적으로 처리하고 새 조회에서 이전 오류와 결과를 비웁니다. 생성 및 새로고침 중 중복 요청을 막습니다.

실행 결과, 오류, 수정 요청 카드는 각각 Artifact·Issue·Tool API와 미연동 상태임을 안내합니다. 고정된 성공 결과나 수정 진행을 실제 데이터처럼 표시하지 않습니다.

## 8. API 연동 구조

### 인증·전자문서 백엔드

현재 코드에는 `http://127.0.0.1:8000` 주소가 직접 지정되어 있으며, 별도의 프론트엔드 API 주소 환경변수를 사용하지 않습니다.

| 메서드 | API 경로 | 화면에서 사용하는 데이터 |
| --- | --- | --- |
| POST | `/api/auth/signup` | 요청의 `email`, `password`, 응답의 가입 메시지 |
| POST | `/api/auth/login` | 요청의 `email`, `password`, 응답의 `access_token` |
| POST | `/api/documents` | 요청의 `file`, `assignee_id`, 응답의 문서 메타데이터 |
| GET | `/api/documents?view=submitted` | 응답의 `documents` 배열 |
| GET | `/api/documents?view=assigned` | 응답의 `documents` 배열 |
| GET | `/api/documents/{document_id}/download` | PDF 바이너리 응답 |

문서 API 요청에는 저장된 토큰을 다음 형식의 헤더로 붙입니다.

```http
Authorization: Bearer <access_token>
```

문서 제출자 ID를 클라이언트에서 지정하지 않습니다. 제출·배정 목록의 조회 기준 사용자와 다운로드 권한은 서버가 JWT로 확인합니다. 서버 내부 저장 경로나 저장 파일명은 문서 API 응답과 화면에 노출하지 않습니다.

### Orchestrator와 개발용 프록시

`PipelinePage`는 상대 경로로 요청합니다. `vite.config.js`는 `/api/v1`로 시작하는 요청과 `/health` 요청을 경로 변경 없이 `http://127.0.0.1:8001`로 전달합니다. 현재 화면에서는 `/health`를 호출하지 않습니다.

```text
브라우저의 /api/v1/runs 요청
  → Vite 개발 서버
  → http://127.0.0.1:8001/api/v1/runs
```

| 메서드 | API 경로 | 화면에서 사용하는 응답 구조 |
| --- | --- | --- |
| POST | `/api/v1/runs` | `run`, `firstStep`, `dispatchStatus` |
| GET | `/api/v1/runs/{runId}` | Run 객체 자체 |
| GET | `/api/v1/runs/{runId}/steps` | `runId`, `steps` |
| GET | `/api/v1/runs/{runId}/events` | `runId`, `events`, `total` |

현재 Pipeline 요청에는 인증·전자문서용 JWT 헤더를 붙이지 않습니다. 두 서버의 인증 방식이 통합되었다고 가정하지 않습니다. 배포 환경의 API 주소와 프록시 구성은 별도 통합 작업이 필요합니다.

## 9. 구현 현황과 남은 검증

### 현재 코드에 구현된 기능

- 회원가입·로그인 API 연결과 토큰 저장, 로그아웃 처리가 구현되어 있습니다.
- 인증 상태에 따른 화면 전환과 두 시나리오 진입 화면이 구현되어 있습니다.
- PDF 업로드, 제출·배정 목록 조회, 문서별 다운로드가 실제 API에 연결되어 있습니다.
- Run 생성, 현재 Run 상태, 실제 Step·현재 에이전트, 실행 이벤트 조회가 연결되어 있습니다.
- Run·Step·Trace 통합 수동 새로고침과 요청별 오류 처리가 구현되어 있습니다.

### 미구현 또는 미연동 항목

로그인 상태 유지, 소셜 로그인, 비밀번호 찾기, 초기 토큰 유효성 확인, 토큰 자동 갱신은 구현되어 있지 않습니다. 담당자 목록 조회와 사용자 이름 표시도 없습니다.

Pipeline의 Artifact 결과, Issue·수정 요청, 수정·재검증 상세 진행, Usage·Cost, 취소·재개 API는 UI에 연결되어 있지 않습니다. 기존 Run 복원, 이벤트 추가 페이지 조회, 자동 반복 조회도 없습니다.

시나리오 2는 일반 FastAPI 문서 기능으로 연결되어 있으며 전자문서 에이전트 파이프라인 실행을 제공하지 않습니다. Dashboard의 설명에는 다운로드 준비 중 문구가 남아 있지만, `DocumentPage`에는 실제 다운로드 API 연결이 구현되어 있습니다.

### 앞으로 필요한 통합 검증

실제 LLM·Docker를 포함한 End-to-End 실행은 아직 검증 전입니다. 승인된 실행 설정(configuration), 에이전트 실행 환경, Orchestrator 연결 조건을 먼저 확인해야 합니다. 현재 Run 생성 UI는 `scenarioId`와 `requestText`만 보내며 `configuration`을 구성하거나 전송하지 않습니다.

Orchestrator 환경에 따라 `dispatchStatus=NOT_CONFIGURED`로 실행 전달이 미설정일 수 있으며, Step이 `PENDING`에 머무를 수 있습니다. 설정 검사가 활성화된 환경에서는 `OWNED_AGENT_CONFIGURATION_INVALID`와 함께 HTTP 422로 생성이 거부될 수도 있습니다. Run 생성 접수나 일부 이벤트 조회만으로 실제 에이전트 실행 성공을 판단하지 않습니다.

실제 서버와 브라우저에서는 토큰 만료, 사용자별 문서 권한, 한글 파일명 다운로드와 원본 바이트 일치, 병렬 에이전트 상태 전환, 최종 판정, 통신 실패 후 새로고침 복구를 확인해야 합니다. 구현 완료 목록은 코드 연결 상태를 설명하며 이 통합 검증이 모두 완료되었다는 뜻은 아닙니다.
