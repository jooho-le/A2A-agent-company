# 19. 공통 LLM 연결·구조화 응답·Tool Loop·예산·사용량

작성일: 2026-10-07

> 범위: 1번+2번의 공통 실행 라이브러리. 기존 18번 역할 Prompt와 기존 ModelConfiguration을 재사용한다.
> 기준: 개발정의서 §2·§4·§8·§10·§11·§11-A 및 15번의 확정 후속 번호.
> 실제 역할 Executor·MCP 서버·Workspace/Snapshot/Sandbox·Orchestrator 연결·3번 웹 서비스·4번 평가 실험은 이번에 구현하지 않았다.

## 1. 이번에 개발한 내용

- Provider 공통 요청/응답, 구조화 JSON Schema, Tool 정의/호출/신뢰 Context, 토큰 사용량과 오류 계약을 추가했다.
- 선택 가능한 OpenAI Responses REST 어댑터를 기존 `httpx`로 구현했다. 새 SDK·의존성·Lock 변경은 없다.
- LLM → 승인된 Tool → LLM → Schema 검증 JSON으로 끝나는 공통 비동기 엔진을 추가했다.
- 정적 역할 Prompt와 작업 데이터는 분리한다. Prompt의 역할/버전/System 규칙이 서비스 역할과 다르면 호출하지 않는다.
- 역할 allowlist 및 실제 등록 Tool 목록 둘 다 확인하며, 한 응답의 모든 Tool 입력과 continuation을 검증한 뒤에만 첫 Tool을 실행한다.
- 공유 wall-clock 예산, 모델/Tool 호출 횟수, 호출 timeout, 출력 토큰/반환 사용량 한도와 JSON 크기 제한을 추가했다.
- 실제 확인한 사용량을 메모리 Record로 남긴다. 실패 시에도 Record를 오류에 첨부하고, 선택적인 신뢰된 sink로 전달할 수 있다.

JSON 검증 통과는 모델 출력 형식 검증일 뿐이다. Build/Test/Security PASS·A2A COMPLETED·Workflow SUCCESS·Final Verdict를 만들거나 기존 상태를 덮어쓰지 않는다.

## 2. 모델 설정과 Provider 경계

| 설정 | 정책 |
| --- | --- |
| `AGENT_LLM_PROVIDER` | 기본 미설정. 현재 어댑터는 `openai`만 제공. 다른 Provider는 명시적 구현 필요 |
| `AGENT_LLM_MODEL_ID` | 기본 미설정. 자동 추천/선택·fallback 없음 |
| `AGENT_LLM_MODEL_REVISION` | 선택 설정. OpenAI에서는 실제 wire Model ID로 사용하고 반환 Model과 정확히 일치해야 함 |
| `AGENT_LLM_TEMPERATURE` | 기본 미설정. 호출 전 직접 설정. OpenAI 범위 0~2, 모델별 미지원은 API 오류로 반환하고 생략/대체하지 않음 |
| `AGENT_LLM_SEED` | 지원 Provider용 선택 값. 현재 Responses 어댑터는 미지원이므로 설정 시 명시적 오류 |
| `AGENT_LLM_API_KEY` | 기본 미설정. SecretStr·일반 직렬화 제외. HTTP Authorization에서만 사용 |
| `AGENT_LLM_LIMITS` | 운영자가 명시할 수 있는 JSON 제한값. 아래 기본값 참고 |

`model_from_settings(settings, frozen_model=...)`는 기존 ModelConfiguration을 만들고 동결 Run 모델과 같아야 반환한다. Provider·모델·Revision·Temperature·Seed를 자동 변경하지 않는다. 현재 Orchestrator의 Run 설정을 이 함수로 자동 연결한 것은 아니다.

Revision 미지정 모델 별칭은 동일 ID 또는 `요청ID-YYYY-MM-DD` 형태의 Snapshot 응답만 허용하고 실제 반환 ID를 기록한다. 다른 형식/모델은 오류로 처리한다. 재현성을 위해 모델이 제공하는 실제 Snapshot ID를 Model ID/Revision에 직접 선택하는 것이 좋다. 날짜 형태 검사 자체가 공급자의 불변 Revision 보장이나 결과의 결정성을 증명하지는 않는다.

설정 객체 생성·import·Provider 생성은 실제 API 호출을 하지 않는다. `provider_from_settings()`는 선택/temperature/Key/지원 Provider를 확인한다. 이후 실행 전 어댑터가 옵션과 응답 Schema를 검증한다. 실제 권한·모델 접근 가능 여부는 API 응답으로 확인한다.

OpenAI 요청은 고정 HTTPS endpoint를 사용한다. 환경 Proxy·redirect·HTTP 자동 Retry·remote MCP·built-in Shell/Web Tool은 사용하지 않는다. `store=False`, `background=False`, `stream=False`이며, 암호화 reasoning continuation은 서명에 영향을 주지 않도록 wire-only 원값으로 재전달한다. 이 설정이 조직의 전체 데이터 보존 정책을 대신한다는 의미는 아니다.

## 3. 구조화 응답과 Tool Loop

### 내부 JSON과 최종 Artifact를 구분

`JsonSchema`는 Runtime이 제공하는 self-contained 내부 모델/Tool 계약이다. 원격 `$ref`·resource `$id`를 막고 로컬 Schema로 검증한다. 일반 JSON 검증은 duplicate key, NaN/Infinity/지수 오버플로, 비문자 key, tuple 등 JSON 원본 타입 위반, Markdown, 초과 필드, format 오류를 거부한다. 자동 JSON 복구·Schema 완화는 하지 않는다.

OpenAI 최종 응답은 `text.format.type=json_schema`, `strict=True`로 요청한다. 지원되는 폐쇄형 object·필수 필드 형태인지 먼저 확인한다. Provider의 전체 Schema 지원 범위까지 자체 증명하는 것은 아니며 미지원은 API 오류로 남는다.

기존 Project Artifact Schema에는 Runtime ID·Manifest·ToolEvidence·원격 계약 참조가 있다. 이 Schema를 그대로 모델에 넘겨 가짜 값으로 채우거나 Provider 제한에 맞춰 완화하지 않는다. 30~33번 Executor가 내부 모델 출력과 실제 Host/Tool 값을 조립하고 18번 완료 출력 검증기로 최종 Artifact를 검증해야 한다.

Tool 입력 Schema의 optional 필드를 임의 required로 바꾸지 않기 위해 OpenAI function Tool은 `strict=False`를 명시한다. Runtime에서 원본 입력/출력 Schema를 별도로 검증하므로 모델이 고른 인자는 권한 승인이 아니다.

### 실행 전 검사

- 서비스 역할의 기존 `ROLE_TOOL_NAMES`와 등록된 Tool 이름을 둘 다 확인한다.
- Tool 등록 중복·실행기 누락·다른 역할의 Tool은 구성 오류로 거부한다.
- call ID는 원값을 보존하며 중복/재실행·빈 ID·미등록 이름을 차단한다.
- 모델의 `workspaceId`가 있으면 Host Context와 정확히 일치해야 한다. trim/재발급하지 않는다.
- continuation은 assistant message/reasoning/function call만 허용한다. System/User 승격이나 숨은 호출을 거부하고 call ID·이름·원본 arguments를 실행 예정 목록과 1:1 대조한다.
- Tool 결과도 출력 Schema로 확인한 정제 복사본만 모델에 돌려준다. Tool의 제품 실패 결과를 모델 설명으로 PASS로 바꾸지 않는다.
- 모델/Tool timeout·취소·거부·incomplete·Schema/권한 오류는 별도 제어 오류다. 실제 실패/불명확 Write는 자동 재실행하지 않는다.

등록된 ToolExecutor는 신뢰된 주입 지점이지 실제 MCP 세션이나 Sandbox가 아니다. 이번 테스트의 Fake Tool은 오프라인 경계 검증용이며 운영 기본 성공 구현이 아니다.

## 4. Source·비밀정보·사용량

Prompt/Tool/최종 JSON의 일반 데이터는 기존 redaction을 적용한 복사본을 사용한다. 인식 가능한 Credential이 모델의 일반 Tool 인자에 포함되면 실행을 거부한다. API Key는 모델 Context나 Tool 인자로 보내지 않는다.

일반 코드의 `password = request.password`를 실제 비밀번호로 오인하여 코드가 변형되는 일을 막기 위해 신뢰된 Tool 정의에만 Source 필드 annotation을 허용한다.

| 방향 | 허용 Tool·필드 |
| --- | --- |
| 입력 | `write_source_file.content`, `apply_patch.patch`, `write_test_file.content` |
| 출력 | `read_project_file.content` |

정확한 Tool/필드와 string Schema만 허용한다. Model 인자/Tool 설명으로 `authorization`·`apiKey` 등 다른 필드를 예외로 만들 수 없다. Source 변수 대입은 원문·줄바꿈을 보존하지만 인식 가능한 Credential literal/Bearer/JWT/Password Hash는 거부한다. 이 검사는 패턴 기반 보조 경계이며 언어별 AST·완전한 Secret/taint 검사가 아니다. Secret 파일 접근·실제 MCP 강제는 22~23번 범위다. 테스트용 비밀번호 literal도 차단될 수 있으므로 실제 QA 테스트 작성 시 Runtime 생성 데이터/승인된 fixture 경계를 따로 설계해야 한다.

UsageRecord에는 역할·요청 모델 설정·반환 모델 ID·Provider 응답/오류 상태·소요 시간·실제 토큰만 담는다. Provider 응답 완료 상태는 전체 Agent 작업 완료를 뜻하지 않는다. Prompt·전체 Source·Tool 인자/결과·API Key·Provider 예외 본문·encrypted reasoning은 Record/sink로 보내지 않는다.

확인하는 토큰은 input/output/total, 제공된 cached input/reasoning output이다. 미제공 사용량은 `None`, 불완전한 전체 합도 `None`이다. 이미 확인한 토큰은 refusal/incomplete/잘못된 응답에서도 보존한다. 가격/청구 비용을 선택·확인하지 않았으므로 `cost_usd=None`이며 무료/0달러로 만들지 않는다. 비용 계산과 영속 Run Trace는 37번에서 연결해야 한다.

## 5. 한도와 공유 예산

| 항목 | 기본값/정책 |
| --- | --- |
| 모델 호출 | 최대 8회 |
| Tool 호출 | 최대 20회, 한 응답의 전체 batch가 남은 한도에 들어야 시작 |
| 모델 응답 출력 | 최대 2,048 tokens, API 최소 16 |
| 모델/Tool 단일 timeout | 각각 60초, 전체 남은 시간이 더 짧으면 그 시간 사용 |
| JSON 크기 | 기본 1 MiB. 원문 입력·응답·continuation을 제한 |
| 전체 토큰 | 기본 미설정. 실제 반환 사용량으로 검사, 미측정 시 한도 설정 실행은 fail-closed |
| 전체 시간 | 기본값 없음. Host가 `ExecutionBudget(runtime_budget_ms=..., limits=...)`로 전달 |

같은 ExecutionBudget 객체를 재사용하면 역할/반복 호출이 바뀌어도 wall-clock deadline·호출 횟수·토큰 합을 초기화하지 않는다. 현재는 단일 프로세스 공통 구조다. 분산 Agent·Run 재개·프로세스 재시작까지 동결 Run 전체 예산을 영속 강제하는 구현은 34/37번에서 이어간다. Run 전체 예산을 역할별로 새로 만들어 재설정하면 안 된다.

토큰 한도는 응답 뒤 실제 usage로 검사한다. 다음 출력 한도는 남은 측정 토큰으로 줄이지만 입력 토큰·이미 진행 중인 호출 과금까지 정확히 사전 예약하지 않는다. 따라서 전체 토큰/달러의 엄격한 선불 상한이라고 주장하지 않는다.

이 한도는 기존 Code Fix 최대 3회·동일 논리 MCP Tool Retry 최대 2회와 다른 실행 제한이다. 이번에 MCP Retry 정책을 새로 만들거나 Fix 횟수를 바꾸지 않았다. 실제 MCP Retry/evidence는 29번이다.

## 6. 실행 라이브러리 사용 경계

실제 호출은 설정과 신뢰된 Host 입력을 준비한 뒤 명시적으로 수행한다. 아래는 연결 지점 예시이며 제품 Agent나 회원가입 시연 실행 예제가 아니다.

```python
from agents.llm.configuration import model_from_settings, provider_from_settings
from agents.llm.engine import LLMEngine

async def invoke_model(settings, *, frozen_model, prompt, output_contract, shared_budget):
    model = model_from_settings(settings, frozen_model=frozen_model)
    provider = provider_from_settings(settings)
    try:
        return await LLMEngine(role=settings.role, provider=provider).run(
            prompt=prompt, model=model, output=output_contract,
            budget=shared_budget,
        )
    finally:
        await provider.aclose()
```

Tool을 쓰려면 신뢰된 등록 목록과 실제 ToolExecutor를 별도로 주입해야 한다. 기존 `/health.executionReady=False`, transport-only Agent Card, 기본 Bootstrap의 REJECTED 응답은 유지한다. LLM 설정만 입력해도 기존 Agent 서버가 제품을 개발하는 것은 아니다.

## 7. 개발정의서 준수 점검

| 기준 | 이번 구현과 남은 범위 |
| --- | --- |
| 책임 경계 §2 | 모델 분석/Tool 요청만 처리. Final Verdict·Registry·실제 검증 결과를 모델에 맡기지 않음 |
| Fix/Retry §4 | 기존 최대 3/2 불변. 자동 모델·Tool Retry 없음, 실제 MCP Retry29번 |
| Tool 권한 §8 | 기존 allowlist·Schema·신뢰 Context 검사. 실제 경로/ACL/Network/Sandbox20~29번 |
| Source/Secret §8·§11 | 복사본 정제, 제한 Source annotation, 원문 Source/Credential/오류 본문 accounting 비기록. 완전한 Secret 탐지 주장은 없음 |
| 성공 판정 §10 | 구조화 JSON을 Build/Test/Security PASS나 Workflow SUCCESS로 쓰지 않음 |
| 모델·예산·사용량 §11-A | 기존 동결 ModelConfiguration 비교, 공통 deadline, 실제 토큰/호출 기록. 비교 실험·가격·분산 영속 합계는 완료 아님 |
| A2A/MCP 규격 | 기존 A2A1.0 binding/상태/Schema·MCP2026-07-28 정책 변경 없음 |
| 담당 범위 | 역할3 서비스/역할4 평가 코드 수정 및 실제 팀원 연결 없음 |

앞서 보류한 `protectedTestSuiteRef`/`scannerProfileRef` Credential URL 보완과 드문 기존 SQLite 초기 WAL 잠금 문제는 이번에도 변경하지 않았다. 이번 테스트 통과로 기존 이슈가 해결됐다고 표시하지 않는다.

## 8. 검증·커밋·다음 작업

신규 오프라인 테스트 94개 통과: 공통 Runtime/JSON/Source58개 + OpenAI MockTransport28개 + 설정8개. Model/Tool 권한, whole-batch 검증, continuation 위조, 실패 usage, 취소 전파, no-retry, 공통 한도, Source 원문 보존 및 비밀정보 차단을 검증했다.

```bash
PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -p test_llm_runtime.py
PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -p test_openai_responses.py
PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -p test_llm_configuration.py
PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -q
.venv/bin/python -m pip check
git diff --check
```

전체 회귀 첫 실행은 477개, Source 추가 테스트 후 최종 실행은 **485개 모두 통과**했다. `compileall`·`pip check`·diff check도 통과했다. 현재 로컬 환경에 pytest가 없어 `python -m pytest`는 실행되지 않았다. 평가 모듈의 pytest 기반 실험 검증이나 실제 Cloud·모델 접근·가격/과금 검증 완료로 표시하지 않는다. 실제 LLM API 호출은 수행하지 않았다.

커밋 메시지: `공통 LLM 연결과 구조화 응답·Tool Loop·예산 및 사용량 기록 구현`

다음 작업: **20번 — 실제 Workspace Registry·경로·역할 권한.** Orchestrator가 발급한 workspace ID를 실제 안전한 작업 경로 및 역할 권한에 연결한다. Git commit/push는 수행하지 않았다.

## 9. 공식 API 근거

OpenAI Docs 지침으로 공식 Responses 규격을 확인하여 stateless continuation·엄격한 JSON 응답·선택 함수 인자와 실제 usage 처리를 적용했다.

- Function call/call output 및 reasoning 이전: [Function calling](https://developers.openai.com/api/docs/guides/function-calling)
- 폐쇄 object·필수 필드·refusal/불완전 응답 처리: [Structured model outputs](https://developers.openai.com/api/docs/guides/structured-outputs)
- HTTP 요청·store/include·model/temperature·max_output_tokens·usage: [Create a model response](https://developers.openai.com/api/reference/python/resources/responses/methods/create)
