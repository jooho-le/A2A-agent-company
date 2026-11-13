# AI 에이전트 회사 제품 백엔드

최근 갱신: 2026-11-13. Project·ProjectRun 및 프로젝트 API 4개, `/api/v1/runs` 읽기 전용 중계, React 프로젝트 목록·로그인 연결의 현재 상태를 반영했습니다. 실제 DB 적용·테스트 결과는 이전 작업 기록이며 이번에는 DB·서버·API·테스트를 실행하지 않습니다.

현재 상태: 프로젝트 모델·API 4개와 React 목록 GET은 구현됐고 실제 개발 DB 테이블도 이전 승인 단계에서 적용했습니다. Host 조회 중계 GET 5개는 구현됐지만 서비스 인증 미검증으로 503 차단 중입니다. 제품 Run 생성·Submission·ProjectRun 자동 연결은 미구현이며 프론트엔드 Run 보호 전송기와 Vite 8000 전환도 대기 중입니다.

## 빠른 시작

macOS·Linux에서 저장소 최상위 디렉터리를 기준으로 실행합니다. 이미 `backend/.venv`가 준비되어 있으면 가상환경 생성 줄은 생략합니다. 아래 비밀키 생성 명령은 개발용이며 실제 값을 출력하지 않습니다.

```bash
cd backend
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
export JWT_SECRET="$(.venv/bin/python -c 'import secrets; print(secrets.token_urlsafe(32))')"
.venv/bin/python -m uvicorn main:app --host 127.0.0.1 --port 8000 --reload
```

서버 실행 후 `http://127.0.0.1:8000/docs`에 접속합니다. 비밀키를 다시 생성하면 기존 토큰을 사용할 수 없으므로, 이미 설정한 키를 유지하려면 생성 줄도 생략합니다. 자세한 설정과 주의 사항은 4절을 참고합니다.

## 1. 프로젝트 개요와 담당 역할

이 디렉터리는 역할 3인 웹 애플리케이션 및 시연 화면(Web Application / Demo)이 사용하는 FastAPI 제품 백엔드입니다. 회원가입·로그인, JWT 인증, PDF 전자문서 제출·조회·다운로드, 사용자별 프로젝트 생성·조회·이름 수정과 인증 기반 Host 조회 중계 코드를 제공합니다. Host 실통신은 인증 계약 확인 전까지 차단됩니다.

사용자와 문서 메타데이터는 SQLite에 저장하고, PDF 원본은 로컬 파일 시스템에 저장합니다. AWS·S3 등 외부 저장소는 사용하지 않습니다. 이 서버에서 LLM을 호출하거나 Agent·MCP·Docker를 실행하지 않습니다.

Orchestrator는 별도의 시스템입니다. 제품 백엔드의 사용자·문서 기능과 Orchestrator의 Run·Step·Trace 기능을 구분해야 합니다.

현재 프론트엔드 첫 화면은 로그인 없이 열리는 `AI 에이전트 회사` 오피스입니다. 계정 메뉴에서 로그인하고 성공하면 오피스로 복귀합니다. 프로젝트 화면은 제품 `GET /api/projects`의 본인 저장 목록과 현재 화면의 Run을 함께 표시하고, 보관함은 현재 Run의 Artifact 등록 정보를 표시합니다. 파란색 업무 요청창은 JWT가 없거나 승인 Configuration이 없으면 실제 POST를 차단합니다. 로그인 전용 직원 포즈·후광·중앙 정렬 변경은 인증 API나 DB 계약을 바꾸지 않습니다. 105초 목업은 별도 시연입니다.

## 2. 폴더 구조와 주요 파일

```text
backend/
├── README.md                 # 제품 백엔드 실행·연동 안내
├── main.py                   # FastAPI 앱, 테이블 생성, CORS, 라우터 등록
├── database.py               # SQLite 경로, engine, SessionLocal, Base, get_db
├── models.py                 # User·Document·Project·ProjectRun ORM 모델
├── project_schema.py         # 프로젝트 테이블만 명시적으로 생성하는 확장 함수
├── project_schemas.py        # 프로젝트 공개 요청·응답 모델
├── project_scenarios.py      # 로컬 SCN-001 등록 UUID 확인
├── host_reads.py             # 허용 GET·서비스 인증 준비·실통신 차단
├── schemas.py                # 요청 검증과 공개 응답 구조
├── security.py               # Argon2 비밀번호 처리와 JWT 발급·검증
├── requirements.txt          # 버전이 고정된 Python 의존성
├── .gitignore                # 가상환경·DB·업로드 파일 등의 Git 제외 설정
├── routers/
│   ├── auth.py               # 회원가입·로그인·현재 사용자 인증
│   ├── documents.py          # PDF 업로드·목록·다운로드
│   ├── projects.py           # 본인 프로젝트 생성·목록·상세·이름 수정
│   └── host_runs.py          # JWT·ProjectRun 소유권 기반 명시적 GET 중계
├── tests/
│   ├── test_documents.py     # 문서 API와 기존 인증 기능 회귀 테스트
│   ├── test_project_models.py # 임시 SQLite의 프로젝트 모델·스키마 확장 검사
│   ├── test_projects_api.py   # 임시 DB 프로젝트 API 검사
│   └── test_host_reads.py     # Mock Host·인증·경로·오류 검사
├── .venv/                    # 로컬 Python 가상환경
├── app.db                    # 실행 과정에서 생성되는 SQLite DB
└── uploads/                  # 최초 업로드 시 생성되는 PDF 저장 폴더
```

`.venv/`, `app.db`, `uploads/`는 로컬 환경의 파일이며 Git 추적 대상에서 제외합니다.

`main.py`는 서버 시작 시 `create_all()` 대상을 기존 User·Document 두 테이블로 제한합니다. 프로젝트 테이블은 `project_schema.create_project_tables(target_engine)`을 명시적으로 호출해야 생성됩니다. 기존 테이블의 컬럼을 자동 변경하는 마이그레이션 기능은 없습니다.

`engine`은 DB 접근을 관리하고, `SessionLocal`은 요청에서 사용할 DB 세션을 만드는 설정입니다. `Base`는 ORM 모델의 공통 기반이며 테이블 정의를 모읍니다. `get_db`는 요청에 세션을 제공하고 사용 후 닫습니다.

## 3. 사용 기술과 주요 의존성

| 기술·패키지 | 용도 |
| --- | --- |
| Python | 서버와 테스트 구현 |
| FastAPI | HTTP API와 요청·응답 처리 |
| Uvicorn | ASGI 서버 실행 |
| SQLAlchemy | ORM 모델, DB 세션, 조회·저장 |
| SQLite | 로컬 사용자·문서 메타데이터 저장 |
| Pydantic | 요청 형식·값 검증과 응답 필드 제한 |
| pwdlib, argon2-cffi | Argon2 비밀번호 해시와 검증 |
| PyJWT | JWT Access Token 발급과 검증 |
| python-multipart | 파일·폼 업로드 처리 |
| unittest | Python 표준 자동 테스트 실행 |

정확한 설치 버전과 간접 의존성은 `requirements.txt`를 기준으로 합니다. 현재 테스트는 별도의 pytest나 HTTP 테스트 클라이언트를 요구하지 않습니다.

## 4. 개발 환경 준비와 서버 실행

### 가상환경과 패키지 설치

Python 3.10 이상을 기준으로 합니다. 아래 명령은 저장소 최상위 디렉터리에서 시작하며 macOS·Linux 환경의 경로를 사용합니다.

```bash
cd backend
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
```

이미 `backend/.venv`가 준비되어 있다면 가상환경 생성은 생략할 수 있습니다. 루트의 Orchestrator용 환경과 `backend/.venv`를 구분합니다.

다운로드 구현은 `os.O_DIRECTORY`, `os.O_NOFOLLOW`, `dir_fd` 등 파일 접근 기능을 사용합니다. Windows에서 동일하게 동작한다고 검증된 상태는 아닙니다.

### JWT 비밀키 설정

`security.py`는 `JWT_SECRET` 환경변수를 읽습니다. 코드에 기본 비밀키가 없으며, 값이 없거나 공백이면 토큰 발급·검증 시 설정 오류가 발생합니다. 이 오류는 관련 API에서 HTTP 500으로 반환됩니다. 서버 시작만으로 설정 누락을 검사하지는 않습니다.

개발용 비밀키는 같은 터미널에서 다음처럼 생성할 수 있습니다. 명령은 생성된 값을 화면에 출력하지 않고 환경변수에 넣습니다.

```bash
export JWT_SECRET="$(.venv/bin/python -c 'import secrets; print(secrets.token_urlsafe(32))')"
```

실제 값을 문서·소스·Git에 저장하지 않습니다. 비밀키가 바뀌면 기존 토큰의 서명 검증이 실패하므로 서버 재시작 시 같은 키를 유지해야 기존 토큰을 사용할 수 있습니다. 현재 코드에는 `.env` 파일을 자동으로 읽는 기능이 없습니다.

### 서버 실행

다음 명령은 `backend/`에서 실행합니다.

```bash
.venv/bin/python -m uvicorn main:app --host 127.0.0.1 --port 8000 --reload
```

| 주소 | 용도 |
| --- | --- |
| `http://127.0.0.1:8000` | 제품 백엔드 기본 주소 |
| `http://127.0.0.1:8000/docs` | 대화형 API 문서 |
| `http://127.0.0.1:8000/openapi.json` | API 명세 |

`GET /`는 HTTP 200과 다음 JSON을 반환합니다. 이 응답은 JWT 설정이나 Orchestrator 실행까지 확인하는 검사가 아닙니다.

```json
{
  "message": "A2A backend is running"
}
```

`--reload`는 개발용 설정입니다. 기본 DB 파일은 실행 디렉터리와 관계없이 `backend/app.db`에 생성됩니다. `uploads/`는 첫 파일 저장 때 생성됩니다.

### React 연동과 CORS

현재 CORS는 `http://localhost:5173`, `http://localhost:5174`만 출처로 허용합니다. 자격 증명(credentials), 모든 HTTP 메서드와 헤더는 허용하지만 출처에 `"*"`를 사용하지 않습니다.

React의 인증·문서 요청은 `http://127.0.0.1:8000`으로 직접 전송합니다. Vite가 다른 포트에서 실행되거나 `127.0.0.1` 주소로 프론트엔드에 접속하면 현재 허용 출처와 다를 수 있습니다.

### 로그인 없는 데모 화면과 서버 인증의 구분

`frontend/src/App.jsx`는 공개 오피스를 기본으로 열고 계정 메뉴에서 기존 로그인·회원가입 화면으로 이동합니다. 이전 `REQUIRE_PLATFORM_LOGIN` 플래그는 더 이상 사용하지 않습니다. 비로그인 상태의 프로젝트·보관함·전자문서 메뉴는 로그인으로 안내하며, 요청창 제출도 토큰이 없으면 차단합니다. 제품 서버는 토큰 존재만 믿지 않고 기존 `read_current_user`로 인증합니다.

로그인 성공 시 `sessionStorage.access_token`에 저장한 제품 JWT를 문서·프로젝트 목록 API에 Bearer로 전달합니다. 프론트엔드의 Run 보호 전송기는 준비되어 있지만 `PRODUCT_RUN_API_READY=false`이며 기존 8001로 제품 JWT를 보내지 않습니다. 상세 화면 전환과 입력 초안 보존은 [프론트엔드 README](../frontend/README.md)를 참고합니다.

## 5. 회원가입·로그인과 JWT 인증

### 회원가입

`POST /api/auth/signup`은 인증 없이 JSON 요청을 받습니다. 아래 계정 정보는 형식 설명을 위한 가상 예시이며 실제 사용자 정보가 아닙니다.

```json
{
  "email": "user@example.com",
  "password": "Password123"
}
```

이메일은 서버의 정규식과 길이 조건으로 검사하고 소문자로 변환합니다. 비밀번호는 최소 8자여야 합니다. 등록 전 이메일을 대소문자 구분 없이 조회하고, DB의 고유 제약조건으로 중복 저장도 방지합니다.

비밀번호는 Argon2 해시로 변환하여 `password_hash`에만 저장합니다. 평문 비밀번호 컬럼은 없습니다. `role`은 서버에서 `"user"`로 지정합니다. 요청 스키마가 추가 필드를 금지하므로 클라이언트가 `role`을 보내면 HTTP 422로 거부합니다.

성공하면 HTTP 201과 다음 JSON을 반환합니다.

```json
{
  "success": true,
  "message": "회원가입에 성공했습니다."
}
```

### 로그인

`POST /api/auth/login`은 회원가입과 같은 이름의 `email`, `password` 필드를 JSON으로 받습니다. 이메일로 사용자를 조회한 후 기존 Argon2 설정으로 입력 비밀번호와 저장된 해시를 검증합니다.

사용자가 없어도 임시 해시 검증을 수행하여 응답 시간 차이를 줄입니다. 사용자 없음과 비밀번호 불일치는 모두 HTTP 401과 같은 메시지를 반환합니다.

```json
{
  "detail": "이메일 또는 비밀번호가 올바르지 않습니다."
}
```

성공하면 HTTP 200으로 다음 형태를 반환합니다. 토큰 문자열은 실제 값 대신 설명용 자리표시자로 표시했습니다.

```json
{
  "access_token": "<access_token>",
  "token_type": "bearer"
}
```

### JWT 발급·검증과 현재 사용자

JWT는 `JWT_SECRET`으로 서명하며 알고리즘은 `HS256`, 만료시간은 발급 후 30분입니다. 발급 시 다음 정보만 넣으며 비밀번호·해시·사용자 역할을 넣지 않습니다.

| 필드 | 의미 |
| --- | --- |
| `sub` | 사용자 `id`를 문자열로 저장 |
| `email` | 사용자의 이메일 |
| `exp` | UTC 기준 토큰 만료시각 |

보호된 API에는 다음 헤더가 필요합니다.

```http
Authorization: Bearer <access_token>
```

`GET /api/auth/me`와 문서 API는 `routers/auth.py`의 `read_current_user`를 재사용합니다. 서버는 `HS256` 서명과 만료시간을 검증하고 `sub`, `exp` 존재를 요구합니다. 이어서 `sub`를 정수로 변환하여 현재 DB에 실제 사용자가 있는지 확인합니다.

토큰 없음·잘못된 토큰·만료·DB 사용자 없음은 HTTP 401입니다. 정상 사용자 조회는 HTTP 200으로 다음 공개 정보만 반환합니다.

```json
{
  "id": 1,
  "email": "user@example.com",
  "role": "user"
}
```

토큰 자동 갱신, 서버 로그아웃·토큰 폐기, 소셜 로그인, 비밀번호 재설정, 관리자 권한 기능은 구현되어 있지 않습니다.

## 6. 전자문서 PDF API

모든 문서 API는 JWT 인증이 필요합니다. 사용자 ID를 요청에서 신뢰하지 않고 인증된 현재 사용자를 기준으로 처리합니다. 담당자는 기존 가입 사용자 중 ID로 지정하며 별도의 담당자 역할 제한은 없습니다.

### 업로드

`POST /api/documents`는 `multipart/form-data`를 받습니다.

| 필드 | 자료형 | 조건 |
| --- | --- | --- |
| `file` | 파일 | 한 번에 PDF 1개 |
| `assignee_id` | 정수 | 0보다 크고 DB에 존재하는 사용자 ID |

`submitter_id`는 서버가 현재 사용자 ID로 지정합니다. 클라이언트가 추가 폼 필드로 보내더라도 제출자를 바꾸지 못합니다.

파일 크기는 최대 10MiB인 10,485,760바이트까지 허용합니다. 확장자나 클라이언트의 `Content-Type` 대신 시작 바이트 `%PDF-`를 확인합니다. 전체 PDF 구조 검증이나 악성코드 검사는 수행하지 않습니다.

저장 파일명은 `uuid4()` 기반의 `.pdf` 이름입니다. 원본 이름을 경로로 사용하지 않으며, 기존 파일을 덮어쓰지 않는 `xb` 모드로 저장합니다. DB 저장 실패 시 롤백하고 이번 요청이 만든 파일의 삭제를 시도합니다. 삭제까지 실패하면 서버 로그에 기록합니다.

성공하면 HTTP 201로 다음 공개 메타데이터를 반환합니다. 아래 값은 응답 형식 설명을 위한 가상 예시입니다.

```json
{
  "id": 1,
  "original_filename": "증빙자료.pdf",
  "file_size": 2048,
  "content_type": "application/pdf",
  "submitter_id": 1,
  "assignee_id": 2,
  "created_at": "2026-10-16T10:00:00"
}
```

`stored_filename`, `file_path`는 응답에서 제외합니다. 실제 PDF는 `backend/uploads/`에 있고 DB에는 PDF 바이너리를 넣지 않습니다.

### 제출·배정 목록

`GET /api/documents`는 `view` 쿼리로 목록을 구분합니다.

| 요청 | 조회 조건 |
| --- | --- |
| `/api/documents` | 기본값인 `submitted` 사용 |
| `/api/documents?view=submitted` | `submitter_id`가 현재 사용자 ID인 문서 |
| `/api/documents?view=assigned` | `assignee_id`가 현재 사용자 ID인 문서 |

목록은 `created_at` 내림차순, 같은 시각이면 `id` 내림차순입니다. HTTP 200으로 `{"documents": [...]}` 구조를 반환하며 각 항목의 필드는 업로드 성공 응답과 같습니다. 문서가 없으면 오류 없이 다음을 반환합니다.

```json
{
  "documents": []
}
```

다른 `view` 값은 HTTP 422입니다. 클라이언트가 `submitter_id`, `assignee_id` 쿼리를 추가해도 조회 조건에 사용하지 않습니다. 현재 목록에는 페이지 나누기나 사용자 이름 조회가 없습니다.

### 다운로드

`GET /api/documents/{document_id}/download`는 문서 ID로 DB를 조회합니다. 현재 사용자가 제출자 또는 담당자이면 허용하고, 관계없는 사용자는 HTTP 403으로 거부합니다. 제출자와 담당자가 같은 경우에도 다운로드할 수 있습니다.

성공 응답은 JSON이 아닌 HTTP 200의 PDF 바이너리이며 `Content-Type`은 `application/pdf`입니다. 원본 파일명의 경로·출력 불가능 문자를 정리하고, `Content-Disposition`에 안전한 기본 파일명과 UTF-8 인코딩한 원본 파일명을 함께 넣습니다.

서버는 UUID v4 저장 파일명과 DB 경로를 확인하고, 업로드 디렉터리와 파일을 심볼릭 링크를 따라가지 않고 엽니다. 일반 파일 여부·하드 링크 수·메타데이터와 파일 크기 일치를 검사한 뒤 열린 파일에서 원본 바이트를 그대로 읽습니다. 클라이언트가 제공한 경로로 파일을 읽지 않습니다.

문서 ID가 없으면 HTTP 404입니다. 실제 파일 누락, 조작된 저장 경로, 링크, 파일 접근 오류 등도 내부 경로나 예외를 노출하지 않고 HTTP 404로 처리합니다. 파일 크기가 같다는 검사만으로 저장 후 변조를 모두 탐지하는 것은 아니며, 파일 해시를 DB에 저장·대조하는 기능은 없습니다.

## 7. DB 스키마와 테이블 관계

`database.py`는 `backend/app.db`의 SQLite를 사용하며 `check_same_thread=False`로 연결합니다. `SessionLocal`은 `autocommit=False`, `autoflush=False`로 구성되어 있습니다.

### 팀원에게 스키마 공유하기

| 확인 대상 | 원본 또는 접속 주소 |
| --- | --- |
| DB 스키마 원본 | [backend/models.py](models.py) |
| API 요청·응답 모델 | [backend/schemas.py](schemas.py) |
| Swagger UI | `http://127.0.0.1:8000/docs` |
| OpenAPI JSON | `http://127.0.0.1:8000/openapi.json` |

폼 입력과 라우터가 직접 반환하는 응답은 `backend/routers/`도 함께 확인합니다. Swagger UI와 OpenAPI JSON은 서버 실행 후 확인할 수 있습니다. User와 Document의 연결은 다음과 같으며, 상세 컬럼·제약조건은 아래 표를 참고합니다.

```text
User (users.id) 1 ── N Document (documents.submitter_id): 제출자
User (users.id) 1 ── N Document (documents.assignee_id): 담당자
```

이 스키마는 제품 백엔드의 사용자·전자문서 계약입니다. Orchestrator 내부의 Run·Step·Trace DB 스키마와는 다른 계약이며 서로 대체하거나 같은 테이블로 취급하지 않습니다.

### users 테이블

| 컬럼 | ORM 자료형 | 제약조건·기본값 | 의미 |
| --- | --- | --- | --- |
| `id` | `Integer` | 기본키, 자동 증가, NULL 불가 | 사용자 식별자 |
| `email` | `String` | NULL 불가, 고유값, 인덱스 | 이메일 |
| `password_hash` | `String` | NULL 불가 | Argon2 비밀번호 해시 |
| `role` | `String` | NULL 불가, DB 기본값 `"user"` | 사용자 역할 |
| `created_at` | `DateTime` | NULL 불가, DB 기본값 `func.now()` | 계정 생성 시각 |

### documents 테이블

| 컬럼 | ORM 자료형 | 제약조건·기본값 | 의미 |
| --- | --- | --- | --- |
| `id` | `Integer` | 기본키, 자동 증가, NULL 불가 | 문서 식별자 |
| `submitter_id` | `Integer` | NULL 불가, 외래키 `users.id` | 제출자 |
| `assignee_id` | `Integer` | NULL 불가, 외래키 `users.id` | 담당자 |
| `original_filename` | `String` | NULL 불가 | 사용자 업로드 원본 파일명 |
| `stored_filename` | `String` | NULL 불가, 고유값 | 서버의 UUID 기반 저장 파일명 |
| `file_path` | `String` | NULL 불가 | 서버 내부 저장 경로 |
| `file_size` | `Integer` | NULL 불가 | 바이트 단위 파일 크기 |
| `content_type` | `String` | NULL 불가 | 업로드 API가 저장하는 `application/pdf` |
| `created_at` | `DateTime` | NULL 불가, DB 기본값 `func.now()` | 제출 시각 |

한 사용자는 여러 문서의 제출자 또는 담당자가 될 수 있고, 각 문서에는 제출자 1명과 담당자 1명이 연결됩니다. 두 외래키가 같은 사용자를 참조해도 됩니다.

ORM 모델에는 문자열 길이, 파일 크기 제한을 위한 DB CHECK, 삭제 시 연쇄 삭제, `relationship()` 속성이 별도로 정의되어 있지 않습니다. 10MiB·PDF·담당자 존재 검사는 업로드 API의 검증입니다. `DateTime`에는 시간대 설정이 없습니다.

외래키는 모델과 테이블 정의에 포함되지만 현재 `database.py`에는 SQLite의 `PRAGMA foreign_keys=ON` 설정이 없습니다. 모델의 외래키 선언과 DB 실행 시 강제 검사는 구분해야 합니다. 업로드 API는 저장 전에 담당자 존재 여부를 확인하며 제출자는 JWT 인증으로 조회합니다.

## 8. API 구조와 상태 코드 요약

| 메서드·경로 | 입력 | 정상 응답 | 주요 실패 상태 |
| --- | --- | --- | --- |
| `GET /` | 없음 | 200, 실행 확인 JSON | 일반 서버 오류 |
| `POST /api/auth/signup` | JSON `email`, `password` | 201, `success`, `message` | 409 중복, 422 입력 오류, 500 저장 오류 |
| `POST /api/auth/login` | JSON `email`, `password` | 200, `access_token`, `token_type` | 401 로그인 실패, 422 입력 오류, 500 JWT 설정 오류 |
| `GET /api/auth/me` | Bearer 토큰 | 200, `id`, `email`, `role` | 401 인증 실패, 500 JWT 설정 오류 |
| `POST /api/documents` | Bearer 토큰, 폼 `file`, `assignee_id` | 201, 문서 메타데이터 | 400 파일 수·담당자·파일명 오류, 401, 413 크기 초과, 415 PDF 서명 오류, 422 입력 오류, 500 |
| `GET /api/documents` | Bearer 토큰, 선택적 `view` | 200, `documents` 배열 | 401, 422 잘못된 `view`, 500 |
| `GET /api/documents/{document_id}/download` | Bearer 토큰, 정수 문서 ID | 200, PDF 바이너리 | 401, 403 권한 없음, 404 문서·파일 접근 실패, 422 경로 입력 오류, 500 |
| `POST /api/projects` | Bearer 토큰, `name`, `scenarioId` | 201, 프로젝트 공개 정보 | 401, 422, 503 |
| `GET /api/projects` | Bearer 토큰 | 200, `projects` 배열 | 401, 503 |
| `GET /api/projects/{projectId}` | Bearer 토큰, UUIDv4 | 200, 공개 정보·`runs` | 401, 404, 422, 503 |
| `PATCH /api/projects/{projectId}` | Bearer 토큰, UUIDv4, `name` | 200, 공개 정보 | 401, 404, 422, 503 |
| `GET /api/v1/runs/{runId}` 및 steps/events/issues/artifacts | Bearer 토큰, 소유한 ProjectRun | 인증 준비 후 Host JSON | 401, 404, 422, 현재 실통신 503 |

JWT 설정 오류는 문서 API에서도 인증 단계의 HTTP 500으로 발생할 수 있습니다. 여러 조건이 동시에 잘못된 요청에서는 실제 검증 순서에 따라 반환되는 오류가 달라질 수 있습니다.

일반적인 명시적 실패는 `detail` 문자열을 반환합니다. 다음은 중복 이메일 응답 예시입니다.

```json
{
  "detail": "이미 등록된 이메일입니다."
}
```

Pydantic 요청 검증 실패는 HTTP 422와 `detail` 배열을 반환합니다. `main.py`의 처리기는 항목별 `loc`, `msg`, `type`만 포함하고 비밀번호 등 입력값이나 원래 예외 객체를 응답에 넣지 않습니다.

```json
{
  "detail": [
    {
      "loc": ["body", "email"],
      "msg": "Value error, 올바른 이메일 형식이 아닙니다.",
      "type": "value_error"
    }
  ]
}
```

프론트엔드는 성공 여부를 먼저 확인하고 JSON 오류와 PDF 성공 응답을 구분해야 합니다. 브라우저에서 파일을 보낼 때는 `FormData`를 사용하고 `Content-Type`의 multipart 경계값을 직접 지정하지 않습니다.

## 9. 자동 테스트 실행

현재 `backend/tests/test_documents.py`는 Python 표준 `unittest` 기반입니다. `backend/`에서 다음 명령을 실행합니다.

```bash
.venv/bin/python -B -m unittest discover -s tests -v
```

`-B`는 테스트 중 Python 바이트코드 캐시를 만들지 않도록 합니다. 루트의 `tests/`는 Orchestrator·Agent 등 다른 시스템의 테스트이므로 이 명령의 `backend/tests/`와 구분합니다.

테스트는 임시 SQLite DB·업로드 디렉터리·임시 JWT 비밀키를 사용합니다. `get_db`, 서버 시작 시 사용하는 `engine`, `UPLOAD_DIRECTORY`를 테스트용으로 바꾸어 실제 `app.db`와 `uploads/`를 사용하지 않습니다. HTTP 서버를 띄우지 않고 실제 ASGI 앱을 호출하므로 별도 서버 실행이 필요 없습니다.

검증 범위는 다음과 같습니다.

- 서버 시작 시 테이블 생성과 외래키 정의를 확인합니다.
- 인증·토큰 만료·DB 사용자 없음과 사용자별 문서 접근을 확인합니다.
- 정상 PDF, 정확히 10MiB, 크기 초과, 잘못된 형식, 담당자와 파일 수를 검사합니다.
- 동일 원본 파일명, 파일·DB 저장 실패, 부분 파일 정리와 UUID 충돌을 확인합니다.
- 제출·배정 목록의 필터링, 빈 목록, 정렬, 내부 필드 제외를 확인합니다.
- 다운로드 권한, 원본 바이트·SHA-256 일치, 한글 파일명과 헤더 처리를 확인합니다.
- 경로 조작, 심볼릭·하드 링크, 비정규 파일, 경로 교체, 파일 누락·크기 불일치를 확인합니다.
- 기존 회원가입·중복·입력 검증·로그인·현재 사용자 조회의 회귀를 확인합니다.

2026-10-16 문서 작성 시 위 명령으로 자동 테스트 50개가 모두 통과했습니다. 이 결과는 임시 환경의 ASGI 테스트이며 실제 브라우저나 배포 서버에서의 검증 결과는 아닙니다.

2026-11-08 README 갱신 당시에도 같은 명령으로 50개 테스트를 다시 실행하여 모두 통과했습니다. 테스트용 DB·업로드 폴더와 JWT 설정을 사용하는 격리된 ASGI 테스트의 기록이며, Uvicorn 서버·실제 브라우저·유료 LLM·Docker 실행 결과가 아닙니다. 이번 2026-11-09 문서 갱신에서는 코드를 대조했으며 자동 테스트나 아래 수동 테스트를 다시 실행하지 않았습니다.

### 과거 실제 브라우저·수동 통합 테스트 기록

다음은 사용자가 확인한 과거 개발 과정의 실제 수동 검증 기록입니다. 위 자동 테스트 50건과 별개이며 이번 문서 보완 작업에서 새로 실행한 테스트가 아닙니다.

| 검증 항목 | 확인된 결과 |
| --- | --- |
| 제출자 A의 PDF 제출·다운로드 | 업로드와 다운로드 성공 |
| 담당자 B의 배정 문서 조회·다운로드 | 배정된 문서 조회와 다운로드 성공 |
| 관계없는 제3자 C의 직접 다운로드 API 호출 | HTTP 403 Forbidden 확인 |
| 문서 ID 2의 원본 PDF와 다운로드 파일 비교 | SHA-256 해시 일치 확인 |

테스트 계정의 이메일·토큰·비밀번호, 파일의 민감한 내용과 실제 해시값은 기록하지 않습니다. 이 결과는 해당 수동 검증 범위에 한정하며 운영 환경 전체의 검증 완료를 뜻하지 않습니다.

## 10. 구현 완료와 추가 검증 사항

### 코드에 구현된 기능

회원가입·Argon2 비밀번호 저장, 로그인·JWT 발급, 현재 사용자 인증, PDF 1개 업로드, 사용자별 제출·배정 목록, 제출자·담당자 다운로드가 구현되어 있습니다. 공개 응답에서 비밀번호 해시·내부 저장 경로를 제외하며 파일·DB 저장 실패에 대한 기본 정리 처리가 있습니다.

### 아직 구현하지 않은 기능

토큰 갱신·폐기, 서버 로그아웃, 비밀번호 재설정, 소셜 로그인, 담당자 목록 API, 문서 삭제·수정·검색·페이지 나누기, 클라우드 저장소, 악성코드 검사, 버전 관리형 DB 마이그레이션은 구현되어 있지 않습니다. 프로젝트 두 테이블의 명시적 최초 생성 함수는 별도로 제공합니다. `role` 컬럼이 있다는 이유로 관리자 권한 시스템이 구현된 것은 아닙니다.

### 별도 환경에서 확인할 사항

이번 갱신에서는 제품 백엔드 프로그램·DB·API를 변경하거나 백엔드 서버·테스트를 실행하지 않았습니다. 프론트엔드의 3D 외형·카드·배치 검증은 제품 백엔드의 회귀 테스트와 별개입니다. 과거 자동·수동 검증 기록과 별개로, 최근 프론트엔드의 로그인 진입 설정·메뉴 전환에서 인증 후 문서 작업이 이어지는 흐름과 토큰 만료·재로그인을 다시 확인해야 합니다. 실제 브라우저의 CORS·한글 파일명 다운로드, 배포 환경의 파일 권한·백업·동시 요청, SQLite 외래키 강제 정책과 기존 DB 구조 변경 절차도 별도 확인이 필요합니다.

자동 테스트의 원본 바이트 일치는 다운로드 구현 검증이며, 운영 저장 파일의 지속적 무결성 감시를 제공한다는 뜻은 아닙니다. 전체 운영 보안과 부하·장애 복구가 모두 검증된 상태로 설명하지 않습니다.

## 11. Orchestrator와 제품 백엔드의 경계

| 구분 | 제품 백엔드 | Orchestrator |
| --- | --- | --- |
| 주요 코드 | `backend/` | `src/orchestrator/` |
| 담당 기능 | 사용자 인증·전자문서·프로젝트 저장·권한 있는 Run 조회 중계 준비 | Agent 협업 실행과 상태·산출물 관리 |
| 현재 프론트엔드 연결 주소 | `http://127.0.0.1:8000` | 기본 `8001`, 실제 Host 준비 대상 `8002`를 Vite 환경변수로 선택 |
| 주요 API | `/api/auth/*`, `/api/documents`, `/api/projects`, `/api/v1/runs/{runId}` 관련 GET 5개 | `/api/v1/runs` 및 관련 조회 |
| 데이터 | `backend/app.db`, `backend/uploads/` | 별도의 플랫폼 저장소와 실행 설정 |

제품 백엔드 실행 명령은 Orchestrator나 네 Agent를 시작하지 않습니다. 제품 사용자의 JWT를 플랫폼 공통 인증으로 사용하는 기능도 현재 연결되어 있지 않습니다. 현재 전자문서 API 호출은 Agent가 전자문서 기능을 자동 개발·검증하는 실행과 다릅니다.

파란색 입력창의 `업무 시작`은 `useOfficeRunSubmission → agentRunSubmission → POST /api/v1/runs`를 사용합니다. 프로젝트 이름과 요구사항은 기존 `requestText`에 함께 전달하며 새 백엔드 필드를 추가하지 않았습니다. 생성된 `run.runId`를 자동 선택해 최초 Trace부터 관측합니다. 별도 생성·조회 패널과 UUID 입력은 제거했고 승인 Configuration 미공급 시 POST를 차단합니다. 105초 목업 재생은 실제 Run을 생성하지 않습니다.

생성 응답으로 자동 선택한 Run의 Run·Step·Event·Configuration·Issue·Artifact를 Orchestrator에서 GET 조회합니다. 같은 모니터링 Hook을 실제 오피스와 프로젝트·보관함에서 재사용하며, 제품 `/api/auth/*`나 `/api/documents`를 통해 실행하는 구조가 아닙니다. Run의 `FINISHED`만으로 성공을 판단하지 않고 `verdict=SUCCESS`를 별도로 확인합니다. 3D 역할 표시 번호는 오케스트레이터 1, 계획 2, 개발 3, QA 4, 보안 5이며 서버 AgentRole·인덱스·Step 계약은 변경하지 않았습니다.

UI의 `오케스트레이터 · 리더`는 협업을 조율하는 역할 표시이며, 계획 에이전트가 리더인 구조가 아닙니다. 이 표시는 `User.role`의 관리자 권한이나 JWT 권한 상승으로 연결되지 않습니다. 네 작업 캐릭터는 Step 상태를, 리더는 Run과 확인된 A2A 호출·응답 Event를 사용합니다. 5인 세리머니와 직원 카드 이미지 변경에도 제품 인증·전자문서 API 계약은 그대로입니다. 제품 JWT를 Orchestrator 요청에 전달하는 공통 접근 권한 연동은 별도 합의가 필요합니다.

현재 준비 구분은 제품 백엔드 `8000`, 기존 Orchestrator `8001`, 실제 Multi Agent Host Orchestrator `8002`, Agent `8101~8104`입니다. 실제 Host 실행·인증 검증은 이전 작업에서 대기 상태이며 이번 문서 갱신에서 실행 프로세스를 다시 확인하지 않았습니다. 프론트엔드에서 `ORCHESTRATOR_PROXY_TARGET=http://127.0.0.1:8002 npm run dev`로 대상을 선택할 수 있으며, 변경 시 Vite만 재시작합니다. `/api/v1`과 `/health`는 같은 대상으로 전달합니다. 프록시 전환은 서버 실행이나 DB 통합을 수행하지 않으며 기존 8001의 Run이 8002로 자동 복사되지 않습니다.

Orchestrator의 승인 설정과 실행 계약은 [역할 1·2 인계 안내](../docs/45-owned-platform-handoff.md)와 현재 플랫폼 코드를 기준으로 합니다. 제품 웹 UI를 포함한 실제 LLM·Docker End-to-End 통합 실행은 아직 검증 전이며, 이 제품 백엔드의 자동 테스트 통과나 3D 완료 시연과 구분합니다.

### 프로젝트·산출물 연결과 제품 API의 구분

프로젝트 카드는 제품 `GET /api/projects`에서 읽은 본인 저장 목록과 현재 화면의 Run을 함께 표시합니다. 저장된 카드에는 프로젝트 ID·이름·시나리오 ID·생성일만 표시하며 Agent 상태나 진행률을 추측하지 않습니다. 실제 개발 DB의 두 프로젝트 테이블 적용과 React 목록 연결은 완료됐습니다. 다만 React의 생성·수정·상세 연결, ProjectRun 선택·이전 Run 복원은 미구현이며 현재 Run과 제품 Project를 자동으로 연결하지 않습니다.

보관함은 `GET /api/v1/runs/{runId}/artifacts` 응답을 기존 팝업 조회와 공유합니다. 개별 `GET /api/v1/runs/{runId}/artifacts/{artifactId}`도 존재하지만 JSON 등록 정보 조회이며 소스 파일 다운로드 API는 아닙니다. 공개 등록명·종류·생성 주체·버전·보고서 요약만 표시하고 원본 설정·manifest·민감 로그는 복사하지 않습니다. `SOURCE`는 Snapshot 메타데이터이고 변경 보고서 경로는 파일 존재나 내용의 보장이 아닙니다.

제품 백엔드의 `/api/documents/{document_id}/download`는 업로드된 PDF 계약입니다. 이를 Agent 코드 파일 다운로드로 재사용하지 않습니다. 소스 원문·다운로드를 제공하려면 팀장과 별도 접근 권한·저장소·HTTP 계약을 확정해야 하며 이번 변경에서는 제품 API·DB를 수정하지 않았습니다.

### 승인 Configuration 공급 경계와 남은 계약

- 프론트엔드 입력은 `approvedRunSubmission = { approved: true, scenarioId, configuration }`입니다. 현재 `App.jsx`는 이를 공급하지 않아 기본 `null`이며 POST를 차단합니다.
- Host는 서버의 runtime·tools·source에서 공개 `RunConfiguration`을 구성합니다. `approved_run_configuration(platform)`과 `app.state.approved_host_configuration_json`은 내부 Python 접근 경로이며 HTTP 조회 API가 아닙니다.
- `GET /api/v1/runs/{runId}/configuration`은 이미 생성된 Run의 고정된 설정입니다. 신규 Run의 현재 승인 설정이나 승인 증명으로 사용할 수 없습니다. 제품 백엔드에도 이 승인 설정을 관리·공급하는 경로는 없습니다.
- 실제 Host의 제출 검증기는 모델·제한·환경·시작 commit/snapshot·검증 참조를 승인값과 대조하고 민감정보를 검사합니다. 불일치는 `422 OWNED_AGENT_CONFIGURATION_INVALID`로 거부합니다. 일반 Orchestrator의 기본 `submission_validator=None`에는 같은 보장이 없습니다. 클라이언트의 `approved` 표시는 서버 승인을 대신하지 않습니다.
- 공개 전달 대상은 모델·실행 제한·이미지 digest·dependency lock hash·Source 식별값·검증 참조입니다. OpenAI API 키·A2A 토큰·비밀번호·Host 내부 경로와 원본 설정 파일은 서버에만 둡니다. 제품 로그인 JWT와 Agent 인증은 별도로 관리합니다.

팀장과 승인된 공개 설정의 조회 또는 서버 측 설정 참조 계약, 접근 권한과 설정 변경 시 재검증 방식을 먼저 확정해야 합니다. 모델·예산·도구 이미지·Source Snapshot 승인값도 필요합니다. 현재 안전한 공급 API가 없어 새 계약이나 백엔드 코드를 임의로 구현하지 않았습니다.

최근 프론트엔드 작업 기록은 요청창 통합의 격리 UI 검사 1개와 관련 기존 Mock 5개 PASS, 프로젝트·보관함 전용 Mock 3개 PASS입니다. 변경 파일 ESLint·공백 검사도 통과했으며 번호 1~5 변경은 표시식·공백만 확인했습니다. 기존 64항목 브라우저·103개 단위 테스트·105초 시연은 재실행하지 않았습니다. 실제 Host·LLM 성공 검증과 구분하며, 이번 문서 갱신에서는 테스트·백엔드·DB·서버 실행을 수행하지 않습니다. 상세 범위는 [연동 안내](../FRONTEND_BACKEND_INTEGRATION.md)를 참고합니다.

## 12. 프로젝트 DB 모델 1단계 — 2026-11-11

1단계 당시에는 Project·ProjectRun 모델과 명시적인 신규 테이블 생성까지만 구현했습니다. 프로젝트 API는 아래 2단계에서 추가했습니다. Submission은 상태·요청 식별 규칙이 미확정이어서 모델·테이블을 추가하지 않았으며, Host 호출·중복 생성 처리 로직도 구현하지 않았습니다. 기존 User·Document 모델 및 인증·전자문서 API는 변경하지 않았습니다.

| 테이블 | 컬럼·제약 |
| --- | --- |
| `projects` | `project_id`: UUIDv4 문자열 PK, `owner_user_id`: users.id FK 및 인덱스, `name`, `scenario_id`, `created_at`, `updated_at` |
| `project_runs` | `id`: 정수 PK, `project_id`: projects.project_id FK 및 인덱스, `host_key`: 기본 local-multi, `run_id`, `configuration_id`, `linked_at` |

제품 백엔드의 SQLAlchemy 기본값이 `uuid4()`로 Project ID를 생성합니다. SQLite 자체의 UUID 함수나 Host의 ID를 사용하지 않습니다. 동일 사용자의 여러 프로젝트와 동일 프로젝트의 여러 Run을 허용합니다. `(host_key, run_id)`는 UNIQUE이므로 같은 Host Run을 중복 연결하거나 여러 프로젝트에 동시에 연결하지 않습니다. Run·Step·Trace 원본, 승인 설정 본문, API 키는 저장하지 않습니다.

시간 컬럼은 기존 모델과 같은 `DateTime`·DB 현재 시각 관례를 사용합니다. `updated_at`은 SQLAlchemy를 통한 UPDATE에서 갱신되며 임의의 원시 SQL UPDATE를 감지하는 트리거는 아닙니다. `scenario_id`는 아래 API에서 로컬 SCN-001 레지스트리로 확인합니다. 실제 Host와의 일치 및 `configuration_id` 승인·유효성은 Host 연결 단계에서 추가 검증해야 합니다. API의 camelCase 요청·응답 이름은 아래 2단계에서 정의했습니다.

### 안전한 적용 범위

`create_project_tables(target_engine)`은 호출자가 명시한 Engine에 기존 users/documents가 있는지 먼저 확인한 뒤, 지정된 두 신규 테이블에만 `create_all(checkfirst=True)`을 적용합니다. 기존 테이블·행은 삭제하거나 재작성하지 않으며 반복 호출로 기존 프로젝트/Run 연결도 지우지 않습니다. 이는 최초 테이블 추가 함수이며, 이미 존재하는 프로젝트 테이블의 다른 컬럼 구조를 최신 형태로 변환하거나 검증하는 마이그레이션 도구는 아닙니다.

**1단계 당시에는 실제 app.db에 미적용이었으며, 이후 백업 후 projects·project_runs 적용을 완료했습니다.** 서버 시작 시 신규 테이블을 자동 생성하지 않으므로 모델 파일 추가·개발 서버 재로딩만으로 실제 DB가 확장되지 않습니다. 다른 개발 DB에 최초 적용할 때에도 백업·대상 DB 확인 후 명시적으로 진행합니다.

현재 `database.py`의 SQLite 연결에는 `PRAGMA foreign_keys=ON`이 없습니다. 이번에는 기존 Document의 동작을 바꾸지 않도록 이 설정을 유지했습니다. 따라서 FK는 선언되어 있지만 운영 연결에서 관계 무결성을 강제하려면 기존 데이터의 외래키 위반을 점검하고 모든 DB 연결에 FK 활성화를 적용하는 후속 결정이 필요합니다. UNIQUE·PRIMARY KEY 제약은 FK 설정과 독립적으로 동작합니다. FK 강제 시 연결된 프로젝트/Run이 있는 부모 삭제는 RESTRICT이며 자동 연쇄 삭제하지 않습니다.

### 검증과 다음 합의

임시 SQLite만 사용한 `test_project_models.py` **6개 PASS**: 기존 스키마·데이터 보존과 반복 적용, User→Project·Project→ProjectRun 관계, 다중 연결, UUIDv4·중복 제약, 시간 갱신, 서버 시작 시 자동 확장 방지를 확인했습니다. 관계 테스트는 임시 DB 연결에만 FK 검사를 켰으며 기존 정책을 유지하는 FK OFF 호환성도 별도 확인했습니다.

저장소 루트에서 이 테스트만 실행하는 명령은 다음과 같습니다.

```bash
backend/.venv/bin/python -B -m unittest discover -s backend/tests -p test_project_models.py -v
```

기존 백엔드 50개·프론트엔드·브라우저 전체 검증은 재실행하지 않았습니다. 실제 DB 변경·Host/Docker 실행·OpenAI 호출·Git 작업도 수행하지 않았습니다.

주호님과 합의할 Submission 항목은 처리 상태·초기 상태·전이, 요청 식별값의 대상 필드와 정규화/계산 규칙, 클라이언트 키의 형식·보관 기간, Host 내부 생성 키의 생성 주체·범위·재사용 규칙, 응답 유실 시 확인·재시도 정책입니다. 동일 사용자·프로젝트·클라이언트 키의 UNIQUE 요구사항은 다음 모델에 반영하되 이번에는 임의 구현하지 않았습니다.

1단계에서 후속으로 남겼던 프로젝트 생성·목록·상세·이름 수정과 소유권·로컬 시나리오 검증은 아래 2단계에서 구현했습니다. 실제 ProjectRun 연결 생성은 아직 후속 범위입니다. 실제 Run 생성 접수·중복 처리·Host 호출은 Submission 및 승인 Configuration 계약 합의 후 진행합니다. 동일 Host Run을 여러 프로젝트가 공유해야 한다면 현재 UNIQUE 정책을 적용 전에 재검토해야 합니다.

## 13. 프로젝트 API 2단계 — 2026-11-12

제품 백엔드에 다음 API를 추가했습니다. 모두 기존 Bearer JWT 검증과 DB 세션을 사용하며 Host를 호출하지 않습니다.

| API | 요청 | 응답 |
| --- | --- | --- |
| `POST /api/projects` | `name`, `scenarioId` | 201, 프로젝트 공개 정보 |
| `GET /api/projects` | 없음 | 200, `{ "projects": [...] }`, 본인 프로젝트만 최신 생성순 |
| `GET /api/projects/{projectId}` | UUIDv4 경로 | 200, 프로젝트 공개 정보와 `runs` 배열 |
| `PATCH /api/projects/{projectId}` | `name`만 | 200, 수정된 프로젝트 공개 정보 |

공개 프로젝트 필드는 `projectId`, `name`, `scenarioId`, `createdAt`, `updatedAt`입니다. `name`은 앞뒤 공백 제거 후 1~60자입니다. 생성 시 서버가 UUIDv4를 발급하고 JWT로 확인한 사용자 ID를 소유자로 저장합니다. 요청의 추가 필드는 거부하므로 소유자·Project ID를 지정하거나 PATCH로 시나리오를 바꿀 수 없습니다. 다른 사용자 소유와 존재하지 않는 프로젝트는 동일하게 404를 반환합니다. 인증 실패는 401, 입력 오류는 422입니다.

상세의 `runs`에는 기존 ProjectRun에 저장된 `hostKey`, `runId`, `configurationId`, `linkedAt`만 최신 연결순으로 반환합니다. 연결이 없으면 빈 배열이며, 이 API는 Run을 만들거나 연결하지 않습니다. 소유자 ID·DB 내부 연결 ID·비밀정보·Run 상태·Step·Trace 원본은 반환하지 않습니다. 시간 형식은 기존 모델의 timezone 없는 DateTime 관례를 유지합니다.

### 시나리오 검증 경계

`project_scenarios.py`는 같은 체크아웃의 `src/orchestrator/domain/scenario_registry.py` 정적 정의를 재사용합니다. 이 파일은 표준 라이브러리만 사용하며 Host 패키지 초기화·실행기·DB를 불러오지 않습니다. 현재 MVP는 등록된 `SCN-001`의 UUID만 허용하고 문자열 `SCN-001` 자체는 `scenarioId`로 받지 않습니다. 레지스트리를 읽을 수 없으면 503으로 생성을 차단하며, 미등록 UUID는 422로 거부합니다.

이는 **로컬 코드의 등록 확인**이며 실행 중인 Host의 등록 상태·승인 Configuration·실행 가능 여부를 보장하지 않습니다. 배포 시 해당 레지스트리 파일을 함께 제공해야 하며, 실제 Run 중계 단계에서는 대상 Host와 레지스트리 버전 일치 및 실행 전 재검증 방식을 합의해야 합니다.

### 실제 DB 적용 상태와 다른 환경의 적용 절차

2단계 API 구현 당시에는 실제 DB를 변경하지 않았고 이후 별도 승인 단계에서 `backend/app.db`에 `projects`·`project_runs`를 적용했습니다. 적용 당시 users 6건·documents 4건, 기존 스키마와 `PRAGMA integrity_check=ok`를 확인했습니다. 이 수치는 현재 재조회한 건수가 아닙니다. 서버 시작 시 자동 생성 대상은 기존 User·Document뿐입니다. 신규 스키마가 없거나 프로젝트 DB 작업에 실패하면 SQL·경로·파라미터를 노출하지 않는 503을 반환합니다.

적용 전 대상 DB 경로와 백업을 확인하고, 기존 users/documents 및 이미 존재하는 프로젝트 테이블의 구조를 점검해야 합니다. 이후 승인된 적용 단계에서만 `create_project_tables(target_engine)`을 명시적으로 호출하고 신규 테이블·제약과 기존 행 보존을 확인합니다. 이 함수는 기존 테이블 구조를 변경하는 마이그레이션 도구가 아닙니다. SQLite FK 강제 정책은 바꾸지 않았으며, 활성화 전 기존 FK 위반 점검과 연결별 적용 방식을 별도로 결정해야 합니다.

### 이번 검증과 후속 범위

`test_projects_api.py`의 격리된 ASGI·임시 SQLite 테스트 **8개 PASS**: 인증 사용자 생성, 본인 목록·상세, 타 사용자 차단, 이름 수정과 시간 갱신, 소유자/추가 필드 거부, JWT 검증, 로컬 시나리오 검증 실패, 저장된 Run 연결 이력, 기존 User·Document 스키마·행 보존, 신규 스키마 미적용 시 안전한 오류를 확인했습니다. 테스트용 Run 연결 행은 임시 DB fixture이며 실제 Host Run 생성이 아닙니다.

```bash
backend/.venv/bin/python -B -m unittest discover -s backend/tests -p test_projects_api.py -v
```

기존 모델 6개·백엔드 50개·프론트엔드·브라우저 전체 테스트는 이번에 반복하지 않았습니다. 실제 DB·Host·Docker·OpenAI와 프론트엔드는 변경하거나 실행하지 않았습니다.

후속 Host 중계 범위는 소유권 검증 후 승인 Configuration 확인, Submission 계약에 따른 Run 생성 접수·중복 방지·응답 유실 복구, 확인된 Run의 ProjectRun 연결, 권한 있는 Run·Step·Trace·Artifact 조회입니다. 이 중 읽기 전용 조회는 아래 3단계에서 구현했고 실통신은 차단 상태입니다. Submission 상태와 요청 식별 규칙, Host 내부 생성 키 계약, 공개 Configuration 공급 방식은 주호님과 합의 후 구현합니다.

## 14. 인증 기반 Host 조회 중계 3단계 A — 2026-11-13

제품 백엔드에 읽기 전용 중계를 추가했습니다. 위 1·2단계의 DB 미적용 기록 이후, 실제 개발 DB에는 `projects`·`project_runs` 적용이 완료됐으며 기존 users 6건·documents 4건과 무결성을 확인했습니다. 이번 3단계 A에서는 실제 DB를 변경하지 않고 임시 DB만 사용했습니다.

| 제품 백엔드 경로 | Host 경로 |
| --- | --- |
| `GET /api/v1/runs/{runId}` | `GET /api/v1/runs/{runId}` |
| `GET /api/v1/runs/{runId}/steps` | `GET /api/v1/runs/{runId}/steps` |
| `GET /api/v1/runs/{runId}/events` | `GET /api/v1/runs/{runId}/events` |
| `GET /api/v1/runs/{runId}/issues` | `GET /api/v1/runs/{runId}/issues` |
| `GET /api/v1/runs/{runId}/artifacts` | `GET /api/v1/runs/{runId}/artifacts` |

기존 `read_current_user`로 제품 JWT를 검증하고, `(host_key=local-multi, run_id)`의 ProjectRun을 찾은 뒤 기존 `owned_project`로 소유권을 검사합니다. 무인증은 401, 연결 없음·타 사용자·다른 Host 연결은 동일한 404이며 Host 요청 전에 종료합니다. 이 API는 ProjectRun 연결을 생성하지 않습니다.

Events의 `limit`은 기본 100, 범위 1~500이고 `offset`은 기본 0, 최솟값 0입니다. Host JSON의 필드·배열 순서·페이지 정보는 그대로 전달합니다. 임의 URL·Host 선택·중복/미지원 쿼리는 받지 않으며, Configuration·개별 Artifact·Run 생성/제어 등 나머지 경로는 중계하지 않습니다. 프론트엔드와 Vite 연결은 변경하지 않았습니다.

### 서비스 인증 준비 상태와 실제 연결 차단

현재 `src/orchestrator/main.py`·Run 라우트 및 제품용 Host composition에는 Host 조회 요청의 서비스 토큰을 검증하는 구현이 없습니다. 역할별 Agent 서버의 Bearer 인증은 이 경계와 별개입니다. 저장소에서 `A2A_HOST_API_TOKEN`의 기존 Host 수신 구현도 확인되지 않았습니다.

따라서 `host_reads.py`의 `HOST_SERVICE_AUTH_VERIFIED=False`로 실제 전송을 차단합니다. 환경변수나 브라우저 입력만으로 해제할 수 없습니다. 테스트에서만 이 경계를 대체하고 HTTP 연결을 Mock으로 교체합니다. **현재 소유권 검사를 통과한 실제 요청도 503이며, 실제 Host 연결 완료 상태가 아닙니다.**

준비한 서버 설정은 `A2A_HOST_BASE_URL`(기본 `http://127.0.0.1:8002`)과 `A2A_HOST_API_TOKEN`입니다. 토큰을 `Authorization: Bearer`로 보내는 방식은 Mock으로만 준비했으며, 주호님과 Host 수신 헤더·검증 계약을 확정해야 합니다. 토큰 누락도 503으로 차단합니다. 실제 환경변수·비밀키는 설정하거나 사용하지 않았습니다.

기본 HTTP 클라이언트는 표준 라이브러리를 사용하므로 의존성 추가가 없습니다. 연결/소켓 작업의 타임아웃은 5초, 응답 상한은 8MiB이고 응답·연결은 성공/실패 시 닫습니다. 리다이렉트·환경 프록시·자동 재시도를 사용하지 않으며 평문 HTTP는 loopback 주소에만 허용합니다. 주소에 인증정보·추가 경로·쿼리는 허용하지 않습니다.

Host 연결 실패·타임아웃·서비스 인증 실패·잘못된 JSON/Run ID·과대 응답은 원문 없이 고정 503으로 반환합니다. Host의 404는 고정 404로 변환합니다. 응답 헤더나 원본 오류 본문은 전달하지 않고 서비스 토큰이 성공 JSON에 반사된 경우에도 차단합니다. Agent의 FAILED 또는 최종 판정은 정상 조회 JSON으로 유지하므로 연결 장애와 혼동하지 않습니다. Host 성공 응답의 일반 로그/Artifact 비밀정보 제거 정책은 Host 측 계약을 유지·확인해야 합니다.

### 최소 검증과 남은 합의

`test_host_reads.py` **4개 PASS**: 본인 Run의 5개 GET·서비스 인증 헤더·Event 순서/페이징·FAILED 응답 보존, 타 사용자/미등록/다른 Host 차단, JWT 및 허용 경로/쿼리 제한, 인증 미검증·토큰 미설정·연결/응답 오류의 안전한 실패를 검증했습니다. 모든 전송은 Mock HTTP 연결이고 DB는 임시 SQLite이며, 각 테스트에서 네 테이블의 행이 변경되지 않았는지도 확인합니다. 기존 프로젝트 테스트의 fixture만 재사용하며 기존 테스트 8개는 실행하지 않습니다.

```bash
backend/.venv/bin/python -B -m unittest discover -s backend/tests -p test_host_reads.py -v
```

실제 연결 전 주호님과 서비스 토큰 수신·누락/오류 거부·교체 정책, `local-multi`와 대상 Host의 고정 매핑, 공개 응답의 비밀정보 제거, 응답 크기 제한을 합의해야 합니다. 이후 Host 인증 구현을 검증하고 차단 코드를 해제하는 별도 변경이 필요합니다. 실제 Run 생성·Submission·ProjectRun 연결 기록은 다음 단계이며 이번 중계에 포함하지 않습니다.

### 3단계 B — 서비스 경로 정합성 (2026-11-13)

제품 중계 prefix를 `/api/runs`에서 `/api/v1/runs`로 변경했습니다. `main.py`의 라우터 등록에는 추가 prefix가 없고 Vite에도 rewrite가 없으므로, 기존 React의 `/api/v1/runs/...`와 동일한 경로를 직접 받습니다. 이전 `/api/runs/...` 별칭은 두지 않습니다. 명시적인 GET 5개만 등록하여 향후 `POST /api/v1/runs`와 충돌하지 않으며 기존 `/api/auth`·`/api/documents`·`/api/projects` 경로는 유지합니다.

최종 연결은 브라우저 `/api/v1/runs/...` → 제품 8000의 동일 경로 → Host 8002의 동일 경로입니다. 이번에는 Vite 대상을 변경하지 않았으므로 아직 이 전체 흐름으로 전환된 상태는 아닙니다. React의 제품 JWT 전송기 코드는 3단계 C에서 준비했지만 비활성입니다. `/configuration`·`/api/v1/scenarios` 및 제품 Run 생성 계약은 후속 범위입니다. Host 서비스 인증 미확인에 따른 실통신 차단도 유지합니다.

새 경로 검증 1개만 실행하여 Run·Events Mock GET, 무인증 401, 타 사용자 404, 실통신 차단 503, 이전 경로 및 미구현 POST의 404를 확인했습니다. 기존 4개 테스트는 반복하지 않았습니다.

```bash
cd backend/tests
../.venv/bin/python -B -m unittest test_host_reads.HostReadAPITests.test_service_path_alignment_and_access_guards -v
```


## 15. 현재 React 연결과 다음 작업 — 2026-11-13

- **3단계 C:** 제품 Run용 Bearer 전송기·JWT 누락 차단·401 안내를 Mock 3개로 확인했습니다. `PRODUCT_RUN_API_READY=false`로 기존 8001에 제품 JWT를 보내지 않습니다.
- **3단계 D:** 공개 오피스의 계정 메뉴→로그인/회원가입→오피스 복귀가 연결됐습니다. 요청 초안은 메모리에서 유지하고 토큰이 없는 업무 제출을 차단합니다. 격리 Mock UI 테스트 1개에서 세 가지 핵심 흐름과 실패 처리를 확인했습니다.
- **3단계 E:** `frontend/src/api/projects.js`가 `GET http://127.0.0.1:8000/api/projects`를 제품 JWT로 직접 호출합니다. Vite Orchestrator 프록시와 별개입니다. 목록·빈 상태·JWT 누락/401 등의 Mock 테스트 3개가 통과했으며 실제 Host 통신을 검증한 결과는 아닙니다.

제품 프로젝트 API 4개 중 React에서 연결한 것은 목록 GET뿐입니다. 401은 재로그인 안내, 403/404는 목록 권한/API 오류, 네트워크·서버 오류는 별도 안내로 표시합니다. 조회 중 로그인 상태가 바뀌거나 화면을 떠나면 이전 응답을 적용하지 않습니다. 현재 Run 카드는 별도로 유지하고 저장된 Project와 같은 ID로 취급하지 않습니다.

최종 서비스 경로는 **브라우저 `/api/v1/runs/...` → 제품 8000(JWT·소유권) → Host 8002(서비스 인증)**입니다. 지금은 Vite 기본값 8001·프론트 보호 전송기 비활성·Host 실통신 차단이 모두 유지됩니다. Host 직접 8002 전환 기능은 제품 소유권 중계를 완성하는 방법이 아닙니다.

다음 합의·구현 범위는 Host 서비스 토큰 수신/거부 계약, 안전한 승인 Configuration 공급, 제품 `/configuration`·시나리오 계약, Submission 중복 방지·응답 유실 복구, Run 생성 중계와 ProjectRun 저장입니다. 그 뒤 프로젝트 상세·Run 복원 UI와 프록시/보호 전송기를 함께 연결해야 합니다. `/health`도 현재는 Orchestrator 전용이므로 제품 8000 전환 시 별도 계약 확인이 필요합니다.

이번 문서 갱신에서는 기존 테스트를 재실행하지 않고 코드·문서 링크·공백만 확인했습니다. 실제 DB·비밀키·실행 중 서버에는 접근하거나 변경하지 않았습니다.
