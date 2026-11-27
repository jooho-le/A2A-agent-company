# 64. QA·Security 독립 기준과 생성 테스트 기대값 검증

## 목적

60번 실험에서 관측한 “설명·가정이 후속 역할의 필수 검사 조건으로 바뀌는 문제”를 줄이기 위해, QA·Security의 검증 기준을 [63번 역할별 원장 대조](63-developer-requirement-scope.md)와 연결한다.

이번 작업은 1·2번 담당의 Agent/Host 검증 경계다. 4번 담당의 보호된 평가 시험지나 비교 실험을 대신 구현하지 않는다.

## 역할별 입력

- QA·Security가 기존 Task·Source·metadata를 검증한 뒤, Host 동결 원장에서 `requirementAuthority`와 `requirementScope`를 만든다.
- 역할에 배정된 요구사항의 원문·수용 기준을 전달한다. 기존 `qaRequirements`·`securityRequirements` 필드도 유지한다.
- 검증 역할의 `taskBindings`는 빈 목록이다. Planner의 작업 설명을 검사 기준으로 가져오지 않는다.
- `priorRoleOutcomeIsEvidence=false`를 명시한다. Developer 자체 테스트·이전 QA PASS·스캐너 경고 수는 새 Snapshot의 독립 근거를 대신하지 못한다.
- 재개 답변의 원장·검증 범위 덮어쓰기는 모델·검사 재실행 전에 거절한다.

### 생성 테스트의 기대값

`validate_generated_case_expectations()`가 모델이 만든 QA `cases`와 Security `testCases`를 검사한다.

| 항목 | 규칙 |
| --- | --- |
| 기준 출처 | 매번 동결 Run Configuration에서 복원 |
| Requirement | 현재 역할에 배정된 UUID만 허용 |
| `expectedResult` | **해당 Requirement의 Acceptance Criteria 문자열 하나와 정확히 일치** |
| 여러 수용 기준 | 각각 별도 테스트를 연결할 수 있음 |
| 추가 조건·완화·의역·다른 요구사항 기준 | 거절 |
| 누락된 Requirement·빈 목록·과도한 Case | 거절 |
| 판정 근거 | 기존 실제 Tool Receipt·Source/환경 검증 유지 |

예를 들어 원장에 “정상 입력 시 사용자 1건 생성 및 성공 응답”이 있으면 이를 그대로 사용한다. “동아리 회원만 가입해야 한다”, “가입 응답만 오면 충분하다” 같은 기대값은 원장에 없으므로 거절한다. 세부 테스트 방법은 코드에 작성할 수 있지만, `expectedResult` 문구를 새 승인 기준으로 사용하지 않는다.

## 강제 위치와 오류 처리

1. 멀티 QA·Security Executor가 모델 Draft를 구조 검증한 다음 검사한다.
2. Single도 같은 함수를 호출한다. 단계는 달라도 동일 승인 기준을 사용한다.
3. Host의 QA/Security `finalize()`에서도 다시 검사한다. Executor를 통하지 않고 서비스가 직접 호출돼도 생성 테스트의 기대값을 승인할 수 없다.
4. 실패한 Draft는 테스트 실행과 완성 Report 게시를 중단한다. 모델 원문을 담지 않는 `REQUIREMENT_SCOPE_INVALID` 또는 서비스의 기존 안전한 Binding/Review 오류를 반환한다.

실패 전 모델이 QA/Security 출력 폴더에 작성한 테스트 파일은 남을 수 있고, Security 스캔은 모델 검토 전에 이미 실행될 수 있다. 이것을 테스트 실행 완료나 검증 성공으로 표시하지 않는다. 오류를 맞는 문구로 자동 수정하거나 모델을 자동 재호출하지 않는다.

### 보호된 평가 테스트

Host가 승인한 `protected_cases`는 모델 생성 Case와 구분하며, 기존 보호된 Scope·시험지·기대값·Receipt 정책을 유지한다. 원장 문장과 표현이 다르다는 이유로 평가 담당의 더 구체적인 승인 테스트를 삭제·완화하지 않는다. 새 문자열 대조는 **모델 생성 Case**에 적용한다.

## 호환성

- A2A 최초 요청·Snapshot 전달·공식 Task 상태 및 MCP 권한은 유지한다.
- `requirements.json`, `qa-report.json`, `security-report.json`의 제품 Schema는 유지한다.
- Developer/QA/Security 모델 Draft의 필드도 유지한다. 단, 생성 Case의 자유로운 `expectedResult`는 이제 Runtime 대조를 통과해야 한다. 이전의 일반적인 설명을 원장 문구로 자동 보정하지 않는다.
- 공통 역할 Prompt와 Single Prompt 버전은 **3**이다. 내부 Planner Draft는 62번의 `planner_decision_v2`를 유지한다.
- 프론트·사업전략 HTML·기존 PDF 실험 보고서·로컬 자격증명·Docker 설정은 변경하지 않는다.

## 개발정의서 점검

[개발정의서](https://www.notion.so/3e9d062130a9801b943ff3bb463af38a)의 요구사항 불변, 독립 QA/Security, 같은 불변 Source와 환경, Tool 근거 판정, 공식 A2A 상태, 동일 싱글/멀티 조건을 유지한다. Security 의미 검증기가 없는 경우 `UNVERIFIED`, 스캐너 후보는 `SUSPECTED`라는 기존 원칙도 유지한다.

수정 최대 3회·Tool 재시도 최대 2회·Run 예산·인증·Sandbox·네트워크 제한을 변경하지 않는다. 특정 문자열을 맞췄다는 이유로 PASS나 SUCCESS를 부여하지 않는다.

## 검증

기본 역할 범위·Context·Prompt 검증은 [63번](63-developer-requirement-scope.md)의 101개다. 확장 검증 명령은 다음과 같다.

```bash
PYTHONPATH="$PWD/src:$PWD/tests" .venv/bin/python \
  -W 'ignore:A VersionedTaskStore was configured without an event_stream:UserWarning' \
  -m unittest test_developer_agent test_qa_agent test_security_agent \
  test_qa_services test_security_agent_services test_owned_agent_pipeline \
  test_single_agent_pipeline test_single_agent_fix_pipeline test_single_agent_controls -q
```

### 실행 결과

| 묶음 | 결과 |
| --- | --- |
| 역할 범위·Context·Prompt | 101개 통과, 3.221초 |
| 확장 1차 | 188개 중 172개 통과. 실패 14개·오류 2개, 434.387초 |
| 보정 후 싱글 전체 경로·QA 서비스 2항목 | 34개 모두 통과, 227.813초 |

확장 실패 16개의 원인은 두 곳이었다. 싱글 전용 보안 응답 Fixture 한 곳이 기존 일반 기대값인 “기준 충족”을 계속 사용해 싱글 경로 15개가 연쇄 실패했다. 이를 승인 기준 원문으로 맞췄다. 새 QA 차단 검증은 아직 첫 입력이 stage되지 않아 생성되지 않은 테이블을 조회했으므로, 해당 테이블 자체가 만들어지지 않았음을 확인하도록 고쳤다. 운영 검증 규칙을 완화하지 않았다.

보정한 싱글 Fixture를 사용하는 세 모듈 전체와 QA 서비스의 조기 차단·보호된 기준 보존 항목을 다시 실행했다. 보호된 테스트에는 원장과 표현이 다른 Host 승인 기대값을 넣어 보존을 확인했다.

```bash
PYTHONPATH="$PWD/src:$PWD/tests" .venv/bin/python \
  -W 'ignore:A VersionedTaskStore was configured without an event_stream:UserWarning' \
  -m unittest test_single_agent_pipeline test_single_agent_fix_pipeline test_single_agent_controls \
  test_qa_services.QAServicesTests.test_direct_finalize_rechecks_generated_expectation_before_container_or_report \
  test_qa_services.QAServicesTests.test_protected_scope_hidden_and_always_executed_after_ready -q -f
```

기본 101개와 확장 188개, **관련 항목 289개를 확인**했다. 재검증 34개는 기존 확장 묶음의 일부이므로 총수에 중복해서 더하지 않는다. 188개 전체를 보정 후 다시 실행한 것은 아니다. 저장소 전체 테스트 통과나 위 실행시간을 서비스 성능 측정으로 해석하지 않는다. `git diff --check`와 변경 Python 파일의 구문·새 문서 링크 확인도 통과했다.

실제 A2A ASGI·SQLite·임시 Git 후보·MCP ACL·저장된 Receipt 경로를 확인하고 Provider/Docker는 통제된 어댑터를 사용한다. 유료 OpenAI API나 실제 Docker 데몬을 실행한 결과가 아니며, 기존 59·60번 보고서를 개선 수치로 덮어쓰지 않는다.

## 아직 증명하지 않은 것

기대값 문자열을 원장에 맞췄어도, 테스트 코드가 그 조건을 제대로 검사하는지나 모든 수용 기준의 모든 분기를 검사하는지는 별도 문제다. 이번 검사는 Requirement별 연결과 생성 기대값의 출처를 확인한다. 세부 의미 정확성·모든 자연어 할루시네이션 제거·성능 개선율은 보호된 시험과 실제 모델 반복 실행으로 추가 검증해야 한다.

## 다음 작업

다음 묶음은 **65·66번**이다. 역할별 출력 계약과 전달 경계 보강을 이어간다.
