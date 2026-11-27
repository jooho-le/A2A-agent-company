# 83번 — Host 서비스 인증

## 목적과 적용 범위

제품 서버 → 실제 Host 사이에 전용 Bearer 인증을 추가했다. 브라우저의 제품 JWT는 제품 서버가 검증하고, 제품 서버는 별도의 `A2A_HOST_API_TOKEN`으로 Host를 호출한다. 기준은 [Host·프론트엔드 연동 실행 기준 §10](https://app.notion.com/p/414d062130a981cb9c52c76b89b4369e)과 개발정의서의 비밀정보·역할 분리 원칙이다.

`create_host()`로 조립하는 Multi·Single Host에 동일하게 적용한다. 기본 Bootstrap과 낮은 수준의 `create_platform()`/`create_single_platform()` 조립 함수는 기존 개발·테스트 경로이며, 인증된 제품 진입점으로 대신 사용하지 않는다. 이번 작업은 `jooho`에서 수행했으며 최신 `dev`와 Host 구현 파일이 같은 것을 확인했다. 제품/프론트 변경을 병합하거나 수정한 작업은 아니다.

## 구현

- `host_access.py`: 라우팅·본문 검증·DB 접근 전에 `/api/v1`과 하위 경로의 토큰을 검사하는 ASGI middleware.
- `create_host()`: 프로세스 환경 또는 명시적으로 전달한 `environ`에서 전용 토큰을 읽어 앱에 연결.
- `run_host --check-secrets`: 기존 역할/모델 자격증명과 Host 자격증명을 함께 검사. 일반 오프라인 검사는 계속 자격증명을 요구하지 않음.
- 토큰을 기존 메모리 비밀정보 마스킹 계층에 등록. 요청 본문·Trace·Artifact·공개 설정에 토큰을 추가하지 않음.

토큰은 32~8,192자의 Bearer 호환 ASCII 문자열이어야 하며, 모델 API 키나 역할별 토큰을 재사용하면 거절한다. 값이 없거나 형식이 잘못되면 DB·Provider 생성 전에 Host 조립을 거절한다. 설정 파일에는 실제 토큰 값을 넣지 않는다.

## HTTP 계약

| 상황 | 응답 |
| --- | --- |
| 전용 `Authorization: Bearer …` 일치 | 기존 라우트 처리 |
| 없음·다른 토큰·제품 JWT·잘못된 방식·중복 Authorization | `401 HOST_API_UNAUTHORIZED` |
| 인증이 필요한 앱에 서버 토큰이 연결되지 않은 상태 | `503 HOST_API_NOT_CONFIGURED` |
| 인증 오류 헤더 | `WWW-Authenticate: Bearer`, `Cache-Control: no-store` |

등록 목록, Run 생성·조회, Step·Trace·Issue·Artifact, 취소·재개, 앞으로 추가할 Submission·다운로드에도 같은 경로 경계를 적용한다. `/health`, `/docs`, `/openapi.json`은 실행 자격증명을 제공하지 않는 진단 경로로 유지한다. 브라우저 직접 접근용 CORS나 인증 예외를 추가하지 않았다.

## 사용 설정

운영자가 안전하게 발급한 **서로 다른** 자격증명을 두 서버 프로세스에 주입한다. `.env.example`에는 변수 이름만 추가했다. 실제 `.env`와 사용자 설정은 변경하지 않았다. Owned Host는 `.env`를 자동으로 읽지 않으므로 파일에 값이 있는 것만으로 준비 완료라고 보지 않는다.

## 검증과 한계

인증 없는 잘못된 JSON도 본문 오류 대신 401로 차단하며 Run이 생기지 않는 것을 확인했다. 실제 Host factory를 Fake Provider로 조립해 인증과 비밀정보 없는 승인 조회를 확인했다. Single도 기존 실행 검사에 전용 인증 헤더를 적용했다.

최종 관련 회귀 검사는 240개 통과했다. 가상환경에 pytest가 없어 기존 unittest 검사로 실행했으며, 실제 유료 API·제품 서버·Docker 연결 성공을 의미하지 않는다. 실행 명령은 [85번](85-idempotent-run-submission.md)에 기록한다.

제품 JWT·사용자/프로젝트/Run 소유권 검사는 3번 담당의 제품 중계 책임이다. Host 서비스 토큰으로 사용자 권한 검사를 대체하지 않는다. 실제 실행 확인은 기존 81·82번으로 남긴다.
