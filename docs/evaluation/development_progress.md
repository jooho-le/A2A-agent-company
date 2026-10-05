# 개발진행사항

최종 갱신: 2026-10-04 · 평가 패키지 0.2.0 · 작업 브랜치 seokmin.
공통 계약 기준: dev `2b4fc6037e1a7d8296040c01404c56e8a2a0f22e`.

## 현재 상태

| 기능 | 상태 | 범위 |
|---|---|---|
| 검사 집계 | 구현 | 필수·선택, 누락·중복·증거 UUID, 실패·미검증 보존 |
| Manifest | 구현 | 코드·환경 일치, 공통 camelCase 변환, 해시 형식 검사 |
| 공통 보고서 | 신규 구현 | QA_REPORT/SECURITY_REPORT 데이터 생성, dev 스키마 검증 |
| ID 대응 | 신규 구현 | 호출자가 제공한 내부 검사 UUID→외부 testId·요구사항 ID 대응 |
| 상태·증거 | 신규 구현 | ERROR/NOT_RUN→UNVERIFIED, details에 내부 상태·원래 상태·사유·증거 ID 보존 |
| 실행 근거 | 신규 구현 | Tool 역할·Manifest·연속 시도·안전 재시도·제품 결과 정합성 검사 |
| 설치·의존성 | 신규 구현 | dev 공통 패키지·루트 uv.lock, 평가 스키마 동봉 |
| 실제 MCP/Registry/API/DB 연결 | 미구현 | 실행·증거 수집·진위 확인·요청 소유 관계는 연결 필요 |
| A2A 포장·전송 | 미구현 | 실제 Task/Context 및 Artifact 연결 담당 합의 필요 |
| 비교 실험 | 미구현 | 전체 통합과 고정 기준 이후 진행 |

## 이번 구현의 입력·출력

`build_report`는 신뢰된 context, 기대/관측 Manifest, 고정 필수 plan, 정규화 results,
명시적 bindings, 실제 실행기가 제공한 tool_evidence를 받는다.
Security는 findings를 빈 목록이라도 명시적으로 제공해야 한다.

출력은 공통 보고서 JSON data payload다. localSummary를 그대로 보내지 않는다.
Artifact/Run/Step/A2A ID·생성 시각·요구 범위는 호출자가 제공하며 새 ID나 실행 이력을 만들어내지 않는다.
기준 Manifest와 다르거나, PASS/FAIL에 실행 근거가 없거나, 시도 이력이 모순되면 거부한다.
결과가 누락되면 UNVERIFIED와 RESULT_MISSING을 기록하며 도구 이력을 추정하지 않는다.

ID는 등록 여부를 조회하지 않는다. 텍스트·증거 마스킹은 호출자 책임이며
HTTP URL·Artifact URI를 다운로드하거나 도구를 실행하지 않는다.

## 제한과 회의 결정 항목

| 항목 | 현재 처리 | 남은 결정 |
|---|---|---|
| testId | 내부 UUID 유지, 외부 ID 명시적 매핑 | 발급 주체·공통 대응표·suite 버전 |
| ERROR/NOT_RUN | UNVERIFIED 변환, details JSON으로 원인 보존 | 팀 공통 사유 필드 채택 여부 |
| N/A | 변환 거부 | 승인된 적용 제외를 외부 계약에 표현할 방법 |
| Security 다중 검사 | 요구사항당 사전 집계 결과 1개만 지원, 중복 요구 결과 거부 | 여러 검사·증거를 손실 없이 묶는 공통 규격 |
| Tool evidence | 형식·상호 일치·재시도 규칙 검사 | 실제 생성·등록·조회·마스킹 책임 |
| 필수 plan | 입력 plan과 binding, Step 요구 범위 완전성 검사 | 권위 있는 전체 검사 목록·해시 제공자 |
| 정책 | 기존 OPEN 유지 | Argon2id·이메일·혼합 최종 FAIL 우선순위 승인 |
| 설치 | dev 공통 패키지와 루트 lock으로 통합 | 팀 공통 의존성 변경 검토 |

선택 검사는 필수 외부 보고서에 섞지 않는다. 내부 집계와 최종 Verdict는 서로 다른 책임이다.
보안 finding의 위험도·차단 여부에 대한 새 판정 정책은 추가하지 않았다.

## dev에서 확인한 내용

Planner → Developer → Build 결과 확인 → QA/Security, 수정·재검증, Run/Step/Task/Trace·Artifact,
Run Configuration·Workspace·Tool 시도 기록과 재개·복구·취소가 구현되어 있다.
실제 Build/Test/Scan은 Agent/MCP 담당이며 원본 bytes·격리 권한·제품 검증은 별도 범위다.

- [실행 흐름](https://github.com/jooho-le/A2A-agent-company/blob/2b4fc6037e1a7d8296040c01404c56e8a2a0f22e/src/orchestrator/application/dispatch.py)
- [보고서·최종 판정](https://github.com/jooho-le/A2A-agent-company/blob/2b4fc6037e1a7d8296040c01404c56e8a2a0f22e/src/orchestrator/application/validation_output.py)
- [구현 제한](https://github.com/jooho-le/A2A-agent-company/blob/2b4fc6037e1a7d8296040c01404c56e8a2a0f22e/docs/14-orchestrator-contract-compliance.md)

Scenario UUID는 f7f9e5c3-ffc3-4b3f-918b-21e1b956ce76이며 요구 UUID는 Run의 동결 scenarioContract를 사용한다.
QA 요구는 REQ-001/002/003/004/007, Security는 REQ-003/005/006, REQ-008은 Orchestrator다.

코드에 들어 있는 Argon2id(memoryKiB=19456, iterations=2, parallelism=1)·이메일 공백 제거와
local/domain 소문자화는 팀 승인 여부를 확인해야 한다. dev는 필수 FAIL과 UNVERIFIED 혼합 시
FAIL 분기를 먼저 수행하고 수정 소진 시 FAIL이다. 본 명세의 OPEN-01/04 승인 상태는 유지한다.

## 검증 기록

- 평가 자체 테스트: 91개 통과.
- dev의 실제 QAReportArtifact/SecurityReportArtifact 모델: 합성 보고서 8개 수용.
- 평가 wheel 빌드 및 작업 폴더 밖에서 schema 포함 패키지 import·보고서 생성 확인.
- 고정 커밋의 공통 스키마 4개와 동봉 사본 동일성 확인.
- dev 자체 unittest 172개 통과는 앞선 별도 임시 환경 검토 결과이며 이번 평가 테스트와 합산하지 않는다.

모델 수용은 A2A parser/Task 소유 관계·실제 서비스 실행 통합 검증을 뜻하지 않는다.
공통 보고서 예시는 `.venv/bin/python -m evaluation.report_demo`로 재생하며 모두 mock이다.

## 다음 작업과 완료 조건

1. 검사 ID·suite·N/A·Security 다중 증거 규격 및 정책 승인을 기록한다.
2. 실제 MCP 결과와 증거 저장소를 연결하고 A2A 포장·Task/Artifact 식별을 검증한다.
3. 격리 Build → 준비 → 가입 → 응답/DB → 증거 → 정리를 한 실행에서 확인한다.
4. 오류·보안 실패 → Issue → 수정 후 같은 새 버전 전체 재검증으로 확장한다.
5. 고정 조건과 실제 근거를 갖춘 뒤 Single/Multi 비교 실험을 진행한다.

## 변경 이력

| 날짜 | 변경 |
|---|---|
| 2026-10-04 | 초기 집계·Manifest 비교 및 dev 호환 해시 형식 검사 |
| 2026-10-04 | 환경/명세서/개발진행사항 문서 구조 정리 |
| 2026-10-04 | 공통 보고서 어댑터·명시적 ID 매핑·Tool 근거 검증·합성 예시 추가 |
| 2026-10-04 | 평가 패키지·lock·스키마 동봉 및 dev 모델 호환 확인 |

환경은 [환경 설정](environment_setup.md), 정책은 [명세서](evaluation_spec.md)에서 관리한다.

## 2026-10-05: 새 개인 브랜치에 평가 작업 추가 (이전 구조)

- 최신 dev `2b4fc60`에서 `seokmin` 브랜치를 만들었다.
- 기존 평가 코드·테스트·문서의 최종 내용만 옮기고 기존 개인 커밋 이력은 가져오지 않았다.
- dev의 코드·문서·루트 README는 그대로 유지하고 평가 안내는 `evaluation/README.md`에 두었다.
- 공통 의존성 설치 후 평가 패키지를 추가 설치하는 통합 실행 절차를 문서화했다.
- 검증: 동일 환경에서 전체 pytest 263개 통과, dev 모델이 합성 보고서 8개 수용. 실제 A2A/서비스 통합 검증은 별도다.

## 2026-10-05: dev 공통 환경으로 통일

이 절이 현재 설치 구조이며 위의 독립 패키지·별도 lock 기록은 이전 구현 이력이다.

- 최신 dev `2b4fc60`에서 seokmin을 새로 만들고 평가 코드를 `src/evaluation/`에 추가했다.
- 루트 pyproject와 uv.lock, .venv 하나를 사용한다. 평가 전용 pyproject와 lock은 제거했다.
- 평가 문서는 `docs/evaluation/`, 평가 테스트는 `tests/evaluation/`로 구분한다.
- 기존 Orchestrator 코드·문서·테스트와 실행 의존성은 유지한다.
- 검증: 공통 환경 pytest 263개 통과, 기존 unittest 172개 통과, dev 모델이 합성 보고서 8개 수용.
- 공통 wheel에서 두 모듈 로딩·스키마 4개 포함·합성 보고서 8개 생성을 확인했다.
- 기존 dev lock의 패키지 버전은 모두 유지했다. 실제 서비스 통합 검증은 별도다.
