# 의미 검토 보류를 기존 입력 대기 흐름에 연결

이 문서는 추가 보완 **2번**의 기록이다. 81·82번 실제 실행 작업을 대체하지 않는다.

## 상태와 결과 전달

| 상황 | Agent Task / Step | Run 및 후속 처리 |
| --- | --- | --- |
| 승인 범위에 맞고 필드별 근거도 유효 | 기존 최종 처리 계속 | Build·검사·Artifact 검증 후 기존 다음 단계 |
| 범위 밖 내용, 의미 불명확, 근거 누락, 잘못된 검토 응답 | `INPUT_REQUIRED` / `WAITING_INPUT` | Planner는 `WAITING_INPUT`; 개발·검증은 기존 정의대로 `HUMAN_REVIEW`에서 대기 |
| 호출·토큰·시간 한도 초과 또는 provider 오류 | 기존 `FAILED` 등 오류 정책 | 허위 통과·자동 재시도 없음 |
| 인증 필요 / 모델 거절 | 기존 `AUTH_REQUIRED` / `REJECTED` 정책 | 기존 인증·검토 흐름 유지 |
| 실행 취소 | 기존 취소 처리 | 알려진/미확인 사용량과 취소된 검토를 보존 |

보류된 Agent는 그 결과의 최종 Artifact를 게시하지 않는다. Planner 보류 시 개발을 시작하지 않고, Developer 보류 시 새 후보 Source·Build를 만들거나 QA/Security로 넘기지 않는다. 기존에 병렬로 실행되는 QA/Security의 다른 역할이 이미 진행된 경우에는 그 사실과 결과를 보존한다.

질문 메시지에는 `code=SEMANTIC_REVIEW_INPUT_REQUIRED`, `questions`, `semanticReview`를 싣는다. 검토 ID·원인·후보/기준 해시·재개 정책·필드 위치·모델 판정 및 짧은 승인 기준 인용으로 원인을 추적할 수 있다. `modelStatus=SUPPORTED`가 있어도 Host의 최종 `status=HELD`라면 통과가 아니다. 게시 전 후보 JSON과 구조화된 검토 JSON은 비공개 DB에 남기며, 추론 원문을 저장하는 기능은 추가하지 않는다.

## 답변 후 재개 정책

### `REGENERATE_AND_REVIEW`

모델이 파일을 쓰지 않은 상태의 보류다. 기존 `/api/v1/runs/{runId}/resume`에 해당 `workflowStepId`와 `inputData.answer`를 전달한다. 답변은 기존 요구사항 범위 안의 설명으로만 취급한다. 같은 Task·Context에서 결과를 다시 생성하고 **새 검토를 다시 통과해야** 한다. 이전 검토는 덮어쓰지 않는다. 동결 기준과 Run 예산도 그대로 유지한다.

“승인했으니 통과”라는 문장 자체가 새 기능·완화된 수용 기준·검토 결과를 승인하지 않는다. `semanticReview`, `reviewId`, `reviewDecision` 등의 신뢰 필드를 답변에 넣어 검토를 덮어쓰는 것도 거절한다. 요구사항 변경에는 별도 승인 기준과 새 Run이 필요하다.

### `NEW_RUN_REQUIRED`

Developer/QA/Security는 최종 답변을 내기 전에 파일 도구를 실행할 수 있다. 이 경우 검토에서 보류되어도 **이미 수행된 쓰기가 자동으로 없어지는 것은 아니다**. 작업 사본과 private Tool journal을 보존하며, 자동 롤백·기존 쓰기 재실행·검토 없는 Artifact 게시를 하지 않는다.

`write_source_file`, `write_test_file`, `apply_patch`가 수행되었거나 실행된 도구 내역을 확정할 수 없으면 새 Run이 필요하다고 안내한다. 같은 Task의 재개 요청이 와도 이전 검토를 조회한 뒤 모델·MCP·checkpoint를 다시 실행하지 않고 기존 보류를 반환한다. 기준을 바꾸는 실행을 같은 Run의 답변으로 몰래 승인할 수 없다.

단순 읽기·Source를 수정하지 않는 측정까지 모두 파일 쓰기로 간주하지는 않는다. Source에 대한 재실행 정책과 기존 private receipt·불명확한 쓰기 금지 규칙은 계속 적용된다.

## 검증 방법

`tests/test_product_semantic_review.py`에서 실제 제품 플랫폼·공식 A2A SDK의 ASGI 통신·Host/Agent SQLite·Task 상태·Trace·private 저장을 사용한다. 모델 응답과 Docker는 합성 어댑터이며 생성된 코드/테스트를 Host에서 실행하지 않는다.

확인 범위는 네 역할의 게시 차단, 근거 없는 통과 거부, 다른 기준/후보/필드의 응답 거부, 개인정보 범위 확대 보류, 답변 후 재검토, 승인 기준 불변, 파일 쓰기 후 재개 시 모델·MCP 재실행 없음, 검토 기록의 수정/삭제 금지, Single의 동일 identity/context/budget, 공통 호출 한도 준수다. 기존 실행·수정·재검증·제어·사용량·가격 계산도 함께 회귀 검사한다.

이는 **연결과 제어 흐름의 검증**이며 모델이 실제 자연어 의미를 모두 정확히 판별했다는 증거가 아니다. 실 OpenAI/Docker를 사용하는 81·82번에서 정확도·오탐·지연·실제 구현 품질을 별도로 확인한다.

### 이번 검증 결과 — 2026-11-13

| 묶음 | 중복 제외 검사 수 | 결과 |
| --- | ---: | --- |
| 제품 검토 관문·Multi/Single 실행·수정·재개·사용량·가격 회귀 | 94 | 통과 |
| LLM 엔진·요구사항 권한·역할/Single Prompt 계약·플랫폼 구성·도메인 상태 | 150 | 통과 |
| 합계 | **244** | 통과 |

제품 묶음의 실패 항목은 수정 후 해당 5개를 기본 폴링 설정에서 다시 확인했다. 추가한 검토 결과 위조 방지 1개도 확인했다. 재실행 수를 합계에 중복으로 더하지 않았다. 제품 묶음 전체 검사에서는 검증 프로세스 안에서만 A2A 상태 조회 간격을 20ms로 설정했으며, 제품의 폴링 기본값·실행 한도·제어 정책은 변경하지 않았다. 이 시간은 실제 서비스 성능 지표가 아니다.

제품 검토 관문만 다시 검사할 때는 다음 명령을 사용한다. 유료 API나 Docker 데몬이 필요하지 않다.

```bash
PYTHONPATH=src:tests .venv/bin/python -m unittest test_product_semantic_review -q
```

Python 구문 검사와 `git diff --check`도 통과했다. 실제 OpenAI 호출, 실제 Docker 실행, Git commit/push는 이번 작업에서 수행하지 않았다.
