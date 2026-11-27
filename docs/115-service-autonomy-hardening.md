# 115. 서비스 요청 해석·공통 구현 명세·실패 복구 보강

작성일: 2026-11-26.

## 목적과 기존 실패 원인

짧은 요청에도 직원들이 통상적인 구현 방법을 정하고 같은 인터페이스로 작업하도록 보강했다. 앞서 확인한 실패는 요청이 짧다는 이유만으로 설명되지 않는다. API 요청에도 화면이 강제됐고, 개발 코드는 승인 이미지에 없는 Jinja2·폼 파서를 사용했으며, QA는 폼 API에 JSON을 보내려고 했다. 보고서가 없는 상태에서 추측한 보고서 참조를 읽어 도구 오류로 끝나는 경로도 있었다.

이번 작업은 코드·문서·격리 검사까지 수행했다. **서비스 서버, Docker, 실제 OpenAI 호출, 생성된 제품 코드는 실행하지 않았다.** 기존 `.env`, 실행 설정, 사용자 작업 기록을 변경하지 않았다.

## 요청한 10가지의 반영 내용

| 번호 | 작업 | 반영한 동작 |
| --- | --- | --- |
| 1 | 서비스와 실험 분리 | 새 서비스 Run에 `SERVICE_AUTONOMY_V2` 정책을 동결한다. SCN-001 요구사항·보호시험·채점 기준은 유지한다. 실험 보호시험 설정으로 새 서비스 요청을 실행하려 하면 모델 호출 전 준비 단계에서 거절한다. |
| 2 | 짧은 요청 해석 | Planner가 원문을 보존하고 `API` 또는 `WEB`을 선택한다. API 전용 요청에는 HTML·브라우저 검사를 강제하지 않는다. |
| 3 | 통상적인 선택 자동화 | JSON, 승인된 SQLite, 함수명 등은 `implementationDecisions`에 기록하도록 한다. 제품 목적·권한·외부 연동처럼 중대한 결정만 질문하도록 역할 지침을 수정했다. |
| 4 | 공통 구현 명세 | Planner의 `serviceDesign`을 불변 Requirement Artifact에 포함한다. Developer·QA·Security, Single·Multi가 같은 값을 읽는다. |
| 5 | 패키지 사전 확인 | 승인된 lock의 해시를 확인하고 패키지·버전 목록을 Developer에게 제공한다. 사용할 수 없는 import와 실제 Form/Jinja2 사용을 정적으로 검사한다. lock 수정으로 패키지가 설치된다고 가정하지 않는다. |
| 6 | 개발 완료 전 기동 확인 | 서비스 전용 Build에서 문법·앱 import·lifespan·원래 DB 연결·GET / 준비 상태·명세의 경로와 명시된 입력 형식을 검사하도록 연결했다. 실제 실행은 다음 사용자의 서비스 실행 시 Sandbox 안에서만 수행한다. |
| 7 | 작업 종류별 검사 | API는 단위/API 검사, WEB은 단위 검사와 설정된 브라우저 검사를 사용한다. 모든 직원이 명세의 JSON/FORM·필드·경로를 따르도록 한다. |
| 8 | 원인별 수정 경로 | 개발 기동 실패는 기존 BuildReport→Issue→Developer 수정 경로를 사용한다. QA 시험 작성 오류는 기존 1회 시험 수정 경로를 사용하며, 인프라·인증·예산 실패는 성공으로 바꾸지 않는다. |
| 9 | 작업 제어와 실패 안내 | 실패 코드·현재 Task·도구 기록으로 안전한 원인 안내를 제공한다. 추가 설명을 서버에 저장하고 다음 직원 입력에 포함한다. 중단·답변의 기존 인증과 중복 방지는 유지한다. |
| 10 | 다음 실사용 검증 준비 | 자연어 요청 3개와 확인 항목을 `configs/service-validation-cases.json`에 준비했다. 실제 실행과 결과 채점은 아직 하지 않았다. |

`SERVICE_AUTONOMY_V2`는 서비스 처리 규칙의 버전명이다. 성능 실험의 **Ver2**와는 관계없다. 기존 서비스 시나리오 ID는 호환성을 위해 그대로 사용하며, 그 안에서 API/WEB을 구분한다.

## 공통 명세가 전달되는 방식

1. 새 Run 생성 시 원문과 서비스 요구사항·보안 기준·승인 설정을 동결한다.
2. Planner가 원문 범위 안에서 `serviceDesign`을 작성한다. PLAN에는 명세가 필수이고, INPUT_REQUIRED/REJECTED에는 명세 대신 null을 사용한다.
3. Host가 구조·원문 범위에 대한 기존 의미 검토·요구사항 연결을 확인한다. SCN-001에 이 서비스 명세를 넣어 실험 기준을 바꾸는 것은 거절한다.
4. 이후 직원은 같은 Requirement Artifact의 명세를 사용한다. QA가 코드에 맞추려고 합의된 JSON/FORM이나 경로를 임의로 바꾸지 않도록 한다.

명세에는 `delivery`, `endpoints`, `storage`, `dataModel`, `implementationDecisions`가 있다. 엔드포인트마다 method·path·bodyFormat·requestFields·responseFormat·성공/거절 상태를 기록한다. 이는 구현 합의이며, 사용자가 요청하지 않은 기능이나 권한을 승인하는 수단이 아니다.

## 기동 검사와 실패 처리의 범위

- 기동 검사는 고정 Python 프로그램과 별도 JSON 인자로 구성한다. 모델이 임의의 셸 명령·이미지·경로를 선택하지 않는다.
- 승인된 이미지·실행 제한·네트워크 차단·불변 Source 정책을 재사용한다. SQLite 검사 DB는 컨테이너의 `/work`에 한정한다.
- 실제 앱의 lifespan과 원래 `database.get_db()` 연결을 사용한다. 검사 코드가 빠진 제품 테이블을 대신 만들지 않는다.
- OpenAPI에 명시된 경로·입력 인코딩·필드가 명세와 충돌하면 개발 실패로 보고한다. 사용자 정의 body parser처럼 정적으로 확인되지 않는 형식은 기능 검사에서 확인해야 한다.
- Build 통과는 기동과 기본 인터페이스 확인이다. 기능·보안의 최종 통과가 아니다. 실제 성공/실패 응답, 저장 결과, 브라우저 동작은 QA·Security의 측정 대상이다.
- QA에 실제로 게시된 보고서 참조만 허용한다. 보고서가 없으면 `read_test_report`를 제공하지 않으며, 없는 경로를 추측해 읽지 않도록 한다.
- 수정 횟수·모델/도구 예산·시험 기대값 보존 규칙을 늘리거나 완화하지 않았다. 허용된 수정 후에도 확인되지 않으면 검토 또는 실패로 남는다.

## 추가 설명·중단·재개

`POST /api/v1/runs/{runId}/commands`에 `ANNOTATE`를 추가했다. 제품 서버의 JWT·프로젝트 소유권 검사와 Host 서비스 토큰을 그대로 사용한다. 입력은 4,000자, Run당 최대 16개이며, `Idempotency-Key`로 접수와 receipt를 함께 저장한다. `workflowStepId`는 보내지 않는다.

| 표시 상태 | 정확한 의미 |
| --- | --- |
| 접수 성공 | 서버에 보충 설명과 처리 기록이 저장됨 |
| `QUEUED` | 다음 직원 입력에 아직 포함되지 않음 |
| `INCLUDED` | 이후 직원의 작업 입력을 구성할 때 포함됨. 모델이 읽고 구현했거나 검사를 통과했다는 뜻은 아님 |
| `NOT_APPLIED` | 다음 직원 입력에 포함되지 않은 채 작업이 종료됨 |

현재 요청 안의 보충 설명만 전달한다. 이미 진행 중인 모델 호출을 새 입력으로 덮어쓰지 않는다. 새 기능·권한·합의된 API 변경은 새 승인 작업으로 시작해야 한다. 대기 중인 질문에는 기존 RESUME으로 답하고, 실패한 단계를 답변만으로 되살리거나 예산을 초기화하지 않는다.

기존 코드 정책으로 시작된 Run은 새 Host에서 재개할 수 없을 수 있다. 실행된 과거 작업에 정책 기록이 없는 경우도 포함한다. 이 경우 질문에 답변 버튼을 노출하지 않고 새 작업이 필요하다고 안내한다. 아직 최초 실행 준비 전인 새 Run과는 구분한다. 기존 기록과 동결 정책은 변경하지 않는다. CANCEL은 기존의 실제 작업 중단이며 ANNOTATE와 다르다.

## 주요 파일

- `src/orchestrator/domain/service_design.py`: 공통 서비스 명세와 정책 구분.
- `src/agents/roles/planner_contract.py`, `src/agents/runtime/service_scope.py`: 기획 출력과 공통 역할 지침.
- `src/agents/runtime/service_capabilities.py`: 승인 패키지 목록과 정적 import 검사.
- `src/orchestrator/sandbox/service_start_contract.py`, `src/agents/platform/host_services.py`: 기동 검사와 API/WEB 검사 선택.
- `src/agents/runtime/test_fix.py`: 없는 보고서 참조 차단과 서비스용 시험 수정 안내.
- `src/orchestrator/infrastructure/run_feedback.py`: 추가 설명 접수·전달 기록.
- `src/orchestrator/application/work_failures.py`, `src/orchestrator/application/run_interactions.py`: 실패 안내와 재개 가능 여부.
- `frontend/src/components/atelier/RunWorkPanel.jsx`, `frontend/src/hooks/useRunInteraction.js`: 실제 추가 설명 접수·조회와 작업 제어 화면.
- `configs/service-validation-cases.json`: 회원가입 API·할 일 웹·블로그 웹의 다음 수동 검증 입력.

## 정의서 점검과 검증 범위

A2A 공식 Task 상태, 기존 Run 상태 전이, Artifact 계보, 역할별 MCP 권한, Source 불변성, 인증, 보호시험과 독립 최종 평가 기준을 변경하지 않았다. 기능 성공은 모델의 READY 주장 대신 기존 Host의 측정 근거로 판단한다. 서비스 명세와 추가 설명이 권한·예산·승인 기준을 덮어쓰지 않도록 경계를 유지했다.

최종 확인 결과:

| 검사 | 결과 |
| --- | --- |
| 서비스 계약·질문/답변·추가 설명·시험 수정·의미 검토 회귀 묶음 | Python 145개 통과 |
| Host·Single Host·서비스 실행 설정·격리된 전달 경계 묶음 | Python 102개 통과. 위 묶음과 일부 중복이 있어 단순 합산하지 않음 |
| 추가 설명의 다른 Run/직원 차단·정책 누락·질문 재개 마지막 확인 | Python 20개 통과 |
| 제품 서버의 JWT·소유권·추가 설명 중계 | 7개 통과 |
| 실제 React 컴포넌트와 Hook, 임시 Chrome 프로필 | 1개 UI 검사 안의 7개 흐름 통과. 실제 서버 연결 대신 격리된 응답 사용 |
| 변경·추가 Python 44개 파일 구문 / JSON 2개 파일 | 통과 |
| 프론트 lint / production build / diff 공백 검사 | 통과 |
| 5173·8000·8002·8101~8105 리스너 읽기 전용 확인 | 실행 중인 서비스 리스너 없음 |

Python에는 기존 SDK의 cross-replica streaming 비활성 경고가, 프론트에는 기존 3D 번들 크기 경고가 남는다. Docker 프로세스 목록 조회는 OS 제한으로 확인할 수 없었으며 이번 작업에서 Docker를 시작하지 않았다.

격리 검사는 모델과 Docker를 대체하므로 자연어 이해 정확도·실제 생성물 완성도를 입증하지 않는다. 다음 단계는 서버를 다시 켠 뒤 **새 작업**으로 준비된 자연어 요청을 실행하고 결과를 확인하는 것이다. 이번 작업에서는 그 실행을 하지 않았다.

추천 커밋 메시지: `서비스 공통 명세와 기동 검사 및 사용자 작업 제어 보강`
