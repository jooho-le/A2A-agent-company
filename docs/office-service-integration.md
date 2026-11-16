# 오피스 실제 서비스 연결

## 바뀐 동작

목업/실제 전환, 105초 재생바, 배속, 오류 연출, 리허설·성공 기록 초기화 버튼을 제거했다. 직원의 작업 자세는 조회된 Step 상태로, 문서 이동은 저장된 Trace 이벤트로만 결정한다. 업무가 없으면 기본 대기이며 조회 실패를 직원의 작업 실패로 바꾸지 않는다. 최종 성공 연출은 실제 FINISHED + SUCCESS 확인 후에만 기존 자동 로직을 사용한다.

전체 화면 오피스와 글라스 패널, 기존 색상·캐릭터는 유지했다. 프로젝트 이력에서 Run을 다시 열고, 새로고침 시 저장된 프로젝트를 인증된 API로 재조회해 복원한다. 직원 소개는 역할 설명이며 가상의 프로젝트 진행률을 표시하지 않는다.

## 서비스 경로

브라우저 → 제품 API **8000**(사용자 JWT·소유권·접수 원장) → Multi-Agent Host **8002**(별도 서비스 Bearer) → Planner/Developer/QA/Security **8101~8104**.

Vite의 /api 전체를 제품 서버로 전달한다. 브라우저에는 Host/Agent 토큰이나 OpenAI 키를 공급하지 않는다. 로그인·문서·프로젝트 API도 같은 출처의 상대 경로를 사용한다.

| 제품 API | 처리 |
| --- | --- |
| GET /api/v1/scenarios | 실제 등록된 지원 시나리오 |
| GET /api/v1/host/approved-configuration?scenarioId=… | 승인된 공개 설정과 식별자 |
| POST /api/v1/runs | 자연어 원문·프로젝트명을 분리해 접수, 프로젝트와 Run 연결 저장 |
| GET /api/v1/submissions/{requestId} | 동일 요청의 접수 결과 조회·복구 |
| GET /api/v1/runs/{runId} 및 /steps, /events, /issues, /artifacts, /configuration | 소유권 확인 후 Host 조회 |

현재 등록된 **SCN-001 회원가입** 시나리오만 실행한다. 임의의 자연어를 다른 시나리오로 승인한 것처럼 처리하지 않는다.

## 접수와 장애 복구

- 브라우저는 UUIDv4 Idempotency-Key와 projectName, requestText, scenarioId, configurationId를 보낸다.
- 제품 서버가 최신 승인 설정을 확인하고 Host에 보낼 설정·원문·별도 내부 키를 DB에 먼저 동결한다. 사용자 입력으로 승인 설정이나 서버 주소를 바꾸지 않는다.
- RunSubmission은 PENDING / UNKNOWN / CREATED / REJECTED를 보관한다. 제품 접수 상태는 A2A·Run 상태와 별개다.
- 같은 사용자·키는 기존 영수증을 반환한다. 같은 키의 요청 변경은 409다.
- POST 타임아웃·응답 유실은 UNKNOWN이다. **접수 상태 확인**은 GET만 사용하며 새 POST를 자동 전송하지 않는다. Host의 내부 키·원문 해시를 대조한 경우만 ProjectRun을 복구한다. 404도 미생성 확정으로 취급하지 않는다.
- 승인 설정 조회 등 **Host POST 이전**의 실패는 제품 서버가 503과 X-Submission-Status: NOT_ACCEPTED를 함께 반환한다. 이 경우만 미접수로 확정하고 승인 설정 재확인 후 명시적 재요청을 허용한다. 일반 503은 UNKNOWN으로 유지한다. 409로 승인 설정이 바뀐 경우도 재조회 버튼을 표시한다.
- 미확인 접수 ID는 브라우저 세션에 계정별로 분리해 보관하여 새로고침·재로그인 후 GET 복구한다. 다른 계정에 이전 접수나 본문을 넘기지 않는다. JWT의 sub는 복원 후보 키를 나누는 데만 사용하며 실제 인증·소유권은 제품 서버가 검증한다.
- SCHEDULED는 실행 예약이며 완료가 아니다. 승인 설정 부재·서버 장애 시 차단하고 이유를 표시한다.

## 로컬 실행

저장소 루트에서 각각 별도 터미널을 사용한다.

~~~bash
.venv/bin/python scripts/run_local_service.py product
.venv/bin/python scripts/run_local_service.py host
cd frontend
npm run dev
~~~

실행 스크립트는 루트 .env를 읽되 프로세스 환경을 우선한다. Host 설정의 API 키 변수에 값이 없으면 기존 OPENAI_API_KEY를 프로세스 안에서 연결한다. 키를 다른 파일에 복사하거나 출력하지 않는다.

제품 실행 스크립트의 명시적인 초기화 단계에서 backend/project_schema.py가 프로젝트·Run·접수 테이블을 추가한다. 기존 사용자·문서 데이터와 일반 FastAPI의 기본 lifespan은 유지했다.

로컬 준비에서 누락된 JWT·Host·역할별 인증값을 서로 다르게 발급했다. 기존 .env는 /private/tmp/a2a-service-config-backup-7wiqs39h/.env에 권한 600으로 백업했다. 제품과 겹치던 로컬 Host 포트만 8000→8002로 바꾸었으며 원본 runtime JSON도 같은 비공개 디렉터리에 백업했다. 실제 키와 local 설정·DB·가상환경은 Git 제외 대상이다.

## 검증과 실제 확인 범위

- 제품 백엔드 unittest **81개 통과**: 기존 인증·문서·프로젝트, Host 조회, 멱등 접수, 응답 유실 복구, 타 사용자 접근 차단, 미접수 확정·잘못된 Host 응답·시나리오 미준비 차단.
- 프론트 Node/격리 UI 테스트 **195개 통과**: 로그인 복귀·초안 보존, 승인 공급, 자연어 원문 전달, 단일 POST, 접수 조회 복구, Run/Trace·산출물 조회, 계정별 미확인 요청 복원.
- 프로덕션 빌드와 DOM 바인딩 확인. 실제 브라우저에서 재생 컨트롤이 없는 오피스와 직원 렌더링을 확인했다.
- 실제 제품 8000·Host 8002의 HTTP 응답, 서비스 Bearer로 시나리오·승인 설정 조회, 양 서버의 무인증 401 거부를 확인했다.
- 후속 점검은 [실제 서비스 점검 결과](office-service-audit.md)에 정리했다.
- 유료 Run 생성·OpenAI 코드 생성·Docker Build/QA/Security를 끝까지 수행한 결과는 아니다. 실제 완성 판정은 로그인 후 업무를 시작하여 해당 Run의 최종 기록으로 확인한다.
- 산출물 화면은 기존 등록 목록/보고서를 표시한다. 제품용 소스 ZIP 다운로드 중계는 이번 작업에 추가하지 않았다.

개발정의서의 A2A 상태·Task/Message/Artifact 계약, 역할 분리, 불변 Snapshot, 승인 Configuration, 수정·재시도 한도, 예산·판정 규칙은 변경하지 않았다. 프로젝트 commit/push는 수행하지 않았다.
