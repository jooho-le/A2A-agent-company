# 61. 승인 요구사항 원장과 출처 참조

## 목적과 적용 범위

60번 자연어 통신 실험에서 발견한 “초기 이용자 설명을 접근 제한으로 확정하는 문제”에 대응하는 첫 보강이다. 이번 단계는 1·2번 담당의 요구사항 권한 경계이며, 프론트·제품 API 중계·로그인·프로젝트 DB 작업과 구분한다.

기존 Run Configuration에 동결한 `scenarioContract`를 승인 기준으로 유지하고, 그 기준의 내용·출처·Run 연결을 명시하는 불변 원장 뷰를 추가했다. 별도의 요구사항 DB나 사용자가 조건을 승인하는 HTTP API를 새로 만들지는 않는다.

## 구현

- `src/orchestrator/domain/requirement_authority.py`의 `freeze_requirement_authority()`가 기존 `RunConfigurationArtifact`에서 원장을 생성한다.
- 요구사항 UUID·표시 키·원문·Acceptance Criteria·검증 담당을 불변 객체와 tuple로 보존한다.
- 현재 Scenario Registry를 다시 조회하지 않아, 진행 중인 Run의 동결 기준이 최신 등록 내용으로 교체되지 않는다.
- 잘못된 UUID, 누락·중복 기준, 중복 JSON 키, NaN/Infinity, 알려진 Credential, 공백 정규화가 필요한 원문은 승인 뷰 생성 단계에서 거절한다.
- 실패는 원문·경로·중첩 예외를 노출하지 않는 `REQUIREMENT_AUTHORITY_INVALID`로 반환한다.

### 모델에 전달하는 참조

| 필드 | 의미 |
| --- | --- |
| `runId`, `scenarioId` | 동결 기준을 사용하는 실행과 시나리오 |
| `configurationArtifactId` | 기준을 보관한 기존 Configuration Artifact |
| `scenarioContractSha256` | 기존 동결 JSON 바이트의 SHA-256 |
| `approvalSource` | `HOST_FROZEN_SCENARIO_CONTRACT` |
| `approvedRequirementIds` | 동결 기준에 존재하는 Requirement UUID |
| `requestCanChangeBaseline` | `false` |
| `clarificationCanChangeBaseline` | `false` |
| `modelCanApproveConditions` | `false` |
| `unapprovedConditionsAction` | `INPUT_REQUIRED` |

기존 `scenarioContract`가 이미 입력에 있으므로 원문과 수용 기준을 모델 입력에 또 복제하지 않고 참조·ID·권한 규칙만 추가한다. SHA-256은 기준 식별값이며 인증 토큰이나 승인 증명으로 사용하지 않는다. 신뢰된 Host 공급 경로가 승인 경계다.

## 개발정의서 점검

- [개발정의서](https://www.notion.so/3e9d062130a9801b943ff3bb463af38a)의 요구사항 보존, UUID 식별, Run Configuration 동결, Agent의 임의 요구 변경 금지를 유지한다.
- 공식 A2A Task·Message·Artifact·상태 및 MCP 권한은 변경하지 않는다.
- 기존 `requirements.json`의 `schemaVersion=1`과 등록된 요구사항·보안·이메일 정책을 변경하지 않는다.
- 모델이 직접 만든 `APPROVED` 문구나 JSON 객체를 원장 입력으로 받지 않는다.

## 검증

`tests/test_requirement_authority.py`에서 동결 원문·해시·ID 보존, Registry 재조회 금지, 불변성, 입력 객체 위조와 손상된 JSON·Credential 거절을 확인한다. Agent와의 연결 검증은 [62번](62-planner-unapproved-assumptions.md)에 이어서 기록한다.

```bash
PYTHONPATH=src:tests .venv/bin/python -m unittest test_requirement_authority -q
```

이 단계의 원장은 기존 승인 조건을 식별한다. 자유로운 자연어에서 정답 요구사항을 자동 승인하거나 문장의 의미가 완전히 정확하다고 판정하는 기능은 아니다.
