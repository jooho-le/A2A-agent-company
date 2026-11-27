# 109. API 오류·시간 초과 원인 확인과 안전한 진단

작성일: 2026-11-23. 기존 보완 대상 **4번**을 진행했다. 108번의 제품·파일·시작 규격 보완에 새 기능을 붙인 것이 아니라, 실제 실행을 중단시킨 외부 호출의 원인을 구분하고 기존 한도 안의 대응을 정리했다.

**새 실험 Run과 유료 OpenAI 모델 호출은 모두 0회**다. 기존 기록을 읽었고, 현재 연결은 키나 HTTP 요청을 보내지 않는 DNS·TCP·TLS 접속으로 한 번 확인했다.

## 과거 실패에서 확인한 원인

| 조건 | 확인 내용 | 해석과 한계 |
| --- | --- | --- |
| Single Ver1 | `api.openai.com`으로 보낸 POST의 **HTTP 500 응답 3건**을 Host 로그에서 확인 | 이 세 오류는 API 서버 오류 응답이다. 키 오류나 잔액 부족으로 단정할 근거가 아니다. |
| Single Ver2 | 모델 시도 17번의 `LLM_EXECUTION_TIMEOUT`, **120.018초**, 모델 호출 제한 120초 | 전체 Run 한도는 1,200초였다. 모델 호출 경계의 제한에 도달한 것으로 볼 수 있으나 당시 기록만으로 Read/Write/Engine timeout을 더 구분할 수는 없다. |
| Multi Ver2 | 두 시도 모두 `LLM_PROVIDER_ERROR`, 사용량은 확인된 0, 소요 시간 3ms·7ms | **당시 어댑터의 ConnectError 분기와 일치하는 코드 추론**이다. DNS·TLS 등 구체적인 연결 실패 이유는 원본에 없다. |

Single Ver1의 모델 시도 36·61·62번은 각각 47.470초·62.991초·63.601초이며 사용량은 미확인이다. HTTP 오류를 이유로 사용량이나 비용을 0으로 바꾸지 않았다.

Multi Ver2의 추론에는 107번에 고정한 어댑터 파일 해시와 현재 Git 기준 판본의 해당 파일 해시가 같음을 먼저 확인했다. 당시 어댑터에서 `LLM_PROVIDER_ERROR`와 전송 전의 확인된 0 사용량을 함께 만드는 경로가 `httpx.ConnectError`다. 이것은 과거 기록에 새 오류 유형을 써 넣거나 실제 예외 메시지를 복구했다는 뜻은 아니다.

현재 확인한 DNS·TCP·인증서 검증 TLS 접속은 **정상 · TLSv1.3**이다. 실제 어댑터와 같은 certifi 신뢰 목록을 사용했다. 키를 읽거나 API 요청을 전송하지 않았으며, 이 결과가 과거 연결 상태나 현재 모델 API 응답까지 증명하지는 않는다.

근거: `.data/verification/109-provider-audit-20261123.json`. 원본 실험 DB는 읽기 전후 해시가 같고 기존 보고서·수치도 보존했다.

## 코드 보완

### 1. 원문 대신 닫힌 오류 사실 전달

`FailureDiagnostic`을 추가하고 다음 경계를 구분했다.

| 경계 | 구분하는 내용 |
| --- | --- |
| HTTP | 실제 상태 코드, 인증·권한·요청 거절·서버 오류·요청 제한·잔액/지출/사용 한도 |
| TRANSPORT | 연결 실패, Connect·Pool·Read·Write timeout과 기타 전송 오류 |
| MODEL_CALL | Engine이 모델 호출 전체에 적용한 시간 초과와 해당 제한 |
| TOOL_CALL | 도구 호출의 시간 초과와 해당 제한 |
| RUN_BUDGET | 실제 Run 마감 시각 소진. 기존 `LLM_BUDGET_EXHAUSTED`를 유지 |
| RESPONSE | 응답 형식·식별 규격 검증 실패 |

오류 본문은 최대 8KiB 안에서만 해석하고, 허용 목록에 있는 코드·유형만 선택한다. 중복 JSON 키·비정상 JSON·알 수 없는 코드·초과 크기 본문은 임의 복구하지 않는다. 확인할 수 없는 429는 `HTTP_429_UNCLASSIFIED`로 남긴다.

저장·공개하는 값은 상태 코드·닫힌 사유·허용된 공급자 코드·검증한 숫자뿐이다. **원문 오류 메시지, 요청 본문, 프롬프트, 키, URL, 원본 헤더, 공급자 임의 필드는 저장하지 않는다.** 검증을 통과하지 못한 선택적 진단은 실제 사용량이나 기본 오류 코드를 없애는 이유로도 쓰지 않는다.

기존 `LLM_PROVIDER_ERROR`·`LLM_AUTH_REQUIRED`·`LLM_EXECUTION_TIMEOUT` 및 A2A 상태는 유지한다. 세부 사유는 다음에 추가로 전달한다.

- Engine의 실패·사용량 기록과 복구 결정: `failureDiagnostic`.
- Planner·Developer·QA·Security·Single의 A2A 실패 상태 메시지: `llmFailure`.
- Host의 append-only 사용량 Trace·가격표 검증 경로.

선택적 진단이 없으면 기존 메시지 형태를 유지한다. 사용자 입력이 `llmFailure`를 Host 사실처럼 주입하지 못하도록 기존 보호 필드에도 추가했다.

### 2. 재시도 횟수를 늘리지 않고 필요한 대기만 반영

OpenAI는 429의 일시적 요청 제한과 잔액·한도 부족을 구분하고, 유효한 `Retry-After`를 따르면서 재시도 횟수·전체 시간을 제한하도록 안내한다. 잔액·지출·사용 한도 문제는 반복 호출로 해결되지 않는다. [오류 기준](https://developers.openai.com/api/docs/guides/error-codes), [재시도 안내](https://developers.openai.com/api/docs/guides/rate-limits).

- 확인된 잔액·지출·사용 한도 오류는 자동 재시도하지 않는다. 구체적인 코드가 넓은 오류 유형보다 우선한다.
- 일시적 429·서버 오류와 전송 전 연결 오류는 **기존 승인된 재시도 한도**만 사용한다. 실험 설정의 최대 1회는 그대로다.
- 숫자·HTTP-date 형태의 유효한 `Retry-After`를 최소 대기 시간으로 반영한다. 기존 기본 대기 1초도 유지한다.
- 필요한 대기가 60초를 넘거나 남은 Run 시간에 들어가지 않으면 더 빨리 재전송하지 않고 원래 오류로 멈춘다.
- 요청이 전달됐을 수 있는 Read·Write timeout이나 불명확한 실행 결과는 무작정 재호출하지 않는다. 중단·취소도 기존대로 유지한다.

모델 호출 제한 120초·Run 제한 1,200초·모델/도구/토큰 예산을 늘리지 않았다. 대기는 원래 Run 마감 안에 포함되며 새 호출도 기존 공유 예산에 반영한다. TLS 인증서 검증·고정 엔드포인트·인증·승인·역할 권한을 완화하지 않았다.

### 3. 진단과 과금 근거를 분리

연결이 이루어지지 않은 것으로 확인된 경우의 0 사용량은 기존 규칙을 유지한다. 반면 HTTP 오류·응답 대기 시간 초과·취소의 사용량은 확인되지 않았다면 계속 미확인이다. 나중의 성공이 앞선 실패의 미확인 사용량을 채우지는 않는다.

진단 정보로 비용·청구 금액을 제출하거나 바꿀 수 없다. Host의 고정 가격표와 실제 사용량이 계속 과금 근거이며, 새 진단을 담은 Trace도 엄격한 필드·해시 검증을 통과해야 한다. 확인된 0 토큰과 가격표 비용 완전성은 별개이므로 과거 Multi Ver2 비용을 임의로 0달러 확정 처리하지 않았다.

## 검증 결과

| 검사 묶음 | 결과 |
| --- | --- |
| 실제 REST 어댑터 MockTransport·Engine·예산·비노출·진단 공개·사용량·가격표 | **328 PASS** |
| Planner·QA·Security·Single·Host·재검증·평가 지표 경로 | **322 PASS** |
| 기존 실행 기록·어댑터 해시·DB 불변 확인 | 통과 |
| 무료 DNS·TCP·인증서 검증 TLS 확인 | 통과. HTTP·모델 요청 0회, 키 로드 없음 |
| `git diff --check` | 통과 |

두 검사 묶음은 일부 fixture가 겹치므로 고유 검사 수로 합산하지 않는다. SDK의 스트리밍 미설정 경고와 기존 테스트 클래스 수집 경고는 남았고 실패는 없었다.

검증한 핵심 경계는 다음과 같다.

- 같은 429에서도 요청 제한과 잔액·각 한도를 구분하며, 원문·키·초과 크기 데이터·알 수 없는 코드를 공개하지 않는다.
- 실제 MockTransport → 어댑터 → Engine → 사용량·복구 기록까지 사유를 전달한다. 재시도가 가능한 경우에도 기존 최대 1회에서 멈춘다.
- 짧은 서버 대기는 따르고, 너무 긴 대기·부족한 Run 시간·영구 오류에서는 추가 요청을 보내지 않는다.
- 모델·도구·Run 마감의 시간 초과를 구분한다. 결과 불명 쓰기를 반복하지 않는다.
- A2A 상태 메시지에 새 사유가 전달되면서 기본 오류 코드·상태·Artifact 미생성 규칙은 유지된다.
- 오류 진단이 사용량·가격표 권한을 대신하지 않고, 미확인 토큰·비용은 미확정으로 남는다.

```bash
.venv/bin/python -B -m pytest \
  tests/test_provider_failure_diagnostics.py tests/test_failure_diagnostic_publication.py \
  tests/test_openai_responses.py tests/test_llm_runtime.py tests/test_llm_runtime_telemetry.py \
  tests/test_pricing.py tests/test_pricing_store.py tests/test_failure_recovery_routes.py \
  tests/test_budget_reservation_wait.py tests/test_agent_durable_budgets.py \
  tests/test_agent_platform_budgets.py tests/test_single_agent_durable_budgets.py -q -x

.venv/bin/python -B -m pytest \
  tests/test_planner_agent.py tests/test_qa_agent.py tests/test_security_agent.py \
  tests/test_test_data_pipeline.py tests/test_single_agent_pipeline.py tests/test_validation_artifacts.py \
  tests/test_loop_validation_feedback.py tests/test_per_test_setup_recovery.py tests/test_owned_host.py \
  tests/evaluation/test_run_metrics.py tests/evaluation/test_recovery_evidence.py -q -x
```

## 개발정의서 점검·다음 작업

Single·Multi가 같은 진단·재시도·공유 예산 정책을 사용한다. `providerFailurePolicy`와 진단 모듈·어댑터·LLM 계약 파일의 SHA-256을 Host 실행 정책에 포함했다. A2A 상태·Task·Artifact·불변 Snapshot·MCP 권한·Sandbox·보호시험·독립 최종 시험은 유지했다.

`.env`·실행 설정·키·사용자 설정·기존 실험 DB·Source·보고서는 수정하지 않았다. 프로젝트 Git 커밋·푸시는 하지 않았다. 이번 확인으로 외부 API의 500 오류가 앞으로 발생하지 않는다거나 네 조건이 실제로 완주했다고 주장하지 않는다.

**기존 1·2·3번은 108번, 기존 4번의 원인 확인·진단·한도 내 재시도 보완은 109번으로 마무리했다.**

**다음 110번:** 구현을 동결하고 네 조건을 각각 한 번 실제 실행해 표를 작성한다. 성공할 때까지 추가 Run을 반복하지 않으며, 실행 오류·미평가가 생기면 그 사실과 중단 사유를 남긴다. 이번 작업에서 해당 유료 실행은 하지 않았다.

커밋 메시지 제안: `OpenAI 오류 원인 구분과 제한 재시도 및 안전한 진단 기록 보완`
