# 57. Agent 통신 검증실

## 목적과 확인 범위

1. Orchestrator가 보낸 `100`을 Agent가 실제로 `100`으로 받는지 확인한다.
2. 전달 중 데이터 변경과, 받은 입력에 근거하지 않은 응답을 구분한다.
3. 송신 준비·요청 전달·Agent 처리·완료 결과 확인 시간을 따로 측정한다.

**실제 프로젝트 A2A 클라이언트·Agent 서버·HTTP·Task 저장소를 사용하고, 업무 실행기만 목업으로 주입하는 별도 진단 도구다.** LLM·MCP·Docker·제품 개발은 실행하지 않는다. 현재 운영 Agent의 모델 응답, 실제 개발 파이프라인, 일반적인 자연어 할루시네이션 전체를 검증한 결과가 아니다.

**현재 기본 자료는 엄격 검증 v2의 합성 가입 요청이다.** 자료 3개·역할 지정·21종 시나리오·고정 정답 대조·본문 크기 선택을 추가했다. 아래 기본 통신 설명에 더해 [58번 안내](58-strict-agent-communication-validation.md)와 [최신 PDF](reports/agent-communication-strict-v2-2026-11-08/report.pdf)를 참고한다.

## 실행

프로젝트 루트에서 실행한다. 기존 개발 환경의 `.venv`를 사용하며 API 키와 Docker가 필요 없다.

```bash
PYTHONPATH=src .venv/bin/python -m diagnostics.communication
```

브라우저: **http://127.0.0.1:8766/**

포트 변경:

```bash
PYTHONPATH=src .venv/bin/python -m diagnostics.communication --port 8767
```

종료: 실행한 터미널에서 `Ctrl+C`.

화면 HTML은 `src/diagnostics/communication/dashboard.html`이다. API로 실제 실험을 실행하므로 파일을 더블클릭해서 열기보다 위 로컬 주소로 접속한다.

## 먼저 해볼 순서

1. **정상 전달 확인** → 보내는 값 `100`, 반복 `3`, 검증 시작.
2. Developer 행 선택 → `100 / 100`, 단계별 일치 여부, Task·Context 확인.
3. **구간별 시간** 탭 → Orchestrator 준비와 Agent 처리, 결과 확인 시간 비교.
4. **요청 수신 단계에서 값 변경** → HTTP 수신 뒤 파서에 넘기기 전 `100 → 101`을 주입한다. 네트워크는 일치하고 수신 처리 경계가 불일치하는지 확인.
5. **틀린 수치·근거 없는 주장 추가** → 전달은 정상인데 지정 필드의 근거 대조가 실패하는지 확인.
6. **응답 누락** → 전송·수신·완료 결과 개수가 달라지고, 이후 단계는 미전송으로 표시되는지 확인.
7. **결과 JSON 저장** → 현재 완료 실험의 전체 관측값·해시·변경 필드·시간·집계를 저장.

## 구현 경로

```text
브라우저
  → 진단 Orchestrator (업무 Orchestrator와 별도)
  → 프로젝트 A2AAgentClient / 공식 SDK
  → 실제 loopback TCP HTTP
  → 수신 관측 미들웨어
  → 프로젝트 agents.main.create_app / 인증 / 메타데이터 검증 / SQLite Task 저장소
  → SafeAgentExecutor → MockProbeExecutor
  → 실제 A2A Artifact와 Task 상태 갱신
  → A2AAgentClient.get_task() 폴링
  → 브라우저로 실측 관측값 스트림
```

멀티는 Planner → Developer → QA·Security 병렬 순서이며, 역할 간 결과는 진단 Orchestrator가 중계한다. Developer가 받은 `upstream`에는 실제 수신한 Planner 결과가 들어간다. QA·Security도 실제 수신한 Developer 결과를 전달받는다. `upstreamHash`로 앞 단계 결과의 연결을 확인한다.

싱글 모드는 실제 `SINGLE_AGENT` 서비스 식별자를 가진 진단 서버 하나로 보낸다. **싱글과 멀티의 실제 개발 품질·성능 비교 실험은 아니다.**

## 1. 송신과 수신 검증

각 요청은 다음 경계를 따로 보관한다.

| 관측 위치 | 의미 |
| --- | --- |
| 최초 송신 입력 | 진단 Orchestrator가 의도한 목업 데이터 |
| A2A 클라이언트 송신 JSON | 실제 HTTP 클라이언트 전송 훅에서 읽은 데이터 |
| 서버 HTTP 수신 JSON | 수신 본문을 읽은 서버에서 독립 관측한 데이터 |
| 서버 파서 입력 | 수신 미들웨어를 거쳐 실제 프로젝트 파서에 전달하는 데이터 |
| Agent 실행기 입력 | 실제 `RequestContext.message`에서 꺼낸 데이터 |
| Agent 실행기 생성 결과 | 실제 Artifact로 게시하기 전의 결과 |
| Orchestrator 완료 결과 | 실제 A2A Task 조회로 받은 Artifact |

관측한 양쪽 JSON의 변경·추가·누락·타입·배열 순서와 SHA-256을 비교한다. Protobuf Struct가 정수 `100`을 `100.0`으로 표현하는 차이는 같은 JSON 숫자로 취급한다. 문자열 `"100"` 및 `true`와 숫자 `100`/`1`은 다르게 취급한다.

해시는 프로젝트 진단용 정규화 JSON의 해시이며, TCP 바이트 전체의 동일성이나 RFC 표준 정규화 구현을 주장하지 않는다. 공백·JSON 키 순서는 비교 대상이 아니다. 입력 숫자는 안전한 작은 정수로 제한한다.

Run·Step·Scenario 메타데이터를 송신/실행기/반환 Task에서 대조한다. v2는 송신 Message ID와 ACK·조회 Task/Context를 실행기가 독립적으로 기록한 실제 ID와도 비교한다. ACK와 조회 응답이 같은 거짓 Context를 반환해도 정상으로 판정하지 않는다. 관측값이 없으면 `UNVERIFIED`이며 `PASS`로 채우지 않는다.

예정 요청, 실제 송신, Agent 입력 수신, 완료 결과 수신을 따로 센다. 접수 ACK를 완료 응답으로 세지 않는다.

## 2. 전달 오류와 근거 없는 응답 구분

| 실험 | 전송 대조 | 지정 필드 근거 | 해석 |
| --- | --- | --- | --- |
| 정상 `100 → 100`, 응답 `100` | PASS | PASS | 이 목업과 지정 필드에서 일치 |
| 입력 수신 후 `100 → 101`, Agent는 받은 `101`을 응답 | 수신 처리 경계 FAIL | 받은 입력 기준 PASS / 원래 입력 기준 FAIL | 전달 과정 변경을 Agent의 자체 창작과 구분 |
| 받은 값 `100`, Agent가 `101` 및 배포 성공을 주장 | 입력·응답 전달 PASS | FAIL | 틀린 수치·등록된 근거 없는 주장 탐지 |
| 실행기 응답 `100`, 송출 미들웨어가 `101`로 변경 | 응답 FAIL | 실행기 주장 PASS / 최종 수신 주장 FAIL | Agent 결과 생성 이후의 변형 |
| 결과 없음 | 완료 결과 미수신 | UNVERIFIED | 누락·시간 초과이며 성공 아님 |

기본 숫자 자료는 실제 관측한 입력으로 다음 값을 계산한다.

- `claims.value`: 받은 숫자와 같은지
- `claims.label`: 받은 문자열과 같은지
- `claims.requirementCount`: 실제 요구사항 개수와 같은지
- `claims.upstreamHash`: 실제 전달받은 앞 단계 결과의 해시와 같은지
- 등록되지 않은 추가 주장은 `UNSUPPORTED_CLAIM`

v2의 가입·전자문서·추리게임 자료는 위 네 필드 외에 `claims.specification`을 Agent 입력 밖에 고정한 `*.golden.json`과 대조한다. 요구사항·의존 관계·합격 기준 연결·제약·권한 관계·미결정 질문·미실행 증거를 포함하며, 받은 요청이 변형됐다고 정답까지 다시 계산하지 않는다. 목업 실행기는 이 참조를 읽지 않는다. 참조는 합성 자료의 고정 계약이며 실제 고객사에서 검증한 사업 정답은 아니다.

실행기가 스스로 적은 ‘검증 완료’나 JSON 형식의 정상 여부로 사실성을 판정하지 않는다. **자유 서술의 사실성과 실제 LLM의 할루시네이션은 미검증**이다. 실제 모델 검증으로 확장하려면 동일한 관측 경계에 실제 실행기를 연결하고, 요구사항·Source·도구 영수증 등 검증할 주장별 정답 근거를 별도로 준비해야 한다.

## 3. 시간 측정 정의

진단 Orchestrator와 5개 Agent 서버는 한 Python 프로세스의 서로 다른 TCP 포트에서 실행한다. 따라서 같은 `perf_counter_ns()` 단조 시계로 구간을 잰다. 다른 컴퓨터의 시계를 빼서 가짜 편도 지연을 만들지 않는다.

| 화면 구간 | 실제 측정 범위 |
| --- | --- |
| Orchestrator 준비 | 준비 시작 → Agent Card 조회 직전. 첫 요청의 설정 지연 포함 |
| Agent Card 조회 | 실제 Card 조회·검증에 걸린 시간. 요청별 별도 측정 |
| 직렬화·전송 준비 | Card 조회 종료 → 클라이언트 전송 훅. JSON/protobuf 변환과 전송 전 준비 포함 |
| 요청 전달·대기 | 클라이언트 전송 훅 → 서버가 요청 본문을 전부 받은 시점 |
| 수신 후 실행 대기 | 본문 수신 → 실행기 입력 관측. 파싱·인증·Task 접수·스케줄링 등 포함 |
| Agent 처리 | 실행기 입력 관측 → 결과 객체 생성. 설정한 목업 대기 포함 |
| 완료 결과 확인 | 결과 객체 생성 → Orchestrator가 완료 Artifact 관측. 게시·DB·폴링·HTTP·역직렬화 포함 |
| 접수 ACK 왕복 | 송신 → 최초 A2A 접수 응답. 다른 구간과 중첩하므로 합산하지 않음 |
| 전체 | 준비 시작 → 최종 관측/종료 시점 |

폴링 간격은 20ms다. 요청 전달은 순수 네트워크 지연이 아니며, 결과 확인도 순수 응답 전송 시간만은 아니다. 계측·이벤트 직렬화 오버헤드가 포함된다. 미관측 구간은 `null`/`—`로 두며 0ms로 만들지 않는다.

역할별 처리시간의 p50·p95와 표본 수를 표시한다. 최대 5회인 소표본에서 p95는 최대값과 같을 수 있다. 목업 처리시간을 실제 LLM 속도나 아키텍처 우열로 해석하지 않는다.

## 기본 시나리오 — 엄격 오류 14종은 58번에 추가 정리

| 선택 | 주입하는 조건 |
| --- | --- |
| 정상 | 같은 입력·근거 기반 응답 |
| 요청 변경 | Developer/Single의 서버 수신 후 `sample.value + 1` |
| 응답 변경 | Developer/Single의 결과 생성 후 서버 송출에서 `claims.value + 1` |
| 근거 없는 응답 | Developer/Single 실행기가 수치 +1, 근거 없는 `deploymentSucceeded=true` 주장 |
| 느린 Orchestrator | 첫 요청 준비에서 실제 10초 대기 |
| 느린 Agent | Developer/Single 처리에서 실제 1초 대기 |
| 응답 누락 | Developer/Single 결과 보류 → 1.5초 제한 후 실제 A2A 취소 → 후속 단계 미전송 |

역할별 반복은 1~5회, 처리 대기는 0~1000ms, 첫 준비 대기는 0~10000ms다. 전체 실험은 45초 한도이며 한 번에 하나만 실행한다. 브라우저 중단 시 생성한 작업을 취소하고, 관측한 실제 Task ID로 원격 종료를 확인한다. ACK 전 수신측만 Task ID를 관측한 경우도 취소한다. Task ID나 원격 종료 상태를 확인하지 못하면 미검증으로 남긴다. 완료 보고서 없이 끊긴 스트림을 성공으로 처리하지 않는다.

## 운영 환경과 분리

- `.env` 파일이나 실제 모델/API 키를 로드하지 않는다. LLM API 키는 명시적으로 `None`이다.
- 프로덕션 Orchestrator, Agent, MCP, 서비스·평가 코드의 동작을 수정하지 않는다.
- 공식 A2A 1.0 HTTP+JSON, 프로젝트 요청/메타데이터/상태·인증 코드를 재사용한다.
- 역할별 임시 DB·무작위 Bearer 자격증명·임시 loopback 포트를 사용한다. 인증값을 UI/보고서에 반환하지 않는다.
- 대시보드는 loopback에만 바인딩하고, Host·Origin·커스텀 헤더·본문 크기·입력 범위를 제한한다. 사용자가 임의 원격 URL을 지정하는 기능은 없다.
- 기존 Agent Card의 실행 준비 판정을 우회하지 않는다. 목업 실행기는 실제 업무 준비 상태를 광고하지 않는다.
- A2A `COMPLETED`는 진단 Task의 완료이지, 제품의 `SUCCESS`나 QA/Security 합격을 의미하지 않는다.
- 실험 결과는 메모리에 두고, 완료한 보고서는 사용자가 JSON으로 내려받는다. 운영 DB·Git·외부 서비스에 쓰지 않는다.

## 검증

신규 오라클·입력 경계·실제 HTTP·연결 중단 검증:

```bash
PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -p 'test_communication_lab.py' -v
```

현재 등록된 21개 시나리오를 각 1회 실제 HTTP 실행 후 종료하는 간단 확인:

```bash
PYTHONPATH=src .venv/bin/python -m diagnostics.communication --smoke
```

최신 검증에서는 전체 행렬 86/86조건, 통신 단위·회귀 23개, 별도 프로토콜 4개, Chrome 화면 16항목을 확인했다. 조건별 수치와 검증 범위는 [58번 안내](58-strict-agent-communication-validation.md)에 정리했다.

SDK의 `cross-replica streaming is disabled` 경고는 기존 서버의 다중 복제본 이벤트 스트림 설정에 대한 것이다. 이 도구는 한 프로세스와 A2A Task 폴링을 사용한다. 화면의 관측 스트림은 별도의 HTTP NDJSON이며 A2A 스트리밍 지원을 광고하지 않는다.

## 최신 엄격 검증과 PDF 보고서 — 2026-11-08

[엄격 검증 결과 PDF](reports/agent-communication-strict-v2-2026-11-08/report.pdf)에 복잡한 합성 요청 3개, 18종 오류 주입, 구간별 시간과 검증 한계를 정리했다. [조건별 요약 JSON](reports/agent-communication-strict-v2-2026-11-08/summary.json), [전체 송수신 원본](reports/agent-communication-strict-v2-2026-11-08/observations.jsonl.gz), [편집·재인쇄용 HTML](reports/agent-communication-strict-v2-2026-11-08/report.html)도 같은 폴더에 보관한다.

- 정상 조건 338건의 송수신·구조화 근거 대조를 통과했다.
- 의도한 오류 84건을 탐지했고, 본문 크기 초과 6건은 실행 전에 거절했다.
- 숫자·라벨·개수·앞 단계 해시가 모두 같아도 내용이 틀린 응답 32건을 고정 계약 대조로 탐지했다.
- 정상 기준선은 자료·역할별 20회이며, 실제 LLM의 할루시네이션이나 개발 품질은 미검증이다.

커밋 메시지 제안: `Agent 송수신 무결성·근거 대조·구간별 시간 검증실 구현`

다음 확장 후보: 실제 LLM 실행에서 동일한 경계의 관측과 주장별 도구·Source 근거 연결. 이번 목업 실험의 완료 범위와 구분한다.
