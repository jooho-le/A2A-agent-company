# 84번 — 승인 Configuration 조회

## 목적과 기준

제품 서버가 실제 Host의 승인 설정을 HTTP로 받아 프론트 제출 조건에 사용할 수 있도록 했다. [Host·프론트엔드 연동 실행 기준 §4](https://app.notion.com/p/414d062130a981cb9c52c76b89b4369e)의 계약을 구현하며, 기존 `RunConfiguration` 스키마와 제출 승인 검사는 유지한다.

## API

```text
GET /api/v1/host/approved-configuration?scenarioId={등록된 UUIDv4}
Authorization: Bearer <제품 서버 전용 Host 토큰>
```

| 필드 | 의미 |
| --- | --- |
| `scenarioId` | 등록된 시나리오 UUIDv4 |
| `configurationId` | 아래 공개 configuration JSON의 SHA-256 |
| `configuration` | 기존 `RunConfiguration` 공개 객체 전체 |

`configurationId`는 UTF-8, 키 정렬, `ensure_ascii=False`, 구분자 `(',', ':')`, 불필요한 공백 없이 직렬화한 바이트로 계산한다. 누락 가능한 필드도 서버가 반환한 `null`과 기본값을 포함해 해시하며, 반환 객체에서 필드를 임의로 빼고 재계산하지 않는다. 식별자는 기존 Configuration 객체 **바깥**에 둔다.

브라우저는 숫자 직렬화 방식 차이 등을 피하도록 Host가 준 식별자를 그대로 전달한다. 제품 서버가 최신 조회 결과와 대조하는 계약이며 브라우저에서 승인 식별자를 새로 발급하는 구조가 아니다.

## 검증과 공개 범위

`create_host()`가 가진 `approved_run_configuration(platform)`을 재사용한다. 조회 시 등록된 시나리오, Configuration 재검증, 비밀정보 마스킹 필요 여부, 현재 제출 승인 검사를 확인한다. 닫힌 Host나 읽을 수 없는 승인 상태는 503으로 차단한다.

원본 runtime/tools/source 파일과 경로, 모델 API 키, 역할별 Bearer, Host 서비스 토큰, 보호 시험 내용은 반환하지 않는다. 승인 조회는 Run·Workspace·예산·LLM 호출을 생성하지 않는다. 응답과 승인 조회 오류에 `Cache-Control: no-store`를 적용한다.

| 상황 | 응답 |
| --- | --- |
| 인증되고 현재 승인 설정을 조회할 수 있음 | `200` |
| 잘못된 UUID 또는 등록되지 않은 시나리오 | `422` |
| 승인 provider 없음·닫힘·설정 검증 실패·비밀정보 감지 | `503 HOST_CONFIGURATION_UNAVAILABLE` |
| 서비스 토큰 인증 실패 | [83번](83-host-service-authentication.md)의 `401` |

## 제출 경계

제품 API는 프로젝트 소유권과 최신 `configurationId`를 대조한다. Host 본문에는 기존 `scenarioId`, `requestText`, `configuration`만 전달한다. `projectId`, `configurationId`, `approved`를 Host 본문에 추가하지 않는다. Host의 기존 승인 검증은 계속 수행하며 다른 모델·Source·한도를 허용하지 않는다.

`configurationId`는 공개 설정의 식별자이며, 사용자 승인이나 인증 토큰이 아니다. 프론트 표시용 `approved: true`도 서버 인증을 대신하지 않는다. 제품 API에서의 `409 CONFIGURATION_CHANGED` 변환과 프론트 연결은 3번 담당 범위다.

## 검증과 정의서 점검

반복 조회의 객체·해시 일치, 등록되지 않은 시나리오, 준비되지 않은 Host, 비밀정보 포함 설정, 닫힌 Host, 기존 제출 검증을 확인했다. 실제 승인 파일·Docker로 실행한 결과는 아니다.

기존 Run Configuration의 동결·해시·요구사항 기준, A2A 상태·metadata, 판정, MCP 권한, Single/Multi의 조건은 변경하지 않았다. Source와 시험지 연결 규격은 이번 공개 설정 조회와 별개다.
