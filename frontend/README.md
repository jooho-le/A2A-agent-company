# AI 에이전트 회사 프론트엔드

최종 대조 기준: 2026-11-09의 현재 로컬 코드입니다. 화면의 회사명은 `AI 에이전트 회사`이며, `atelier`는 내부 파일명과 보관된 디자인 자료에 남아 있습니다.

## 1. 프로젝트 소개와 담당 역할

A2A Agent Company는 여러 인공지능 에이전트(Agent)의 협업 흐름을 보여주는 프로젝트입니다. 이 디렉터리는 역할 3인 웹 애플리케이션 및 시연 화면(Web Application / Demo)을 담당합니다.

현재 첫 화면은 로그인 없이 열리는 atelier 오피스입니다. React로 변환한 오피스·프로젝트·직원·업무 흐름·결과물 화면과 3D 오피스 시연을 제공합니다. 기존 회원가입·로그인, 전자문서 제출·조회·다운로드, 실제 에이전트 실행 현황 화면도 유지되어 있습니다.

오피스의 프로젝트·진행률·결재함·예시 결과물은 목업입니다. 3D 오피스는 기존 105초 목업 시연과 실제 Run 조회 모드를 분리하여 제공합니다. 기존 Run UUID를 입력하면 Orchestrator의 Run·Step·Event와 실행 방식을 조회하고, 실제 Step 상태를 캐릭터별 동작에 반영합니다. 실제 인증과 전자문서 처리는 FastAPI 제품 백엔드에 요청하며, 별도 `PipelinePage`에는 Run 생성 기능이 남아 있습니다. 프론트엔드에서 직접 LLM을 호출하거나 Docker를 실행하지 않습니다.

화면에 API 연결이 구현되어 있다는 사실과 실제 에이전트의 전체 실행이 검증되었다는 사실은 구분해야 합니다. 제품 웹 UI와 실제 LLM·Docker를 연결한 전체 흐름(End-to-End) 통합 실행은 아직 검증 전입니다.

## 2. 사용 기술

- React 19와 React DOM으로 화면을 구성합니다.
- Vite 8과 `@vitejs/plugin-react`로 개발 서버와 빌드를 구성합니다.
- 자바스크립트(JavaScript), JSX와 CSS를 사용합니다.
- React의 `useState`, `useEffect`, `useRef`로 화면 상태와 요청을 관리합니다.
- 브라우저 기본 `fetch`, `FormData`, `Blob`, `AbortController`를 사용합니다.
- `sessionStorage`에 로그인 토큰을, `localStorage`에 목업·실제 성공 세리머니의 독립된 순서 기록을 저장합니다.
- ESLint로 코드의 정적 검사를 수행합니다.
- 3D 화면은 WebGL과 `office3dScene.js`에 포함된 Three.js r160 렌더러를 사용합니다. 별도의 `three` npm 의존성을 설치한 구조는 아닙니다.

React Router나 별도의 UI 라이브러리는 사용하지 않습니다. `App.jsx`의 상태로 주요 페이지를 전환하고, atelier 내부 메뉴는 상태와 `#/home` 등의 해시 경로를 함께 사용합니다. 현재 React 화면은 iframe을 사용하지 않습니다.

## 3. 폴더 구조

```text
frontend/
├── README.md
├── package.json                 # 의존성과 실행 명령
├── package-lock.json            # 의존성 버전 고정 정보
├── vite.config.js               # React 설정과 개발용 프록시
├── eslint.config.js             # 정적 검사 설정
├── index.html                   # 애플리케이션 진입 HTML
├── public/
│   ├── characters/              # 직원 PNG와 기존 캐릭터 이미지
│   ├── agent-office-3d/         # 3D 오피스 사진 배경
│   └── atelier-preview.html     # 보관된 HTML 미리보기, 현재 앱에서는 사용하지 않음
└── src/
    ├── main.jsx                 # React 진입점과 StrictMode 설정
    ├── App.jsx                  # 로그인 진입 설정과 페이지 전환
    ├── App.css                  # 인증·대시보드 공통 스타일
    ├── index.css                # 전역 스타일과 테마 변수
    ├── assets/                  # 배경 이미지와 아이콘
    ├── api/
    │   └── agentRuns.js         # Run 생성·조회·Step·Event·실행 방식 조회 함수
    ├── hooks/
    │   ├── useAgentRunMonitor.js # 조회 결과·취소·수동 갱신·자동 갱신 관리
    │   └── agentRunPolling.js  # 중복 없는 예약과 재시도 간격 계산
    ├── components/atelier/
    │   ├── AtelierChrome.jsx    # 메뉴·계정 메뉴·현재 날짜
    │   ├── AtelierViews.jsx     # 오피스·프로젝트·직원·업무 흐름·결과물 화면
    │   ├── AtelierCards.jsx     # 공통 카드와 업무 요청 폼
    │   ├── AtelierDialog.jsx    # 직원 소개·기획안·파일 등 모달
    │   ├── atelierMock.js       # 프로젝트·업무 기록·예시 결과물 목업
    │   ├── humanAgents.js       # 사람 캐릭터 PNG와 3D 외형 매핑
    │   ├── AgentLeadership.jsx  # 기획 총괄 중심의 역할 안내
    │   ├── AgentRunMonitor.jsx # 실제 Run 입력·상태·Event 페이지 표시
    │   ├── agentRunDisplayState.js # Run·Step을 캐릭터 표시 상태로 변환
    │   ├── office3dRunController.js # 실제 모드의 역할별 독립 모션 제어
    │   ├── runSuccessCeremony.js # 실제 성공 조건·포즈 배정·중복 방지
    │   ├── officeCelebrationSequence.js # 목업 시연의 세리머니 순서
    │   ├── AgentOffice3D.jsx    # 목업/실제 모드 UI와 장면 수명 주기
    │   ├── AgentOffice3D.css    # 3D 영역에 한정된 스타일
    │   ├── agentOfficeRuntime.js # 재생·카메라·이벤트와 렌더링 수명 주기
    │   ├── office3dScene.js     # 원본 HTML에서 분리한 렌더러·장면·기본 동작
    │   ├── office3dLayout.js    # 책상·의자·소품 배치와 합류 경로
    │   ├── office3dLeader.js    # 총괄 의상 소품과 한글 명패
    │   ├── office3dPlannerCamera.js # 파일명은 유지하며 개발·QA·보안 정면 근접샷에 사용
    │   ├── office3dPerformance.js # 완료 연출 세 가지와 경고 표정
    │   └── useAtelierMotion.js  # 모션·스크롤·움직임 감소 설정
    └── pages/
        ├── LoginPage.jsx        # 로그인 화면과 API 요청
        ├── LoginPage.css        # 로그인 배경과 글래스모피즘 스타일
        ├── SignupPage.jsx       # 회원가입 화면과 API 요청
        ├── AtelierDashboard.jsx # 기본 화면·목업 및 실제 Run 조회 공유
        ├── AtelierDashboard.css # atelier 공통 디자인과 반응형 스타일
        ├── DashboardPage.jsx    # 기존 시나리오 선택 화면, 현재 App에서는 사용하지 않음
        ├── DocumentPage.jsx     # PDF 업로드·목록·다운로드
        ├── DocumentPage.css     # 전자문서 화면 스타일
        ├── PipelinePage.jsx     # Run·Step·Trace 조회와 표시
        └── PipelinePage.css     # 파이프라인 화면 스타일
```

`frontend/tests/`에는 상태 변환·실제 모션 제어·세리머니·폴링의 Node.js 자동 테스트가 있습니다.

위 구조는 주요 파일만 표시합니다. 기존 싱글 모드 관련 컴포넌트와 이미지도 남아 있지만 현재 `AtelierDashboard`는 멀티 모드만 표시합니다. HTML 디자인 원본과 미리보기 파일은 보관용이며 현재 React 진입 화면과 구분합니다.

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

오피스 목업은 백엔드 없이 확인할 수 있습니다. 3D 장면에는 WebGL을 지원하는 브라우저가 필요합니다. 실제 API 기능을 사용하려면 해당 서버도 별도로 실행되어 있어야 합니다.

- 인증·전자문서 백엔드는 `http://127.0.0.1:8000`을 사용합니다.
- Orchestrator는 현재 Vite 프록시 기준 `http://127.0.0.1:8001`을 사용합니다.
- 로그인 토큰을 발급·검증하는 백엔드에는 `JWT_SECRET` 환경변수 설정이 필요합니다. 프론트엔드에 이 비밀키를 넣지 않습니다.
- 백엔드의 교차 출처 요청 허용(CORS) 설정은 `http://localhost:5173`과 `http://localhost:5174`를 허용합니다. 다른 주소로 접속하면 인증·전자문서 요청이 차단될 수 있습니다.

제품 백엔드 실행 방법은 [백엔드 README](../backend/README.md)를 참고합니다. 실제 Owned Host의 Orchestrator 포트는 승인 설정에 따릅니다. 제품 백엔드의 `8000` 포트와 충돌하지 않게 조율하고, 현재 Vite 프록시 대상인 `8001`과 일치하는지 확인해야 합니다.

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

`package.json`에 `npm test` 스크립트는 없습니다. 저장된 자동 테스트는 Node.js 기본 테스트 실행기로 실행합니다.

```bash
node --test tests/*.test.mjs
```

3D 렌더러를 포함한 지연 로딩 번들 때문에 빌드에서 500kB 초과 경고가, 린트 과정에서 큰 파일에 관한 Babel 안내가 나올 수 있습니다. 최근 구현 작업에서는 빌드·린트가 통과했으며 이러한 안내와 실행 실패를 구분합니다. 빌드·정적 검사 통과만으로 실제 서버 연동이나 브라우저 다운로드까지 검증되는 것은 아닙니다.

## 5. 회원가입·로그인과 화면 흐름

### 기본 화면 흐름

```text
최초 접속
  → AtelierDashboard의 오피스 화면
      → 오피스 / 프로젝트 / 우리 직원 / 업무 흐름 / 결과물 보관함
      → 오피스의 실제 Run 조회: 기존 UUID 입력 → 상태 조회·실제 3D 표시
      → 사이드바 하단 대표님 계정 메뉴
          → Agent Pipeline 열기: 실제 Run·Step·Trace 화면
          → 전자문서 관리: 실제 PDF API 화면
```

`App.jsx`의 `REQUIRE_PLATFORM_LOGIN`은 현재 `false`입니다. 로그인 여부와 관계없이 기본 오피스를 표시합니다. `DashboardPage.jsx`는 보존되어 있지만 현재 `App.jsx`에서 렌더링하지 않습니다. Pipeline과 전자문서 화면의 돌아가기 버튼도 현재는 atelier 오피스로 돌아옵니다.

로그인이 필요한 화면 흐름을 다시 사용하려면 `App.jsx`의 기존 `REQUIRE_PLATFORM_LOGIN`을 `true`로 설정합니다. 이 경우 `access_token`이 없으면 로그인 화면을 표시하고, 회원가입 성공 후 로그인, 로그인 성공 후 atelier 오피스로 이동합니다. 현재 기본 설정에서는 로그인 화면으로 직접 이동하는 메뉴가 없으므로, 문서 API를 계정으로 검증할 때는 이 설정과 로그인 절차를 함께 확인해야 합니다.

`App.jsx`는 `sessionStorage`의 `access_token` 존재 여부를 초기 인증 상태로 사용합니다. atelier 내부 메뉴의 해시 경로는 새로고침 후 복원하지만, 메모리의 목업 프로젝트 변경·선택·시연 재생 위치, Pipeline·전자문서 페이지 선택과 Run ID는 복원하지 않습니다.

이 화면 전환은 토큰의 유효성 검증을 대신하지 않습니다. 프론트엔드에서 `/api/auth/me`를 호출하는 초기 인증 확인, 토큰 자동 갱신, 만료 시 자동 로그아웃은 구현되어 있지 않습니다. 보호된 전자문서 요청의 최종 인증·권한 검사는 백엔드에서 수행합니다.

### 회원가입

이메일, 비밀번호, 비밀번호 확인을 입력합니다. 입력값은 `useState`로 관리하며, 비밀번호 확인이 일치하지 않으면 화면에서 제출을 막습니다.

`POST /api/auth/signup`에 `email`과 `password`를 JSON으로 전송합니다. 이메일 형식, 비밀번호 최소 8자, 중복 이메일의 최종 검사는 백엔드에서 수행합니다. 성공하면 로그인 화면으로 이동하고 가입 안내를 표시합니다. 실패하면 백엔드 메시지를 표시합니다.

### 로그인과 로그아웃

`POST /api/auth/login`에 `email`과 `password`를 JSON으로 전송합니다. 성공 응답의 `access_token`을 같은 이름의 `sessionStorage` 키에 저장하고 atelier 오피스로 이동합니다. `token_type`은 별도로 저장하지 않습니다.

비밀번호 표시·숨김, 요청 중 버튼 비활성화, 실패 메시지와 서버 연결 오류 안내를 제공합니다. 로그아웃하면 `access_token`과 페이지 전환 상태를 비웁니다. `REQUIRE_PLATFORM_LOGIN=true`이면 로그인 화면으로, 현재처럼 `false`이면 오피스 화면으로 돌아갑니다. 별도의 로그아웃 API는 호출하지 않습니다.

로그인 상태 유지, 비밀번호 찾기, Google·GitHub 로그인은 비활성화된 준비 중 UI이며 실제 기능은 연결되어 있지 않습니다.

### atelier 오피스와 3D 시연

현재 오피스는 멀티 에이전트 전용입니다. 목업 프로젝트와 실제 Run 조회 결과는 별도로 관리합니다. 상단 업무 요청 폼, 프로젝트 선택·검색, 대표님 결재함, 직원 소개, 단계별 업무 흐름과 결과물 보관함을 제공합니다. 업무 시작·승인·수정 요청은 브라우저 메모리의 예시 데이터를 바꾸며 실제 Run을 생성하지 않습니다. 결과물 보관함의 파일 보기·다운로드도 `atelierMock.js`의 예시 텍스트이며 Orchestrator Artifact가 아닙니다.

직원은 기획·개발·QA·보안의 사람 캐릭터 4명입니다. 기획 에이전트에는 `기획 총괄 · 팀 리더` 배지와 역할 안내를 적용했습니다. 이는 시각적 역할 설명이며 DB의 관리자 권한이나 실제 Orchestrator 역할을 새로 부여하는 기능이 아닙니다. 날짜와 요일은 `Asia/Seoul` 기준으로 자동 갱신합니다.

멀티 오피스의 기존 동물 카드 4개와 업무 보고 목록 대신 `AgentOffice3D`를 표시합니다. 기존 테이블을 유지한 채 기획 총괄은 앞쪽에 앉고, 나머지는 총괄을 바라보는 2+1열로 배치했습니다. 한글 명패는 총괄 책상 앞 중앙에 정렬합니다. 플래너는 원래 보이는 방향에서 줌인하고, 개발·QA·보안은 정면 근접샷을 사용합니다. 개발자는 노트북에 얼굴이 가리지 않도록 촬영 높이를 조정했습니다.

3D 시연에는 재생·일시정지, 타임라인 이동, 배속, 전체 보기·수동 카메라, 전체 화면, 에이전트 상태 팝업, 오류 연출 선택이 있습니다. 완료 연출은 기존 협업 포즈, 서류 던지기·박수, 달려와 원형으로 모이는 포옹의 세 가지입니다. 경고 시 곤란·화남·진땀 표정을 보여줍니다. 모션 끄기와 `prefers-reduced-motion`을 반영하며 화면 이탈 시 렌더러·이벤트·그래픽 자원을 정리합니다.

105초 목업의 상태·완료·오류와 프로젝트 진행률은 시연용입니다. 상단 업무 시작은 목업 프로젝트를 추가하고, 3D 시연 재생은 별도로 조작합니다. 어느 동작도 실제 Run을 생성하지 않습니다. 실제 Run 조회 모드의 상태·모션·성공 세리머니는 7절의 API 응답과 조건을 사용하며 목업 타임라인으로 자동 진행하지 않습니다.

## 6. 전자문서 업로드·목록·다운로드

atelier 사이드바 하단 대표님 계정 메뉴의 `전자문서 관리`로 `DocumentPage`에 진입합니다. 이 화면은 FastAPI 문서 API를 직접 사용하며 Orchestrator나 에이전트를 호출하지 않습니다. 로그인 없이 오피스를 열었더라도 실제 문서 작업에는 유효한 `access_token`이 필요합니다. 토큰이 없으면 화면에서 로그인 필요 메시지를 표시하고 요청을 보내지 않으며, 유효하지 않은 토큰은 백엔드가 HTTP 401로 거부합니다.

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

## 7. 실제 Run 조회와 3D 상태 연결

### 데이터 흐름과 조회 UI

오피스의 실제 Run 조회 영역에 기존 멀티 Run UUID를 입력합니다. 조회 자체는 새 Run이나 LLM 호출을 생성하지 않습니다. `AtelierDashboard`가 `useAgentRunMonitor()`를 한 번 호출하고, 결과를 `AgentRunMonitor`와 `AgentOffice3D`에 공유합니다.

```text
기존 Run UUID 입력
  → useAgentRunMonitor: Run·Step·Event·Configuration GET 조회
  → AgentRunMonitor: 상태·오류·Event 페이지 표시
  → agentRunDisplayState: 캐릭터 표시 상태로 변환
  → AgentOffice3D → office3dRunController: 역할별 모션 반영
```

Run의 `status`, `verdict`, `codeVersion`, `fixAttempt`와 Step의 `workflowStepId`, `agentRole`, `status`, `attempt`, `codeVersion` 등을 보존합니다. `FINISHED`는 종료 상태일 뿐이며, `verdict=null`은 최종 판정 없음입니다. Hook의 `status=success`는 조회 성공이며 Agent 실행 성공이 아닙니다.

Run·Step·Event·실행 방식의 로딩·빈 목록·오류를 각각 표시합니다. 전체 새로고침, 조회 취소, 조회 해제와 Event 페이지 이동을 제공합니다. Event는 한 번에 20건이며 최초 조회·수동 전체 새로고침 때 최신 구간을 선택합니다. 자동 갱신은 현재 선택한 페이지를 유지합니다. Run UUID 선택은 메모리 상태이므로 새로고침이나 Dashboard 재진입 후 다시 입력해야 합니다.

### 캐릭터 상태 매핑

| 실제 Step 상태 | 실제 모드의 캐릭터 표시 |
| --- | --- |
| `PENDING` 또는 Step 미생성 | 기본 대기 |
| `RUNNING` | 해당 역할의 기존 작업 모션 반복 |
| `SUCCEEDED` | 작업 종료 후 안정된 자세, 전체 성공으로 처리하지 않음 |
| `FAILED` | 작업 중단과 오류 표정 |
| `WAITING_INPUT` | 작업 중지와 입력 대기 표시 |
| `CANCELED` | 작업 중단과 취소 표시 |
| 조회 오류·미정의·판단 불가 | 상태 확인 불가 표시, Agent 실패로 단정하지 않음 |

QA와 Security는 독립적으로 동시에 작업할 수 있습니다. 같은 Run·Step·상태의 재조회로 모션 시간을 초기화하지 않으며, Run 변경 시 이전 상태를 초기화합니다. 같은 역할의 여러 Step은 현재 코드 버전·재시도 정보를 확인해 선택하고, 충돌하거나 판별할 수 없는 경우 임의로 실행 중인 Step을 정하지 않습니다. Event로 역할을 추측하거나 실제 진행률을 계산하지 않습니다.

갱신 중에는 마지막으로 확인된 데이터를 유지합니다. 조회 실패도 마지막 정상 데이터를 보존하지만 화면에는 조회 오류를 표시하며, 해당 캐릭터는 마지막 자세를 멈춰 상태 확인 불가로 표시합니다. 오류가 났다고 목업 시연으로 자동 전환하지 않습니다.

### 3초 자동 갱신

오피스 화면의 실제 Run 모드에서 Run ID가 있을 때 자동 갱신합니다. 실행 방식 조회에서 `SINGLE_AGENT`로 확인되면 중지합니다. 실행 방식 확인 전에도 초기 조회·재시도가 발생할 수 있으며, 이를 멀티 실행이 확인된 것으로 해석하지 않습니다.

- 기존 `refresh()`를 재사용하며 한 조회 묶음이 완료된 후 3초 뒤 다음 조회를 예약합니다. 진행 중인 동일 요청은 공유하여 중복을 막습니다.
- Run 종료 상태인 `FINISHED`, `ABORTED`를 확인하면 반복 조회를 중지합니다. 성공 여부는 별도로 `verdict`를 확인합니다.
- 조회 실패 시 마지막 정상 데이터를 유지하고 재시도 간격을 6초·12초·24초·최대 30초로 늘립니다. 정상 복구 후 3초로 돌아옵니다.
- 자동 갱신 표시와 일시정지·재개 버튼을 제공합니다. 조회 취소도 반복 조회를 멈추며, 일시정지 상태의 수동 새로고침은 자동 갱신을 다시 켜지 않습니다.
- Run 변경, 목업 복귀, 다른 메뉴 이동과 컴포넌트 해제 시 예약·진행 중인 요청을 정리합니다.

### 실제 성공 세리머니

실제 모드에서 조회가 정상 완료되고, Configuration의 `architecture=MULTI_AGENT`, Run의 `status=FINISHED`, `verdict=SUCCESS`를 모두 확인해야 합니다. Step 하나의 `SUCCEEDED`, `FAIL`, `UNVERIFIED`, `HUMAN_REVIEW`, `null` 판정으로 전체 성공 연출을 실행하지 않습니다. 3D 장면과 캐릭터가 준비되고 모션이 허용되어야 재생합니다.

관측 중인 Run이 미종료 상태에서 최종 성공으로 바뀌면 자동 재생 후보가 됩니다. 처음부터 성공한 과거 Run을 조회한 경우에는 자동으로 재생하지 않고 사용자가 성공 포즈 재생 버튼을 누릅니다. 오류나 기록 저장 실패 시 순서를 임의로 소비하지 않습니다.

서로 다른 성공 Run에 `team → cheer → hug`를 하나씩 배정하며, 네 번째부터 다시 `team`으로 순환합니다. 재생 직전에 Run UUID·포즈·다음 순서를 함께 저장하여 같은 Run의 재조회·새로고침·재진입으로 중복 소비하지 않습니다. 이 기록은 재생 시작 기록이므로 중간에 화면을 나간 경우에도 자동으로 다시 재생하지 않습니다. 여러 탭의 동시 실행에 대한 잠금은 없으며 브라우저 한 탭 기준입니다.

| 용도 | `localStorage` 키 |
| --- | --- |
| 기존 목업 시연 순서 | `atelier.office3d.ceremony-order.v1` |
| 실제 성공 Run 이력·순서 | `ai-agent-company.office3d.success-ceremonies.v1` |

두 기록과 초기화 기능은 독립적입니다. 사용자가 명시적으로 초기화하지 않는 한 기록을 지우지 않습니다. 저장된 순서는 실제 LLM 실행 검증의 증거가 아닙니다.

## 8. Agent Pipeline 화면

atelier 계정 메뉴의 `Agent Pipeline 열기`는 `PipelinePage`로 화면만 전환합니다. 화면 진입만으로 Run을 생성하지 않으며, 사용자가 실제 시나리오 실행 버튼을 눌렀을 때만 요청합니다. 이 별도 화면의 Run ID는 Dashboard와 자동 공유되지 않습니다. 오피스의 실제 3D에 표시하려면 Dashboard에서 해당 UUID를 조회해야 합니다.

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

## 9. API 연동 구조

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

`agentRuns.js`와 `PipelinePage`는 상대 경로로 요청합니다. `vite.config.js`는 `/api/v1`로 시작하는 요청과 `/health` 요청을 경로 변경 없이 `http://127.0.0.1:8001`로 전달합니다. 현재 화면에서는 `/health`를 호출하지 않습니다.

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
| GET | `/api/v1/runs/{runId}/events?limit=20&offset=0` | `runId`, `events`, `total`, `limit`, `offset` |
| GET | `/api/v1/runs/{runId}/configuration` | `RUN_CONFIGURATION` Artifact에서 `configuration.architecture`만 사용 |

현재 Pipeline 요청에는 인증·전자문서용 JWT 헤더를 붙이지 않습니다. 두 서버의 인증 방식이 통합되었다고 가정하지 않습니다. 배포 환경의 API 주소와 프록시 구성은 별도 통합 작업이 필요합니다.

### 공통 API 함수와 Run 생성의 연결 범위

`src/api/agentRuns.js`의 함수는 다음과 같습니다. 조회 함수는 Dashboard의 공통 Hook에서 사용하며, `signal`로 요청 취소를 지원합니다.

| 함수 | 역할 |
| --- | --- |
| `createAgentRun(scenarioId, requestText, architecture = 'MULTI_AGENT', { signal } = {})` | Run 생성, 기존 2·3개 인자 호출과 호환 |
| `getAgentRun(runId, { signal } = {})` | Run 객체 조회 |
| `getAgentRunSteps(runId, { signal } = {})` | Step 목록 조회 |
| `getAgentRunEvents(runId, { limit = 100, offset = 0, signal } = {})` | Event 페이지 조회, `limit` 1~500 |
| `getAgentRunArchitecture(runId, { signal } = {})` | Configuration 조회 후 `{ runId, architecture }`만 반환 |

생성 함수의 `architecture`는 `MULTI_AGENT`, `SINGLE_AGENT`만 허용하며 요청 본문에 `configuration: { architecture }`를 포함합니다. 오류의 `status`, `code`, `detail`을 보존하고 네트워크 오류·응답 형식 오류·요청 취소를 구분합니다. 따라서 `503 SINGLE_AGENT_RUNTIME_NOT_CONFIGURED` 같은 원본 실패도 식별할 수 있습니다. 함수가 싱글 인자를 지원해도 현재 UI는 멀티 전용입니다.

현재 업무 시작 버튼은 이 생성 함수를 호출하지 않습니다. `PipelinePage`도 공통 함수를 사용하지 않고 8절의 고정 JSON을 직접 `fetch`하며 `configuration`을 보내지 않습니다. 최소한의 `{ architecture }`는 실제 Owned Host의 승인된 모델·실행 환경·실행 한도 설정 전체를 대신하지 않습니다. 승인 설정의 출처·전달 구조는 팀과 확정해야 하며 임의의 모델이나 비용 한도를 채워 넣지 않습니다. API Key는 브라우저에 전달하거나 저장하지 않습니다.

## 10. 구현 현황과 남은 검증

### 현재 코드에 구현된 기능

- 회원가입·로그인 API 연결, 토큰 저장과 로그아웃, 로그인 없는 기본 오피스 진입이 구현되어 있습니다.
- PDF 업로드, 제출·배정 목록, 문서별 다운로드가 제품 백엔드 API에 연결되어 있습니다.
- iframe 없는 React 오피스와 멀티 에이전트 전용 화면, 사람 캐릭터·총괄 배지·교실형 배치·카메라가 구현되어 있습니다.
- 기존 UUID로 Run·Step·Event·실행 방식 조회, 부분 오류 처리·취소·Event 페이지 이동이 구현되어 있습니다.
- 실제 모드의 역할별 3D 모션, QA·Security 병렬 작업, 수동·자동 갱신과 실제 성공 세리머니가 연결되어 있습니다.
- 별도 Pipeline의 Run 생성·Run·Step·Trace 조회는 유지되어 있습니다.

### 목업 또는 미연동 항목

오피스 업무 시작은 목업 프로젝트 추가이며 실제 Run 생성으로 연결되지 않습니다. 프로젝트 진행률, 결재함·승인·수정 요청, 업무 기록, 결과물 보관함의 예시 텍스트·다운로드는 실제 Artifact·Issue가 아닙니다. 실제 진행률을 제공하는 것처럼 해석하지 않습니다.

Artifact 결과, Issue·수정 요청, 수정·재검증 상세, Usage·Cost, 서버 Run 취소·재개는 현재 UI에 연결되지 않았습니다. UI의 조회 취소는 GET 요청과 폴링을 중지하며 서버의 Run 실행을 취소하지 않습니다. 싱글 UI는 제공하지 않고, 전자문서 화면도 문서 Agent 파이프라인을 실행하지 않습니다.

로그인 상태 유지·소셜 로그인·비밀번호 찾기·초기 토큰 유효성 확인·토큰 자동 갱신과 담당자 목록·사용자 이름 조회는 구현되어 있지 않습니다. 제품 JWT와 Agent Run 접근 권한도 통합되어 있지 않습니다.

### 검증 기록과 범위

`frontend/tests/`에는 상태 변환, 캐릭터별 실제 상태 제어, 성공 세리머니, 폴링의 Mock 기반 테스트가 있습니다. 이전 UI 작업에서 브라우저로 목업·실제 표시 모드와 세리머니 포즈 등을 검증했지만 실제 LLM이 생성한 성공 결과를 검증한 것은 아닙니다.

가장 최근 폴링 작업에서는 핵심 단위 테스트 8개와 `npm run build`, `npm run lint`, `git diff --check`가 통과했습니다. 해당 단계는 최소 검증 범위에 따라 전체 기존 테스트와 3D 브라우저 회귀·실제 서버 폴링 검증을 다시 실행하지 않았습니다. 이번 README 갱신에서는 코드와 문서를 대조했으며 테스트·서버·API·LLM·Docker는 실행하지 않았습니다.

### 앞으로 필요한 통합 검증

제품 웹 UI를 포함한 실제 LLM·Docker End-to-End 실행은 아직 검증 전입니다. 승인 configuration, 실제 Multi Host, LLM 모델·API Key의 서버 측 설정과 Orchestrator의 포트·DB 경로를 먼저 합의해야 합니다. 실행 계약은 [역할 1·2 인계 안내](../docs/45-owned-platform-handoff.md)와 현재 Orchestrator 코드를 함께 확인합니다.

환경에 따라 `dispatchStatus=NOT_CONFIGURED`로 전달이 미설정이거나 Step이 `PENDING`에 머무를 수 있습니다. 승인 설정 검사가 활성화된 환경에서는 `OWNED_AGENT_CONFIGURATION_INVALID`와 HTTP 422로 생성이 거부될 수 있습니다. Run 접수나 조회 성공만으로 실제 Agent 실행 성공을 판단하지 않습니다.

실제 서버·브라우저에서는 다음을 추가 확인해야 합니다.

- 승인된 Run 생성 연결과 제품 JWT·Run 접근 권한의 경계
- 폴링 중 병렬 실행·재시도·통신 장애·종료 상태 반영과 세리머니 1회 재생
- Event 페이지 이동 중 갱신과 장시간 실행 시 리소스 정리
- 토큰 만료·재로그인, 사용자별 문서 권한, 한글 파일명 다운로드와 원본 바이트 일치

구현 완료 목록은 현재 코드의 연결 범위를 뜻하며 위 통합 검증까지 모두 완료되었다는 의미는 아닙니다.
