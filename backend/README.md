# A2A Agent Company 제품 백엔드

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

이 디렉터리는 역할 3인 웹 애플리케이션 및 시연 화면(Web Application / Demo)이 사용하는 FastAPI 제품 백엔드입니다. 회원가입·로그인, JWT 인증, PDF 전자문서 제출·조회·다운로드를 제공합니다.

사용자와 문서 메타데이터는 SQLite에 저장하고, PDF 원본은 로컬 파일 시스템에 저장합니다. AWS·S3 등 외부 저장소는 사용하지 않습니다. 이 서버에서 LLM을 호출하거나 Agent·MCP·Docker를 실행하지 않습니다.

Orchestrator는 별도의 시스템입니다. 제품 백엔드의 사용자·문서 기능과 Orchestrator의 Run·Step·Trace 기능을 구분해야 합니다.

## 2. 폴더 구조와 주요 파일

```text
backend/
├── README.md                 # 제품 백엔드 실행·연동 안내
├── main.py                   # FastAPI 앱, 테이블 생성, CORS, 라우터 등록
├── database.py               # SQLite 경로, engine, SessionLocal, Base, get_db
├── models.py                 # User·Document ORM 모델
├── schemas.py                # 요청 검증과 공개 응답 구조
├── security.py               # Argon2 비밀번호 처리와 JWT 발급·검증
├── requirements.txt          # 버전이 고정된 Python 의존성
├── .gitignore                # 가상환경·DB·업로드 파일 등의 Git 제외 설정
├── routers/
│   ├── auth.py               # 회원가입·로그인·현재 사용자 인증
│   └── documents.py          # PDF 업로드·목록·다운로드
├── tests/
│   └── test_documents.py     # 문서 API와 기존 인증 기능 회귀 테스트
├── .venv/                    # 로컬 Python 가상환경
├── app.db                    # 실행 과정에서 생성되는 SQLite DB
└── uploads/                  # 최초 업로드 시 생성되는 PDF 저장 폴더
```

`.venv/`, `app.db`, `uploads/`는 로컬 환경의 파일이며 Git 추적 대상에서 제외합니다.

`main.py`는 서버 시작 시 `Base.metadata.create_all(bind=engine)`으로 없는 테이블을 생성합니다. 기존 테이블의 컬럼을 자동 변경하는 마이그레이션 기능은 없습니다.

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

토큰 갱신·폐기, 서버 로그아웃, 비밀번호 재설정, 소셜 로그인, 담당자 목록 API, 문서 삭제·수정·검색·페이지 나누기, 클라우드 저장소, 악성코드 검사, DB 마이그레이션은 구현되어 있지 않습니다. `role` 컬럼이 있다는 이유로 관리자 권한 시스템이 구현된 것은 아닙니다.

### 별도 환경에서 확인할 사항

이번 문서 작업에서는 실제 Uvicorn 서버나 브라우저를 실행하지 않았습니다. 실제 브라우저의 CORS·토큰 만료 처리, 한글 파일명 다운로드, 배포 환경의 파일 권한·백업·동시 요청, SQLite 외래키 강제 정책과 기존 DB 구조 변경 절차는 별도 확인이 필요합니다.

자동 테스트의 원본 바이트 일치는 다운로드 구현 검증이며, 운영 저장 파일의 지속적 무결성 감시를 제공한다는 뜻은 아닙니다. 전체 운영 보안과 부하·장애 복구가 모두 검증된 상태로 설명하지 않습니다.

## 11. Orchestrator와 제품 백엔드의 경계

| 구분 | 제품 백엔드 | Orchestrator |
| --- | --- | --- |
| 주요 코드 | `backend/` | `src/orchestrator/` |
| 담당 기능 | 사용자 인증과 전자문서 | Agent 협업 실행과 상태·산출물 관리 |
| 현재 프론트엔드 연결 주소 | `http://127.0.0.1:8000` | Vite 프록시 대상 `http://127.0.0.1:8001` |
| 주요 API | `/api/auth/*`, `/api/documents` | `/api/v1/runs` 및 관련 조회 |
| 데이터 | `backend/app.db`, `backend/uploads/` | 별도의 플랫폼 저장소와 실행 설정 |

제품 백엔드 실행 명령은 Orchestrator나 네 Agent를 시작하지 않습니다. 제품 사용자의 JWT를 플랫폼 공통 인증으로 사용하는 기능도 현재 연결되어 있지 않습니다. 현재 전자문서 API 호출은 Agent가 전자문서 기능을 자동 개발·검증하는 실행과 다릅니다.

Orchestrator의 승인 설정과 실행 계약은 팀 공용 플랫폼 문서를 기준으로 합니다. 실제 LLM·Docker를 포함한 End-to-End 실행은 아직 검증 전이며, 이 제품 백엔드의 자동 테스트 통과와 구분합니다.
