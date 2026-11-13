# 73번 — 보강 후 실제 OpenAI 통신 재검증

## 목적과 범위

61~72번 보강 이후 기존 자연어 요청 3개를 실제 OpenAI API로 다시 실행했다. 확인 항목은 **송수신 보존, 응답의 역할·내용, 구간별 지연**이다. 입력을 새로 단순화하거나 실패한 응답을 재호출로 교체하지 않았다.

이번 경로는 로컬 A2A HTTP·실제 SQLite 수신 기록·OpenAI Responses API를 연결한 **진단 실행**이다. 제품 Host의 승인 원장·실제 코드 생성·MCP·Docker·빌드·수정 반복·보호된 QA/보안 테스트를 실행한 것은 아니다. 따라서 이 실험만으로 61~64번의 제품 승인 정책이나 서비스 전체 완성을 증명하지 않는다.

## 실제 실행 설정

| 항목 | 값 |
| --- | --- |
| 실행 시각 | 2026-11-13 14:56:50~14:59:00 KST |
| 모델 | `gpt-4.1-mini-2025-04-14` — 응답의 모델 식별자도 일치 |
| 온도 / 출력 상한 | `0` / 호출당 `8192` tokens |
| 타임아웃 / 호출 상한 | `90`초 / 총 `15`회 |
| 입력 | 블로그·전자문서·추리게임 자연어 원문. [기존 요청](../src/diagnostics/communication/fixtures/natural) 유지 |
| 구조 | 케이스별 Multi 4역할 + Single 1역할, 총 6회 진단 실행 |
| Multi 순서 | Planner → Developer → QA·Security 병렬 |
| 실행 순서 | 각 케이스에서 Multi → Single. 별도 워밍업 호출 없음 |
| 모델 입력 | 자연어 원문·일반 역할 지시·이전 역할의 실제 결과. 평가 기준/정답 제외 |
| Retry / 대체 모델 | 자동 재시도 없음 / 모델 교체 없음 |
| 관측 / 연결 | `FINAL_ONLY` / `DIAGNOSTIC_SESSION` |

키는 루트 `.env`에서 기존 방식으로 읽었다. 키 값·인증 헤더·설정 파일 내용은 결과와 보고서에 기록하지 않았다. 추가 유료 판정 모델도 사용하지 않았다.

실행에 사용한 명령은 아래와 같다. **다시 실행하면 실제 API 비용이 발생**하므로 보고서 재생성만 할 때는 이 명령을 사용할 필요가 없다.

```bash
PYTHONPATH=src .venv/bin/python -m diagnostics.communication.natural \
  --model gpt-4.1-mini-2025-04-14 \
  --out .data/diagnostics/natural-language-3cases-2026-11-13-hardened
```

## 확인 결과

| 항목 | 실제 관측 |
| --- | --- |
| API 호출 / HTTP 200 / 서버 Request ID | 15회 / 15회 / 15개 |
| 완료 / 건너뜀 / 후속 전달 허용 | 15행 / 0행 / 15행 |
| 원문 → 직렬화 → 서버 → 접수 → 실행기 | 각 대조 지점 15/15 일치 |
| 실제 DB 접수 기록 대조 | 15/15 일치 |
| API로 보낸 입력 대조 | 15/15 일치 |
| 응답·요청 metadata·응답 metadata | 각각 15/15 일치 |
| 출력·인용·실행 주장 불변조건 | 15행에서 위반 0건 |
| 다른 역할 `roleWork` 필드 | 채운 필드 0개 |
| 실행 상태 선언 | 15/15개 응답 `NOT_EXECUTED` |
| 입력 / 출력 토큰 | 25,237 / 17,666 |
| Client / Card 재사용 | 각각 10행. TCP 연결 재사용 자체를 직접 계측한 값은 아님 |

원자료의 상태는 `CAPTURED_REVIEW_REQUIRED`로 보존했다. 모든 전송이 맞아도 의미 정확성 검토까지 자동 통과시키지 않는다. 사후 검토에서 블로그의 미승인 조건 검사 기준화와 추리게임의 개인정보 범위 확대 위험이 확인됐다. 근거와 읽는 방법은 [74번](74-communication-revalidation-report.md)과 [새 PDF](reports/agent-communication-revalidation-2026-11-13/revalidation-report.pdf)에 정리했다.

## 실행 근거 보존

`src/diagnostics/communication/provenance.py`를 추가했다. 호출 전에 선택한 실행 코드·패키지 설정·잠금 파일 총 25개의 SHA-256, Git 기준 커밋, 작업트리 변경 유무, Python/주요 패키지 버전, Transport 종류, 실행 순서와 Cache 미확인 상태를 기록한다. `.env`나 환경변수 전체를 수집하지 않는다.

실행 종료 후 같은 소스들의 해시를 다시 비교했고 `sourceStability=STABLE`이었다. 이는 **선택한 파일이 실행 도중 유지됐다는 뜻**이며, 전체 환경 재현이나 다른 날짜의 동일한 API 서버 부하를 보장하지 않는다. 작업트리가 수정된 상태임도 숨기지 않았다.

| 근거 | 값 |
| --- | --- |
| 원자료 | `.data/diagnostics/natural-language-3cases-2026-11-13-hardened/run.json` |
| 원자료 SHA-256 | `f4276e97efb6fc4cd1cccbd3299da3626fe83abef689b7fa814a96d32fe0723f` |
| 선택 소스 manifest SHA-256 | `2cfef6ff6e1e4d52c92e238c96da17116b9b333ea7ee5f78888a961bcea10368` |
| Git 기준 커밋 | `63364f52584d96b878065b18784417ae1b592bd8` |

`run.json`은 수정하지 않고, 같은 Git 제외 폴더에 사후 해석 `review.json`과 재계산 가능한 `summary.json`을 별도로 저장했다. 서버 Request ID와 원래 응답은 원자료에 남아 있다.

## 개발정의서 점검

- A2A 공식 상태·metadata·Artifact 계약·MCP 권한·인증 정책을 변경하지 않았다.
- 기존 요구사항·원문·모델·온도·출력 상한을 유지하고, 역할별 닫힌 진단 Schema를 실제 API에 적용했다.
- 미검증·미실행을 제품 성공으로 취급하지 않고, 가정과 실제 실행 증거를 구분했다.
- 이전 실험과 Prompt·Schema·관측 조건이 달라졌음을 공개했다. 동일 환경의 정식 성능 비교나 개선율로 발표하지 않는다.
- 3번 담당 프론트와 4번 담당의 독립 평가 구현을 수정하지 않았다.

검증 명령·90개 회귀 검사 결과·보고서 출력 검증은 [74번](74-communication-revalidation-report.md)에 함께 기록한다. 다음 작업은 **74번 결과 보고서·남은 이슈 정리**다.
