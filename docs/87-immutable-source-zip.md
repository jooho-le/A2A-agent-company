# 87번 — 불변 Source ZIP 다운로드

## 목적과 API

등록된 Source Artifact의 보존 Snapshot을 ZIP으로 제공한다. [Host·프론트엔드 연동 실행 기준 §8](https://app.notion.com/p/414d062130a981cb9c52c76b89b4369e)의 다운로드 계약을 구현했다.

```text
GET /api/v1/runs/{runId}/artifacts/{artifactId}/download
Authorization: Bearer <제품 서버 전용 Host 토큰>
```

두 ID는 UUIDv4다. 경로·URI·프로젝트명·버전 번호를 별도로 입력받지 않으며 추가 query도 거절한다. 실제 Host 서비스 인증이 필요하고, 인증 없는 Bootstrap에서는 503으로 차단한다. 제품 JWT·프로젝트/Run 소유권은 제품 서버가 먼저 검사한다.

## 검증·변환 순서

1. 해당 Run의 프로젝트 Artifact Registry에 **SOURCE로 등록**되어 있는지 검사한다. Build 전 보관된 staging BLOB만으로는 다운로드할 수 없다.
2. 보존 BLOB의 길이·내용 해시·메타데이터 해시·Run/Step 연결·읽기 권한·동결 실행환경을 기존 저장 계층으로 검사한다.
3. 등록된 Source와 보존 Source의 불변 식별자를 비교한다. 완료 때 붙는 A2A Task/Artifact ID만 달라질 수 있다.
4. Sandbox에서 사용하던 정규화 tar 검사를 재사용한다. 경로·파일 종류·중복·Unicode 별칭·숨은 후행 데이터·자격증명을 검사한다.
5. 같은 파일 경로·바이트·실행 권한을 ZIP에 담는다. 디스크로 추출하거나 생성 코드를 실행하지 않는다.

Working Tree와 Host 파일을 읽어 ZIP에 섞지 않는다. 작업 폴더가 바뀌거나 없어져도 보존 BLOB이 유효하면 다운로드할 수 있다. Multi·Single은 각 Host가 가진 같은 ArtifactStore와 제한을 재사용한다.

## 응답과 해시

파일명은 `source-{runId}-v{codeVersion}.zip`이다. 프로젝트명·사용자 입력은 파일명에 넣지 않는다.

| 헤더 | 의미 |
| --- | --- |
| `Content-Type: application/zip` | ZIP 응답 |
| `Content-Disposition` | 위 파일명으로 attachment |
| `Content-Length` | ZIP 바이트 수 |
| `X-Snapshot-Sha256` | **원본 Source tar**의 SHA-256 |
| `X-Archive-Sha256` | **이번 ZIP**의 SHA-256 |
| `X-Source-Artifact-Id`, `X-Code-Version` | 요청한 Source와 실제 코드 버전 |
| `Cache-Control: no-store`, `X-Content-Type-Options: nosniff` | 저장 캐시·타입 추측 방지 |

서로 다른 압축 형식이므로 Snapshot 해시와 ZIP 해시가 같다고 주장하지 않는다. ZIP은 검사를 모두 마친 뒤 64 KiB 단위로 전달한다. 같은 환경의 같은 Snapshot은 파일 순서·시각·권한을 고정해 같은 ZIP을 만들지만, 압축 라이브러리 버전이 다른 환경까지 ZIP 해시 동일성을 보장하지 않는다.

## 제한과 차단

기본 상한은 파일 1,000개, 파일당 1 MiB, 내용 합계 16 MiB, tar·ZIP 20 MiB다. Host에 더 작은 Git Snapshot 제한이 설정되어 있으면 그것도 적용한다. 기존 Sandbox/Source 보안의 더 엄격한 상한은 그대로 유지한다.

Secret/Git 경로, symlink·hardlink·특수 파일, 중복·경로 충돌, `.data` 등 Host 작업 디렉터리, DB·WAL·로그 파일, `owned-*.local.json`, `.DS_Store`는 차단한다. 비밀정보 탐지는 기존 패턴과 Host가 등록한 비밀값 범위다.

위험한 파일만 빼고 정상 Source라고 반환하지 않는다. 전체 다운로드를 거절하며 불변 원본도 수정하지 않는다.

| 상황 | 응답 |
| --- | --- |
| 해당 Run의 등록 Source/보존 Snapshot 없음·다른 Run·다른 Artifact 종류 | `404 SOURCE_NOT_FOUND` |
| 형식·무결성·안전한 내용 정책 위반 | `409 SOURCE_DOWNLOAD_REJECTED` |
| 승인 용량·파일 제한 초과 | `413 SOURCE_DOWNLOAD_TOO_LARGE` |
| 추가 query | `422 SOURCE_DOWNLOAD_INPUT_INVALID` |
| 저장소·차단 기록 저장 장애 | `503 SOURCE_DOWNLOAD_UNAVAILABLE` |

409/413 차단은 `SOURCE_DOWNLOAD_REJECTED` Trace에 Source ID·버전·Snapshot 해시로 기록한다. 같은 Source의 반복 다운로드 실패는 한 사건으로 남긴다. 원문·경로·비밀값은 기록하지 않으며, 이 사건만으로 기존 Verdict를 바꾸거나 Developer 수정 작업을 자동 생성하지 않는다. 검토해야 할 Source 차단 기록이다.

## 검증과 정의서 점검

실제 임시 Git·SQLite·ASGI로 Working Tree 변경/삭제 후 다운로드, v1/v2 선택, 실행 권한·한글·빈 파일·binary 보존, 원본/ZIP 해시 분리를 확인했다. 미등록 staging·다른 Run·비 Source·변조·위험한 tar·자격증명·DB/로그·용량 초과·미인증 접근을 차단했다. 실제 다운로드 거절의 Trace 중복 방지와 Verdict 유지도 확인했다.

Agent 역할 권한과 Snapshot 읽기 권한을 확장하지 않았다. 성공 판정·최종 평가·코드 수정 한도·Source 원본은 유지한다. 실제 제품 중계·브라우저 다운로드는 3번 담당 연결과 81·82번 실행 확인으로 남긴다.
