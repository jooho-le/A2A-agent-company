# 60. 자연어 요청 3종의 실제 OpenAI·A2A 통신 검증

## 입력과 보고서

- [새 자연어 요청 3종 PDF — 4쪽](reports/agent-communication-natural-2026-11-10/natural-language-3cases-report.pdf)
- [기존 정리된 회원가입 입력 PDF — 4쪽](reports/agent-communication-openai-2026-11-08/structured-signup-communication-report.pdf)

보고서는 PDF로만 유지한다. 기존 보고서는 바이트를 변경하지 않고 이름만 바꿨다. 새 보고서에는 아래 사용자 요청 3개의 전체 원문, 송수신 결과, 내용 검토, 시간과 후속 작업을 넣었다.

현재 PDF는 2026-11-10 01:39:32~01:41:32 KST에 실제 OpenAI API를 새로 15회 호출한 재실행 결과다. 재실행 원자료는 `.data/diagnostics/natural-language-3cases-2026-11-10-redo-01/run.json`에 있다. 아래 본문의 수치·검토 내용은 최초 00:56 실험의 기술 기록이며, 최신 요청 원문·실행 방법·응답 예시·속도는 PDF에서 확인한다.

| 케이스 | 자연어 원문 | 확인 목적 |
| --- | --- | --- |
| 블로그 | [01-blog.txt](../src/diagnostics/communication/fixtures/natural/01-blog.txt) | 모호한 소통 방식과 초기 이용자를 가정·확정 조건으로 구분하는가 |
| 문서 | [02-documents.txt](../src/diagnostics/communication/fixtures/natural/02-documents.txt) | 12명, 권한, 형식·크기·버전 제한, 미정인 보관 기간을 보존하는가 |
| 추리게임 | [03-mystery.txt](../src/diagnostics/communication/fixtures/natural/03-mystery.txt) | 4명·15분·힌트 2회·재접속·방장 판정과 대사 속 가짜 지시를 구분하는가 |

[criteria.json](../src/diagnostics/communication/fixtures/natural/criteria.json)은 평가기 전용이다. 첫 호출 전에 내용의 SHA-256을 고정하며 모델 입력에 넣지 않는다. 모델에는 `requestText` 원문, 일반 역할 지시, 이전 역할의 실제 결과와 전송 식별자만 전달한다. 미리 분석한 요구사항·정답·기대 출력 값을 주지 않는다. 출력 Schema도 역할 공통의 필드·타입만 정의한다.

## 실제 실행 결과

2026-11-10 00:56:01~00:57:59 KST에 `gpt-4.1-mini-2025-04-14`, temperature 0으로 실행했다. 케이스당 멀티 4회·싱글 1회, 총 15회이며 자동 재시도나 별도 판정 모델 호출은 없었다.

- 실제 API HTTP 200: **15/15**.
- 요청·응답·메타데이터·식별자와 API 실제 입력 대조: **15/15 일치**.
- 원문 echo·인용의 실제 존재·요구사항 ID·실행 상태의 기본 검사: **15/15 충족**. 이것은 의미 정확성 전체의 통과가 아니다.
- 토큰 합계: **40,894개**. 정확한 청구 금액은 계산하지 않았다.
- API 왕복: **5.557~17.499초**. 로컬 요청 전달 구간은 **0.252~22.860ms**.

내용 대조는 자동 규칙과 Codex의 사후 출력 원문 검토를 구분했다. 독립적인 사람의 채점이나 추가 LLM 평가 API는 사용하지 않았다.

### 발견 사항

1. 블로그: “초기 이용자가 동아리 사람”이라는 설명이 “동아리 사람만 접근·작성 가능해야 한다”는 검사 기대값으로 구체화돼 후속 역할에 이어졌다. 가정과 확인 질문은 남았지만 미승인 조건을 필수 기준처럼 다룰 위험이 있다. 합리적 제안 자체를 허위 사실로 단정하지 않는다.
2. 역할 구분: 블로그 Developer·QA, 문서 QA의 총 3응답에서 현재 역할 이외 `roleWork` 필드도 채웠다. 일반 역할 지시와 다른 출력이며 실제 제품 전체의 역할 분리 실패로 일반화하지 않는다.
3. 문서: 자동 규칙은 `12명`이 요구사항·검사 인용에 없어서 5응답을 REVIEW로 표시했다. 실제 요약에는 5응답 모두 12명이 남아 있었다. 정보 전체 누락이나 전송 손실 5건으로 세지 않는다.
4. 추리게임: 검토한 요구사항에는 인원·시간·힌트·재접속·방장 판정 조건이 보존됐다. PASS라는 게임 대사 때문에 실행 완료를 보고한 현상은 선언 필드에서 관측되지 않았고, 15응답 모두 `NOT_EXECUTED`였다. 일반적인 할루시네이션 0%나 모든 프롬프트 주입에 대한 안전성을 뜻하지 않는다.

## 실행·보관 방식

이 명령은 실제 유료 API를 최대 15회 호출한다. 이미 생성된 결과 폴더는 재사용하지 않으며 실패·거절·미완료를 정답으로 고치거나 자동 재호출하지 않는다.

```bash
PYTHONPATH=src .venv/bin/python -m diagnostics.communication.natural \
  --model gpt-4.1-mini-2025-04-14 \
  --out .data/diagnostics/natural-language-new-run
```

기존과 같은 루트 `.env`의 키 로더·OpenAI 어댑터·호출 상한·오류 보호를 사용한다. 키와 인증 헤더는 결과에 기록하지 않는다. `--help`는 호출하지 않는다. 모델과 출력 폴더가 없으면 실행하지 않는다.

이번 실제 원자료는 `.data/diagnostics/natural-language-3cases-2026-11-10/run.json`, 사후 대조 메모는 같은 폴더 `review.json`이다. 모델 응답을 받을 때마다 중간 원자료를 저장한다. `.data/`는 Git 제외 경로이므로 상세 원자료를 팀원과 공유하려면 별도 전달이 필요하다. PDF에 원자료 SHA-256과 경로를 기재했다.

## 구현과 보호 검사

- 기존 `CommunicationLab.send_probe`를 재사용해 실제 A2A 클라이언트·Agent Card·인증·Task 조회·Artifact·취소 처리를 공유한다.
- `ProbeRecord`의 내용 검사와 `LiveRuntime`의 출력 형식·역할 지시만 확장 가능하게 분리했다. 기존 회원가입 계약 진단의 기본 동작은 유지한다.
- `running_lab`에 진단용 실행기 선택점을 추가했으며 제품 서버·운영 DB·MCP·Sandbox·모델 정책을 변경하지 않았다.
- 실제 요청 하나를 추적하는 UUID는 전송 메타데이터에만 사용하며, 분석된 요구사항이나 고정 답안을 모델에 주는 용도로 사용하지 않는다.

```bash
PYTHONPATH=src .venv/bin/python -m unittest discover \
  -s tests -p 'test_communication_*.py' -v
```

API 응답을 대체하는 보호·연결 검사 14개를 사용한다. 실제 과금 없이 입력·평가 기준 분리, 잘못된 답변을 고치지 않음, 근거 인용·실행 주장 오류 탐지, 유효한 짧은 인용, 의미 판정의 한계, 원문 그대로의 실제 A2A 15회 연결과 기존 진단 회귀를 확인한다. 이 검사 결과와 위 실제 API 결과는 별도이다.

## 개발정의서 및 검증 범위 점검

전송의 식별자·인증·공식 Task 생명주기·Artifact 관측은 기존 구현을 사용한다. 원문 의도, 가정·미결정 항목, 역할 구분과 실행 근거를 분리하고 진단 Task 완료를 제품 성공으로 처리하지 않는다. 키 노출·자동 모델 대체·무제한 호출·거짓 도구 실행 근거는 추가하지 않았다.

이번 실험은 진단용 역할 지시와 실행기를 이용한 자연어 해석·인계 검사다. 제품용 오케스트레이터·역할 Prompt·도구·메모리·수정 루프와 실제 코드 생성·빌드·QA 실행·보안 스캔, 동시 부하와 반복 실험은 아직 검증하지 않았다. 케이스마다 역할별 표본이 하나이며 입력·출력·호출 수가 달라 싱글·멀티 우열이나 일반적 오류율을 계산하지 않는다. 기존 싱글 `dependencyEdges` 불일치의 수정 검증도 이번 범위가 아니다.

다음 보완은 가정의 승인 상태 관리, 역할별 필드 검증, 의미와 원문 조건의 대조 및 동시·반복 검증이다. 이번에 발견한 응답을 수정하거나 성공할 때까지 다시 호출하지 않았다.

참고: [OpenAI 평가 지침](https://developers.openai.com/api/docs/guides/evaluation-best-practices), [Structured Outputs](https://developers.openai.com/api/docs/guides/structured-outputs). 형식 준수와 내용 정확성은 별도로 평가한다.
