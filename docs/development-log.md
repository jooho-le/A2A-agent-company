# A2A Multi-Agent Demo 개발 기록

작성 기준일: 2026-10-05

이 문서는 사용자 제공 개발·수동 테스트·디버깅 기록과 Codex가 앞선 작업에서 수행한 자동 검증 결과를 정리한 것이다. 과거 수동 테스트를 이번 문서 작성 과정에서 다시 실행한 것은 아니다.

## 1. 프로젝트 개요

개발 대상은 다음 두 시나리오이다.

- 시나리오 1: 회원가입 기능
- 시나리오 2: 전자문서 제출·조회 기능

현재는 시나리오 1 회원가입 기능을 개발 중이다.

### 기술 스택

| 구분 | 기술 |
| --- | --- |
| Frontend | React + Vite |
| Backend | Python + FastAPI |
| Database | SQLite |
| ORM | SQLAlchemy |
| Password Hash | pwdlib + Argon2 |
| Version Control | Git / GitHub |

### 작업 브랜치

- `sehwa`

---

## 2. Git / GitHub 개발환경 구성

### 진행 내용

1. GitHub 저장소 `A2A-agent-company`를 확인하였다.
2. 개인 작업 브랜치 `sehwa`를 생성하였다.
3. GitHub 저장소를 로컬 PC로 clone하였다.
4. `git switch sehwa`로 개인 브랜치로 전환하였다.
5. 이후 개발은 `sehwa` 브랜치에서 진행하였다.

### 사용한 주요 명령

```bash
git clone https://github.com/jooho-le/A2A-agent-company.git
git switch sehwa
git branch --show-current
```

### 결과

`sehwa` 브랜치에서 독립적으로 개발할 수 있는 환경을 구성하였다. 문서 작성 시 현재 브랜치가 `sehwa`이고 원격 저장소가 위 GitHub 주소인 것을 확인하였다.

---

## 3. Frontend 개발환경 구성

### 환경 확인

- Node.js 설치를 확인하였다.
- npm 설치를 확인하였다.
- React + Vite 프로젝트를 생성하였다.

### 주요 명령

```bash
node --version
npm --version
npm create vite@latest frontend -- --template react
```

생성한 `frontend` 폴더에서 개발 서버를 실행하였다.

```bash
npm run dev
```

### 결과

사용자 수동 확인 기록에 따르면 React 개발 서버를 다음 주소에서 정상 실행하였다.

```text
http://localhost:5173
```

---

## 4. React 초기 화면 구현

Vite 기본 로고, 카운터, 예제 링크를 제거하고 다음 프로젝트 시작 화면을 구현하였다.

- 제목: `A2A Multi-Agent Demo`
- 설명: `회원가입 및 전자문서 제출·조회 서비스`
- 회원가입 버튼
- 로그인 버튼

Codex를 사용하여 `frontend/src/App.jsx`와 `frontend/src/App.css`를 수정하였다. 이 단계에서는 회원가입과 로그인 버튼을 모두 비활성화하였다.

### 검증

- 사용자 수동 확인: 브라우저에서 `http://localhost:5173`에 접속하여 화면이 정상적으로 출력되는 것을 확인하였다.
- Codex 자동 검증: `frontend` 폴더에서 다음 명령을 실행하여 모두 통과하였다.

```bash
npm run build
npm run lint
```

---

## 5. 회원가입 Frontend 구현

### 구현 기능

- 홈 화면의 회원가입 버튼을 활성화하여 회원가입 화면으로 이동하게 하였다.
- 이메일 입력창에 `type="email"`을 사용하였다.
- 비밀번호 입력창에 `type="password"`를 사용하였다.
- `비밀번호는 최소 8자 이상이어야 합니다.` 안내 문구를 표시하였다.
- 회원가입 버튼을 만들었다.
- 처음 화면으로 돌아가기 버튼을 만들었다.
- React `useState`로 이메일·비밀번호 입력값과 화면 전환 상태를 관리하였다.
- 폼 제출 시 `event.preventDefault()`로 페이지 새로고침을 막았다.

로그인 버튼은 계속 비활성화 상태로 유지하였다. 비밀번호 8자 조건은 이 단계에서 안내 문구로 표시하였고, 실제 서버 검증은 이후 Backend API에서 구현하였다.

### 생성 및 수정 파일

| 파일 | 작업 | 역할 |
| --- | --- | --- |
| `frontend/src/pages/SignupPage.jsx` | 생성 | 회원가입 입력 폼과 제출·복귀 버튼 |
| `frontend/src/App.jsx` | 수정 | 홈 화면과 회원가입 화면 전환 |
| `frontend/src/App.css` | 수정 | 화면 배치, 입력창·버튼 스타일 |

### 검증

사용자 수동 테스트 기록:

| 항목 | 결과 |
| --- | --- |
| 홈 → 회원가입 화면 이동 | PASS |
| 회원가입 화면 → 홈 화면 복귀 | PASS |
| 이메일 입력 가능 | PASS |
| 비밀번호 입력 가능 | PASS |

Codex 자동 검증으로 `npm run build`와 `npm run lint`도 모두 통과하였다.

현재 단계에서는 실제 API 호출을 연결하지 않았다.

---

## 6. Backend 개발환경 구성

### Python 환경

Python `3.14.7`을 사용하였다. 문서 작성 시 가상환경의 Python 버전도 확인하였다.

### 가상환경 생성

`backend/.venv`를 생성하였다. 다음 명령은 `backend` 폴더에서 사용한 환경 구성 명령이다.

```bash
python3 -m venv .venv
source .venv/bin/activate
```

### FastAPI 설치

FastAPI와 Uvicorn을 설치하였다.

```bash
python -m pip install fastapi uvicorn
```

가상환경과 패키지는 Backend 코드 구현 전에 준비되어 있었으며, Codex의 코드 구현 작업에서는 새 패키지를 설치하지 않았다.

### 초기 API

`backend/main.py`에 FastAPI 애플리케이션을 생성하고 `GET /` 요청에 다음 JSON을 반환하도록 구현하였다.

```json
{
  "message": "A2A backend is running"
}
```

### 서버 실행 명령

`backend` 폴더에서 다음 명령으로 실행할 수 있다.

```bash
.venv/bin/python -m uvicorn main:app --reload
```

### 검증 결과

- 사용자 확인 기록: `GET /` 요청 결과 HTTP `200 OK`를 확인하였다.
- Codex 자동 검증: 서버를 장시간 실행하지 않고 FastAPI 애플리케이션에 내부 ASGI 요청을 보내 `200 OK`, JSON 응답 형식, 정확한 메시지를 확인하였다.
- Uvicorn을 정상적으로 불러올 수 있는 것도 확인하였다.

---

## 7. Python 의존성 관리

가상환경 자체를 GitHub에 업로드하지 않고 동일한 개발환경을 재구성할 수 있도록 `backend/requirements.txt`를 생성하였다.

가상환경을 활성화한 `backend` 폴더에서 사용한 명령:

```bash
python -m pip freeze > requirements.txt
```

또한 `backend/.gitignore`에 다음 항목을 추가하였다.

```gitignore
.venv/
__pycache__/
*.pyc
.DS_Store
app.db
```

| 제외 항목 | 이유 |
| --- | --- |
| `.venv/` | 컴퓨터별로 다시 구성하는 Python 가상환경 |
| `__pycache__/` | Python이 자동 생성하는 캐시 폴더 |
| `*.pyc` | Python이 자동 생성하는 컴파일 파일 |
| `.DS_Store` | macOS의 폴더 표시 정보 파일 |
| `app.db` | 사용자 정보와 테스트 데이터가 들어가는 실행 데이터 파일 |

Codex가 `git check-ignore -v`로 각 제외 규칙이 적용되는 것을 확인하였다. `app.db`는 DB 생성 후 별도 작업에서 추가하였고, 기존 제외 항목은 유지하였다.

---

## 8. SQLite / SQLAlchemy 구성

SQLAlchemy를 설치하였다.

```bash
python -m pip install sqlalchemy
```

SQLite 데이터베이스로 `backend/app.db`를 사용하도록 구성하였다. 실행 위치가 달라도 `database.py`가 있는 폴더를 기준으로 DB 경로가 결정되도록 하였다.

### 생성 파일

- `backend/database.py`: SQLite 연결 경로, `engine`, `SessionLocal`, `Base`를 구성하였다.
- `backend/models.py`: `User` 모델을 만들고 `users` 테이블에 연결하였다.

`backend/main.py`는 필요한 범위에서 수정하여 서버 시작 시 `Base.metadata.create_all()`로 테이블이 없으면 생성하도록 하였다.

### 주요 구성 요소

- `engine`: Python 코드와 SQLite 사이의 연결을 관리한다.
- `SessionLocal`: 데이터를 읽고 저장하는 DB 작업용 Session을 생성한다.
- `Base`: 테이블 모델의 공통 기반이며, 모델에서 정의한 테이블 구조를 등록한다.

### users 테이블

| 컬럼 | 구조 |
| --- | --- |
| `id` | 정수, primary key, 자동 증가 |
| `email` | 문자열, 필수, unique, index |
| `password_hash` | 문자열, 필수 |
| `role` | 문자열, 필수, 기본값 `user` |
| `created_at` | 생성 시각, 기본값 현재 시각 |

비밀번호 평문을 저장하는 `password` 컬럼은 만들지 않았다.

`primary key`는 각 회원을 구분하는 고유 번호이고, `unique`는 같은 이메일이 중복 저장되는 것을 막는 DB 조건이다.

### 실제 테이블 생성 검증

사용자 확인 기록에서는 `backend` 폴더에서 다음 명령으로 SQLite 내부 테이블을 조회하였다.

```bash
sqlite3 app.db ".tables"
```

결과:

```text
users
```

SQLAlchemy `User` 모델이 실제 SQLite `users` 테이블로 생성된 것을 확인하였다.

Codex 자동 검증에서도 서버 시작 처리, 반복 시작 처리, 컬럼 구조, 자동 증가, 이메일 중복 금지, 필수 값 조건, 기본 역할과 생성 시각을 확인하였다. 이 단계의 검증용 회원 데이터는 롤백하여 저장하지 않았다.

---

## 9. 비밀번호 해시 처리 준비

비밀번호를 평문으로 저장하지 않기 위해 pwdlib + Argon2 라이브러리를 설치하였다.

```bash
python -m pip install "pwdlib[argon2]"
```

설치 후 `requirements.txt`를 다시 갱신하였다.

회원가입 API 구현 전에 pwdlib와 Argon2가 이미 설치되어 있음을 확인하였다. API 구현 작업에서는 추가 설치 없이 기존 패키지를 사용하였다.

---

## 10. 회원가입 API 구현

### API

```http
POST /api/auth/signup
Content-Type: application/json
```

POST는 서버에 데이터를 보내 처리하도록 요청하는 HTTP 방식이다. 이번 API에서는 새 회원을 생성하기 위해 사용한다.

### 요청 형식

요청 JSON은 클라이언트가 서버에 보내는 데이터이며, 이메일과 비밀번호만 받는다.

```json
{
  "email": "user@example.com",
  "password": "Password123"
}
```

### 구현 기능

- Python 표준 정규식으로 일반적인 이메일 형식을 검사한다.
- 이메일을 소문자로 통일하고, DB 조회에서도 대소문자 구분 없이 중복을 검사한다.
- 비밀번호가 8자 미만이면 거부한다.
- 저장 전에 같은 이메일을 가진 사용자가 있는지 검사한다.
- pwdlib의 `Argon2Hasher`로 비밀번호 해시를 생성한다.
- 해시만 `password_hash` 컬럼에 저장한다.
- 사용자는 요청에서 `role`을 지정할 수 없다. `role` 등 정의되지 않은 항목은 거부하고, 서버에서 역할을 `user`로 지정한다.
- 성공 시 회원 데이터를 저장하고 성공 메시지를 반환한다.
- DB의 unique 조건으로 동시 가입 시의 중복도 막고, 실패한 저장 작업은 롤백한다.
- 검증 실패 응답에서 비밀번호 등 원본 입력값이 그대로 노출되지 않도록 처리한다.

### 성공 응답

HTTP `201 Created`:

```json
{
  "success": true,
  "message": "회원가입에 성공했습니다."
}
```

### 실패 응답

HTTP 상태코드는 요청 처리 결과를 나타내는 숫자이다. 실패 이유는 응답의 `detail`에 담는다.

| 상황 | HTTP 상태코드 | 응답 내용 |
| --- | --- | --- |
| 잘못된 이메일 형식 | `422 Unprocessable Entity` | `올바른 이메일 형식이 아닙니다.` |
| 비밀번호 8자 미만 | `422 Unprocessable Entity` | `비밀번호는 최소 8자 이상이어야 합니다.` |
| 필수 입력 누락, 잘못된 자료형, role 등 추가 항목 | `422 Unprocessable Entity` | 해당 요청 검증 실패 이유 |
| 이메일 중복 | `409 Conflict` | `이미 등록된 이메일입니다.` |
| 중복 외 DB 무결성 오류 | `500 Internal Server Error` | `회원가입 처리 중 오류가 발생했습니다.` |

이메일·비밀번호 검증 메시지는 `detail` 배열의 `msg`에 포함되며, 중복 이메일 메시지는 문자열 `detail`로 반환된다.

### 주요 파일

| 파일 | 역할 |
| --- | --- |
| `backend/main.py` | FastAPI 앱, 서버 시작 시 테이블 생성, 라우터 연결, 검증 오류 응답 처리 |
| `backend/database.py` | DB 연결 설정과 요청별 Session 생성·종료를 위한 `get_db()` |
| `backend/models.py` | `users` 테이블 구조를 정의하는 `User` 모델 |
| `backend/schemas.py` | 회원가입 요청 형식과 이메일·비밀번호 검증 |
| `backend/security.py` | Argon2 기반 비밀번호 해시 생성 |
| `backend/routers/auth.py` | 회원가입 API, 이메일 중복 검사, DB 저장, 성공·실패 응답 |

API 구현 작업에서는 `schemas.py`, `security.py`, `routers/auth.py`를 생성하고 `main.py`, `database.py`를 수정하였다. Frontend, 로그인 기능, CORS 설정은 변경하거나 추가하지 않았다.

---

## 11. QA 테스트 기록

다음 QA-SIGNUP-001부터 QA-SIGNUP-004까지는 사용자 제공 수동 테스트 결과이다. 자동 검증 결과는 이 절의 마지막에 별도로 정리하였다.

### QA-SIGNUP-001 정상 회원가입

**관련 요구사항**

- REQ-001
- REQ-006

**입력**

```json
{
  "email": "test1@example.com",
  "password": "Password123"
}
```

**기대 결과**

- 회원가입 성공
- DB 사용자 생성
- 성공 메시지 반환

**실제 결과**

- HTTP `201 Created`
- `success = true`
- `회원가입에 성공했습니다.`

**판정: PASS**

---

### QA-SIGNUP-002 잘못된 이메일

**관련 요구사항**

- REQ-002

**입력**

```json
{
  "email": "testexample.com",
  "password": "Password123"
}
```

**기대 결과**

회원가입 거부.

**실제 결과**

`올바른 이메일 형식이 아닙니다.` 반환.

**판정: PASS**

---

### QA-SIGNUP-003 짧은 비밀번호

**관련 요구사항**

- REQ-004

**입력**

```json
{
  "email": "test2@example.com",
  "password": "1234"
}
```

**기대 결과**

회원가입 거부.

**실제 결과**

`비밀번호는 최소 8자 이상이어야 합니다.` 반환.

**판정: PASS**

---

### QA-SIGNUP-004 중복 이메일

**관련 요구사항**

- REQ-003

**사전 조건**

`test1@example.com` 사용자가 이미 DB에 존재한다.

**입력**

```json
{
  "email": "test1@example.com",
  "password": "Password123"
}
```

**기대 결과**

두 번째 회원가입 거부.

**실제 결과**

- HTTP `409 Conflict`
- `이미 등록된 이메일입니다.`

**판정: PASS**

### Codex 자동 검증 기록

회원가입 API 구현 직후 기존 가상환경의 Python으로 문법 검사를 수행하고, 별도 메모리 SQLite에 DB 의존성을 연결하여 내부 ASGI 요청을 보냈다. HTTP 테스트용 패키지는 설치하지 않았으며, 장시간 서버도 실행하지 않았다.

| 검증 항목 | 결과 |
| --- | --- |
| Python 코드 문법 검사 | PASS |
| 기존 `GET /` 응답 유지 | PASS |
| 정상 가입의 `201` 응답과 성공 JSON | PASS |
| 잘못된 이메일 및 길이·도메인 조건 거부 | PASS |
| 비밀번호 8자 미만 거부 | PASS |
| 누락·잘못된 자료형·잘못된 JSON 거부 | PASS |
| 요청의 `role` 지정 거부 | PASS |
| 이메일 대소문자에 관계없는 중복 거부 | PASS |
| 정확히 8자인 비밀번호와 한글 비밀번호 처리 | PASS |
| 이메일 태그·하위 도메인 처리 | PASS |
| 중복 조회 후 저장 전에 다른 가입이 발생하는 상황 모사 | PASS |
| DB 저장 실패 시 롤백과 오류 응답 | PASS |

자동 검증에는 `user@example.com` 등의 테스트 주소를 사용하였다. 위 수동 QA의 `test1@example.com`과는 별개의 검증이다. 자동 검증 전후 실제 `backend/app.db` 파일의 체크섬이 같음을 확인하여, 실제 DB에 테스트 데이터가 추가되지 않았음을 확인하였다.

---

## 12. Security 테스트 기록

### SEC-SIGNUP-001 비밀번호 평문 저장 여부 검사

**관련 요구사항**

- REQ-005

**검증 명령**

사용자는 `backend` 폴더에서 다음 명령으로 저장 결과를 확인하였다.

```bash
sqlite3 app.db "SELECT email, password_hash FROM users;"
```

**기대 결과**

사용자가 입력한 `Password123`이 DB에 그대로 저장되지 않아야 한다.

**실제 결과**

사용자 제공 확인 결과를 요약하면 다음과 같다. 해시 전체 문자열은 생략하였다.

```text
test1@example.com | $argon2id$...
```

Argon2 기반 해시 문자열이 `password_hash` 컬럼에 저장되어 있음을 확인하였다.

**판정: PASS**

### Codex 자동 보안 검증

- 별도 메모리 DB에서 저장 값이 평문 비밀번호와 다르고 `$argon2id$`로 시작함을 확인하였다.
- 올바른 비밀번호로 해시 검증이 성공하고 다른 비밀번호로는 실패함을 확인하였다.
- 저장된 사용자의 역할이 `user`임을 확인하였다.
- `role="admin"`과 `role="user"`를 요청에 직접 넣는 경우 모두 거부됨을 확인하였다.
- 검증 오류 응답에 원본 비밀번호가 포함되지 않음을 확인하였다.

---

## 13. 디버깅 기록

### DEBUG-001 Codex UI 실행 실패

**증상**

VS Code에서 Codex 실행 시 다음 메시지가 발생하였다.

```text
The extension could not start its user interface
```

**확인**

`Developer: Show Running Extensions`에서 여러 AI 관련 확장이 동시에 활성화되어 있었다.

- Claude Code
- Codex
- GitHub Copilot Chat

**추정 원인**

여러 AI 확장이 동시에 활성화되면서 VS Code Extension Host가 느려졌을 가능성이 있었다. 이는 당시의 추정이며 확정된 원인은 아니다.

**조치**

불필요한 AI 확장을 비활성화하고 `Developer: Reload Window`를 실행하였다.

**결과**

Codex UI가 정상 실행되고 프롬프트 입력이 가능한 상태를 확인하였다.

---

### DEBUG-002 localhost:5173 접속 실패

**증상**

React 화면 확인 중 `사이트에 연결할 수 없음` 메시지가 발생하였다.

**원인**

VS Code를 종료하면서 실행 중이던 Vite 개발 서버 `npm run dev`도 함께 종료되었다.

**해결**

`frontend` 폴더에서 다음 명령을 다시 실행하였다.

```bash
npm run dev
```

**검증**

`http://localhost:5173`에 정상 접속됨을 확인하였다.

**결과: 해결 완료**

---

### DEBUG-003 favicon.ico 404 로그

**증상**

FastAPI 터미널에 다음 로그가 출력되었다.

```text
GET /favicon.ico 404 Not Found
```

**원인**

브라우저가 `favicon.ico`를 자동으로 요청하지만, 백엔드에서는 해당 아이콘 파일을 제공하지 않았다.

**판단**

`GET /` API는 `200 OK`였으며, 핵심 기능과 무관한 브라우저 자동 요청으로 판단하였다.

**조치**

현재 단계에서는 수정하지 않았다.

**결과**

FastAPI 서버가 정상 동작함을 확인하였다.

---

### DEBUG-004 Control + C 입력 후 FastAPI 서버 종료

**상황**

FastAPI 실행 중 `Control + C`를 입력하였다.

**증상**

서버 종료 로그가 출력되었다.

**확인**

터미널 앞의 `(.venv)` 표시는 계속 유지되었다.

**원인**

`Control + C`는 가상환경 종료 명령이 아니라 현재 실행 중인 Uvicorn 프로세스를 중단하는 명령이다.

**결과**

FastAPI 서버만 정상 종료되었으며 Python 가상환경은 유지되었다.

---

### DEBUG-005 git status에서 backend 파일이 개별 표시되지 않음

**증상**

`git status` 실행 시 `backend/`만 표시되고 내부의 `main.py`, `.gitignore` 등이 개별적으로 표시되지 않았다.

**원인**

Git은 새로 생성된 폴더 내부의 untracked 파일을 폴더 단위로 축약해서 표시할 수 있다.

**확인 명령**

프로젝트 루트에서 다음 명령을 실행하였다.

```bash
git status --untracked-files=all
```

**결과**

다음과 같은 개별 파일을 확인하였다.

```text
backend/.gitignore
backend/main.py
```

---

### DEBUG / SECURITY-006 app.db Git 추적 방지

**문제**

SQLite `app.db`에는 사용자 이메일 및 비밀번호 해시 등 실행 데이터가 저장되므로 GitHub 업로드 대상에서 제외할 필요가 있었다.

**해결**

`backend/.gitignore`의 기존 내용을 유지하면서 다음 항목을 추가하였다.

```gitignore
app.db
```

**검증**

- 사용자 확인 기록: `git status`에서 `app.db`가 표시되지 않음을 확인하였다.
- Codex 확인: 프로젝트 루트에서 다음 명령으로 제외 규칙을 확인하였다.

```bash
git check-ignore -v backend/app.db
```

**결과**

`app.db`는 로컬에서는 사용하지만 Git 관리 대상에서는 제외되었다.

---

## 14. 현재 회원가입 시나리오 결과

현재까지 백엔드 회원가입 요구사항 검증 결과는 다음과 같다.

| 항목 | 결과 |
| --- | --- |
| 정상 회원가입 | PASS |
| 이메일 형식 검사 | PASS |
| 비밀번호 최소 8자 | PASS |
| 이메일 중복 방지 | PASS |
| 비밀번호 Argon2 해시 저장 | PASS |
| 성공/실패 메시지 반환 | PASS |

추가 자동 검증에서 서버 기본 역할 `user` 지정, 요청의 role 변경 차단, 검증 오류 응답의 비밀번호 노출 방지도 확인하였다.

현재 Frontend 회원가입 화면과 Backend 회원가입 API는 각각 구현되어 있으나 아직 서로 HTTP API로 연결하지 않은 상태이다. 로그인 기능과 CORS 설정도 아직 추가하지 않았다.

다음 개발 단계는 React 회원가입 화면과 FastAPI `POST /api/auth/signup` API를 연결하는 것이다. 시나리오 2의 전자문서 제출·조회 기능은 이후 개발 대상이다.

---

## Frontend-Backend 회원가입 API 연결

추가 기록일: 2026-10-05

아래 내용은 기존 기록 이후에 진행한 통합 개발 및 사용자 제공 통합 테스트 결과이다. 현재 진행 상태는 문서 마지막의 「현재 시나리오 1 상태」에 정리하였다.

### 구현 내용

- React 회원가입 화면에서 브라우저 기본 `fetch`를 사용하여 FastAPI를 호출하였다.
- `POST http://127.0.0.1:8000/api/auth/signup`으로 회원가입 요청을 전송하였다.
- `Content-Type: application/json`을 설정하였다.
- 이메일과 비밀번호를 `JSON.stringify`로 JSON 문자열로 변환하여 전송하였다.
- 회원가입 성공 시 백엔드에서 받은 성공 메시지를 화면에 표시하였다.
- 회원가입 실패 시 백엔드에서 받은 실패 메시지를 화면에 표시하였다.
- 네트워크 오류로 백엔드에 연결할 수 없으면 `서버에 연결할 수 없습니다.`를 표시하였다.
- 요청 중에는 중복 클릭을 막기 위해 회원가입 버튼을 비활성화하였다.

요청 예시:

```json
{
  "email": "user@example.com",
  "password": "Password123"
}
```

### CORS 설정

다음 두 개발용 React 주소를 허용하였다.

- `http://localhost:5173`
- `http://localhost:5174`

```python
allow_origins=["http://localhost:5173", "http://localhost:5174"]
```

`allow_origins=["*"]`처럼 모든 출처를 허용하는 설정은 사용하지 않았다.

### 5174를 추가한 이유

Vite 개발 서버의 `5173` 포트가 이미 사용 중이어서 새 Frontend 개발 서버가 `5174`에서 실행되었기 때문이다. 브라우저는 포트가 다르면 다른 출처로 판단하므로, `5174`에서 실행 중인 React 화면도 API 응답을 사용할 수 있도록 허용 주소에 추가하였다.

---

## DEBUG-007 FastAPI Address already in use

### 증상

다음 명령으로 FastAPI를 실행하려고 할 때 오류가 발생하였다.

```bash
uvicorn main:app --reload
```

```text
[Errno 48] Address already in use
```

### 원인

기존 FastAPI 서버가 이미 `8000`번 포트에서 실행 중인 상태에서 새로운 Uvicorn 서버를 다시 실행하려고 하였다.

### 확인

브라우저에서 다음 주소에 접속하였다.

```text
http://127.0.0.1:8000/docs
```

### 결과

Swagger UI가 정상 표시되어 기존 Backend 서버가 정상 실행 중임을 확인하였다.

### 해결

추가 Uvicorn 서버를 실행하지 않고 기존 실행 중인 서버를 그대로 사용하였다.

**판정: 해결 완료**

---

## DEBUG-008 React 개발 서버 포트 변경 및 CORS

### 관찰

Frontend가 기존 `localhost:5173`이 아니라 `localhost:5174`에서 실행되었다.

### 원인

`5173` 포트가 이미 사용 중이어서 Vite가 다음 포트인 `5174`를 자동으로 사용하였다.

### 문제 가능성

FastAPI CORS에는 `localhost:5173`만 허용되어 있었기 때문에 `5174`에서 API 요청 시 브라우저가 요청을 차단할 가능성이 있었다.

### 해결

FastAPI CORS의 `allow_origins`에 다음 주소를 모두 추가하였다.

- `http://localhost:5173`
- `http://localhost:5174`

### 검증

React 회원가입 화면에서 FastAPI 요청이 성공하였다.

---

## INT-SIGNUP-001 정상 회원가입 통합 테스트

### 목적

React → FastAPI → SQLite 전체 연결을 검증한다.

### 입력

```json
{
  "email": "test3@example.com",
  "password": "Password123"
}
```

### 실제 결과

- `OPTIONS /api/auth/signup` → `200 OK`
- `POST /api/auth/signup` → `201 Created`
- React 화면에 `회원가입에 성공했습니다.` 표시
- 사용자 DB 저장 성공

**판정: PASS**

---

## INT-SIGNUP-002 중복 이메일 통합 테스트

### 사전 조건

`test3@example.com` 사용자가 이미 등록되어 있다.

### 입력

```json
{
  "email": "test3@example.com",
  "password": "Password123"
}
```

### 기대 결과

중복 가입을 거부하고 오류 메시지를 표시한다.

### 실제 결과

- Backend에서 중복 이메일 감지
- HTTP `409 Conflict`
- React 화면에 `이미 등록된 이메일입니다.` 표시

**판정: PASS**

---

## 현재 시나리오 1 상태

- 회원가입 화면 구현: PASS
- 정상 회원가입: PASS
- 이메일 형식 검사: PASS
- 비밀번호 최소 8자 검사: PASS
- 이메일 중복 검사: PASS
- Argon2 비밀번호 해시 저장: PASS
- React-FastAPI API 연결: PASS
- Backend 성공 응답 표시: PASS
- Backend 실패 응답 표시: PASS

### 다음 개발 단계

로그인 및 사용자 인증 기능 구현.
