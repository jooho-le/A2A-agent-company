# 62. Planner 미승인 가정 분리와 실행 차단

## 목적

모델이 추정한 접근 제한·추가 기능·검사 기준을 승인된 요구사항과 구분하고, 미승인 조건을 보고한 상태에서 실행 계획이 다음 역할로 넘어가는 것을 막는다. [61번 원장](61-approved-requirement-authority.md)을 실제 Planner 입력에 연결한다.

## 변경한 모델 출력 계약

내부 모델 Draft 이름은 `planner_decision_v2`다. 다음 네 필드를 모두 요구하고 알 수 없는 필드는 거절한다.

| 필드 | 규칙 |
| --- | --- |
| `kind` | `PLAN`, `INPUT_REQUIRED`, `REJECTED` |
| `implementationPlan` | 기존 Task·Requirement 참조·의존성 계약 |
| `questions` | 최대 8개, 질문당 최대 1,024자 |
| `unapprovedAssumptions` | 최대 8개, 항목당 최대 1,024자. 중복·빈 문자열·Credential·승인 객체 금지 |

| 분기 | 계획 | 질문 | 미승인 가정 |
| --- | --- | --- | --- |
| `PLAN` | 모든 승인 요구를 연결한 유효한 DAG | 빈 배열 | **빈 배열** |
| `INPUT_REQUIRED` | 빈 배열 | 1~8개 | 0~8개 |
| `REJECTED` | 빈 배열 | 빈 배열 | 빈 배열 |

예를 들어 “초기 이용자가 동아리 사람이다”에서 “동아리 사람만 가입할 수 있다”를 추정했다면, 그 조건을 `unapprovedAssumptions`에 넣고 `INPUT_REQUIRED`로 질문해야 한다. 미승인 가정을 넣은 `PLAN`은 `PLANNER_OUTPUT_INVALID`로 거절하며, 계획으로 바꾸거나 모델을 다시 호출해 정답으로 고치지 않는다.

## 실제 Runtime 연결

- Planner의 최초 입력·Task history·metadata·동결 기준 검증 이후 Host가 `requirementAuthority`를 생성해 모델 입력에 넣는다.
- 최초 A2A 요청의 공개 필드는 기존대로 유지한다. 원장 참조는 Runtime 내부에서 생성한다.
- 재개 답변이 `requirementAuthority`를 덮어쓰려 하면 모델 호출 전 거절한다.
- `INPUT_REQUIRED`의 A2A 상태 Message에는 질문, `unapprovedAssumptions`, Runtime이 지정한 `assumptionApprovalStatus=UNAPPROVED`를 보관한다.
- 질문 대기 중에는 `requirements.json` 완성 Artifact를 만들지 않는다. 기존 Orchestrator의 대기·재개 흐름을 사용한다.
- 답변에 “승인”이라는 표현이 있어도 동결 요구사항을 자동 교체하지 않는다. 새 기준이 필요하면 Host의 별도 승인·새 Run 절차를 따른다.

## 싱글·멀티 동일 기준

Single의 PLANNING은 기존처럼 Planner Draft 검증기를 재사용한다. 멀티 Planner와 동일한 v2 계약·미승인 가정 규칙·Host 원장 참조를 사용하고, 질문 대기 Message에도 같은 미승인 상태를 남긴다.

공통 역할 Prompt 버전과 Single Prompt 버전은 `2`다. 역할 Prompt는 출처가 불분명한 가정·권고·재개 답변을 요구사항 변경 승인으로 취급하지 않도록 명시한다. Developer·QA·Security의 업무 출력 JSON과 도구 권한은 그대로다.

## 호환성과 검증 범위

- **기존 제품 Artifact 유지:** `requirements.json`은 계속 `schemaVersion=1`이며 기존 A2A·Registry·handoff 경로를 사용한다.
- **신규 Draft는 v2:** `unapprovedAssumptions`가 빠진 이전 모델 Draft는 임의의 빈 배열로 보정하지 않고 거절한다. 테스트 Provider의 계획 응답도 v2로 맞췄다.
- OpenAI Strict Schema의 모든 필드 `required`, 객체 `additionalProperties=false` 조건을 유지한다. 분기별 의미 규칙은 Runtime에서 추가 검증한다. [공식 Structured Outputs 문서](https://developers.openai.com/api/docs/guides/structured-outputs)
- **한계:** 모델이 미승인 조건을 작업 설명 속에 숨기고 이 배열을 비워서 출력하면, 이번 구조 검사만으로 그 의미를 완전히 찾아냈다고 보장할 수 없다. 후속 요구사항 대조·독립 QA/Security 검증·실제 모델 반복 검증이 필요하다.
- 이번 작업은 구조·권한 경계 및 로컬 실행 흐름의 회귀 검증이다. 기존 PDF의 실제 API 실험 결과를 다시 실행하거나 개선 수치로 덮어쓰지 않았다.

## 개발정의서 점검

[개발정의서](https://www.notion.so/3e9d062130a9801b943ff3bb463af38a)의 요구사항 불변, 책임 분리, A2A 공식 상태, 질문 후 반환, 성공 판정과 Task 완료 분리, 같은 조건의 싱글·멀티 비교 기준을 유지한다. 수정 3회·도구 재시도 2회·예산·인증·Sandbox도 바꾸지 않는다.

## 검증 명령

```bash
PYTHONPATH=src:tests .venv/bin/python -m unittest \
  test_requirement_authority test_planner_contract test_planner_agent \
  test_single_agent_prompts test_agent_role_contracts -q
```

이 묶음은 **123개 통과**했다. 실제 LLM·Docker 실행을 대체한 로컬 검증이며, 질문 상태 저장·미승인 계획 거절·권한 참조 덮어쓰기 거절을 실제 A2A/SQLite 경로에서도 확인한다.

### Pipeline·재개·예산 회귀

```bash
PYTHONPATH="$PWD/src:$PWD/tests" .venv/bin/python -m unittest \
  test_single_agent_controls test_owned_agent_pipeline test_owned_agent_controls \
  test_owned_agent_telemetry test_developer_agent test_planner_context \
  test_run_configuration test_llm_runtime -q
```

이 묶음은 173개 중 171개가 최초 통과했고, 아래 두 가지 기존 테스트 준비 문제를 보정한 뒤 해당 2개를 따로 재검증하여 모두 통과했다. 앞의 123개와 합쳐 **서로 다른 관련 검증 296개를 확인**했다. 173개 전체를 보정 후 다시 돌렸다고 기록하지 않는다.

- **별도 Python 프로세스:** 부모 unittest의 `sys.path`·환경 상태에 의존하던 import 보호 테스트에 저장소 `src`의 절대 경로를 지정했다. 원래의 네트워크·파일 쓰기 금지 검사 자체는 유지한다.
- **읽기 전용 DB 내보내기:** 특수문자 경로 테스트의 SQLite 백업본은 WAL 헤더는 있지만 WAL/SHM 부속 파일이 없는 단독 복사본이었다. 일반 경로에서도 같은 문제를 재현했고, 테스트 복사본만 `journal_mode=DELETE`로 준비해 두 경로의 읽기 결과 일치를 확인했다. 실제 Agent DB와 운영 내보내기의 `mode=ro`·`query_only` 보호는 변경하지 않았다.

```bash
PYTHONPATH="$PWD/src:$PWD/tests" .venv/bin/python -m unittest \
  test_owned_agent_pipeline.OwnedAgentPipelineTests.test_a2a_message_bodies_export_for_exposure_checks \
  test_llm_runtime.JSONRuntimeContractTests.test_importing_runtime_does_not_open_network_or_write_files -q
```

재검증 2개 통과, `git diff --check` 통과. 위 296개는 전체 저장소의 모든 검증을 뜻하지 않으며 실제 유료 LLM·Docker 실행이나 의미 오류 개선율을 입증한 결과도 아니다.

## 다음 작업

다음 묶음은 **63·64번**이다. 이후 번호에서 역할별 요구사항 대조·독립 검증을 이어서 보강한다.
