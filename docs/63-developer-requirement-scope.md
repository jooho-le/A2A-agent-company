# 63. Developer 승인 요구사항 대조와 작업 연결

## 목적

[61번 원장](61-approved-requirement-authority.md)과 [62번 Planner 가정 분리](62-planner-unapproved-assumptions.md)를 실제 개발 단계로 이어 붙인다. Developer가 받은 계획과 자신에게 배정된 요구사항을 Host의 동결 기준에 다시 대조하고, Planner의 작업 설명을 새로운 승인 기준으로 취급하지 않도록 한다.

이 단계는 1·2번 담당의 Agent 실행 경계다. 제품 프론트·로그인·BFF·프로젝트 DB 및 평가 담당의 시험지 구현은 포함하지 않는다.

## 변경 내용

`src/agents/runtime/requirement_scope.py`에 I/O 없는 공통 대조 함수를 추가했다.

1. 기존 `RunConfigurationArtifact`에서 승인 원장을 복원한다. 현재 Scenario Registry는 조회하지 않는다.
2. Run·Scenario·Planner Artifact 소유 관계, 전체 요구사항 ID·순서, 원문·Key·Acceptance Criteria를 대조한다.
3. 기존 `PlannerPlan` 검증으로 Task의 요구사항 참조·의존성·전체 요구사항 연결을 확인한다.
4. 역할별 배정은 동결 기준으로 계산한다. Developer는 전체 요구사항을, QA·Security는 해당 validator가 지정된 요구사항을 받는다.
5. 검증한 데이터를 불변 `RequirementScope`와 분리된 JSON 목록으로 반환한다. 실패는 원문과 중첩 예외를 포함하지 않는 `REQUIREMENT_SCOPE_INVALID`다.

기존 Context Loader도 요구사항 복사본을 검증하고 있었다. 이번 변경은 그 검증을 대체하는 것이 아니라 **61번 원장과 연결한 역할별 명시적 입력 및 후속 검증의 공통 경계**를 추가한다.

### Developer 내부 모델 입력

기존 Task·history·metadata admission 이후 Runtime이 다음 필드를 생성한다. 최초 A2A 요청의 여섯 필드는 유지한다.

| 필드 | 의미 |
| --- | --- |
| `requirementAuthority` | 승인 원장의 Run·Configuration Artifact·원문 해시·권한 참조 |
| `requirementScope.role` | 신뢰된 실행 역할 `DEVELOPER` |
| `assignedRequirementIds` | 원장에 있는 전체 승인 요구사항 UUID |
| `requirements` | 동결 원문과 Acceptance Criteria |
| `taskBindings` | Task 표시 키와 승인 Requirement UUID의 연결 |
| `planningArtifactId` | 기존 Requirement Artifact UUID |
| `planningPayloadSha256` | 정규화한 Planner payload의 식별 해시 |
| `plannerTaskTextCanChangeBaseline` | `false` |
| `priorRoleOutcomeIsEvidence` | `false` |
| `coverageIsNotSuccess` | `true` |

동결 Scenario JSON 원문의 해시와 정규화한 Planner payload 해시는 서로 다른 대상이다. 두 값 모두 인증 토큰이나 사람이 승인했다는 증명으로 사용하지 않는다.

재개 답변으로 `requirementAuthority`, `requirementScope`, `runtimeInstructions`, `clarifications`를 덮어쓰려 하면 모델과 MCP 서비스를 다시 호출하기 전에 거절한다. 범위에 없는 정책 선택은 질문으로 돌리며, 답변만으로 동결 기준을 변경하지 않는다.

## 싱글·멀티 적용

Single은 기존처럼 같은 Developer 입력 함수를 사용한다. 구현·수정 단계 모두 같은 대조를 거치며, 별도 개발 Agent를 호출하거나 정체성을 분리하지 않는다.

Developer Draft와 Source/Change/Build Artifact 형식, 실제 파일 변경 관측·Snapshot 생성·Build 실행 근거는 유지한다. 요구사항 연결이 확인됐다는 사실로 코드가 요구사항을 만족한다고 판정하지 않는다.

## 개발정의서 점검

기준: [개발정의서](https://www.notion.so/3e9d062130a9801b943ff3bb463af38a)의 책임 경계·불변 요구사항·Snapshot 검증·싱글/멀티 공정성 계약.

- Orchestrator의 최종 판정 권한과 A2A 공식 상태를 유지한다.
- Developer의 Source 수정 권한과 QA/Security 기준 수정 금지를 유지한다.
- 모델 요약·Task 완료를 Build 또는 제품 성공으로 바꾸지 않는다.
- 기존 Run 예산·수정 최대 3회·Tool 재시도 최대 2회·Context·Source/환경 동결을 유지한다.
- 새 요구사항 승인 API나 DB를 만들지 않는다.

## 검증

`tests/test_requirement_scope.py`에서 동결 원문 대조, 역할별 배정, 외부 Run·누락·중복·순서 변경·조작된 Planner Artifact 거절, 불변성과 Registry/DB 재조회 금지를 확인한다. Developer A2A 검증에는 실제 모델 입력의 원장·Task 연결과 재개 답변의 승인 원장 덮어쓰기 거절을 포함한다.

```bash
PYTHONPATH="$PWD/src:$PWD/tests" .venv/bin/python -m unittest \
  test_requirement_scope test_developer_context test_qa_context \
  test_security_agent_context test_agent_role_contracts test_single_agent_prompts -q
```

기본 검증 **101개 통과**. Agent·Pipeline·수정·재개·예산 회귀 결과는 함께 수행한 [64번 문서](64-independent-validation-criteria.md)에 기록한다.

## 한계와 다음 작업

Planner 작업 설명의 모든 문장이나 생성 코드의 의미를 자동 판독한 것은 아니다. 숨은 추가 조건은 개발 모델이 따를 수 있으므로 독립 테스트·보안 근거·실제 모델 반복 검증이 계속 필요하다. 이어지는 64번에서 생성 테스트의 기대값을 승인 기준과 대조한다.
