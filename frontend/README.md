# AI 에이전트 회사 프론트엔드

최근 갱신: 2026-11-14. 프로젝트 상세·Run 연결 이력 조회와 선택 프로젝트 재조회 복원, 로그인·회원가입 공통 디자인, 오피스 화면 크기·카메라 조정을 반영했습니다. 이번 갱신은 문서만 수정하며 테스트·서버·API·DB는 실행하거나 변경하지 않습니다. 검증 수치는 각 구현 단계의 과거 기록입니다.

## 1. 프로젝트 소개와 담당 역할

A2A Agent Company는 여러 인공지능 에이전트(Agent)의 협업 흐름을 보여주는 프로젝트입니다. 이 디렉터리는 역할 3인 웹 애플리케이션 및 시연 화면(Web Application / Demo)을 담당합니다.

현재 첫 화면은 로그인 없이 열리는 AI 에이전트 회사 오피스입니다. 홈은 기존 파란색 업무 요청창과 오케스트레이터를 포함한 5인 3D 오피스로 구성합니다. 사이드바·헤더와 회원가입·로그인·전자문서 기능은 유지하며, 별도 Run 생성·조회 패널과 UUID 입력은 사용자 화면에서 제거했습니다.

프로젝트는 제품 백엔드에 저장된 본인 프로젝트 목록과 현재 화면에서 생성한 Run을 함께 표시합니다. 결과물 보관함은 현재 Run만 표시하며, 조회할 데이터가 없으면 빈 상태를 보여줍니다. 근거 없는 진행률 숫자와 예시 파일 다운로드는 이 두 화면에서 제거했습니다. 105초 목업 시연과 실제 Run 모드는 분리되며, 직원 소개·업무 흐름의 역할 예시와 보존된 목업 데이터는 실제 결과가 아닙니다. 실제 인증·전자문서는 제품 백엔드를 사용하고, 프론트엔드가 직접 LLM이나 Docker를 실행하지 않습니다.

화면에 API 연결이 구현되어 있다는 사실과 실제 에이전트의 전체 실행이 검증되었다는 사실은 구분해야 합니다. 제품 웹 UI와 실제 LLM·Docker를 연결한 전체 흐름(End-to-End) 통합 실행은 아직 검증 전입니다.

## 2. 사용 기술

- React 19와 React DOM으로 화면을 구성합니다.
- Vite 8과 `@vitejs/plugin-react`로 개발 서버와 빌드를 구성합니다.
- 자바스크립트(JavaScript), JSX와 CSS를 사용합니다.
- React의 `useState`, `useEffect`, `useRef`로 화면 상태와 요청을 관리합니다.
- 브라우저 기본 `fetch`, `FormData`, `Blob`, `AbortController`를 사용합니다.
- `sessionStorage`에 로그인 토큰과 선택한 프로젝트 ID만 저장하고, `localStorage`에 목업·실제 성공 세리머니의 독립된 순서 기록을 저장합니다.
- ESLint로 코드의 정적 검사를 수행합니다.
- 3D 화면은 WebGL과 `office3dScene.js`에 포함된 Three.js r160 렌더러를 사용합니다. 별도의 `three` npm 의존성을 설치한 구조는 아닙니다.

React Router나 별도의 UI 라이브러리는 사용하지 않습니다. `App.jsx`의 상태로 주요 페이지를 전환하고, 오피스 내부 메뉴는 상태와 `#/home` 등의 해시 경로를 함께 사용합니다. 현재 React 화면은 iframe을 사용하지 않습니다.

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
│   ├── characters/              # 직원 카드 office-*.png, 로그인 업무 포즈 login-*.png
│   ├── agent-office-3d/         # 3D 오피스 사진 배경
│   └── atelier-preview.html     # 보관된 HTML 미리보기, 현재 앱에서는 사용하지 않음
├── scripts/
│   ├── loginCharacterScene.js   # 기존 3D 모델에 로그인 전용 관절 자세·소품 적용
│   └── renderLoginCharacters.mjs # 오프라인 Chrome/WebGL PNG 렌더링
└── src/
    ├── main.jsx                 # React 진입점과 StrictMode 설정
    ├── App.jsx                  # 공개 오피스·계정 로그인·입력 초안과 페이지 전환
    ├── App.css                  # 인증·대시보드 공통 스타일
    ├── index.css                # 전역 스타일과 테마 변수
    ├── assets/                  # 배경 이미지와 아이콘
    ├── api/
    │   ├── projects.js          # 제품 8000의 본인 프로젝트 목록·상세 GET·JWT·오류 처리
    │   ├── productRunRequest.js # 제품 Run 보호 전송기 준비, 현재 비활성
    │   ├── agentRuns.js         # 승인 공개 설정으로 생성·조회·Step·Event·실행 방식 조회
    │   ├── agentRunSubmission.js # 공개 설정 검사·중복 제출 방지·생성 상태 처리
    │   └── agentArtifactCatalog.js # 보관함용 공개 등록 정보·보고서 요약 추출
    ├── hooks/
    │   ├── projectSelection.js  # 선택 ID 저장·상세 재조회·세션 변경 시 정리
    │   ├── useSavedProjects.js  # 프로젝트 메뉴 진입·재시도·취소·목록 상태
    │   ├── useAgentRunMonitor.js # 공통 조회·자동 갱신·Trace/결과 조회 활성 조건
    │   ├── useOfficeRunSubmission.js # 파란색 요청창의 승인·접수·오류 상태
    │   ├── agentRunPolling.js  # 중복 없는 예약과 재시도 간격 계산
    │   ├── agentRunSelection.js # 신규/기존 Run 선택과 초기 수집 옵션 유지
    │   └── agentRunEventFeed.js # 화면 페이지와 독립된 초기·증분 Trace 수집
    ├── components/atelier/
    │   ├── SavedProjectDetail.jsx # 저장된 프로젝트 상세·Run 연결 이력 대화상자
    │   ├── AuthRoleAccent.jsx   # 로그인 직원별 업무 장식 SVG
    │   ├── OfficeAuthLayout.jsx # 로그인·회원가입 중앙 폼과 직원 이미지 배치
    │   ├── AtelierChrome.jsx    # 메뉴·계정 메뉴·현재 날짜
    │   ├── AtelierViews.jsx     # 오피스·프로젝트·직원·업무 흐름·결과물 화면
    │   ├── AtelierCards.jsx     # 공통 카드와 업무 요청 폼
    │   ├── AtelierDialog.jsx    # 직원 소개·기획안·파일 등 모달
    │   ├── atelierMock.js       # 프로젝트·업무 기록·예시 결과물 목업
    │   ├── humanAgents.js       # 작업 캐릭터 4명과 리더 1명의 외형·직원 이미지 매핑
    │   ├── AgentLeadership.jsx  # 오케스트레이터 리더와 네 작업 역할 안내
    │   ├── AgentRunMonitor.jsx # 보존된 조회 패널, 현재 사용자 화면에서 미렌더링
    │   ├── AgentRunCreate.jsx  # 보존된 별도 생성 폼, 현재 사용자 화면에서 미렌더링
    │   ├── agentRunProject.js  # 현재 접수한 이름·Run·Step을 실제 프로젝트 카드로 변환
    │   ├── officeTraceEvents.js # Trace 순서·대상 확인과 실제 전달 큐
    │   ├── agentRunDisplayState.js # Run·Step을 캐릭터 표시 상태로 변환
    │   ├── office3dRunController.js # 실제 모드의 역할별 독립 모션 제어
    │   ├── runSuccessCeremony.js # 실제 성공 조건·포즈 배정·중복 방지
    │   ├── officeCelebrationSequence.js # 목업 시연의 세리머니 순서
    │   ├── AgentOffice3D.jsx    # 목업/실제 모드 UI와 장면 수명 주기
    │   ├── AgentOffice3D.css    # 3D 영역에 한정된 스타일
    │   ├── agentOfficeRuntime.js # 재생·카메라·이벤트와 렌더링 수명 주기
    │   ├── office3dScene.js     # 원본 HTML에서 분리한 렌더러·장면·기본 동작
    │   ├── office3dLayout.js    # 책상·의자·소품 배치와 합류 경로
    │   ├── office3dOrchestrator.js # 리더 캐릭터·Run 기반 표시·한글 명찰
    │   ├── office3dAppearance.js # 연결된 긴 머리와 귀 주변 외형 정리
    │   ├── office3dLimbSurface.js # 부드러운 팔·다리 표면과 관절 가중치
    │   ├── office3dLeader.js    # 오케스트레이터 의상 소품과 한글 명패
    │   ├── officeHandoffEvents.js # 보존된 전달 보조 코드, 현재 Trace 큐는 officeTraceEvents 사용
    │   ├── office3dPlannerCamera.js # 파일명은 유지하며 다섯 캐릭터 정면 근접샷에 사용
    │   ├── office3dPerformance.js # 완료 연출 세 가지와 경고 표정
    │   └── useAtelierMotion.js  # 모션·스크롤·움직임 감소 설정
    └── pages/
        ├── LoginPage.jsx        # 로그인 화면과 API 요청
        ├── LoginPage.css        # 사무실 배경·글래스 카드·직원 배치·반응형 스타일
        ├── SignupPage.jsx       # 회원가입 화면과 API 요청
        ├── AtelierDashboard.jsx # 기본 화면·목업 및 실제 Run 조회 공유
        ├── AtelierDashboard.css # atelier 공통 디자인과 반응형 스타일
        ├── DashboardPage.jsx    # 기존 시나리오 선택 화면, 현재 App에서는 사용하지 않음
        ├── DocumentPage.jsx     # PDF 업로드·목록·다운로드
        ├── DocumentPage.css     # 전자문서 화면 스타일
        ├── PipelinePage.jsx     # Run·Step·Trace 조회와 표시
        └── PipelinePage.css     # 파이프라인 화면 스타일
```

`frontend/tests/`에는 상태 변환·실제 모션 제어·세리머니·폴링·이벤트 전달·5인 합류 경로·근접샷·머리와 관절 표면의 Node.js 자동 테스트가 있습니다.

위 구조는 주요 파일만 표시합니다. 기존 싱글 모드 관련 컴포넌트와 이미지도 남아 있지만 현재 `AtelierDashboard`는 멀티 모드만 표시합니다. HTML 디자인 원본과 미리보기 파일은 보관용이며 현재 React 진입 화면과 구분합니다.

기본 첫 화면은 AI 에이전트 회사 오피스입니다. 계정 메뉴에서 여는 로그인·회원가입은 같은 `OfficeAuthLayout`과 업무 장식 스타일을 사용합니다. 3D 오피스의 사진 배경 위에 반투명·배경 흐림 효과를 적용한 중앙 인증 카드를 배치합니다. 기존 오피스 모델로 렌더링한 `public/characters/login-*.png` 다섯 장은 폼 양옆에, 작은 화면에서는 폼 위에 표시합니다. 별도의 파란색 직원 소개 패널과 준비 중 옵션은 표시하지 않으며 인증·오피스 복귀 기능을 유지합니다.

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

- 인증·전자문서·저장된 프로젝트 목록·상세 백엔드는 `http://127.0.0.1:8000`을 사용합니다.
- Orchestrator 프록시 기본값은 `http://127.0.0.1:8001`이며, 실제 Host용 `8002`는 `ORCHESTRATOR_PROXY_TARGET`으로 선택합니다.
- 로그인 토큰을 발급·검증하는 백엔드에는 `JWT_SECRET` 환경변수 설정이 필요합니다. 프론트엔드에 이 비밀키를 넣지 않습니다.
- 백엔드의 교차 출처 요청 허용(CORS) 설정은 `http://localhost:5173`과 `http://localhost:5174`를 허용합니다. 다른 주소로 접속하면 인증·전자문서·프로젝트 요청이 차단될 수 있습니다.

제품 백엔드 실행 방법은 [백엔드 README](../backend/README.md)를 참고합니다. 현재 준비 구분은 제품 백엔드 `8000`, 기존 Orchestrator `8001`, 실제 Multi Agent Host Orchestrator `8002`, 역할별 Agent `8101~8104`입니다. Host는 아직 실행 전이며 포트 지정만으로 실행 준비가 완료되지는 않습니다.

기존 Host 직접 개발 연결은 아래 명령으로 선택할 수 있지만, 제품 JWT·소유권 중계를 완성하는 서비스 전환 명령은 아닙니다. 현재는 기본 8001을 유지합니다. 환경변수를 바꾼 뒤에는 Vite만 재시작해야 하며, 이 명령이 Host를 시작하지는 않습니다.

```bash
ORCHESTRATOR_PROXY_TARGET=http://127.0.0.1:8002 npm run dev
```

변수를 지정하지 않고 `npm run dev`를 실행하면 기본 `http://127.0.0.1:8001`로 돌아갑니다. `/api/v1`과 `/health`는 항상 같은 대상으로 연결됩니다. 이 변수는 Vite 서버 프로세스에서만 읽으며 브라우저용 `VITE_*` 설정이 아닙니다. 8001·8002 서버는 종료하거나 재시작하지 않습니다.

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
      → 파란색 업무 요청창: 이름·요구사항 → 로그인·승인 확인 → 생성 → Run 자동 연결 → 실제 3D 표시
      → 사이드바 하단 대표님 계정 메뉴
          → 로그인 / 다시 로그인 → 기존 로그인·회원가입 → 오피스 복귀
          → 새 업무 맡기기: 같은 파란색 입력창으로 이동
          → 전자문서 관리: 실제 PDF API 화면
```

로그인 여부와 관계없이 기본 오피스를 표시합니다. `DashboardPage.jsx`와 `PipelinePage.jsx`는 코드에 남아 있지만 현재 사용자 메뉴는 Pipeline을 열지 않습니다. 계정 메뉴와 헤더의 새 업무 버튼은 동일한 파란색 입력창으로 이동합니다.

사이드바 하단 대표님 계정 메뉴의 `로그인`으로 기존 LoginPage를 엽니다. 회원가입은 기존 SignupPage를 사용하고, 로그인 성공 시 오피스로 돌아옵니다. 로그인 화면의 `오피스로 돌아가기`로 취소할 수도 있습니다. 토큰이 있는 경우에도 `다시 로그인`을 제공하며, 토큰 존재만으로 유효성을 보장하지 않습니다. 비로그인 상태에서 프로젝트·보관함·전자문서 메뉴를 선택하면 로그인으로 안내합니다.

`App.jsx`는 `sessionStorage`의 `access_token` 존재 여부를 초기 인증 상태로 사용합니다. 오피스 메뉴의 해시 경로와 선택한 저장 프로젝트 ID는 새로고침 후 다시 사용할 수 있습니다. 프로젝트 이름·Run 연결 이력은 저장값으로 복원하지 않고 제품 상세 API가 인증·소유권을 확인한 응답으로 표시합니다. 현재 업무의 Run 모니터·입력 초안·시연 재생 위치는 새로고침으로 복원하지 않습니다. 서버의 Run을 삭제한다는 뜻은 아닙니다.

이 화면 전환은 토큰의 유효성 검증을 대신하지 않습니다. 프론트엔드에서 `/api/auth/me`를 호출하는 초기 인증 확인, 토큰 자동 갱신, 만료 시 자동 로그아웃은 구현되어 있지 않습니다. 보호된 전자문서 요청의 최종 인증·권한 검사는 백엔드에서 수행합니다.

### 회원가입

이메일, 비밀번호, 비밀번호 확인을 입력합니다. 입력값은 `useState`로 관리하며, 비밀번호 확인이 일치하지 않으면 화면에서 제출을 막습니다.

`POST /api/auth/signup`에 `email`과 `password`를 JSON으로 전송합니다. 이메일 형식, 비밀번호 최소 8자, 중복 이메일의 최종 검사는 백엔드에서 수행합니다. 성공하면 로그인 화면으로 이동하고 가입 안내를 표시합니다. 실패하면 백엔드 메시지를 표시합니다.

### 로그인과 로그아웃

`POST /api/auth/login`에 `email`과 `password`를 JSON으로 전송합니다. 성공 응답의 `access_token`을 같은 이름의 `sessionStorage` 키에 저장하고 AI 에이전트 회사 오피스로 이동합니다. `token_type`은 별도로 저장하지 않습니다.

비밀번호 표시·숨김, 요청 중 버튼 비활성화, 실패 메시지와 서버 연결 오류 안내를 제공합니다. 로그아웃하면 `access_token`·요청 초안·현재 오피스의 Run 표시 상태를 비우고 공개 오피스로 돌아갑니다. 로그아웃과 모든 로그인 성공 시 이전 프로젝트 선택 ID도 지웁니다. 별도의 로그아웃 API는 호출하지 않습니다.

로그인 상태 유지, 비밀번호 찾기, Google·GitHub 로그인은 구현되지 않았으며 로그인 화면에도 표시하지 않습니다.

### AI 에이전트 회사 오피스와 3D 시연

현재 오피스는 멀티 에이전트 전용입니다. 파란색 요청창의 프로젝트 이름·요구사항·업무 시작 버튼이 실제 생성의 유일한 사용자 진입점입니다. 별도 생성 폼·Run 조회 패널·UUID 입력·개발용 상세 조회 카드와 홈의 목업 진행률·결재 카드는 표시하지 않습니다. 생성 중·차단 사유·실패·최종 상태는 요청창 안에서 안내하며 실패를 목업 성공으로 바꾸지 않습니다. 프로젝트·보관함의 실제 연결 범위는 7절을 참고합니다.

화면에는 오케스트레이터·계획에이전트·개발에이전트·QA에이전트·보안에이전트의 다섯 캐릭터가 있습니다. 리더는 오케스트레이터이며 계획 에이전트는 요청 분석·기능과 작업 순서 정리를 담당합니다. 오케스트레이터는 실제 관리 서버를 캐릭터로 표현한 것이며 다섯 번째 `AgentRole`이나 작업 Step, LLM Agent를 추가한 것이 아닙니다. 화면의 리더 표시는 `User.role`이나 JWT 권한과 무관합니다. 날짜와 요일은 `Asia/Seoul` 기준으로 자동 갱신합니다.

우리 직원 카드와 소개 모달에는 현재 3D 모델을 정면 렌더링한 `public/characters/office-*.png` 다섯 장을 사용합니다. 의상·안경·머리·명찰을 3D 캐릭터와 맞췄으며 기존 원본 PNG·SVG는 보존했습니다. 모델 외형을 나중에 바꾸면 정적 카드 이미지도 다시 렌더링해야 합니다.

오피스의 직원 카드·업무 보고 영역 대신 `AgentOffice3D`를 표시합니다. 기존 테이블을 유지하며 앞쪽 리더석의 오케스트레이터와 2열로 앉은 네 작업 캐릭터가 마주 봅니다. 책상·의자·캐릭터의 비율과 좌석 간격은 함께 유지하면서 배경 사진의 빈 바닥에 맞춰 배치했습니다. 배경은 충돌 메시가 없는 사진이므로 모든 수동 카메라 각도에 대한 물리적 충돌 검증을 의미하지 않습니다. 보안 캐릭터 몸통과 겹치던 오른쪽 수납장 그룹 `shared_sideboard_1`은 숨겼고 배경 식물은 유지합니다.

계획 에이전트를 포함한 다섯 캐릭터 모두 정면 근접샷을 사용합니다. 근접샷에서는 다른 캐릭터·책상·공용 장식을 잠시 숨기고 전체 보기에서 복원합니다. 개발자는 노트북에 얼굴이 가리지 않도록 촬영 높이를 조정했습니다. 한글 명찰과 리더 명패는 시스템 글꼴 텍스처를 사용하며, 오케스트레이터의 깻잎 앞머리와 긴 뒷머리는 하나의 곡면으로 연결했습니다. 팔꿈치·무릎에는 촘촘한 표면과 둥근 연결부를 적용하고 기존 리그·동작은 유지합니다.

3D 시연에는 재생·일시정지, 타임라인 이동, 배속, 전체 보기·수동 카메라, 전체 화면, 에이전트 상태 팝업, 오류 연출 선택이 있습니다. 다섯 캐릭터의 완료 연출은 기존 협업 포즈, 서류 던지기·박수, 달려와 원형으로 모이는 포옹의 세 가지입니다. 경고 시 곤란·화남·진땀 표정을 보여줍니다. 모션 끄기와 `prefers-reduced-motion`을 반영하며 화면 이탈 시 렌더러·이벤트·그래픽 자원을 정리합니다.

105초 목업의 상태·완료·오류는 시연용이며 재생 컨트롤을 그대로 유지합니다. 목업 재생은 실제 Run을 생성하지 않습니다. 파란색 업무 시작은 승인된 실제 생성 경로에 연결되지만 현재 승인 설정 미공급으로 POST가 차단됩니다. 3D 역할 버튼은 **1 오케스트레이터 → 2 계획 → 3 개발 → 4 QA → 5 보안**이며 내부 Agent 인덱스·좌석·카메라·모션은 변경하지 않았습니다.

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

### 데이터 흐름과 사용자 화면

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

### 신규 Run 생성과 승인 Configuration

파란색 요청창의 `업무 시작`이 실제 실행 요청입니다. `useOfficeRunSubmission.js`가 기존 `agentRunSubmission.js`를 재사용하며 명시적인 클릭 없이 자동 제출하지 않습니다. 사용자가 입력한 프로젝트 이름과 요구사항은 기존 `requestText`에 함께 전달합니다. 서버에 별도 `projectName` 필드를 추가하지 않았습니다.

1. `AtelierDashboard`의 `approvedRunSubmission` prop으로 `{ approved: true, scenarioId, configuration }`을 받습니다. `scenarioId`는 등록된 UUID이며 `configuration`은 승인된 공개 `RunConfiguration` 전체입니다.
2. `agentRunSubmission.js`가 허용 필드·필수값·민감정보 패턴을 검사하고, `/api/v1/scenarios`에서 해당 시나리오 등록 여부를 확인합니다. 승인 설정이 없으면 POST하지 않습니다.
3. `POST /api/v1/runs`에 `scenarioId`, 프로젝트 이름과 요구사항을 포함한 `requestText`, `configuration`을 전달합니다. `approved`와 내부 `confirmed`는 브라우저 제출 조건이며 서버 승인 증명으로 보내지 않습니다.
4. 성공 응답의 `run.runId`를 자동 선택하고 실제 모드로 전환하면서 `traceFromStart: true`를 유지합니다. `SCHEDULED`는 실행 예약이며 완료·성공 판정이 아닙니다. `NOT_CONFIGURED`는 생성됐지만 실행기가 연결되지 않은 상태로 표시합니다.
5. 진행 중 중복 제출을 막고 실패 시 기존 Run 화면을 유지합니다. 네트워크 장애 등 생성 여부가 불명확한 경우 재전송을 차단하여 중복 Run 생성을 피합니다.

**현재 실제 생성은 차단되어 있습니다.** `App.jsx`가 `approvedRunSubmission`을 공급하지 않아 기본값은 `null`입니다. Host 내부의 `approved_run_configuration(platform)`은 공개 설정을 반환하지만 HTTP 조회 API가 아닙니다. 기존 `GET /api/v1/runs/{runId}/configuration`은 과거 Run의 고정된 설정이며 신규 Run의 현재 승인 템플릿으로 대신 사용할 수 없습니다.

공개 설정에는 모델·실행 제한·컨테이너 이미지 digest·dependency lock hash·네트워크 정책·시작 commit/snapshot·검증 참조를 포함합니다. OpenAI API 키·A2A 토큰·비밀번호·Host 내부 경로와 runtime/tools/source 원본 파일은 브라우저에 공급하지 않습니다. 클라이언트 필드 검사는 서버의 공개 응답 선별과 승인 검증을 대신하지 않습니다.

실제 Host는 `validate_configuration()` 및 제출 검증기를 통해 모델·실행 제한·환경·Source·검증 참조를 서버 승인값과 대조하고 비밀정보를 검사합니다. 일반 Orchestrator의 기본 `submission_validator=None`은 이 승인을 보장하지 않습니다. 팀장과 **승인된 공개 설정의 조회 또는 서버 측 참조 계약, 접근 권한, 설정 변경 시 재검증 방식**을 확정해야 합니다. 해당 API는 아직 구현되지 않았으며 모델·예산·도구 이미지·Source Snapshot 값을 임의로 채우지 않습니다.

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

### 리더 표시와 실제 문서 전달 연출

네 작업 캐릭터의 상태는 Step에서 가져오고, 오케스트레이터의 표시는 Run 상태를 사용합니다. `RECEIVED`는 요청 접수, `WAITING_INPUT`·`HUMAN_REVIEW`는 입력 대기, `FINISHED`·`ABORTED`는 실행 관리 종료로 표시합니다. 나머지 정상 활성 Run은 실행 관리 중으로 표시하며 별도 오케스트레이터 Step은 만들지 않습니다.

실제 전달은 `officeTraceEvents.js`와 `agentRunEventFeed.js`를 사용합니다. `A2A_MESSAGE_SENT`가 같은 Run의 유일한 Step과 연결되고 `actor=Orchestrator` 및 코드 버전 조건을 만족할 때만 리더 → 해당 역할의 문서 전달을 만듭니다. 대상이 불명확하면 임의 전달하지 않으며 Task 상태 변경만으로 반대 방향 전달을 생성하지 않습니다. QA·Security의 조건이 맞는 병렬 전달은 유지합니다.

화면의 20건 Event 페이지와 별개인 Trace 커서가 최대 500건씩 조회합니다. 서버의 `sequence` 기준 응답 배열 순서를 그대로 보존하고 `eventId`로 중복을 제거합니다. 클라이언트가 발생 시각이나 역할로 재정렬하지 않습니다.

- 방금 생성한 Run은 `traceFromStart: true`로 최초 `offset=0`부터 수집해 생성 응답 이전에 저장된 이벤트도 포함합니다.
- 기존 Run 수동 조회 시 과거 이벤트를 생략하는 내부 정책·테스트는 유지합니다. 현재 사용자 화면에는 UUID 입력·수동 선택 UI가 없습니다.
- 페이지 묶음 전체가 성공한 뒤에만 커서·중복 처리 상태를 반영합니다. 실패·취소 시 기존 상태를 보존하여 재시도하고, Run 변경 시 이전 큐를 정리합니다.
- 내부 Event 페이지 처리와 Trace 커서는 독립적이며 현재 사용자 화면에는 페이지 조작 UI가 없습니다. 3초 폴링과 연출 큐 때문에 실제 이벤트 시각과 애니메이션 시작 시각은 다를 수 있습니다.

문서 아이콘은 확인된 호출을 시각화하며 실제 파일 바이트 전송의 증거는 아닙니다. 기존 105초 목업의 전달 시간표는 실제 모드에 사용하지 않습니다.

### 3초 자동 갱신

실제 오피스와 프로젝트·결과물 보관함에서 선택된 Run을 자동 조회합니다. 완료된 조회 묶음 후 3초 뒤 다음 조회를 예약하며 진행 중인 같은 요청은 공유합니다.

- 홈 실제 모드에서는 기존 `collectEvents`로 Trace를 수집하고 결과를 함께 조회합니다.
- 프로젝트·보관함에서는 `includeResults`로 Artifact·Issue 조회를 활성화합니다. 3D Trace 수집과 결과 목록 조회를 분리하여 같은 Hook·API를 재사용합니다.
- `FINISHED`·`ABORTED` 또는 `SINGLE_AGENT` 확인 시 반복 조회를 중지합니다. `FINISHED` 자체가 성공은 아닙니다.
- 핵심 조회 실패 시 마지막 데이터를 유지하고 재시도 간격을 6·12·24·최대 30초로 늘립니다. Artifact·Issue 오류는 독립적으로 처리합니다.
- 프로젝트의 Run 조회 오류와 보관함의 Artifact 조회 오류에는 해당 화면의 `다시 불러오기`를 제공합니다. 이 동작은 GET 재조회이며 새 Run을 만들지 않습니다.
- 기존 Run 조회·일시정지·페이지 조작 버튼은 사용자 화면에서 제거했습니다. 내부 취소·상태 정리 로직은 유지하며, 화면별 활성 조건·Run 변경·언마운트에 맞춰 요청과 타이머를 정리합니다.

### 실제 성공 세리머니

실제 모드에서 조회가 정상 완료되고, Configuration의 `architecture=MULTI_AGENT`, Run의 `status=FINISHED`, `verdict=SUCCESS`를 모두 확인해야 합니다. Step 하나의 `SUCCEEDED`, `FAIL`, `UNVERIFIED`, `HUMAN_REVIEW`, `null` 판정으로 전체 성공 연출을 실행하지 않습니다. 3D 장면과 캐릭터가 준비되고 모션이 허용되어야 재생합니다.

관측 중인 Run이 미종료 상태에서 최종 성공으로 바뀌면 자동 재생 후보가 됩니다. 처음부터 성공한 과거 Run을 조회한 경우에는 자동으로 재생하지 않고 사용자가 성공 포즈 재생 버튼을 누릅니다. 오류나 기록 저장 실패 시 순서를 임의로 소비하지 않습니다.

서로 다른 성공 Run에 `team → cheer → hug`를 하나씩 배정하며, 네 번째부터 다시 `team`으로 순환합니다. 재생 직전에 Run UUID·포즈·다음 순서를 함께 저장하여 같은 Run의 재조회·새로고침·재진입으로 중복 소비하지 않습니다. 이 기록은 재생 시작 기록이므로 중간에 화면을 나간 경우에도 자동으로 다시 재생하지 않습니다. 여러 탭의 동시 실행에 대한 잠금은 없으며 브라우저 한 탭 기준입니다.

| 용도 | `localStorage` 키 |
| --- | --- |
| 기존 목업 시연 순서 | `atelier.office3d.ceremony-order.v1` |
| 실제 성공 Run 이력·순서 | `ai-agent-company.office3d.success-ceremonies.v1` |

두 기록과 초기화 기능은 독립적입니다. 사용자가 명시적으로 초기화하지 않는 한 기록을 지우지 않습니다. 저장된 순서는 실제 LLM 실행 검증의 증거가 아닙니다.

### 프로젝트·결과물 보관함의 실제 데이터

현재 화면의 Run은 `useOfficeRunSubmission`의 이름·요구사항과 생성 응답을 `submittedRunProject()`로 연결합니다. 기존 카드·검색 디자인을 재사용하며 Run 상태와 역할별 Step 상태, QA·Security 병렬 상태를 유지합니다. 저장된 프로젝트 목록은 별도로 제품 `GET /api/projects`에서 읽으며, 프로젝트 ID·이름·시나리오 ID·생성일만 표시합니다. 실제 근거가 없는 진행률 숫자·진행 막대는 표시하지 않습니다.

제품 DB의 저장된 프로젝트 카드를 선택하면 `GET /api/projects/{projectId}`로 상세와 `runs` 연결 이력을 조회합니다. 여러 Run의 `hostKey`, `runId`, `configurationId`, `linkedAt`을 표시하고 빈 이력·조회 오류를 구분합니다. 선택 ID는 새로고침 후 재조회에 사용하며 실제 Host 상태나 과거 Trace를 복원하지 않습니다. **Run 모니터 연결은 현재 화면에서 생성한 Run만** 지원합니다. 현재 Run 카드는 같은 Run의 3D 오피스로 돌아가며 제품 Project와 임의로 연결하지 않습니다.

| Host API | 제공 범위와 현재 UI 사용 |
| --- | --- |
| `GET /api/v1/runs/{runId}/artifacts` | `runId`, `artifacts`. 산출물 JSON과 실행 설정 Artifact를 반환. 기존 조회 한 번으로 팝업 요약과 보관함용 공개 목록을 생성 |
| `GET /api/v1/runs/{runId}/artifacts/{artifactId}` | 해당 Artifact JSON. 개별 조회 계약은 있으나 목록 응답에 필요한 정보가 있어 현재 UI는 별도 요청하지 않음 |
| 소스 파일 원문·바이너리 다운로드 | 현재 HTTP API 미제공. UI에 파일 원문 보기·다운로드 버튼을 만들지 않음 |

`agentArtifactCatalog.js`는 등록 URI의 마지막 경로 부분을 등록명으로 사용하고, 실제 응답의 종류·생성 주체·등록 시각·산출물/코드 버전·보고서 요약을 표시합니다. 이름이 없으면 파일 이름 미제공으로 안내합니다. 목록에 있다는 사실은 등록됨을 뜻하며 검증 성공이나 다운로드 가능을 뜻하지 않습니다.

`SOURCE`는 소스 Snapshot 등록 정보이며 내부 소스 파일 목록·원문이 아닙니다. `CHANGE_REPORT.fileChanges`의 경로와 작업 종류는 변경 보고서 내용으로 표시하며 현재 파일의 존재를 보장하지 않습니다. QA/보안 보고서도 확인된 결과만 요약합니다. `artifact://` URI를 HTTP 링크로 바꾸거나 임의의 소스 파일·다운로드를 생성하지 않습니다.

기존 `getAgentRunArtifacts()`의 팝업용 `artifacts`는 유지하고 공개 `catalog`를 추가해 같은 응답을 재사용합니다. 원본 Configuration·manifest·Host 내부 경로·민감 로그는 목록 상태에 복사하지 않습니다. 조회 실패는 빈 결과와 구분하고 같은 Run의 마지막 정상 목록이 있으면 유지합니다. 재조회는 GET만 수행하며 Run이 바뀌면 이전 목록을 섞지 않습니다.

보관함의 등록 정보 보기는 안전한 메타데이터·요약을 펼치는 기능입니다. 제품 백엔드의 PDF 다운로드 API와 Agent의 코드 산출물은 별개의 계약입니다. 사용자 화면에 예시 파일을 실제 결과물처럼 표시하지 않습니다.


## 8. 보존된 개발용 화면

`PipelinePage.jsx`, `AgentRunCreate.jsx`, `AgentRunMonitor.jsx`는 코드에 남아 있지만 현재 사용자 메뉴·Dashboard는 이 화면들을 열거나 렌더링하지 않습니다. 계정 메뉴의 이전 Pipeline 진입은 같은 파란색 요청창으로 이동하는 `새 업무 맡기기`로 바뀌었습니다.

보존된 Pipeline의 직접 POST는 고정 `scenarioId`·`requestText`를 사용하며 승인 Configuration을 공급하지 않습니다. 현재의 승인된 실제 실행 경로로 사용하지 않습니다. Run·Step·Event를 조회하는 내부 기능의 존재와 최종 사용자 UI에 노출된 기능을 구분합니다.

## 9. API 연동 구조

### 인증·전자문서·프로젝트 백엔드

현재 코드에는 `http://127.0.0.1:8000` 주소가 직접 지정되어 있으며, 별도의 프론트엔드 API 주소 환경변수를 사용하지 않습니다.

| 메서드 | API 경로 | 화면에서 사용하는 데이터 |
| --- | --- | --- |
| GET | `/api/projects` | 본인 프로젝트의 `projectId`, `name`, `scenarioId`, `createdAt`, `updatedAt` |
| GET | `/api/projects/{projectId}` | 프로젝트 공개 정보와 `runs` 연결 이력 |
| POST | `/api/auth/signup` | 요청의 `email`, `password`, 응답의 가입 메시지 |
| POST | `/api/auth/login` | 요청의 `email`, `password`, 응답의 `access_token` |
| POST | `/api/documents` | 요청의 `file`, `assignee_id`, 응답의 문서 메타데이터 |
| GET | `/api/documents?view=submitted` | 응답의 `documents` 배열 |
| GET | `/api/documents?view=assigned` | 응답의 `documents` 배열 |
| GET | `/api/documents/{document_id}/download` | PDF 바이너리 응답 |

문서·프로젝트 목록·상세 API 요청에는 저장된 토큰을 다음 형식의 헤더로 붙입니다.

```http
Authorization: Bearer <access_token>
```

문서 제출자 ID를 클라이언트에서 지정하지 않습니다. 제출·배정 목록의 조회 기준 사용자와 다운로드 권한은 서버가 JWT로 확인합니다. 서버 내부 저장 경로나 저장 파일명은 문서 API 응답과 화면에 노출하지 않습니다.

### Orchestrator와 개발용 프록시

`agentRuns.js`와 `PipelinePage`는 상대 경로로 요청합니다. `vite.config.js`는 `/api/v1`로 시작하는 요청과 `/health` 요청을 경로 변경 없이 `ORCHESTRATOR_PROXY_TARGET`으로 전달하며, 미지정 시 `http://127.0.0.1:8001`을 사용합니다. 현재 화면에서는 `/health`를 호출하지 않습니다.

```text
브라우저의 /api/v1/runs 요청
  → Vite 개발 서버
  → 선택한 Orchestrator /api/v1/runs (기본 8001, 실제 Host 준비 대상 8002)
```

| 메서드 | API 경로 | 화면에서 사용하는 응답 구조 |
| --- | --- | --- |
| POST | `/api/v1/runs` | `run`, `firstStep`, `dispatchStatus` |
| GET | `/api/v1/runs/{runId}` | Run 객체 자체 |
| GET | `/api/v1/runs/{runId}/steps` | `runId`, `steps` |
| GET | `/api/v1/runs/{runId}/events?limit=20&offset=0` | `runId`, `events`, `total`, `limit`, `offset` |
| GET | `/api/v1/runs/{runId}/configuration` | 실제 실행 방식 확인용 `configuration.architecture`만 사용 |
| GET | `/api/v1/scenarios` | 생성 전 등록된 scenarioId 확인 |
| GET | `/api/v1/runs/{runId}/artifacts` | 팝업 요약 `artifacts`와 보관함용 `catalog`로 변환 |
| GET | `/api/v1/runs/{runId}/issues` | 실제 Issue 요약, 팝업에서 사용 |

현재 Pipeline 요청에는 인증·전자문서용 JWT 헤더를 붙이지 않습니다. 두 서버의 인증 방식이 통합되었다고 가정하지 않습니다. 배포 환경의 API 주소와 프록시 구성은 별도 통합 작업이 필요합니다.

제품 서버에는 `/api/v1/runs/{runId}`와 `/steps`·`/events`·`/issues`·`/artifacts`의 GET 중계 5개가 있습니다. 기존 JWT와 `local-multi` ProjectRun의 Project 소유권을 검사하고, Host 인증이 검증되지 않아 실통신은 503으로 막습니다. 최종 서비스는 **브라우저 → 제품 8000 → Host 8002**이며 현재 직접 8001 경로와 구분합니다.

전환 전에는 `PRODUCT_RUN_API_READY=false`를 유지합니다. 제품에는 Run POST·`/api/v1/scenarios`·Run `/configuration`·`/health` 중계가 아직 없으므로 프록시 대상만 8000으로 변경하지 않습니다. 안전한 승인 설정 공급, Submission 중복 방지, Run 생성과 ProjectRun 저장 및 Host 서비스 인증을 합의·구현한 뒤 보호 전송기와 프록시를 함께 전환해야 합니다. 제품 프로젝트 목록·상세 GET은 이미 8000에 직접 연결되어 있어 이 차단과 별개입니다.

### 공통 API 함수와 Run 생성의 연결 범위

`src/api/agentRuns.js`의 함수는 다음과 같습니다. 조회 함수는 Dashboard의 공통 Hook에서 사용하며, `signal`로 요청 취소를 지원합니다.

| 함수 | 역할 |
| --- | --- |
| `createAgentRun(scenarioId, requestText, architecture = 'MULTI_AGENT', { signal, configuration, approved = false } = {})` | 승인된 공개 설정이 있는 멀티 Run만 생성 |
| `getAgentRun(runId, { signal } = {})` | Run 객체 조회 |
| `getAgentRunSteps(runId, { signal } = {})` | Step 목록 조회 |
| `getAgentRunEvents(runId, { limit = 100, offset = 0, signal } = {})` | Event 페이지 조회, `limit` 1~500 |
| `getAgentRunArchitecture(runId, { signal } = {})` | Configuration 조회 후 `{ runId, architecture }`만 반환 |

생성 함수는 `MULTI_AGENT`, `approved=true`, 유효한 공개 Configuration을 요구합니다. 승인 옵션이 없는 기존 2·3개 인자 호출은 POST 전에 차단됩니다. `getAgentRunScenarios()`는 등록 시나리오를 조회합니다. 생성 UI는 서버 원본 오류 대신 안전한 안내를 표시합니다.

현재 파란색 업무 시작은 `useOfficeRunSubmission`을 통해 공통 생성 함수를 사용합니다. 승인 설정 미공급으로 실제 POST는 차단됩니다. 별도 생성·조회 패널과 Pipeline 진입은 사용자 화면에서 제거했으며, 승인 공급 계약과 프로젝트·보관함의 범위는 7절을 참고합니다.

## 10. 구현 현황과 남은 검증

### 현재 코드에 구현된 기능

- 회원가입·로그인 API 연결, 토큰 저장과 로그아웃, 로그인 없는 기본 오피스 진입이 구현되어 있습니다.
- PDF 업로드, 제출·배정 목록, 문서별 다운로드가 제품 백엔드 API에 연결되어 있습니다.
- iframe 없는 React 멀티 전용 오피스, 오케스트레이터 리더와 네 작업 캐릭터·직원 카드 이미지·5인 배치·정면 카메라가 구현되어 있습니다.
- 생성 응답의 Run ID 자동 선택과 최초 Trace 수집, Run·Step·Event·실행 방식 조회·부분 오류 처리가 구현되어 있습니다. UUID 입력·조회 패널은 사용자 화면에 없습니다.
- 실제 모드의 역할별 3D 모션, QA·Security 병렬 작업, 3초 폴링·오류 재조회와 실제 성공 세리머니가 연결되어 있습니다.
- 파란색 요청창으로 실제 생성 경로를 통합했고 프로젝트·보관함은 같은 Run과 조회 결과를 재사용합니다. 승인 설정 공급은 아직 미연결이며 실제 POST는 차단됩니다.

### 목업 또는 미연동 항목

105초 시연과 직원 소개·업무 흐름의 역할 예시는 목업으로 유지합니다. 프로젝트는 저장된 목록과 현재 Run을, 결과물 보관함은 현재 Run의 산출물을 표시하며 예시 프로젝트·진행률·파일 다운로드를 사용하지 않습니다. `atelierMock.js`와 보존된 예시 파일 코드는 실제 실행 증거가 아닙니다.

실제 5인 팝업에는 조회 가능한 Artifact·Issue·QA/Security Report의 짧은 요약이 연결되어 있습니다. 보관함에도 공개 등록 정보·보고서 요약을 연결했습니다. 소스 파일 원문·다운로드, 저장된 Run의 실행 상태·3D 모니터 복원, Usage·Cost 및 서버 Run 취소·재개 UI는 추가 계약 또는 연결이 필요합니다. 싱글 UI는 제공하지 않으며 전자문서 화면은 Agent 파이프라인을 실행하지 않습니다.

로그인 상태 유지·소셜 로그인·비밀번호 찾기·초기 토큰 유효성 확인·토큰 자동 갱신과 담당자 목록·사용자 이름 조회는 구현되어 있지 않습니다. 제품 서버에는 JWT·ProjectRun 소유권 기반 Run GET 중계가 구현되어 있지만 Host 실통신과 프론트엔드 보호 전송기는 아직 비활성입니다. 제품 프로젝트 생성·수정 API의 React 연결과 저장된 Run 선택 후 모니터 복원은 다음 단계입니다. 프로젝트 상세·저장된 연결 이력 표시와 선택 프로젝트 재조회 복원은 구현되어 있습니다.

### 검증 기록과 범위

`frontend/tests/`에는 상태 변환, 캐릭터별 실제 상태 제어, 성공 세리머니, 폴링의 Mock 기반 테스트가 있습니다. 이전 UI 작업에서 브라우저로 목업·실제 표시 모드와 세리머니 포즈 등을 검증했지만 실제 LLM이 생성한 성공 결과를 검증한 것은 아닙니다.

기존 문서에 남아 있는 2026-11-09 검증 기록: `node --test tests/*.test.mjs`의 118개 테스트와 `npm run build`, `npm run lint`, `git diff --check`가 통과했습니다. 앞선 3D·직원 카드 작업에서는 격리된 브라우저 Mock 검증 48개로 정면 촬영·카드 이미지·병렬 작업·재작업 전달·세리머니를 확인했습니다. 당시 화분 제거는 코드로 확인했으며 브라우저 실행은 승인되지 않아 새 브라우저 검증을 수행하지 않았습니다. 실제 서버 API·새 Run 생성·LLM·Docker 실행은 수행하지 않았으며, 이 기록을 실제 Agent 전체 실행 성공으로 해석하지 않습니다.

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


### 앞으로 필요한 통합 검증

제품 웹 UI를 포함한 실제 LLM·Docker End-to-End 실행은 아직 검증 전입니다. 승인 configuration, 실제 Multi Host, LLM 모델·API Key의 서버 측 설정과 Orchestrator의 포트·DB 경로를 먼저 합의해야 합니다. 실행 계약은 [역할 1·2 인계 안내](../docs/45-owned-platform-handoff.md)와 현재 Orchestrator 코드를 함께 확인합니다.

환경에 따라 `dispatchStatus=NOT_CONFIGURED`로 전달이 미설정이거나 Step이 `PENDING`에 머무를 수 있습니다. 승인 설정 검사가 활성화된 환경에서는 `OWNED_AGENT_CONFIGURATION_INVALID`와 HTTP 422로 생성이 거부될 수 있습니다. Run 접수나 조회 성공만으로 실제 Agent 실행 성공을 판단하지 않습니다.

실제 서버·브라우저에서는 다음을 추가 확인해야 합니다.

- 승인된 Run 생성 연결과 제품 JWT·Run 접근 권한의 경계
- 폴링 중 병렬 실행·재시도·통신 장애·종료 상태 반영과 세리머니 1회 재생
- 프로젝트·보관함 이동 중 상태 갱신과 장시간 실행 시 리소스 정리
- 토큰 만료·재로그인, 사용자별 문서 권한, 한글 파일명 다운로드와 원본 바이트 일치

구현 완료 목록은 현재 코드의 연결 범위를 뜻하며 위 통합 검증까지 모두 완료되었다는 의미는 아닙니다.

### 제품 JWT 연결 준비 — 3단계 C (2026-11-13)

`productRunRequest.js`에 제품 API용 보호 전송기를 추가했습니다. 기존 로그인 화면이 저장하는 `sessionStorage.access_token`을 요청 시 읽어 `Authorization: Bearer` 헤더로 전달합니다. 토큰이 없거나 저장소 접근이 실패하면 요청 전에 `AUTH_REQUIRED`로 차단합니다. 401은 원본 본문을 읽지 않고 고정 로그인 안내로 변환하며, 토큰 갱신·자동 재전송·로그인 화면 강제 이동은 하지 않습니다. 기존 업무 상태 안내에는 인증 오류일 때 로그인 필요 문구를 표시합니다.

현재 `PRODUCT_RUN_API_READY=false`입니다. 기존 8001용 Run API는 JWT를 읽거나 추가하지 않고 유지하며, 제품 인증 흐름은 활성화하지 않았습니다. 보호 전송기 자체도 기본값으로 전송을 차단합니다. 테스트에서만 Mock fetch와 명시적인 준비 옵션을 주입합니다. Host 서비스 토큰은 프론트엔드에서 읽거나 전달하지 않습니다.

제품 전환 후에는 보호 전송기가 실패해도 익명 요청으로 fallback하지 않습니다. 요청 경로는 기존 상대 `/api/v1/...`를 사용하며 리다이렉트를 거부합니다. 보호 요청의 원본 오류·응답 오류 본문·인증 헤더를 오류 객체나 로그에 복사하지 않습니다.

새 `productRunAuth.test.mjs`의 Mock 테스트 3개가 통과했습니다: JWT 헤더 구성 및 현재 8001 경로의 JWT 미전송, 토큰 없는 보호 Run·Step·Trace 요청 차단, 401의 안전한 로그인 안내. 기존 전체 테스트·브라우저 검증은 반복하지 않았고 실제 API 요청도 하지 않았습니다.

실제 연결 전에는 Host 서비스 인증, 제품 중계의 Configuration·Scenario 및 생성 계약, 제품 JWT/ProjectRun 소유권 연결, Vite의 제품 8000 전환을 준비한 뒤 보호 전송기를 활성화해야 합니다. 3단계 C 당시 로그인 진입은 미연결이었으며, 아래 3단계 D에서 연결했습니다. Vite·3D 디자인·백엔드·DB는 변경하지 않았습니다.

### 기존 오피스 로그인 진입 — 3단계 D

기존 계정 메뉴→LoginPage→SignupPage/로그인→오피스 흐름을 연결했습니다. 프로젝트 이름과 요구사항은 App의 메모리에만 보관하여 로그인·회원가입·취소·복귀 시 복원합니다. 새로고침이나 로그아웃 후 초안을 영구 복원하는 기능은 아닙니다. 로그인 성공 후 업무를 자동 제출하지 않으며 승인 Configuration이 없으면 기존 차단도 유지됩니다.

`useOfficeRunSubmission`은 UI 버튼 상태와 별개로 제출 직전에 기존 토큰 존재를 확인하여, JWT가 없으면 시나리오 조회·Run POST 전에 차단하고 로그인 필요를 안내합니다. 토큰 유효성·소유권의 최종 판정은 제품 API의 책임이며 401은 기존 재로그인 안내를 사용합니다. 제품 전송기의 비활성 상태와 8001 대상 Vite 설정은 유지했습니다.

`officeLoginUi.test.mjs`의 격리 Mock UI 테스트 1개에서 요청한 3가지 흐름을 확인했습니다: 비로그인 계정 메뉴 진입, Mock 로그인 성공 후 오피스 복귀와 초안 보존, JWT 없는 UI·직접 Hook 제출 차단. 기존 로그인 실패와 회원가입 화면 이동도 같은 테스트에서 확인했습니다. HTTP는 Mock 로그인 응답만 허용하고 3D 렌더러는 대체했으므로 실제 서버·Run 생성·3D 시연 검증이 아닙니다. 기존 업무/보관함 테스트 fixture에는 합성 토큰만 추가했으며 해당 기존 테스트는 재실행하지 않았습니다.

```bash
node --test tests/officeLoginUi.test.mjs
```

### 제품 프로젝트 목록 조회 — 3단계 E

프로젝트 메뉴 진입과 재시도 시 `useSavedProjects → getProjects → GET http://127.0.0.1:8000/api/projects`로 조회합니다. 기존 로그인과 같은 제품 주소를 사용하며 Vite의 8001 Orchestrator 프록시와 별개입니다. `sessionStorage.access_token`을 Bearer 헤더에 넣고, 토큰 누락 시 요청 전에 차단합니다. 소유권 필터는 기존 제품 서버가 수행합니다. 조회 중 세션 변경·메뉴 이탈 후 도착한 응답은 적용하지 않으며 오류 시 저장된 목록을 비웁니다.

로딩, 빈 목록, 401 로그인 필요, 403 권한 없음, 404 목록 API 없음, 네트워크/서버 오류를 구분합니다. 오류 응답 원문과 토큰은 표시하지 않습니다. 401 안내의 로그인 버튼은 기존 LoginPage 진입을 재사용합니다. 목록 응답에는 실행 상태가 없으므로 저장된 카드에는 Agent·진행률·최종 판정을 추가하지 않습니다. 현재 화면의 Run 카드는 별도로 유지하며 저장된 프로젝트 ID와 Run ID를 같은 값으로 취급하지 않습니다. 생성 시각은 서버가 준 날짜 부분을 표시하고 임의 시간대 변환은 하지 않습니다.

`node --test tests/projectList.test.mjs`의 Mock 테스트 3개가 통과했습니다: JWT가 있는 목록의 실제 카드 표시·현재 Run 카드 유지, 빈 목록·로딩 구분, JWT 누락·401 및 다른 세션 응답 폐기. View만 Node에서 렌더링했으며 서버·브라우저·3D는 실행하지 않았습니다. 기존 프로젝트/보관함 UI fixture에는 빈 제품 목록 응답만 추가했고 기존 테스트는 반복하지 않았습니다.

3단계 E의 목록 연결에 이어 아래 F·G에서 상세와 선택 프로젝트 재조회 복원을 구현했습니다. 프로젝트 생성·수정 UI, 저장된 Run 선택 후 Host 조회·3D 복원과 Run POST의 제품 중계는 후속 범위입니다.

### 프로젝트 상세·선택 복원 — 3단계 F·G

`SavedProjectDetail → loadSelectedProject → getProject`가 기존 제품 JWT로 `GET http://127.0.0.1:8000/api/projects/{projectId}`를 조회합니다. 상세 대화상자는 이름·시나리오·생성일과 서버가 반환한 여러 Run 연결 이력을 표시합니다. `configurationId`는 서버 문자열로 취급하며 UUID로 가정하지 않습니다. Host 상태·성공 여부는 조회하거나 추측하지 않습니다.

상세 조회 성공 후 `sessionStorage`의 `ai-company.selected-project-id`에 프로젝트 ID만 저장합니다. 새로고침 시 이 값은 조회 후보로만 사용하고, 제품 서버의 인증·소유권 검사를 통과한 최신 상세 응답으로 대화상자를 복원합니다. 이름·이력·소유자 정보는 브라우저에 저장하지 않습니다. JWT 누락은 요청 전 차단하고, 401·403·404는 이전 선택을 정리합니다. 네트워크 오류는 빈 이력과 구분하여 재시도를 안내합니다.

로그아웃과 모든 로그인 성공 시 선택을 비우며 취소·세션 변경 전 요청의 늦은 응답은 적용하지 않습니다. 현재 진행 중인 Run 모니터는 프로젝트 상세 조회와 분리되어 있습니다. 저장된 Run ID로 Host GET을 호출하거나 과거 Trace를 재생하지 않으며 ProjectRun 기록을 새로 만들지 않습니다.

구현 당시 `projectDetail.test.mjs` 3개와 `projectSelection.test.mjs` 3개가 각각 PASS했습니다. 상세·복수/빈 이력, JWT 누락·401/404, ID만 저장한 새로고침 재조회, 로그인·로그아웃 격리와 늦은 응답 방지를 Mock으로 확인한 기록입니다. 실제 Host 또는 브라우저 전체 검증이 아니며 이번 문서 갱신에서 재실행하지 않았습니다.

### 로그인·회원가입 디자인과 오피스 표시 — 2026-11-14

로그인·회원가입은 `OfficeAuthLayout reviewAccents`를 함께 사용합니다. 첫 진입은 계속 공개 오피스입니다. `MY AI COMPANY`, 계정·제목·소개와 로그인 안내 문구를 가운데 정렬했습니다. 배경은 `/agent-office-3d/office-background.png`이며 중앙 카드는 반투명 배경과 `backdrop-filter: blur(5px)`를 사용합니다. 배경이 지나치게 드러나지 않도록 카드 불투명도를 조정했고, 필터 미지원 환경에는 더 불투명한 배경을 제공합니다.

`login-*.png`는 기존 오피스 모델의 외형·명찰을 재사용한 정적 이미지입니다.

| 직원 | 로그인 전용 자세와 장식 |
| --- | --- |
| 계획 | 양손으로 차트 확인, 흐름도와 연결선 |
| 개발 | 발판 없이 의자에서 노트북 작업, 코드 창과 커서 |
| QA | 돋보기와 검사 태블릿, 스캔·검사 표시 |
| 보안 | 방패를 든 자세, 지문·접근 확인 패널 |
| 오케스트레이터 | 한 손으로 반대 손목을 받치며 시계 확인, 시계 면은 눈 방향으로 배치 |

오케스트레이터의 태블릿·지휘봉·계급도·말풍선과 강조용 라임 후광은 사용하지 않습니다. 별도 주변 장식도 표시하지 않아 개발 캐릭터를 가리지 않습니다. 작은 화면에서는 직원 이미지를 위에 배치하고 장식을 숨기며 움직임 줄이기 설정을 존중합니다. 실제 3D 모니터나 LLM을 로그인 화면에서 실행하지 않습니다.

필요할 때만 `frontend/`에서 `node scripts/renderLoginCharacters.mjs`로 768×960 투명 PNG 다섯 장을 생성합니다. `node scripts/renderLoginCharacters.mjs orchestrator`처럼 역할을 지정하면 해당 이미지만 갱신합니다. 로컬 Chrome/WebGL을 사용하며 API·Host를 호출하지 않습니다. 구현 중 오케스트레이터 이미지와 격리된 데스크톱 로그인 렌더링을 확인했고, 이후 투명도·회원가입 스타일 변경은 정적 검사로 확인했습니다. 전체 브라우저·인증 회귀 검증은 아닙니다.

오피스 3D 영역은 일반 화면 높이 490px, 폭 600px 이하에서 415px를 사용합니다. 넓은 전체 보기(가로세로 비율 1.6 초과)에 1.05배 확대를 적용해 높이를 더 늘리지 않고 좌우 여백을 조금 줄였습니다. 가로로 모델을 늘리는 처리는 아니며 화면에 따라 여백은 남을 수 있습니다. 전체 화면 모드는 별도 높이를 사용합니다. 개발 에이전트 근접 카메라는 다른 직원과 같은 거리·높이 기준으로 맞췄으며 문서 전달·업무 모션·세리머니 동작은 유지합니다.

업무 흐름 제목은 “다섯 명의 에이전트, 함께 일하는 하나의 팀.”입니다. 프로젝트 메뉴 숫자는 현재 표시 목록이 0개이면 숨기고 1개부터 `01`, `02`로 표시합니다. 저장 프로젝트는 메뉴 진입 시 조회하므로 초기 숫자 없음이 DB의 프로젝트 부재를 확정하지는 않습니다. 이 숫자는 저장 목록과 현재 Run 카드의 표시 개수이며 우리 직원 `05`는 유지합니다.
