# 65. 자연어 진단의 역할별 출력 계약 강화

## 왜 필요한가

60번 실제 API 실험에서는 Developer·QA 응답 일부가 자신의 역할 외 `roleWork` 필드까지 작성했다. 당시 Schema는 네 역할 모두에 일반 문자열을 허용했고, “다른 역할은 빈 문자열로 남긴다”는 Prompt만으로 제한했다.

이번에는 **출력 Schema와 수신 후 검사로 역할별 작성 영역을 제한**한다. [61·62번](62-planner-unapproved-assumptions.md)의 승인 경계와 [63·64번](64-independent-validation-criteria.md)의 제품 Runtime 기준 대조에 이어, 실제 문제를 관측했던 자연어 진단 경로의 역할 혼입을 보완하는 작업이다.

자연어 진단은 사용자의 원문을 해석하는 실험이다. 제품 Runtime의 승인된 Scenario 원장이나 평가용 정답을 모델에 추가로 주지는 않는다.

## 바뀐 동작

`output_contract(role)`는 Host가 선택한 역할을 받아 서로 다른 내부 Schema를 만든다. 요청 본문의 역할 문구나 모델 응답이 Schema를 선택하지 못한다.

| 실행 역할 | 작성할 `roleWork` | 나머지 역할 필드 |
| --- | --- | --- |
| PLANNER | PLANNER | 정확히 빈 문자열 |
| DEVELOPER | DEVELOPER | 정확히 빈 문자열 |
| QA | QA | 정확히 빈 문자열 |
| SECURITY | SECURITY | 정확히 빈 문자열 |
| SINGLE_AGENT | 네 필드 모두 | 작성하지 않는 필드 없음 |

- 작성 대상은 1~4,096자 문자열이어야 한다. 공백만 있는 문장은 별도 로컬 검사로 거절한다.
- 비작성 필드는 `enum: [""]`로 제한한다. 알 수 없는 역할·추가 필드·필수 필드 누락도 허용하지 않는다.
- 이 실험은 제품 도구를 실행하지 않으므로 `executionState`는 `NOT_EXECUTED`만 허용한다.
- 실제 내용은 모델이 작성한다. 잘못된 응답의 다른 역할 필드를 비워서 정답처럼 보정하거나 자동 재호출하지 않는다.

필드 필수 지정, 닫힌 객체와 enum은 [OpenAI Docs — Structured Outputs](https://developers.openai.com/api/docs/guides/structured-outputs)의 제약을 확인하고 기존 어댑터에 적용했다.

### 정상·거절 예시

다음은 Developer 전체 응답 중 역할 부분만 발췌한 예시다.

```json
{
  "roleWork": {
    "PLANNER": "",
    "DEVELOPER": "승인 전 구현 접근과 확인이 필요한 사항을 정리했다.",
    "QA": "",
    "SECURITY": ""
  },
  "executionState": "NOT_EXECUTED"
}
```

여기서 `QA`에 문장을 넣으면 Schema 검사에서 거절한다. `DEVELOPER`에 공백만 넣으면 Schema의 길이 검사를 통과할 수 있지만, 로컬 `MISSING_ROLE_WORK` 검사에서 차단된다. SINGLE_AGENT는 네 관점을 모두 다뤄야 하므로 네 필드 모두 내용이 필요하다.

## 데이터가 흐르는 순서

```text
사용자 자연어 원문
→ Host가 정한 역할의 Schema로 모델 호출
→ 응답 Schema 검사
→ 원문 echo·인용·ID·역할 필드·실행 선언 검사
→ 전송 검사와 내용 검사가 모두 통과한 결과만 다음 역할에 전달
```

모델이 반환한 요구사항·요약·인용·역할 내용은 원래 검사 범위에서 평가한다. 평가용 `criteria.json`은 모델에게 전달하지 않고 기존처럼 실험 전에 해시를 고정한다.

## 송수신과 전달 승인을 구분

`NaturalRecord.view()`에 `handoff.eligible`과 `handoff.reason`을 추가했다.

| 이유 | 뜻 |
| --- | --- |
| `CONTRACT_ADMITTED` | 전송·내용 검사를 통과해 후속 전달 가능 |
| `MODEL_OUTPUT_CONTRACT_REJECTED` | 모델 출력이 역할 Schema에 맞지 않음 |
| `CONTENT_CONTRACT_REJECTED` | 응답은 도착했으나 공백 역할 내용 등 로컬 검사 위반 |
| `NOT_DISPATCHED` | 아직 전송하지 않았거나 앞 단계 차단으로 생략 |
| `RESULT_UNAVAILABLE_OR_TRANSPORT_REJECTED` | 결과 미수신·전송 식별자 불일치 등으로 인수 불가 |

예를 들어 공백뿐인 Planner 결과가 정상 도착하면 HTTP 200·Task `COMPLETED`·원래 응답을 그대로 보관한다. 대신 `handoff.eligible=false`로 표시하고 Developer·QA·Security를 호출하지 않는다. 이때 생략 원인은 `UPSTREAM_CONTRACT_REJECTED`다. 이를 전송 중 데이터가 사라진 것으로 세지 않는다.

Schema 위반은 기존 `LLM_SCHEMA_INVALID` 오류 경로를 사용한다. 이 경우 사용할 수 있는 완성 답변을 만들지 않고 호출 관측과 실패를 남긴다. 모델의 잘못된 답변을 수정한 뒤 정상 Artifact로 게시하지 않는다.

## 파일과 호환성

| 파일 | 변경 내용 |
| --- | --- |
| `src/diagnostics/communication/natural.py` | 역할별 Schema, 수신 검사, 후속 전달 승인, 실행 기록 버전 |
| `tests/test_communication_natural.py` | 역할 혼입·공백·Single 네 관점·전달 차단·기존 원문 분리 검증 |

기록 버전은 `natural-language-3cases-v2`다. 내부 계약 이름은 `natural_language_handoff_v2_planner`처럼 역할별로 다르며, 기존 단일 `schemaSha256` 대신 `schemaSha256ByRole`과 `outputContractNames`를 저장한다. 네 `roleWork` 키와 CLI 인자는 유지한다.

기존 59·60번 실제 API 원자료와 PDF는 그대로다. 새로운 실제 실험은 아래처럼 **새 결과 폴더**를 지정해야 한다. 이 명령은 유료 호출을 발생시키며 이번 작업에서는 실행하지 않았다.

```bash
PYTHONPATH=src .venv/bin/python -m diagnostics.communication.natural \
  --model gpt-4.1-mini-2025-04-14 \
  --out .data/diagnostics/natural-role-contract-new-run
```

최대 15회 호출·자동 재시도 없음·모델 자동 대체 없음·도구 미실행·Root 키 로더·인증 정책은 유지한다.

## 검증 결과와 남은 한계

자연어 및 기존 통신 진단 **20개 통과, 4.638초**. OpenAI 응답은 `httpx.MockTransport`로 대체하고, 인증된 로컬 A2A 서버·Task·Artifact 흐름을 실제로 확인했다. 샌드박스의 포트 열기 제한으로 처음 연결 검사가 막혔고, 로컬 루프백 테스트 권한을 받아 재실행했다. 실제 OpenAI 호출이나 과금은 없다.

정상 3케이스는 15회 연결, 공백 역할 및 다른 역할 혼입은 케이스별 Planner 차단과 독립 Single 실행만 이어져 각각 6회 연결되는 것을 확인했다. 실패를 성공 응답으로 고치거나 차단된 후속 요청을 보내지 않았다.

**검증 한계:** 역할 필드가 올바르더라도 summary·requirements·checks의 의미가 모두 정확한 것은 아니다. 원문 인용이 실제로 있어도 해석이 틀릴 수 있다. 자유 서술은 계속 `UNVERIFIED`, 의미 채점은 `REQUIRES_REVIEW`다. 실제 모델의 개선율·지연시간은 새 API 실험으로 확인해야 한다.

## 개발정의서 대조와 다음 작업

[개발정의서](https://www.notion.so/3e9d062130a9801b943ff3bb463af38a)의 역할 책임, 실행하지 않은 일을 성공 근거로 사용하지 않는 원칙, Single의 네 관점 유지, 비밀정보·재시도 제한을 지킨다. 진단용 Source Quote 검사를 제품 Build/Test/Security 완료로 표시하지 않는다.

이어서 [66번 Host Artifact 인수](66-host-artifact-admission.md)에서 제품 실행 결과의 역할 혼입을 막는다.
