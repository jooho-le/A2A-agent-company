# 14. Orchestrator 개발정의서 준수 보완 및 안전 복구

> 범위: 1번 담당 Orchestrator/A2A Client/상태 DB/Trace.
> 기준: 사용자가 제공한 개발정의서의 확정 정책. 실제 Agent/MCP 구현, 웹 서비스, 비교 실험 수행과 팀 통합은 제외한다.

## 수정한 내용

| 문제·누락 | 반영한 동작 |
| --- | --- |
| 비밀정보가 Message·DB·로그로 노출될 수 있음 | 공통 마스킹을 A2A 요청·Run 입력·보고서/Issue 저장·로그 메시지/예외에 적용. Agent 인증은 환경변수의 HTTP 헤더로만 전달 |
| Report 버전과 코드 후보 번호를 혼용 | Artifact lineage version과 codeVersion을 분리. Build 실패 후 첫 QA Report가 version 1이어도 codeVersion 2의 실패를 수정 가능 |
| 수정 요청에 원인·근거가 부족 | 마스킹한 설명, 기대/실제 결과, Rule·Location, evidenceRef 및 Source/Build/QA/Security Record/URI 전달 |
| 재개할 때 수정 횟수가 증가 | 신규 FIX_REQUIRED→FIXING만 증가. 세 번째 FIXING도 같은 Step/Task로 재개하며 횟수 유지 |
| 마지막 실패 Issue 및 필수 Trace 누락 | 최종 FAIL에서도 Issue 보존. ARTIFACT_REGISTERED, SNAPSHOT_FROZEN, VALIDATION/REVALIDATION, ISSUE_DETECTED, FIX_REQUESTED/COMPLETED, VERDICT_CREATED, RUN_FINISHED 등을 저장 |
| 수정 후 Issue가 해결됐다고 잘못 표시 | Build는 실제 새 후보의 Tool/Build 근거로 판정. Security는 보고서마다 바뀌는 Finding ID가 아니라 Rule·Requirement·Location으로 재발 대조 |
| 서로 다른 Build/QA 원인을 같은 Issue로 간주 | Rule/Test·Requirement·정규화 Location 기반 fingerprint 사용. 동일 오류 2회 연속 수정 후 재발 시 검토 |
| LOW Finding을 검토 대상으로 올림 | LOW/INFO는 Report 기록만 수행. 필수 Requirement의 실제 실패는 별도로 차단. MEDIUM의 수용 정책은 임의 결정하지 않음 |
| 실제 Tool 실행/재시도 근거 없이 최종 판정 | Tool executionId·Manifest·attempt 0~2·오류 종류·retrySafe·근거 참조를 검증·저장. 필수 UNVERIFIED와 안전 Retry 2회 소진 증거가 있어야 최종 UNVERIFIED |
| Run Configuration과 Workspace 발급 누락 | Run 생성 트랜잭션에서 server-issued workspaceId와 불변 설정 Artifact 저장. Scenario 요구사항·보안/이메일 정책도 동결 |
| Planner 결과가 재시작 후 소실 | Requirement Artifact와 payload를 append-only 저장하여 Developer 재개에 사용 |
| Artifact URI 및 조회 경로 부족 | Source/Change/Build/QA/Security/Requirement URI와 Run별 metadata 조회 API 추가 |
| 설정 Artifact와 일반 Artifact UUID가 충돌 가능 | 별도 테이블 간 전역 Artifact UUID 중복도 양방향 거부하고 설정 UUID UNIQUE 인덱스 적용 |
| 전송 중단 후 중복 실행 위험 | 정상 Pipeline과 복구가 같은 PID 기반 DB lease 사용. 알려진 Task는 GET, 전송 전임이 확인되는 Step만 신규 전송 |
| 취소가 로컬 상태만 변경 | 원격 CANCELED 확인을 Task별로 즉시 저장하고, 모든 대상 확인 후 ABORTED. 중간 실패 시 이미 확인한 결과를 보존 |
| 요청 검증 및 opaque Task ID 처리 결함 | 공백 요청/미등록 Scenario 422, Task ID `.`/`..`도 안전한 URL segment로 인코딩. 저장 ID·Context·Hash는 변경하지 않음 |
| Manifest Schema 중복 정의가 달랐음 | gitObjectFormat별 SHA1/SHA256 전체 Hash 길이 조건 통일. 최신 문서 연결 정리 |

## 추가 API

기본 prefix는 `/api/v1`이다.

| API | 용도 |
| --- | --- |
| GET /scenarios | 등록 Scenario ID와 기준 확인 |
| GET /runs/{runId}/artifacts | 불변 Run Configuration 및 Requirement/Source/보고서 metadata 목록 |
| GET /runs/{runId}/artifacts/{artifactId} | 해당 Run 소유 Artifact만 조회 |
| GET /runs/{runId}/issues | 최초 발견 및 append-only 수정/재검증 이력을 합친 Issue 조회 |
| GET /runs/{runId}/configuration | 실행 시작 때 동결한 설정·Scenario 계약·checksum |
| GET /runs/{runId}/workspace | 모델에 Host Path를 노출하지 않는 workspaceId·역할별 경로 계약 |
| GET /runs/{runId}/tool-attempts | MCP 논리 실행별 최초 호출·재시도 근거 |
| POST /runs/{runId}/resume | WAITING_INPUT/HUMAN_REVIEW의 저장 단계 재개 |
| POST /runs/{runId}/recover | 중단된 활성 단계 또는 일시 중지 단계의 명시적 복구 |
| POST /runs/{runId}/cancel | 원격 Task 취소 확인 후 Run 중단 |

재개·복구 요청 예시:

```json
{
  "workflowStepId": "선택할-내부-Step-UUID",
  "inputData": {"scope": "회원가입 MVP 범위 유지"}
}
```

별도 입력이 필요 없는 경우 `{}`로 요청한다. workflowStepId는 선택 항목이며 해당 Run의 현재 단계 Step만 지정할 수 있다. INPUT_REQUIRED는 기존 Task/Context로 사용자 입력을 전달하고 AUTH_REQUIRED는 운영자가 서버 인증 설정을 적용한 뒤 같은 Task를 이어간다. 입력·인증 후속 전송이 불명확하게 중단됐다면 먼저 GET만 수행하며 후속 Message를 자동 재전송하지 않는다.

저장된 Task가 이미 COMPLETED지만 Artifact 소비 전 중단된 경우에도 GET으로 같은 Task를 회수한다. 실패/취소된 Task를 새 요청으로 둔갑시키거나 사람이 임의 SUCCESS를 주는 API는 제공하지 않는다.

## 실행 설정 및 경계

Run 생성 요청의 선택 `configuration`에 모델 ID/리비전/온도, 시작 Commit/Snapshot, Runtime 예산, 이미지·Lock Digest, Hardware·Network 정책, 보호된 테스트/Scanner 참조 등을 담을 수 있다. experimentId를 지정하면 비교 실험 기준을 완전하게 제출해야 한다. 일반 로컬 Run의 미지정 모델/환경은 null이며 가짜 값을 채우지 않는다. 설정과 Scenario 정책은 저장 후 UPDATE/DELETE할 수 없다.

Developer Source의 이미지·Lock Digest가 선언한 실행환경과 다르면 판정을 보류한다. 실제 모델 선택·실행 예산 강제·Sandbox/Network/보호된 테스트 권한은 실행하는 Agent/MCP/Evaluation과의 계약이므로, 설정 metadata를 저장했다는 것만으로 실제 환경 준수를 주장하지 않는다.

MCP Retry의 수행 주체는 Agent/MCP다. Orchestrator는 전달받은 구조화된 실행 이력을 검증하여 Trace에 수집한다. Tool 종료 PASS는 호출 실행 완료를 뜻하며 Build 컴파일 FAIL·QA assertion FAIL·보안 Finding 같은 제품 결과와 구분한다. 동일 실행 ID의 이미 기록된 attempt는 변경할 수 없다. 근거가 없거나 Retry 정책과 다른 이력은 자동 SUCCESS/FAIL/UNVERIFIED의 증거로 사용하지 않는다.

원본 Archive bytes 다운로드, Artifact 저장소 ACL, READ_ONLY 파일 권한, 실제 Build/Test/Scanner 실행은 팀원 구현·연동 범위로 남긴다. workspace Registry는 서버가 해석할 신뢰 경로 metadata이며 모델 입력에 Host Path를 허용하는 기능이 아니다.

## 안전 제한

- 정상 Pipeline/재개/복구/취소 제어가 이미 진행 중이면 409를 반환한다. 진행 중 worker를 강제로 끊는 기능은 없다. Polling 중단/시간 초과 후 저장된 원격 Task를 재개하거나 취소할 수 있다.
- 전송 기록은 있으나 원격 Task ID를 모르면 재전송·취소 완료를 추정하지 않는다. 운영자의 원격 상태 확인이 필요하다.
- DB lease는 단일 호스트 SQLite MVP용이다. PID 생존이 확인되거나 불명확하면 잠금을 빼앗지 않으며, 죽은 PID로 확인되는 잠금만 회수한다. 다중 호스트 운영은 별도 설계가 필요하다.
- 이전 DB는 workspaceId를 안정적으로 보충하지만 없는 Planner payload·실제 모델/환경 값을 만들어내지 않는다. 해당 이력이 없는 Run은 근거 복원 전 재개할 수 없다.
- 과거 append-only Artifact/Trace에 이미 기록된 비밀값은 원본을 자동 삭제·수정하지 않는다. 조회는 마스킹하나 기존 DB 정리·토큰 폐기가 필요하면 별도 승인 작업이다.
- 마스킹은 비밀 필드명과 표준 Token/비밀번호 Hash 패턴을 대상으로 한다. 라벨 없는 임의 문자열을 비밀번호라고 추론하지 않으므로 실제 비밀값을 자연어 요청에 넣지 않는다.

## 검증과 Git 인계

```bash
PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -v
git diff --check
```

검증에는 Build FAIL→수정 Build PASS→첫 QA FAIL→추가 수정 SUCCESS, 최종 후보 Issue 보존, LOW 기록, Retry 소진 UNVERIFIED, Context 재사용, 세 번째 Fix 안전 복구, 완료된 Validator 결과 회수, opaque ID, 마스킹, DB 잠금 충돌을 포함한다. Fake Client 기반 자동 검증이며 실제 팀원 서버/MCP/제품 검증 완료를 의미하지 않는다.

최종 검증 결과: 자동 테스트 172개 통과, `git diff --check` 통과, Project JSON Schema 전체 JSON 구문 확인 및 OpenAPI 신규 경로/유효한 Scenario 요청 예시 확인 완료.

커밋 메시지 제안: `오케스트레이터 정의서 위반 수정 및 안전 복구 보완`

당시 다음 작업 계획: 15번 — 로컬 Mock Agent 데모 실행·조회 안내 정리.

이후 담당 범위가 1번+2번으로 확장되어 15번 계획은 [Agent/MCP 패키지 및 설정 기반](15-agent-mcp-bootstrap.md)으로 변경했다. 이 문서의 Orchestrator 구현 이력은 유지하며, 실제 Agent/MCP 구현은 15번부터 별도로 기록한다. 3번 웹 서비스·4번 평가 및 그 연동은 새 로드맵에서도 제외한다. Git commit/push는 수행하지 않는다.
