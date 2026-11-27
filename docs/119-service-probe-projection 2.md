# 119. test11 개발 도구 준비 실패 수정

작성일: 2026-11-27.

## 확인한 원인

Run `96e388ab-c49d-4f2f-8e63-bf74d68b67bd`는 Planner 이후 Developer의 LLM·MCP 호출 전에 거절됐다. Developer Task의 실제 상태는 `REJECTED`, 코드는 `DEVELOPER_SERVICES_DENIED`였다. 화면에는 이 코드가 공개 오류 목록에 없어 `AGENT_EXECUTION_FAILED`로만 보였다. 사용자가 중단한 후 Run은 `ABORTED`로 남아 있다.

실패한 기획서를 그대로 읽어 비실행 준비 검사로 재현했다. `service_build_argv()`는 시작 검사에 필요하지 않은 `dataModel`과 `implementationDecisions`까지 JSON 명령 인자에 넣고 있었다. `password_hash: str` 같은 자료형 설명이 비밀값 할당 패턴으로 해석돼 BuildConfiguration의 기존 인자 검증에 거절됐다. 프로그램 인자의 길이는 8,046바이트로 허용 범위였고, 실제로 거절된 것은 설명문을 포함한 설계 JSON이었다.

이는 회원가입 요청이 짧아서 생긴 문제나, Developer 모델 응답·QA 판정·OpenAI 서버 오류가 아니다. 실행 준비 계약의 전송 범위가 잘못된 문제다. 앞선 회귀 검증에는 이 자료형 설명 형식이 포함되지 않았다.

## 수정

1. 시작 검사 명령에는 실제 사용하는 `delivery`, `endpoints`, `storage`만 전달한다. 전체 ServiceDesign을 먼저 검증하고, 기획서 원본과 Agent 입력은 유지한다. 설명문을 실행 인자에서 제외할 뿐 비밀값 검증·인자 길이·허용 경로 제한을 완화하지 않는다.
2. 공개 오류 코드에 Developer의 Context·Input·Services 준비 거절을 추가한다. A2A Task의 거절 이유가 일반 실패 코드로 사라지지 않는다.
3. 과거 일반 오류로 저장된 Step은 같은 Developer Task ID·Context ID·종료 상태의 허용된 코드를 읽어 화면 안내를 보완한다. Step·Task·Run 이력은 덮어쓰지 않는다. 다른 역할·다른 Context·임의 오류 메시지는 수용하지 않는다.

## 검증과 적용

- 서비스 실행 인자·Host Developer 서비스 생성·비밀값 차단 유지·오류 연결 회귀 검사: **62개 통과**.
- 실제 실패 Run의 기획서 재검사: **BuildConfiguration 준비 통과**. 설계 JSON 인자는 921바이트에서 필요한 구조 정보 359바이트로 줄었다. 생성 제품이나 OpenAI 요청을 실행한 결과는 아니다.
- 진행 중 Run과 RUNNING Step이 없음을 확인한 뒤 Host·에이전트만 재시작했다. 프론트·제품 서버·Docker와 기존 작업 기록은 유지했다.
- 적용 후 프론트·제품·Host·Agent 4개 모두 HTTP 200, 에이전트 4개 모두 executionReady=true.
- 기존 test11 조회에서도 `DEVELOPER_SERVICES_DENIED / 개발 도구의 실행 설정을 준비하지 못했어요.`가 표시되는 것을 확인했다.
- 새 유료 LLM 작업은 생성하지 않았다. 실제 제품 완성 여부는 변경 코드가 적용된 **새 작업**에서 확인해야 한다. 중단된 test11을 성공으로 바꾸거나 예산을 초기화하지 않는다.

추천 커밋 메시지: `서비스 시작 검사 인자 최소화 및 개발 준비 오류 안내 보완`

다음 작업: **120번 — 수정된 서비스에서 새 작업 1회 확인**.
