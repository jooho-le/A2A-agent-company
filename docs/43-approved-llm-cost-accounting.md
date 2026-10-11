# 43. 승인 가격표와 실측 사용량 기반 LLM 비용 원장

작성 기준: 2026-10-11. 범위: 1번 Orchestrator + 2번 Agent/MCP의 비용 기록·조회. 역할 3 제품 UI/API/DB, 역할 4 평가 기준·Single/Multi 비교 실험, 팀원 서비스 연결은 포함하지 않는다.

## 1. 이번에 구현한 것

37번에 저장하던 실제 LLM 사용량을 운영자가 승인한 가격표와 연결했다. 같은 Run에서는 가격표 전체와 계산 버전을 고정하고, 호출별 금액을 별도 불변 원장에 저장한다. 기존 `/usage`·`/telemetry`는 검증된 비용을 조회한다.

- 실제 응답의 모델 ID·처리 등급(`service_tier`)과 캐시 읽기/쓰기 토큰, 실제 global text 요청 출처를 기록한다.
- 가격표는 승인된 JSON만 사용한다. 모델·가격·처리 등급·누락 토큰을 임의로 선택하거나 공식 사이트에서 자동으로 가져오지 않는다.
- 계산은 `Decimal`로 정확하게 수행하고 USD 소수 문자열로 저장한다. float·SQLite REAL·센트 단위 반올림을 사용하지 않는다.
- Planner·Developer·QA·Security, 실패·거절·수정·입력 재개 호출을 같은 Run의 전역 모델 sequence로 합산한다. 실패했다고 알려진 유료 사용량을 제외하지 않는다.
- 가격·사용량·실제 과금 조건이 불명확하면 해당 호출과 전체 금액은 `null`이다. 확인된 일부 합계는 별도 `knownCostUsd`로 표시한다.

이 금액은 **동결 승인 가격표로 계산한 LLM 텍스트 토큰 비용**이다. 실제 청구서 대조·계정 할인·세금·환율·지역/멀티모달 추가 과금·외부 도구 사용료를 포함한 결제 금액을 검증한 것이 아니다. 현재 운영 모델/가격은 아직 선택하지 않았고 실제 유료 API도 실행하지 않았다.

## 2. 변경 파일

| 파일 | 역할 |
| --- | --- |
| `src/agents/platform/pricing.py` | 승인 JSON 읽기 전용 검사, 동결 descriptor, 정확한 비용 계산 |
| `src/agents/platform/pricing_store.py` | Run 가격표 고정, Usage 원문과 결합한 불변 비용 영수증 |
| `src/agents/platform/telemetry_store.py` | 원본 사용량과 영수증 동시 저장, 비용 조회·전체 합계·누락 검사 |
| `src/agents/llm/contracts.py` | 캐시 쓰기 토큰·실제 처리 등급·과금 출처의 private 계약 |
| `src/agents/llm/openai_responses.py` / `engine.py` | 실제 응답 값과 요청 출처 보존, 실패 시 유효 사용량 유지 |
| `src/agents/platform/host.py` / `composition.py` / `run_host.py` | 가격표를 명시적으로 연결하고 Host 정책에 포함 |
| `configs/owned-price-book.example.json` | 운영 승인이 없는 형식 예시. 원본은 의도적으로 거절 |
| `tests/test_pricing.py` / `test_pricing_store.py` | 가격 계약·수식·불변성·역사 재계산 회귀 |
| `tests/test_priced_telemetry.py` / `test_priced_host.py` / `test_priced_pipeline.py` | 비용 완료 경계·Host/CLI·네 역할과 기존 HTTP 조회 연결 |

Provider/Telemetry 회귀와 README도 갱신했다. 공식 A2A/Artifact/Trace Schema, 공개 RunConfiguration, 상태/판정, 역할 Prompt·MCP 목록/ACL·의존성·팀원 코드는 변경하지 않았다.

## 3. 승인 가격표

운영자가 공식 가격 조건을 확인하고 `configs/owned-price-book.local.json`을 별도로 작성해야 한다. 기존 `configs/*.local.json` 규칙으로 Git에서 제외된다. 예시 파일의 ID/버전은 `null`, 목록은 빈 배열이므로 그대로는 승인되지 않는다.

| 필드 | 기준 |
| --- | --- |
| `schemaVersion` | 정확한 정수 `1` |
| `priceBookId` / `priceBookVersion` | 운영자가 승인한 식별자와 버전 |
| `sourceRef` | OpenAI 공식 HTTPS 문서 참조. 사용자정보·포트·쿼리·fragment 금지 |
| `currency` | `USD` |
| `billingScope` | `OPENAI_RESPONSES_GLOBAL_TEXT` |
| `entries` | 1~128개의 승인 가격 항목 |

각 항목은 아래 필드만 받는다. 누락/추가 key·중복 JSON key·부적절한 자료형은 거절한다.

| 항목 필드 | 기준 |
| --- | --- |
| `entryId` | 가격표 내 유일한 식별자 |
| `provider` | `openai` |
| `requestedModelId` | 실제 요청에 쓰는 modelRevision 또는 modelId와 정확히 일치 |
| `reportedModelId` | 실제 응답 모델 ID와 정확히 일치. 별칭 자동 추론 없음 |
| `serviceTier` | 실제 응답에 명시된 `default/flex/priority/scale/fast/ultrafast` 중 승인값 |
| `minInputTokens` / `maxInputTokens` | 실제 입력 토큰 범위. 양쪽 경계 포함, 최대 `null`은 상한 없음 |
| `cacheWriteMode` | `NOT_APPLICABLE` 또는 `METERED` |
| `inputUsdPerMillion` / `cachedInputUsdPerMillion` / `outputUsdPerMillion` | 백만 토큰당 USD 가격을 나타내는 비음수 소수 문자열 |
| `cacheWriteUsdPerMillion` | `METERED`는 소수 문자열 필수, `NOT_APPLICABLE`은 `null` |

가격은 정수부 최대 6자리·소수부 최대 12자리다. float·지수·음수·NaN/Infinity를 받지 않는다. 같은 모델/실제 모델/등급의 입력 토큰 범위가 겹치면 거절한다. 장문 입력 등 공식 조건에 따라 여러 구간을 승인할 수 있으며 맞는 구간이 없으면 미확인 비용이다.

파일 최대 256 KiB, 정규 파일만 허용한다. 파일/부모 symlink와 읽기 중 변경을 거절한다. 승인 파일은 Workspace·시작 Reference Repository·DB/sidecar 경로와 분리한다. `sourceRef`는 참고 출처이며, URL 존재만으로 기재 가격의 최신성/계정 적용 여부를 인증하지 않는다. 운영자의 확인·승인과 버전 관리가 필요하다.

## 4. Host와 CLI 연결

기존 검사 명령에 선택적으로 `--price-book`을 추가한다.

```bash
PYTHONPATH=src .venv/bin/python -m agents.platform.run_host \
  --runtime configs/owned-runtime.local.json \
  --tools configs/owned-tools.local.json \
  --source configs/owned-source.local.json \
  --price-book configs/owned-price-book.local.json
```

기본 명령은 읽기 전용이다. DB·서버·LLM·MCP·Docker를 시작하지 않으며 `executionReady=false`를 유지한다. `--check-secrets`의 기존 동작도 같다. 실제 승인 설정을 준비한 뒤 명시적으로 `--start`를 추가해야 기존 서버 Runner가 시작하며, Run 제출은 API 비용/Docker 실행을 발생시킬 수 있다. 이번 작업에서는 실행하지 않았다.

Python Host에는 `create_host(..., price_book_file=...)`로 연결한다. 신뢰된 직접 조립은 `create_platform(..., price_book=load_price_book(...))`을 사용한다. HTTP 요청·모델 출력·공개 RunConfiguration에서 가격표/가격/계산 함수를 지정할 수 없다.

가격표가 없으면 기존 Host 정책 Hash와 비용 `null` 동작을 유지한다. 42번 Security 검증기 미지정 시 `UNVERIFIED/HUMAN_REVIEW` 기본값도 바꾸지 않는다.

## 5. 고정 시점과 실제 응답 기록

가격표 전체 + `USD_PER_MILLION_V1`을 기존 private Host 정책 Hash에 포함한다. Host의 최초 Source 준비 전에 private Run 가격표를 고정한다. 신뢰된 직접 조립 경로는 최초 Planner의 모델 호출 시작에서 고정하며, 이미 실행된 Run에 가격표를 뒤늦게 붙이지 않는다.

- 가격표 ID/버전·요금·모델·등급·구간이 달라진 같은 Run은 실행/재개하지 않는다. 무가격 Run을 나중에 유료 산정 Run으로 바꾸지 않는다.
- 새 모델 호출·Host 실행 경계는 실제 승인 파일 변경/삭제를 검사하고 차단한다.
- **이미 호출 중인 모델의 응답은 당시 고정한 가격표로 저장한다.** 응답 도착 중 파일이 바뀌었다고 실제 사용량 영수증을 버리지 않는다. 이후 새 호출은 차단한다.
- 과거 비용 조회는 DB의 동결 JSON을 사용한다. 현재 승인 파일 변경/삭제나 현재 Host 가격표 변경이 과거 금액을 덮어쓰지 않는다.

OpenAI Provider는 실제 global Responses endpoint와 텍스트/function/reasoning 입력만 확인된 경우에만 과금 출처를 기록한다. 지역 endpoint·멀티모달·지원하지 않는 요청 형식은 기존 실행 허용과 별개로 가격 계산 출처를 미확인으로 남긴다. 실제 `service_tier`가 없거나 `auto`/알 수 없는 값이면 `default`로 추정하지 않는다. Provider 요청 옵션·모델·재시도 방식은 변경하지 않았다.

응답 파싱 실패에도 유효한 사용량은 보존한다. 신뢰할 실제 모델/등급/출처가 없으므로 이 경우 비용은 `null`이다. 정상 파싱된 failed/refused/incomplete 응답에 실제 사용량과 일치하는 승인 요금이 있으면 해당 호출 금액을 기록한다.

## 6. 계산식과 미확인 비용

```text
일반 입력 = 입력 토큰 - 캐시 읽기 토큰 - 캐시 쓰기 토큰

USD = (일반 입력 × 일반 입력 단가
     + 캐시 읽기 × 캐시 입력 단가
     + 캐시 쓰기 × 캐시 쓰기 단가
     + 출력 토큰 × 출력 단가) / 1,000,000
```

캐시 읽기/쓰기는 전체 입력의 부분집합이다. 합이 전체 입력보다 클 수 없으며 음수·bool·비정수도 거절한다. reasoning 토큰은 출력에 이미 포함되므로 따로 더하지 않는다. OpenAI Docs 스킬로 [공식 prompt caching 안내](https://developers.openai.com/api/docs/guides/prompt-caching)와 [reasoning 사용량 설명](https://developers.openai.com/api/docs/guides/reasoning)을 확인해 중복 과금 방지에 반영했다. 실제 가격/등급/장문 조건은 [공식 가격표](https://developers.openai.com/api/docs/pricing)의 운영 승인 대상이다.

캐시 읽기 수가 없으면 `TOKEN_DETAILS_UNKNOWN`이다. `METERED`는 캐시 쓰기 수가 필요하다. `NOT_APPLICABLE`은 별도 쓰기 과금이 없다는 운영 승인 아래 누락/0만 허용하며 실제 양수 쓰기 수가 오면 미확인으로 둔다. Provider 원본에서 누락 값을 0으로 바꾸지 않는다.

`costStatus`: `KNOWN`, `USAGE_UNKNOWN`, `MODEL_UNMATCHED`, `TIER_UNKNOWN`, `CONTEXT_UNKNOWN`, `RATE_UNMATCHED`, `TOKEN_DETAILS_UNKNOWN`. 가격표 자체가 없으면 조회 projection은 `PRICING_UNAPPROVED`다.

합성 회귀 예: 입력 100·캐시 읽기 40·출력 30·reasoning 20, 일반 입력/캐시/출력 단가 2/0.5/10이면 `"0.00044"`다. **이 숫자는 테스트 fixture이며 실제 운영 가격/모델이 아니다.** 가격표 예시 파일에 이 값들을 승인값으로 넣지 않았다. 큰 유효 정수 카운트에도 자리수에 맞춘 Decimal 정밀도를 사용하고 반올림/부정확 연산 trap을 유지한다.

## 7. 원본 사용량과 불변 비용 영수증

기존 `UsageRecord.cost_usd`와 저장된 원본 Usage의 `costUsd`는 계속 `null`이다. Provider/모델이 금액을 주장할 수 없다. private `agent_run_pricing`에 동결 정책을, `agent_llm_cost_receipts`에 계산 결과를 별도 저장한다.

비용 영수증은 Run·전역 model sequence·실제 Usage Trace eventId·저장된 원본 Usage JSON 전체 bytes SHA·가격표 SHA·계산 버전에 결합한다. 원본 사용량/Trace와 영수증 발행을 같은 SQLite transaction에서 수행한다. 정확한 동일 재발행만 멱등이며 UPDATE/DELETE/REPLACE·다른 금액/소속 재발행을 차단한다. 기존 runtime 원장도 REPLACE 우회를 추가 차단했다.

읽을 때 원본 사용량/예산/동결 설정/Step/Trace 소속을 확인하고 동결 가격표로 다시 계산하여 영수증을 대조한다. 영수증이 없어졌거나 변경됐으면 미확인 비용을 임의 복구하지 않고 안정적인 조회 실패로 알린다. append/read 실패로 실제 사용량 저장이 불완전하면 기존 안전한 예산 차단/누락 표시를 유지한다.

이는 신뢰된 Host의 저장 무결성 검증이다. DB 관리자 권한으로 모든 정책·사용량·원장·Hash를 동시에 다시 만든 공격까지 외부 서명으로 증명하는 시스템은 아니다. Prompt/Source/Tool 인자·결과 원문·Credential은 비용 원장에 복사하지 않는다. 새로운 공개 Artifact 종류나 HTTP endpoint도 없다.

## 8. 기존 API의 조회 결과

`GET /api/v1/runs/{runId}/usage`와 `/telemetry`의 Usage 항목은 verified projection에 `costUsd`, `costStatus`, `pricingEntryId`, `priceBookId/Version/Sha256`, `calculationVersion`을 표시한다. 금액은 **소수 문자열 또는 null**이며 원본 DB Usage를 수정하지 않는다.

Run summary에는 다음 항목을 추가했다.

| 항목 | 의미 |
| --- | --- |
| `costUsd` | 현재까지 예약된 모든 호출의 사용량과 가격이 확인된 경우의 전체 합. 그 외 `null` |
| `knownCostUsd` | 확인된 호출 금액의 부분 합. 가격표 없음은 `null`. 전체 비용으로 사용하면 안 됨 |
| `costComplete` | 현재 호출 원장의 비용이 모두 확인됐는지. Run 완료/제품 성공 여부 아님 |
| `unknownCostSequences` | 금액 미확인·Usage 누락·아직 응답 대기 중인 전역 모델 sequence |
| `currency` / 가격표 메타데이터 | 동결 USD 가격표와 계산 버전 |

전체 합계는 페이지의 일부 record 합계가 아니다. 조회 시 같은 DB snapshot에서 전체 원장과 예산을 대조한다. Usage 누락인데 `usageComplete=true`가 되는 기존 경계도 수정했다. 알려진 토큰은 가격 미확인과 별개로 정상 집계하므로 **비용 미확인만으로 토큰 예산을 무효화하지 않는다**. 반대로 사용량 자체가 없거나 실제 예약/receipt가 누락된 경우의 기존 실행 차단은 유지한다.

승인 가격표/빈 원장이 있으며 모델 예약도 실제 호출도 0개이면 현재 누적 비용 `"0"`은 가능하다. 미확인 호출을 무료로 표시한 것이 아니며 앞으로의 실행 비용이나 최종 SUCCESS를 보장하지 않는다. MCP/로컬 Docker 사용료는 이 LLM 토큰 합계에 추가하지 않는다.

## 9. 개발정의서 준수 점검

| 정의서 기준 | 이번 확인 |
| --- | --- |
| §0·§3·§6·§7 Protocol/상태 | A2A 1.0 HTTP+JSON·공식 상태·Task/Message/Artifact와 프로젝트 Workflow 구분 유지 |
| §2 역할 경계 | 역할 1·2의 사용량/비용 기반만. 제품 구현·독립 평가/실험 제외 |
| §4 수정/재시도 | Fix 최대 3회·Tool Retry 최대 2회 유지, 비용 누락 때문에 재호출하지 않음 |
| §5·§9 불변 Source/환경 | 기존 Source·Manifest·동결 모델·예산 소속 대조. 제품 코드/환경 변경 없음 |
| §8 Secret/권한/격리 | 승인 파일을 생성 코드 영역과 분리, 원문/비밀 미저장, Host 제품 실행 없음 |
| §10 판정 | 비용 확인을 QA/Security PASS나 최종 SUCCESS로 승격하지 않음 |
| §11·11-A 재현성/지표 | 전체 역할·실패/수정/재개 호출 포함, 가격표/계산 버전 고정, 모르는 전체 비용은 null |

전체 프로젝트의 운영 검증 완료를 선언한 것은 아니다. 보류된 RunConfiguration URL Credential 보호·공유 Agent DB 초기화 경쟁은 수정하지 않았다. 실제 청구/LLM/Docker 검증, 역할 4 비교 실험과 역할 3 제품 연결도 하지 않았다.

## 10. 검증

합성 가격/응답, 실제 임시 SQLite/Git/Source, inert Container/Scanner seam, MockTransport와 in-process HTTP를 사용했다. 실제 외부 API·서버 listener·Docker·생성된 제품 코드는 실행하지 않았다. fixture PASS와 금액은 실제 시연 성공/청구 증거가 아니다.

```bash
PYTHONPATH=src:tests .venv/bin/python -m unittest test_pricing test_pricing_store test_priced_telemetry test_priced_host test_priced_pipeline test_openai_responses test_llm_runtime test_llm_runtime_telemetry test_agent_durable_budgets -q
PYTHONPATH=src:tests .venv/bin/python -m unittest test_owned_host_cli test_owned_host test_host_services test_host_policy_store test_security_host test_owned_agent_composition -q
PYTHONPATH=src:tests .venv/bin/python -m unittest test_owned_agent_telemetry test_owned_agent_pipeline test_owned_agent_fix_pipeline test_agent_platform_budgets test_owned_agent_controls -q
PYTHONPATH=src .venv/bin/python -m compileall -q src tests
.venv/bin/pip check
git diff --check
```

최종 결과는 완료된 검사 기준으로 아래에 기록한다. 중복 재실행을 합산하지 않는다.

- 가격표/영수증/사용량/Host/네 역할 HTTP 비용 연결 + Provider/Runtime/영속 예산 **247개 / 17.728초 PASS**. 신규 가격 관련 95개와 Provider/Runtime/Telemetry/예산 152개다.
- 기존 Host/CLI/서비스/정책/Security 승인/플랫폼 조립 **144개 / 27.673초 PASS**.
- 기존 전체 사용량/최초·수정 Pipeline/공유 예산/재개·취소 **51개 / 174.250초 PASS**.
- 합계 **442개 모두 통과**. 실제 유료 LLM·Docker·제품 실행은 하지 않았다.
- `compileall`, `pip check`, `git diff --check`, 신규 파일 trailing whitespace 검사 통과. pip cache 권한 경고는 있었지만 의존성 오류는 없다.
- `git check-ignore`로 로컬 가격표 제외를 확인했다. 운영 모델·가격·Secret·이미지를 생성하거나 선택하지 않았다.
- 독립 검토에서 실행 중 승인 파일 변경으로 실제 Usage가 사라지는 경계를 찾아 수정했고 변경/삭제 후 완료 기록 보존·신규 호출 차단을 재검증했다. 가격/원장 변조·누락/대기·부분 합/전체 합 구분·과거 파일 없이 재계산·큰 정수 정확 계산도 회귀로 확인했다.

## 11. 커밋 메시지와 다음 작업

커밋 메시지: `승인 가격표와 실측 사용량 기반 LLM 비용 원장 추가`

다음 작업: **44번 — 승인된 설정으로 자체 LLM/Docker 최소 실행 확인**. 운영자가 실제 모델/API 자격증명·한도·가격표·고정 이미지/시작 코드와 필요한 검증 기준을 준비한 뒤 자체 모듈의 작은 실제 Run을 확인한다. 실제 호출 비용과 Docker 실행 승인이 필요한 단계이며 제품/독립 평가/팀원 통합 완료와 구분한다. 현재 미수행이다.

프로젝트 Git commit/push는 하지 않는다. Git 생성/Commit은 자동 정리되는 임시 테스트 fixture에 한정한다.
