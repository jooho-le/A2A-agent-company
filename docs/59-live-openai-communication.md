# 59. 실제 OpenAI 호출을 통한 A2A 통신 검증

## 결과

- [정리된 회원가입 입력의 기존 PDF 보고서 — 4쪽](reports/agent-communication-openai-2026-11-08/structured-signup-communication-report.pdf)

이 문서는 2026-11-08에 정리된 회원가입 자료 1개를 5역할에 전달한 실험이다. 새 자연어 요청 3종·15회 실험은 [60번 문서](60-natural-language-communication.md)와 해당 PDF에서 구분해 확인한다. 기존 PDF는 내용 변경 없이 파일 이름만 명확히 바꿨다.

2026-11-08의 실제 호출 결과를 사용해 2026-11-10에 PDF를 개정했다. 송수신, 할루시네이션·지시 준수, 속도 차이를 쉽게 설명하고 확인된 오류와 추가 검증 사항을 구분했다. 로컬 요청 전달 시간과 API 응답 시간을 별도로 표시했다. 추가 API 호출은 하지 않았다.

요청에 따라 해당 보고서 폴더에는 PDF만 유지하며 기존 부속 HTML·JSON은 제거했다. 전체 입출력·실패 원본과 당시 검증 기록은 개정 전 Git 커밋 `b1a2508`의 동일 폴더에서 확인할 수 있다. PDF에도 원자료 커밋과 SHA-256을 명시했다.

루트 `.env`의 `OPENAI_API_KEY`를 읽어 실제 API를 호출했다. 키 값은 명령줄·모델 입력·결과 파일에 기록하지 않았다. 모델 설정이 없어 이번 초기 진단에는 **`gpt-4.1-mini-2025-04-14`, temperature=0**을 CLI로 명시했다. 자동 모델 대체나 재시도 없이 최대 5회로 제한했다.

| 역할 | API HTTP | 송수신 대조 | 고정 계약 대조 | API 왕복 | 사용 토큰 |
| --- | --- | --- | --- | ---: | ---: |
| Planner | 200 | PASS | PASS | 5.366초 | 4,796 |
| Developer | 200 | PASS | PASS | 3.510초 | 5,407 |
| QA | 200 | PASS | PASS | 6.148초 | 6,337 |
| Security | 200 | PASS | PASS | 3.807초 | 5,234 |
| Single | 200 | PASS | **FAIL** | 8.889초 | 6,347 |

**API 호출 5/5 정상, A2A 송수신 5/5 일치, 구조화 계약 4/5 일치**다. 각 역할 1회인 초기 연결 검사이며 성능의 중앙값·p95나 Single/Multi 품질 우열을 뜻하지 않는다. 토큰은 실제 API 응답의 사용량이며 합계 28,121개(입력 24,454 / 출력 3,667)다. 정확한 청구 금액을 산정한 것은 아니다.

## 실제로 드러난 불일치

싱글 응답의 `roleContract.roles.PLANNER.dependencyEdges`가 요구한 형식과 달랐다.

- 기대: 각 의존 관계를 `[의존 요구사항 ID, 현재 요구사항 ID]`로 표현한 **17개 연결 쌍**.
- 실제: 요구사항별 의존 ID 목록을 묶은 **14개 배열**. 빈 배열·한 항목짜리 배열도 포함했다.
- JSON Schema 형식 검사에는 통과했지만, 고정 계약 대조에서 요구한 연결 쌍 표현을 지키지 못한 것을 발견했다.
- 송신·수신 데이터는 일치했으므로 이번 차이는 전달 손실이 아니라 모델이 작성한 결과의 계약 불일치다.

결과를 호스트에서 정답으로 고치거나 성공할 때까지 재호출하지 않았다. 5회 호출 후 종료했으며 실패를 포함한 당시 원자료는 위 Git 커밋에 남아 있다. 자유 서술 전체의 사실성이나 일반적인 할루시네이션 비율을 이 5개 표본만으로 판단하지 않는다.

## 구현 범위

```text
합성 가입 요청
→ 실제 프로젝트 A2A 클라이언트
→ 인증된 역할별 임시 Agent 서버 / 실제 SQLite Task 저장소
→ OpenAIProbeExecutor
→ 기존 OpenAIResponsesProvider / 실제 Responses API
→ 모델이 작성한 역할별 JSON
→ 실제 A2A Artifact·Task 조회
→ 송수신 및 고정 참조 대조
```

멀티는 Planner → Developer → QA·Security 병렬로 전달했다. 싱글은 별도 `SINGLE_AGENT` A2A 서버에 네 역할의 계약 추출을 한 번 요청했다. 진단의 역할별 실행기를 실제 모델로 연결한 것이며, 제품 개발용 실행기·MCP·Docker·코드 생성·빌드·제품 테스트·보안 스캐너를 실행한 것은 아니다.

모델이 작성하는 값:

- 입력 숫자·라벨·요구사항 개수.
- 요구사항 범위·정책·권한·미결정 질문·역할별 계약.
- 짧은 설명 문장. 이 문장 전체의 사실성은 자동 대조 범위 밖이다.

호스트가 계산하는 전송 증거:

- Probe ID, 요청 fingerprint, 이전 결과 hash.
- 실제 수신 Message·Task·Context ID 및 API 요청 입력 해시.

모델에 SHA-256이나 UUID 계산을 시키지 않았다. 모델 답변의 정책·권한·의존 관계 등은 고정 참조로 교체하지 않는다. 결과 생성 중 정답 파일을 읽지 않으며, 출력 Schema에는 자료에서 추론한 필드·타입만 넣는다. `enum`·`const`로 기대 값이나 정답 배열을 강제하지 않는다. QA 사례 ID처럼 출력 사전의 키는 자료에서 얻으므로 이 검사는 자유로운 설계 생성 평가와도 구분한다.

## 다시 실행

프로젝트 루트 `.env`에 다음 변수 이름으로 키가 있어야 한다. 실제 키는 이 문서나 코드에 작성하지 않는다.

```dotenv
OPENAI_API_KEY=본인_API_키
```

프로세스 환경의 같은 변수가 있으면 우선한다. `docs/.env`·`docs/reports/.env`는 읽지 않으며 환경변수 치환이나 전체 환경변수 덮어쓰기를 하지 않는다. 기존 설정 파일은 수정하지 않는다.

새 결과 폴더를 지정해 실행한다. 기존 결과를 덮어쓰지 않는다.

```bash
PYTHONPATH=src .venv/bin/python -m diagnostics.communication.live \
  --model gpt-4.1-mini-2025-04-14 \
  --profile signup \
  --architecture both \
  --rounds 1 \
  --max-calls 5 \
  --max-output-tokens 8192 \
  --timeout 60 \
  --out /tmp/a2a-openai-new-run
```

이 명령은 실제 유료 API 호출이다. 선택 모델은 결과에 기록하며 자동으로 바꾸지 않는다. 다른 모델을 선택할 때는 기존 OpenAI 어댑터의 temperature·Structured Outputs 호환성을 확인해야 한다. 이번 초기 검사 모델을 향후 운영 모델로 확정한 것은 아니다.

인증·과금·모델·요청 설정 오류는 안전한 코드로 기록하고 이후 새 호출을 중단한다. 미완료·거절·Schema 오류·계약 불일치를 성공으로 바꾸지 않는다. `--architecture SINGLE_AGENT --max-calls 1`이면 싱글만 1회 호출할 수 있다.

`python -m diagnostics.communication`도 위 실제 OpenAI CLI로 연결된다. `--model`과 `--out`을 명시하지 않으면 호출하지 않는다. 목업 전용 대시보드·오류 주입 실행기·보고서·57/58번 문서는 제거했다. 실제 호출 결과 요약은 위 PDF로 확인한다.

## 검증

```bash
PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -p 'test_communication_live.py' -v
```

최초 실제 호출 당시에는 통신 회귀 31개가 통과했으며 당시 검증 기록은 위 Git 커밋에서 확인할 수 있다. 현재는 목업 전용 검사 23개를 제거하고 **실제 API 코드의 보호·연결을 확인하는 회귀 8개**를 유지한다. 루트 `.env`만 읽기, 키 비노출, 고정 참조 미사용, 모델 오류를 호스트가 고치지 않음, 인증·과금 실패 시 재시도 없음, 호출 상한, 실제 A2A 멀티·싱글 연결을 포함한다.

이 회귀 검사에서는 API 응답을 테스트용으로 대체하므로 추가 과금이 없다. 실제 모델 성능 보고서와 별개인 코드 보호 검사다. 실제 호출의 입력·정답 참조·송수신 관측·Schema 생성은 유지했고, Schema 생성은 정답 응답을 만들지 않고 필드·타입만 직접 정의하도록 분리했다.

목업 전용 구현을 정리할 당시 모든 자료·역할의 원본 JSON 및 A2A Struct 표현을 합친 30개 조건에서 정리 전후 Schema의 필드·타입·필수 항목이 동일한지 확인했다. 당시 실제 호출 결과는 변경하지 않았으며 이번 개정에서는 그 측정값으로 PDF만 재작성했다. PDF는 4쪽 A4, 한글 텍스트 추출, 주요 수치 일치, 페이지·표·본문 잘림 여부를 확인했다. 이번 문서 개정으로 API 코드나 테스트는 변경하지 않았다.

개발정의서의 A2A 식별자·상태·인증·Task 저장 보호는 재사용하며, 운영 Orchestrator·모델 정책·Sandbox·MCP 권한을 우회하거나 변경하지 않는다. 이번 `COMPLETED`는 진단 Task 완료이며 제품 성공 판정이 아니다. 순수 API 속도·동시 부하·실제 서비스 완성도·전체 자연어 사실성은 별도 검증이 필요하다.

## 공식 문서

- [OpenAI Structured Outputs](https://developers.openai.com/api/docs/guides/structured-outputs): Responses의 JSON Schema 출력과 형식 검증.
- [GPT-4.1 mini 모델 정보](https://developers.openai.com/api/docs/models/gpt-4.1-mini): 이번 진단에 명시한 모델·스냅샷 및 지원 기능.

커밋 메시지 제안: `루트 환경변수 기반 실제 OpenAI A2A 통신 검증 및 호출 제한 추가`

다음 작업: 싱글 의존 관계 응답의 불일치를 줄일 방법을 정하고, 별도 호출 예산으로 반복 검증한다.
