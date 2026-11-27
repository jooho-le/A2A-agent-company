# 75번 — 자연어 진단과 제품 Agent 실행 경로 대조

## 확인한 차이

73~74번은 실제 API·A2A·SQLite를 사용했지만 제품 Orchestrator의 전체 개발 실행은 아니었다. 특히 자연어 진단의 요구사항 목록은 모델이 원문에서 추출한 목록이며, 제품 Host가 승인 Configuration에서 동결한 원장과 다르다.

| 구간 | 제품 실행 | 기존 자연어 진단 v2 | 이번 guarded 진단 v3 |
| --- | --- | --- | --- |
| 실행기 | `src/agents/runtime/`의 Planner/Developer/QA/Security | `NaturalExecutor` | `GuardedExecutor` |
| Prompt | `src/agents/roles/prompts.py`·역할별 Runtime | `natural.role_prompt()` | `guarded_contracts.role_prompt()` |
| 요구사항 출처 | Host의 동결 `scenarioContract`·`requirementAuthority` | 모델의 원문 해석·인용 | 원문 그대로의 인용 목록·미정 표시 |
| 검사 기대값 | 배정된 Acceptance Criteria와 정확히 일치 | 자유로운 `expected` | 인용 목록의 EXPLICIT 항목과 정확히 일치 |
| 의미 검토 | 보호된 테스트·Tool 근거 등 별도 검증 | 인용 존재 검사와 사후 Codex 검토 | 별도 모델 호출의 보조 검토와 인계 차단 |
| 실제 코드·도구 | MCP·Snapshot·Build·QA·Security 실행 | 실행하지 않음 | 실행하지 않음 |

A2A Client·인증된 Message 접수·Agent Card·SQLite 수신 기록·OpenAI Provider는 기존 기반을 재사용한다. 이것이 제품의 모든 실행기·Prompt·승인 검사를 함께 실행했다는 뜻은 아니다.

## 보완 위치 결정

제품에는 이미 61~64번의 승인 원장·미승인 가정 분리·역할별 범위·생성 기대값 검사가 있다. 이번에는 **해당 규칙을 자연어 진단의 계약과 전달 경계에도 반영**했다. 제품의 동결 원장을 모델 추출 목록으로 바꾸거나, 자연어 문자열 `R1`을 승인 Requirement UUID로 사용하지 않는다.

새 경로는 `python -m diagnostics.communication.guarded`다. 기존 `natural` v2는 과거 실험 재현과 회귀 확인을 위해 동작을 유지한다. v2 결과를 guarded 결과로 표시하거나 과거 원자료를 새 계약으로 다시 채점해 바꾸지 않는다.

## 공통 실행 기반 수정

`NaturalLab`에 두 확장 지점을 추가했다.

- `record_type`: 기존은 `NaturalRecord`, guarded는 `GuardedRecord`를 사용한다.
- `blocked_reason()`: 수신 확인·계약·의미 검토·질문 대기로 실행하지 않은 후속 역할의 이유를 남긴다.

`natural.content_screen()`에는 명시적인 계약 검증기 인자를 추가했다. 기본은 기존 v2이며, guarded만 v3를 전달한다. A2A 송수신·metadata·Task/Context 생성·DB Receipt 로직은 공유한다.

## 승인·상태 경계

v3의 `READY`·`INPUT_REQUIRED`·`REJECTED`는 **진단 응답 내부의 결정 값**이다. 공식 A2A 상태를 새 문자열로 교체하지 않는다. 진단 Task의 `COMPLETED`는 응답 생산 완료이며 제품 성공이 아니다.

진단의 인계 허용에도 `DIAGNOSTIC_ONLY_NOT_PRODUCT_APPROVAL`을 기록한다. 실제 제품의 Configuration 승인·MCP 권한·배포 권한을 주지 않는다. 의미 검토용 API 호출은 진단 보조 처리이며 새 회사 Agent Card나 제품 Agent 역할을 생성하지 않는다.

## 점검과 다음 작업

제품 비즈니스 로직·프론트·평가 시험지·인증·Sandbox·수정/도구 재시도 한도를 변경하지 않았다. 제품 경로의 실제 Docker 실행 검증은 81~82번 범위다. 이번 공통 기반과 기존 진단의 호환성은 [79번](79-guarded-single-multi-regression.md)에서 확인한다.

다음은 [76번](76-natural-condition-classification.md)의 원문 조건·가정·미정 분리다.
