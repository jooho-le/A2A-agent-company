# 66. Host의 역할별 Artifact 인수 경계 강화

## 왜 필요한가

기존 제품 Parser는 필요한 이름의 Artifact가 있는지와 내부 내용은 검사했지만, **그 외에 추가된 Artifact는 무시**할 수 있었다. 예를 들어 Planner가 올바른 `requirements.json` 옆에 다른 역할의 결과물을 붙여도 필요한 계획만 찾아 다음 단계로 진행할 여지가 있었다.

이제 Task 전체의 Artifact 묶음이 해당 역할의 계약과 정확히 맞는지 먼저 검사한다. [65번](65-natural-role-output-contracts.md)이 자연어 진단 모델의 역할 필드를 다룬다면, 이번 단계는 실제 제품 Orchestrator·Agent가 결과를 인수하는 경계다.

## 허용하는 결과물

`src/orchestrator/domain/role_outputs.py`에 프로젝트 정책을 한 곳으로 모았다. 내부 인수 정책 버전은 `1`이다.

| 역할 | 허용 이름 | 정확한 개수 |
| --- | --- | --- |
| Planner | `requirements.json` | 1 |
| Developer | `source-snapshot.json`, `change-report.json`, `build-report.json` | 3 |
| QA | `qa-report.json` | 1 |
| Security | `security-report.json` | 1 |

이것은 **프로젝트의 업무 결과 계약**이다. A2A 표준 자체가 모든 서비스에 같은 이름과 개수를 강제한다는 뜻은 아니다. Source archive bytes는 기존 저장소와 URI로 제공하며, 새로운 추가 Task Artifact를 요구하는 기능으로 바꾸지 않는다.

Agent의 역할 선언도 같은 이름 집합과 일치하는지 확인한다. 이후 역할 선언만 바꾸고 Host 정책을 빠뜨리는 변경이 있으면 조용히 다른 계약으로 실행하지 않는다.

## 인수 검사 순서

`validate_output_envelope()`는 네트워크·파일·DB·모델 호출 없이 다음을 검사한다.

1. 전달받은 객체가 완료된 A2A Task이고, 신뢰된 실행 역할·Run/Step UUIDv4가 유효한지 확인한다.
2. Artifact 이름 집합과 개수가 역할 계약과 정확히 같은지 확인한다. 순서는 달라도 되지만 누락·추가·중복 이름은 거절한다.
3. A2A Artifact ID는 공백만 있는 값을 거절하고 Task 안에서 중복되지 않아야 한다. UUID로 변환하거나 trim해서 바꾸지 않는다.
4. 각 Artifact에 `application/json` data Part가 정확히 하나 있어야 한다.
5. 아래 project metadata를 대조하고, 서로 다른 Artifact의 project UUID도 중복되지 않아야 한다.
6. 이후 기존 역할별 payload·Requirement·Snapshot·실행 근거 검사를 그대로 수행한다.

| Metadata | 규칙 |
| --- | --- |
| `runId` | 현재 Run과 일치 |
| `workflowStepId` | 결과를 낸 현재 Step과 일치 |
| `projectArtifactId` | UUIDv4, 같은 Task 안에서 중복 금지 |
| `artifactVersion` | 양의 정수 값. ProtoJSON의 `1.0`은 허용하고 bool·문자열·소수·NaN/Infinity는 거절 |

Project metadata는 이 네 필드만 허용한다. 임의의 `verdict=SUCCESS` 같은 필드를 승인 근거로 추가할 수 없다. A2A Artifact의 표준 표시 속성을 전부 금지하는 규칙과는 구분한다.

## 실제 적용 경로

세 public Parser에 공통 인수 검사를 연결했다.

```text
완료 Task 회수
→ 역할별 Artifact 묶음 인수
→ 기존 Planner / Developer / QA·Security payload 검증
→ 승인 요구사항·Snapshot·실행 근거 검증
→ Registry 등록 및 후속 Step / Verdict 계산
```

| 파일 | 적용 지점 |
| --- | --- |
| `application/planner_output.py` | Planner 계획 소비 전 |
| `application/developer_output.py` | Source/Change/Build 결과 소비 전 |
| `application/validation_output.py` | QA/Security Report 소비 전 |
| `application/output_envelope.py` | 공통 이름·개수·ID·Part·metadata 검사 |
| `agents/roles/contracts.py` | Agent 선언과 Host 결과 이름 집합 대조 |

Agent의 완성 출력 검사와 Single의 단계별 결과도 이 Parser들을 사용하므로 같은 경계를 거친다. Orchestrator가 Agent 패키지의 Prompt 코드를 직접 실행하는 구조로 바꾸지는 않았다.

### 잘못된 결과가 들어오면

| 상황 | 결과 |
| --- | --- |
| Planner 결과에 추가 Artifact | 계획을 등록하지 않고 Developer 호출 전 검토 상태로 이동 |
| Developer 결과에 추가 Artifact | Requirement만 보존하고 새 후보 등록·QA/Security 호출을 중단 |
| QA 또는 Security 결과에 추가 Artifact | 후보는 보존하고 해당 검증 묶음의 보고서 등록·최종 성공을 차단 |
| 올바른 이름이지만 중복 A2A ID | 내부 payload의 ID도 맞춰 넣었더라도 거절 |
| 올바른 Artifact 묶음이나 잘못된 내용 | 기존 payload·Snapshot·근거 검사에서 추가 거절 가능 |

직접 인수 함수는 원문·알 수 없는 이름·SDK 중첩 예외를 담지 않는 `ROLE_ARTIFACT_ENVELOPE_INVALID`를 반환한다. Public Parser와 Dispatcher는 기존 오류·`HUMAN_REVIEW` 경로를 사용한다. 새 A2A 상태를 추가하거나 이미 완료된 원격 Task를 다른 상태로 위장하지 않는다.

관측용 Task/Trace에 원격 결과의 ID가 남을 수 있다. 이를 유효한 프로젝트 Artifact로 등록했다는 뜻으로 사용하지 않는다. 추가 결과물을 지워 정상 묶음으로 보정하거나 Agent를 자동 재호출하지 않는다.

## 검증 결과

| 검증 묶음 | 결과 | 확인 내용 |
| --- | --- | --- |
| 역할 계약·공통 인수·Parser 등 기본 검증 | 84개 통과, 0.556초 | 혼합/추가/누락/중복, 불변 입력, ID·버전·Part·metadata |
| Host 인수·Dispatch·기존 Workflow 회귀 | 54개 통과, 15.995초 | 실제 SQLite에서 등록·후속 호출 전 차단, 검토 Trace, 기존 수정·인계 |
| 싱글·멀티 Runtime 통합 | 4개 통과, 33.616초 | 정상 후보 검증, 미검증 Security 보존, Single 성공 및 QA 실패 후 수정·재검증 |
| 자연어·기존 통신 진단 | 20개 통과, 4.638초 | 65번 역할별 응답과 전달 승인 |

기본 묶음과 진단 묶음에 공통으로 들어간 9개는 중복 계산하지 않는다. 두 단계에서 **서로 다른 관련 검증 153개**를 확인했다. 전체 저장소 테스트 또는 실제 서비스 성능 측정 결과는 아니다.

```bash
PYTHONPATH=src:tests .venv/bin/python -m unittest \
  test_output_envelope test_agent_role_outputs test_planner_output \
  test_artifact_contract_boundaries test_agent_role_contracts -q

PYTHONPATH=src:tests .venv/bin/python -m unittest \
  test_output_handoff test_dispatch test_workflow_contract_regressions \
  test_planner_developer_continuation -q
```

`tests/test_output_handoff.py`는 Planner·Developer·QA·Security 결과에 각각 추가 Artifact를 주입하고, 결과물이 정상 등록되거나 다음 단계로 넘어가지 않는지 확인한다. Runtime 통합은 실제 A2A ASGI·SQLite·임시 Git·MCP 경로를 사용하되 Provider/Docker는 통제된 어댑터다. 유료 OpenAI·실제 Docker 데몬을 실행하지 않았다.

## 개발정의서 대조와 한계

[개발정의서](https://www.notion.so/3e9d062130a9801b943ff3bb463af38a)의 역할별 Artifact 책임, project UUID와 opaque A2A ID 구분, 공식 상태, 동일 Snapshot·실행 근거 판정, Single/Multi 동일 기준을 유지한다.

제품 Artifact JSON Schema, 기존 Prompt 버전 3, 수정 최대 3회·Tool 재시도 최대 2회, 예산·인증·Sandbox는 변경하지 않는다. 프론트·평가 시험지·사업전략 HTML·기존 PDF·로컬 키 설정도 수정하지 않는다.

**인수 통과는 내용의 진실성이나 제품 성공이 아니다.** 빈 JSON도 외형 조건만 맞으면 이 첫 검사만 통과할 수 있으며, 이후 Parser가 내용을 검증한다. Archive bytes 무결성, 실제 Tool 실행, 보안 의미 검증은 기존 신뢰 경계에서 계속 확인해야 한다.

## 다음 작업

다음 묶음은 **67·68번**이다. 실행 전달의 식별·무결성·누락 관측을 이어서 보강한다.
