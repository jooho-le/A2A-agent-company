# 제품 Agent 실행 경로에 의미 검토 연결

## 이번 작업 범위

75~80번에서 사용한 자연어 진단의 의미 검토 원칙을 실제 제품 실행 경로에 연결했다. 진단의 JSON을 그대로 제품 Artifact로 간주하지 않고, 제품이 이미 사용하는 역할별 Draft를 승인된 기준과 대조한다.

이 문서는 추가 보완 **1번**의 기록이다. 기존 **81번 실제 환경 가동, 82번 실제 전체 실행 확인** 번호는 유지한다.

## 검토가 실행되는 위치

| 실행 경로 | 검토 대상 | 통과 후 기존 처리 |
| --- | --- | --- |
| Multi Planner | 기능 계획의 제목·설명 | Host가 `requirements.json` 조립 |
| Multi Developer | 구현·수정 요약 | 실제 변경 계산, 후보 Source 동결, Build |
| Multi QA | 생성 검사 제목·기대값 | 보호된 기준을 포함한 실제 검사 및 보고서 조립 |
| Multi Security | 요구사항·발견사항의 설명, 생성 검사 제목·기대값 | 실제 측정 근거에 따른 보고서 조립 |
| Single | 같은 역할을 수행하는 PLANNING·IMPLEMENTING·FIXING·QA·SECURITY 단계 | 기존 단일 Agent의 Task·Context·도구 권한과 다음 단계 유지 |

기존 Draft Schema·역할 검증을 먼저 적용한다. QA/Security의 생성 기대값은 기존대로 Host의 승인 수용 기준과 정확히 일치해야 한다. 이미 질문·거절로 끝난 응답은 게시할 결과가 없으므로 추가 검토 호출을 하지 않는다. 검토할 자유 서술이 없으면 그 사실을 기록하고 모델 호출을 생략한다. 단일 결과의 검토 대상이 256개 필드를 초과하면 통과시키지 않는다.

## 기준과 판정

- 기준은 `freeze_requirement_authority()`가 읽은 Host의 동결 시나리오다. Planner는 전체 요구사항을, 다른 역할은 해당 Step에 배정된 요구사항만 참조한다. 제외 기능·이메일·보안 정책도 동결된 원본에서 가져온다.
- 사용자 요청, 추가 답변, Planner의 해석, 앞선 Agent의 요약은 승인 원장이 아니다. 검토가 새로운 기능·가입 제한·개인정보 정책을 승인하지 않는다.
- 필드별 `SUPPORTED / UNSUPPORTED / UNVERIFIED`와 승인 Requirement 참조·원문 인용·사유 코드를 받는다. 모든 필드의 응답이 있어야 하며, 참조는 해당 Step의 기준으로 제한한다.
- 기준 해시와 후보 Draft 해시를 모델 출력의 닫힌 값으로 요구한다. 다른 기준·다른 후보·다른 필드의 검토 응답을 재사용하면 허용하지 않는다.
- 모델이 `SUPPORTED`라고 답해도 근거가 없거나 인용이 승인 기준에 존재하지 않으면 보류한다. 위치정보 제외를 개인정보 전체 미수집으로 넓히는 알려진 표현도 보수적으로 보류한다. 이 보조 규칙은 모든 자연어 표현을 완전히 판별하지 않는다.

검토는 **Draft가 승인 범위를 임의로 넓혔는지 확인하는 추가 관문**이다. 코드 내용을 전부 검증하거나, 요약에 적힌 기능이 실제로 구현되었음을 증명하거나, 제품 성공·보안 안전성을 선언하는 절차가 아니다. 기존 Build·보호된 QA/Security·최종 판정이 계속 필요하다.

## 실행·예산·권한

`agents.semantic_review.ProductSemanticReviewer`를 Multi/Single의 실제 플랫폼 조립 코드가 기본으로 주입한다. HTTP 입력이나 모델 답변으로 검토를 끄는 옵션은 없다. 저수준 Executor 생성자의 선택 주입점은 기존 임베딩·단위 검증용이며 실제 플랫폼은 검토기를 주입한다.

추가 검토도 **동일 provider·모델과 동일 Run ExecutionBudget**을 사용한다. 별도 예산이나 추가 Agent Card·A2A Task·Single Context를 만들지 않는다. Single의 검토 호출과 사용량도 `SINGLE_AGENT`로 기록된다. 검토 모델에는 도구를 제공하지 않으며 도구 실행기를 붙일 수 없다.

Single은 50번의 **고정 System Prompt**도 그대로 사용한다. 검토 규칙은 Host가 만든 `trusted` 작업 지시이며, 해당 단계에서 사용한 것과 동일한 Context ID·이력을 유지한다. 별도 검토 역할의 System Prompt로 교체하거나 이전 이력을 잘라내지 않는다.

일반적인 한 사이클에서 네 단계에 자유 서술이 있으면 검토 호출이 네 번 추가된다. 로컬 정상 어댑터 흐름은 기존 작업 호출 6회에 검토 4회가 더해져 총 10회다. 실제 작업의 도구 사용 횟수에 따라 달라지며 고정된 실제 API 성능 수치가 아니다. 기존 호출·토큰·출력·시간 한도를 자동으로 올리지 않는다. 운영자는 실행 전 `AGENT_LLM_LIMITS` 또는 Host의 승인된 limits에 검토·수정 주기를 포함해 예산을 정해야 한다. 한도 부족 시 실패/보류를 유지한다.

## 기록과 개발정의서 대조

Host SQLite의 `agent_semantic_reviews`는 후보·검토 결과·해시·Run/Step/Task/Context·역할·attempt·codeVersion·관련 모델 호출 sequence를 추가 전용으로 기록한다. 수정·삭제를 금지하는 DB Trigger를 둔다. 원문 후보와 모델 판정은 기존 Host DB와 같은 비공개 영역에 둔다.

`AGENT_SEMANTIC_REVIEWED` Trace와 Runtime Event에는 검토 ID·최종 허용/보류·사유·해시·역할/단계·호출 sequence를 연결한다. 기존 `LLM_MODEL_CALLED`와 usage 기록에 검토 비용·시간이 포함되며, 검토 이벤트를 추가 usage로 세지 않는다. 기존 LLM/MCP 이벤트의 닫힌 Schema를 풀지 않고, 저장된 검토 원본과 일치하는 별도 기록 진입점에서 검증한다.

다음은 유지했다: A2A 1.0 공식 상태 및 metadata, 기존 Artifact Schema, 동결 요구사항·수용 기준, MCP 역할 ACL, 인증·Sandbox, Code Fix 최대 3회, Tool Retry 최대 2회, 동일 Source 기반 QA/Security, `COMPLETED`와 최종 `SUCCESS`의 구분. 제품 코드·프론트·평가 담당자의 별도 구현은 변경하지 않았다.

보류·재개 처리와 검증 결과는 [입력 대기 연결 기록](semantic-review-input-required.md)에 정리한다. 이번 보완에서 실제 OpenAI 호출과 실제 Docker 실행은 수행하지 않는다.
