# 4번 담당 상세 명세: Evaluation / QA / Security / 비교 실험

| 항목 | 내용 |
|---|---|
| 담당자 | 강석민 |
| 작성일 / 버전 | 2026-09-28 작성 · 2026-10-14 갱신 / 0.4 |
| 상태 | 팀 검수용 설계 초안. 집계·보고서·Security 정적 판정·결과 지표·시연 자료 목록은 구현했고(합성 입력으로 검증), 실제 생성물 실행 검사·최종 Snapshot 채점·비교 실험은 아직이다. 구현·실험 완료 보고서가 아님 |
| 담당 범위 | 독립 평가 시스템(보호 테스트·QA/보안 관점 채점), 비교 실험, 결과 분석·발표 근거 |
| 첫 구현 대상 | 회원가입 MVP 확정, 표시 Key SCN-001. 요구사항별 검사는 [scn001_criteria.md](scn001_criteria.md)가 기준 |
| 평가 위치 | 개발 순환(Planner→Developer→QA/Security Agent→수정) 밖에서 최종 결과물을 채점하는 독립 평가. 0.3에서 정리, 팀 확정 대기(OPEN-11) |

## 1. 목적

생성된 소프트웨어의 요구사항 충족 여부를 실제 실행 결과로 검증한다. 기능 오류와 보안 문제를 구분하고, 실패 발견 → 개발 수정 → 재검증 과정을 추적한다. Single-Agent와 Multi-Agent를 동일 조건에서 비교하여 정확도·시간·비용·수정 과정의 장단점을 분석한다.

Multi-Agent의 우수성을 전제하지 않는다. 동일 조건에서 공정하게 비교하고 결과의 원인을 분석한다.

### 1.1 확정 수준

- **확정 정책:** 회원가입 MVP, 8개 요구사항, 공통 식별·버전 계약, 코드 수정 3회·도구 재시도 2회, 최종 판정 체계.
- **설계 제안:** 이 문서가 제안하는 모듈·식별자·필드·상태·평가 정책. 검수 후 확정.
- **협의 필요:** API·도구·실행 한도 등 다른 담당과 정할 내용. 18절에서 관리.

공통 계약을 구체화하는 평가 전용 필드·테스트 Key·디렉터리는 설계 제안이다. 명시한 확정 정책과 구분한다. 실제 구현이 존재한다는 의미가 아니다. 누락된 요구를 임의로 확정하거나, 결과를 통과시키기 위해 평가 기준을 낮추지 않는다.

## 2. 담당 범위와 협업 경계

| 작업 | 4번 책임 | 연결 담당 및 경계 |
|---|---|---|
| QA 관점 평가 | 기능 요구(REQ-001·002·003·004·007)의 기대 결과·경계값·보호 테스트 | 순환 안의 QA Agent(2번)는 자체 테스트로 개발을 돕는 별도 역할 |
| 보안 관점 평가 | 보안 요구(REQ-003·005·006)의 검사 대상·재현·판정 근거·보호 테스트 | 순환 안의 Security Agent(2번)와 별도 |
| 검증 실행 | 보호 테스트 작성, 결과 정규화·집계, 독립 평가 보고서 생성 | 실행 컨테이너·MCP 도구는 2번, 실행 종료 후 최종 Snapshot 전달은 1번 |
| 최종 판단 | 독립 평가 결과(evaluationVerdict)와 근거 | 순환 안의 최종 상태·재시도·종료 결정(finalVerdict)은 1번. 서로 덮어쓰지 않음 |
| 로그 | 검사 증거 생성·필드 정의·분석 | 전체 이벤트 저장·Task 상태 DB는 1번 |
| 대상 서비스 | API·DB·환경에 맞춘 검사 | 서비스·화면·인증·DB 구현은 3번 |
| 비교 실험 | 조건·반복 계획·공통 평가·집계 | Single/Multi 실행 진입점은 1·2번과 연결 |
| 발표 근거 | 실패·수정·재검증 사례·그래프·한계 | 팀 전체 발표 구성과 연결 |

4번은 A2A 서버, Agent 프롬프트 전체, MCP 서버 전체, 서비스 화면, Orchestrator를 중복 구현하지 않는다. QA·Security Agent는 제품 코드를 직접 수정하지 않고 결함을 반환한다. 테스트·판정 코드의 결함은 4번이 수정하고 기준 변경 이력을 남긴다.

### 2.1 담당 간 계약

| 담당 | 제공받을 정보 | 전달할 정보 |
|---|---|---|
| 1번 | 확인 완료: ID 규칙·Artifact·Manifest·실행 한도, Run 조회 API(`/runs/{id}`·events·issues·tool-attempts·usage·configuration), Run Configuration 필드(dev 45번 인계). 아직 없음: 실행 종료 시 최종 Snapshot 소스를 받는 방법(Artifact API는 메타데이터 JSON만 제공), Single 실행기(`architecture`는 `MULTI_AGENT`만 허용) | 독립 평가 보고서와 실험 지표 |
| 2번 | 확인 완료: QA 보호 케이스 연결 규격(unittest, 읽기 전용 `/inputs/tests`, 6필드 케이스, REQ-001·002·003·004·007만 연결 가능), Security 승인 함수 계약(dev 42번). 아직 없음: 평가 전용 보호 테스트를 컨테이너에서 독립 실행하는 방법과 종료 후 실행 권한 | 보호 테스트 묶음과 필요한 결과 필드 |
| 3번 | 수령: 플랫폼 backend의 API 규격·응답 코드·실행 방법·DB 구조·이메일 검증 위치(2026-10-05, 3번필요사항 중간정리본). 이는 플랫폼 웹사이트 코드이며 생성물 규격으로 자동 채택하지 않는다. 아직 없음: 생성물 시작 코드, 회원가입 서비스 코드·API/DB 규격·의존성, 테스트 DB 초기화·서버 로그 수집 방법 | 케이스·테스트 데이터·실패 재현 절차 |

독립 평가 구조에서는 4번 모듈을 QA/Security Agent 내부에 연결하지 않는다. 따라서 Agent 내부 호출 위치와 A2A Artifact 포장은 4번의 필수 업무가 아니다.

실행기가 실제 시간·종료 코드를 기록하고, 모델 호출 담당이 사용량을 제공해야 한다. 4번이 Agent 설명으로 시간·토큰 수·실행 결과를 추정하지 않는다.

## 3. 구현 범위와 단계

### 3.1 초기 범위

우선순위(2차 기업미팅 2026-10-06, 팀장 카톡): 실제로 동작하는 Pipeline·Agent 협업·Trace가 먼저이고, 평가는 기본 검증을 지원하되 보안을 깊게 파지 않는다. Single/Multi 비교는 후순위이며 필요하면 축소한다(폐기 확정은 아님). 다음 기업 미팅은 테스트를 제외한 시연 영상으로 진행하므로, 4번은 평가 기준·검사 항목 정리, 평가 모듈과 실행 정보 연결 준비, 시연 로그·생성 파일·화면 정리를 먼저 하고 실제 테스트와 비교 실험은 미팅 이후 진행할 수 있게 준비한다.

1. 공통 검증 요청·결과 형식, Build 증거 연결과 판정 규칙.
2. 회원가입 요구사항의 단위·통합 테스트와 대표 화면 흐름.
3. 비밀번호 저장·민감정보 노출 등 적용 가능한 보안 검사.
4. 실패 → 수정 → 같은 새 버전·환경의 Build·QA·보안 재검증 연결.
5. 첫 시나리오의 Single/Multi 공통 평가 및 비교.

### 3.2 확장 및 제외

전자문서와 추리게임은 공통 결과 형식에 시나리오별 검사기를 추가한다. 회원가입 → 전자문서 → 추리게임은 초기 구현 순서 제안이며, 모두 첫 구현에서 완료한다는 의미는 아니다.

로그인·OAuth·이메일 인증·비밀번호 찾기·프로필 관리는 회원가입 MVP에서 제외한다. 대규모 부하 시험, 전체 화면 자동화, 침투 테스트 전체 범위, 별도 관측 플랫폼, 외부 배포는 초기 범위에서 제외한다. 보안 통과는 합의된 검사 범위의 충족이며 모든 취약점의 부재를 보장하지 않는다.

## 4. 검증 구조와 공통 평가

검증 대상은 두 종류다.

- **생성된 서비스:** 가입·문서 처리·게임 진행이 요구사항대로 동작하는지 검사.
- **협업 시스템:** 실제 도구 실행, 증거 전달, 수정 분기, 버전 일치, 중단 규칙 검사.

QA Agent의 요구사항 기반 독립 테스트와 팀의 공통 평가 테스트를 구분한다. Agent 테스트는 개발·수정에 활용하고, 비교 점수는 동일한 공통 평가 테스트로 산정한다. 공통 테스트가 Agent의 독립적인 테스트 생성 요구를 대체하지 않는다.

공통 요구사항·테스트는 버전과 해시로 관리한다. Developer는 평가 기준을 수정할 수 없다. 기준 자체의 오류를 고칠 경우 변경 사유·담당·이전 버전을 남기고 양쪽을 새 기준으로 다시 평가한다. 기존 점수를 덮어쓰지 않는다.

### 4.1 독립 평가 원칙 (0.3)

개발 순환 안의 QA·Security Agent는 결과물을 검사해 Developer에게 피드백하는 참가자다. 4번 평가는 순환이 끝난 뒤 순환 밖에서 최종 결과물을 채점하는 심판이다. 0.2의 "공통 평가 피드백을 양쪽에 동일하게 제공" 제안은 아래 원칙으로 대체한다(팀 확정 대기, OPEN-11).

| 원칙 | 내용 |
|---|---|
| 같은 시험지 | Multi와 Single의 최종 결과물을 같은 보호 테스트 묶음(suite 이름·버전·SHA-256)과 같은 컨테이너 이미지·Manifest에서 채점한다. |
| 모든 종료 상태 채점 | SUCCESS뿐 아니라 FAIL·UNVERIFIED·HUMAN_REVIEW로 끝난 실행의 마지막 Snapshot도 채점한다. 실패한 실행을 빼면 성공률이 부풀려진다. |
| 결과 비공개 | 채점 결과를 Agent에게 피드백하지 않는다. 보호 테스트는 Agent 작업공간에서 보이거나 수정될 수 없어야 한다. |
| 중간 버전 | 수정 전후 비교(Regression Count)를 위해 CODE-1, CODE-2 등을 채점할 수 있으나 기록만 하고 순환에 전달하지 않는다. |
| 판정 분리 | 독립 평가는 evaluationVerdict를 남기고 1번의 finalVerdict를 덮어쓰지 않는다. 두 값을 나란히 저장해 "Orchestrator SUCCESS + 보호 테스트 FAIL"(협업이 놓친 결함)을 센다. |

개발정의서 11-A의 "Single-Agent도 테스트 결과를 받고 수정할 기회를 갖는다"는 순환 안에서 Agent가 만든 테스트와 도구 결과에 대한 규칙이다. 보호 테스트 결과를 공개한다는 뜻으로 해석하지 않는다.

## 5. 입력·출력 인터페이스

### 5.1 식별자와 검증 요청

프로젝트 내부 ID는 UUIDv4이며 표시 Key와 분리한다. REQ-001, SCN-001은 UUID를 대신하지 않는다. Agent가 반환한 A2A Task·Context·Artifact ID는 opaque string으로 그대로 보존한다. 내부 workflow_step_id를 A2A Task ID로 사용하지 않는다.

아래 표는 내부 표현이다. 외부 공통 JSON은 runId, workflowStepId, scenarioId, requirementIds 등 camelCase를 사용하고 어댑터에서 명시적으로 변환한다. 평가 전용 요청은 A2A 공식 객체를 수정하지 않고 별도 payload로 전달한다.

| 필드 | 형식·필수 여부 | 의미 |
|---|---|---|
| schema_version | 문자열, 필수 | 평가 요청/결과 구조 버전 |
| run_id / workflow_step_id | UUIDv4, 필수 | 전체 실행 / 내부 검증 단계 |
| a2a_task_id / agent_context_id | 문자열, A2A 실행 시 필수 | 해당 Agent 서버 반환값 |
| scenario_id / scenario_key | UUIDv4 / 표시 문자열, 필수 | MVP 표시 Key SCN-001 |
| architecture | SINGLE_AGENT / MULTI_AGENT, 필수 | 비교 구조 |
| requirement_ids | UUIDv4 배열, 필수 | 등록된 요구사항. 표시 Key 대응표 별도 |
| test_ids | 등록 검사 ID 배열, 필수 | 표시 test_key와 구분. 생성 주체는 협의 |
| requirements_version / suite_version | 문자열, 필수 | 고정 요구사항·검사 기준 버전 |
| attempt | 0~3 정수, 필수 | 최초 구현 0, 코드 수정 회차 |
| suite | qa / security / pipeline, 필수 | 검사 범위. Build는 별도 필수 보고서 |
| workspace_id / snapshot_id | UUIDv4, 필수 | Registry로 해석하는 실행 대상 |
| execution_manifest_id | UUIDv4, 필수 | 6절 공통 Manifest 참조 |
| limits | 객체, 필수 | 시간·출력·자원 및 전체 실행 예산 |
| previous_report_id | 등록 보고서 ID 또는 null | 재검증 연결 |

빈 검사 목록·미등록 ID·필수 대상 누락·잘못된 UUID·지원하지 않는 스키마·허용 밖 대상은 실행 전 REQUEST_INVALID로 거부한다. 실행 전 거부 기록도 실험 계획과 연결하여 누락을 숨기지 않는다. 임의 쉘 명령·Host 절대경로·외부 URL을 요청에서 실행하지 않는다. 비밀값은 Task Metadata에 넣지 않는다.

### 5.2 MCP 결과 어댑터

| 도구 | 주요 입력 | 평가에 사용하는 결과 |
|---|---|---|
| run_build | workspaceId, snapshotId | exitCode, stdoutRef, stderrRef, durationMs, executionManifestId |
| run_unit_tests | workspaceId, snapshotId, testScope | total/passed/failed/skipped, reportRef, executionManifestId |
| run_browser_tests | workspaceId, snapshotId, testSuite | total/passed/failed, traceRefs, executionManifestId |
| run_security_scan | workspaceId, snapshotId, scannerProfile | findings, reportRef, executionManifestId |

개별 검사 결과는 reportRef/traceRefs에서 test ID·요구사항·기대/실제값·증거를 읽어 정규화한다(Unit 결과의 `testResult.tests[].testId/outcome/details`는 dev 26번 형식. 이 정규화 어댑터 자체는 아직 구현하지 않았다). 합계 숫자만으로 항목별 PASS를 만들지 않는다. skipped는 사전 승인된 적용 제외인지 NOT_RUN인지 판단한다. Manifest·보고서 누락은 검증 미완료다.

MCP protocol error, isError=true인 도구 실행 오류, 정상 도구 실행 후 제품 FAIL을 구분한다. QA assertion 실패나 scanner finding 자체는 도구 장애가 아니다. testScope/testSuite/scannerProfile과 보안 HTTP·DB 재현 실행 경로, 보고서 세부 스키마는 2번과 확정한다.

### 5.3 평가 모듈 구성

2026-10-14 기준 실제 모듈 구성은 다음과 같다. 상세 현황은 15.1절에 있다.

| 모듈 | 역할 | 상태 |
|---|---|---|
| aggregation.py | 검사 계획·결과 대조, 상태별 개수·통과율·실패/미검증 목록 | 구현 |
| manifest.py | Manifest 형식 검사, 기준·관측 일치 시에만 집계 | 구현 |
| independent.py | 독립 평가 보고서(INDEPENDENT_EVALUATION) 생성 | 구현 |
| reporting.py | 1번이 받는 QA_REPORT/SECURITY_REPORT 형식 변환(에이전트 호환용 보조 경로) | 구현 |
| security/ | SCN-001 Security 판정(D1): 기준 JSON + dev 42번 assess() | 구현 |
| run_metrics.py | dev Run 조회 JSON → 지표(사건 시각 차이로 시간, 비용·토큰 미확인은 None), 구조별 요약(분모=전체 계획 Run), 비교 가능 여부 검사(모든 Run 대조) | 구현(합성 입력으로 검증) |
| demo_index.py | 시연 자료 목록·해시(청크 단위), 누락 분류 표시, 알려진 비밀값 거부 | 구현 |
| common.py | 두 모듈이 함께 쓰는 구조 이름·시각 파싱 | 구현 |
| protected_tests/ | 회원가입 보호 테스트 | 예정 |
| 결과 어댑터 | 컨테이너 결과 JSON → CheckResult 정규화 | 2번 형식 수령 후 |
| experiments/ | 실행 조건·반복 계획·그래프(지표 계산은 run_metrics.py가 담당) | 예정 |

최종 finalVerdict 계산은 1번 Orchestrator가 담당한다. 4번은 독립 평가 결과와 판정 규칙 검증용 fixture를 제공한다.

## 6. 실행 환경과 생명주기

### 6.1 Execution Manifest

Build·QA·Security 보고서는 같은 불변 소스와 실행 기준을 가리켜야 한다.

| 외부 필드 | 조건 |
|---|---|
| repositoryId | 저장소 식별 문자열 |
| codeVersion | 1~4 정수. 최초 1, 수정마다 증가 |
| projectArtifactId | Registry의 소스 Artifact UUIDv4 |
| commitHash / treeHash | Git 전체 object ID. 이동하는 branch 이름 사용 금지 |
| gitObjectFormat | sha1 또는 sha256 |
| snapshotSha256 | 정규화된 불변 archive의 SHA-256 |
| containerImageDigest | 실행 이미지 digest |
| dependencyLockHash | 의존성 lock의 해시 |

artifact_version은 계보별 증가 정수이고 commitHash가 아니다. 수정 산출물은 새 artifact_id를 발급하고 previous_artifact_id로 연결한다. snapshotId와 Registry 소스 Artifact의 정확한 관계는 공통 스키마에서 확정한다.

### 6.2 실행 순서

1. 등록된 요구·테스트·코드·환경과 Manifest를 검증한다.
2. archive 실제 해시와 Registry 값을 대조하고 불변 소스를 읽기 전용으로 제공한다.
3. 합성 데이터·독립 DB/출력 영역·일회성 컨테이너를 준비한다.
4. 같은 소스·환경에서 Build를 실행한다. 코드 컴파일 오류는 Build FAIL, 컨테이너 시작 실패는 UNVERIFIED로 구분한다. 실행하지 못한 서비스 검사는 NOT_RUN으로 남긴다.
5. QA·Security를 실행하고 병렬 검사 데이터 충돌을 방지한다.
6. 개별 결과·종료 코드·마스킹된 stdout/stderr·증거·Manifest를 수집한다.
7. 보고서 등록 후 필수 집합·버전·환경·증거를 확인하여 Orchestrator에 전달한다.
8. 모든 종료 경로에서 프로세스·컨테이너·임시 데이터를 정리하고 실패도 기록한다.

생성 코드는 Host에서 직접 Build/Test하지 않는다. 컨테이너 외부 네트워크 기본 차단, Workspace 밖 mount·Docker socket·privileged·비밀정보 주입 금지. 쓰기는 임시 Build/Output에 제한한다. CPU·Memory·PID·timeout 수치는 측정 후 합의한다. 의존성 준비는 별도 단계 또는 허용 Host 정책으로 분리한다.

### 6.3 오류 해석

검사 0개·필수 도구 부재·파싱/증거 수집 실패는 ERROR이며 검증 집계에서는 UNVERIFIED다. 종료 코드 0만으로 검사 PASS를 만들지 않는다. timeout은 우선 원인을 기록하며 제품 무한루프/명시 성능 요구 위반이 확인된 경우에만 제품 FAIL로 분류한다. 재현하지 못한 환경 원인은 UNVERIFIED로 보존한다.

Python 평가 코드와 pytest·HTTP 클라이언트는 구현 제안이다. 제품 언어별 도구 결과를 어댑터로 받는다. 로컬 개발 환경 확인 테스트는 생성 코드의 격리 실행 검증을 대체하지 않는다. mock 예시는 실제 실행 증거와 구분한다.

## 7. 회원가입 QA 상세 기준

MVP scenario_key는 SCN-001이다. 내부 scenario_id와 requirement_id는 Registry UUIDv4를 사용한다. 아래 REQ와 검사 Key는 사람이 읽는 표시값이며, 검사별 입력·기대 응답·증거·판정 조건의 전체 표는 [scn001_criteria.md](scn001_criteria.md)가 기준이다(0.4에서 이 절을 그 표에 맞춰 교체).

| 요구 Key | 요구사항 | 검증 연결 |
|---|---|---|
| REQ-001 | 유효한 이메일·비밀번호로 계정 생성 | QA-SIGNUP-001 |
| REQ-002 | 합의된 이메일 파서의 invalid 입력 거부 | QA-SIGNUP-002 |
| REQ-003 | canonical email 중복 금지, DB UNIQUE 및 동시 요청 보호 | QA-SIGNUP-003, QA-SIGNUP-005, SEC-SIGNUP-003 |
| REQ-004 | 비밀번호 최소 8자 | QA-SIGNUP-004 |
| REQ-005 | 승인된 Password Hash 알고리즘·파라미터 | SEC-SIGNUP-001(정적), SEC-SIGNUP-002(실행) |
| REQ-006 | 비밀번호·저장 해시가 API/A2A/로그/Trace에 미노출 | SEC-SIGNUP-004(정적), SEC-SIGNUP-005(실행) |
| REQ-007 | 실제 가입 결과와 사용자 응답 일치 | QA-SIGNUP-006 |
| REQ-008 | 요구→개발→검증→Issue→수정→재검증 추적 | registry상 검증자가 Orchestrator라 독립 평가 범위 밖. PIPE-016과 D4의 Trace 확인 |

| 검사 Key | 입력·절차 | 기대 결과 |
|---|---|---|
| QA-SIGNUP-001 | 미가입 유효 이메일 + 8자 이상 비밀번호로 가입, DB/응답 확인 | 성공 응답, 사용자 1명 생성 |
| QA-SIGNUP-002 | 형식 오류 이메일(@ 없음, 도메인 없음, 빈 값 등), 요청 전후 사용자 수 비교 | 실패 응답, DB 변화 없음 |
| QA-SIGNUP-003 | 가입한 이메일을 대소문자·앞뒤 공백만 바꿔 재가입 | 실패 응답, 사용자 1명 유지 |
| QA-SIGNUP-004 | 7자 비밀번호 / 8자 비밀번호(시험용으로 실행 때 생성) | 7자 거부·미생성, 8자 성공 |
| QA-SIGNUP-005 | 같은 canonical 이메일 동시 요청(N=5) | 정확히 1명 생성, 나머지 실패 |
| QA-SIGNUP-006 | 성공·형식 오류·중복 요청별 응답과 실제 DB 변화 대조 | 응답 성공/실패가 실제 처리와 일치 |

0.3까지 쓰던 QA-SIGNUP-004/005(7자·8자 분리), 006-API/006-UI, 007(동시 요청) 키는 위 키로 재정의했다. 과거 결과를 새 키의 의미로 재해석하지 않는다. 화면(UI) 검사는 2차 기업미팅의 "다수 화면 테스트는 범위 밖"에 따라 MVP에서 뺐다.

각 검사는 사전조건, fixture 버전, 요청·DB 확인·정리 단계, 기대값, 마스킹된 증거를 가진다. 동시 요청 수·동기화 방식과 API 상태/키·DB 조회 인터페이스는 생성물 규격(시작 코드)이 정해져야 확정한다(OPEN-14). 실행 검사는 연결부(가입 요청·저장 조회·DB 초기화·로그 수집)만 교체할 수 있게 짜고, 검사 로직과 분리한다.

이메일 정책(registry 기준): 앞뒤 공백 제거, domain·local-part lowercase, Gmail dot/plus나 provider별 특수 변환 없음, canonical email UNIQUE. 필수값·빈값·최대 길이·유니코드 길이 해석의 미정 기준은 임의 FAIL 근거로 쓰지 않는다. 비밀번호 보호용 시험 값은 실행 때 생성하고 소스·보고서에 literal로 두지 않는다(dev 19번).

## 8. 보안 상세 기준

### 8.1 보안 검사

범위는 REQ-003·005·006이다. 2차 기업미팅에서 보안을 깊게 파지 않고 기본 검증을 먼저 하기로 했고, 기업 Q&A(Q29~32)의 입력검증·SQL Injection·XSS·인증정보 노출은 "범위 축소이지 삭제는 아님"으로 보고 이번 MVP 범위 밖의 확장 후보로만 둔다.

| 검사 Key | 방식 | 확인 대상·기대 결과 | 요구 |
|---|---|---|---|
| SEC-SIGNUP-001 | 정적 | Argon2id 사용·수치 기준 충족·평문/MD5/SHA 등 저장 없음, 해시 호출 결과만 저장 | REQ-005 |
| SEC-SIGNUP-002 | 실행 | 가입 후 저장값: `$argon2id$` 형식, 수치 충족, 같은 비밀번호 두 계정의 salt 다름, 원문과 다름(값은 기록하지 않음) | REQ-005 |
| SEC-SIGNUP-003 | 정적 | 이메일 UNIQUE 선언 + 정규화(strip·lower). pydantic 설정·`CREATE UNIQUE INDEX`도 인정 | REQ-003 |
| SEC-SIGNUP-004 | 정적 | 응답·로그·예외·검증 오류 응답에 비밀번호·해시 경로 없음 | REQ-006 |
| SEC-SIGNUP-005 | 실행 | 성공·실패 요청 후 응답 본문·서버 로그에 비밀번호 원문·저장 해시 문자열 없음(일치 여부만 기록) | REQ-006 |

정적 검사(001·003·004)는 개발 순환 안 Security 판정(`security/assess.py`, dev 42번 승인 함수)으로 쓰고, 실행 검사(002·005)는 최종 독립 평가용이다. 0.3의 SEC-SIGNUP-004~007(SQL 삽입, 화면 스크립트, UI 우회 직접 API, 소스 비밀정보)은 위와 같이 확장 후보로 내렸다. 그래서 OPEN-12(이 항목들의 요구사항 연결)는 해소됐다.

REQ-006 중 A2A Message/Artifact·Trace 노출은 Orchestrator 데이터이므로 위 표에서 제외하고 D4에서 별도로 확인한다.

해시 세부 권장안은 Argon2id, m=19456 KiB, t=2, p=1, 라이브러리가 생성한 고유 salt와 encoded hash다(registry 값). 수치 규칙(이상/정확히 일치)은 OPEN-01에서 확정하며 지금 기준 JSON의 초안은 "이상"이다. 원문과 저장값이 다르다는 이유만으로 안전 판정하지 않는다.

해시 검사는 격리된 검증 경로에서 수행하고 일반 결과에는 알고리즘·파라미터 충족 여부와 마스킹된 증거만 남긴다. 원문/해시를 증거에 복사하여 노출 검사를 통과했다고 보고하지 않는다.

정적 판정은 이름을 단어 단위로 보고(`passed`, `MIN_PASSWORD_LENGTH`는 비밀번호 값이 아님), 모르는 구조는 UNVERIFIED로 남기며, Bandit 경고는 코드 근거가 있을 때만 CONFIRMED로 한다. 호출 흐름·동시성·런타임 설정은 보지 않으므로 같은 요구를 실행 검사로 한 번 더 본다.

### 8.2 발견·해결·검증 상태

평가 전용 설계안으로 finding_state(SUSPECTED / CONFIRMED / FALSE_POSITIVE), resolution_state(OPEN / RESOLVED), validation_verdict(PASS / FAIL / UNVERIFIED)를 분리한다. 공통 Issue와의 필드 매핑은 협의한다. scanner 경고는 즉시 CONFIRMED가 아니며 재현 또는 명확한 코드 근거로 확정한다. 오탐 판단에는 사유·증거가 필요하다. RESOLVED는 새 버전 독립 재검증 후만 설정한다.

명시적 보안 요구 위반, 확인된 HIGH/CRITICAL, 비밀번호·비밀값 노출은 SUCCESS를 차단한다. MEDIUM은 수정 권고를 기본 기록하되 명시 요구 위반이면 차단한다. 그 외 MEDIUM 차단 여부는 미정이다. LOW/INFO는 기록한다. 필수 범위의 의심 항목을 도구/환경 문제로 판정하지 못하면 UNVERIFIED로 보존한다.

사전 승인된 적용 제외만 NOT_APPLICABLE로 표시한다. 필수 MVP 보안 요구를 임의 제외할 수 없다. 도구 부재·시간 부족은 ERROR/NOT_RUN이다. scanner 0 warnings만으로 Security PASS를 선언하지 않는다.

## 9. 확장 시나리오

### 9.1 전자문서

전자문서 확장 채택 시 scenario_id는 별도 UUIDv4를 등록한다. 아래 F/S는 표시 Key이며 내부 requirement_id와 연결한다. 로그인 기본 프로젝트, 제출자 A·담당자 B·무관한 C, 사전 담당자 목록과 합성 PDF를 전제로 한다.

| 검사 | 요구사항 | 기대 결과 |
|---|---|---|
| 정상 등록 | F-01 | 파일과 제출자·담당자 정보 저장 |
| 누락·복수 파일·비허용 입력 | F-02 | 거부, 정상 제출 건으로 남지 않음 |
| 크기 경계 | F-03 | 유효 PDF 10,485,759 / 10,485,760바이트 허용, 10,485,761바이트 거부 |
| 처리 결과 안내 | F-04 | 실제 처리와 성공·실패 표시 일치 |
| QA-FILE-002: 같은 이름 보존 | F-05 | 내용이 다른 같은 이름 PDF 2개의 제출 건·원본 각각 유지 |
| 허용된 목록 정확성 | F-06 | A·B 목록의 문서·제출 정보 일치 |
| 정상 다운로드 | F-07 | 선택한 원본과 바이트 또는 해시 일치 |
| 미인증 요청 | S-01 | 등록·목록·다운로드 거부 |
| SEC-ACCESS-001: 타인 접근 | S-02 | C에게 A 문서 정보·내용 미노출 |
| 식별자·제출자 조작 | S-03 | 타인 접근·타인 명의 제출 불가 |
| 경로 조작 | S-04 | 저장 영역 밖 읽기·쓰기 불가 |
| PDF 위장 | S-05 | 확장자·Content-Type만 바꾼 비허용 파일 거부 |

문서 보존·정확성은 QA, 비인가 접근·입력 조작 방어는 보안으로 구분한다. 오류 상태 코드뿐 아니라 본문·파일 내용·저장 부작용도 확인한다. PDF 표본과 판별 방식은 먼저 확정하며 형식 검사 통과를 악성코드 부재로 해석하지 않는다. 문서 심사·승인·OCR·전자서명·담당자 변경은 이 시나리오의 구현 범위에서 제외한다.

### 9.2 추리게임

확정 게임 명세의 시작 상태·합법적 행동·승리 조건·전이를 검사로 변환한다. 대표 클리어 경로, 진행 불가 상태, 필수 조건 우회, 잘못된 입력, 재시작·저장 복구(기능이 있을 때)를 검증한다. 검사한 경로와 전체 상태 공간을 구분하여 보고한다.

클리어 가능성은 QA이며 재미·이야기 품질은 별도 정성 평가다. 사용자 입력 출력, 외부 자원, 파일·설정 등 실제 공격 표면에 맞춰 보안 범위를 정한다. SQL·계정 기능이 없으면 비대상 근거를 남긴다. 게임 요구사항 확정 전 이 목록을 완료된 상세 테스트로 간주하지 않는다. 실제 UI와 같은 게임 규칙 로직을 검증하고, 상태 탐색 한도 소진은 통과로 처리하지 않는다. 기획 모순은 HUMAN_REVIEW로 전달한다.

## 10. 협업 시스템 통합 검증

아래 PIPE 값은 제안 test_key이며 등록 test_id와 연결한다.

| Key | 조건 | 기대 결과 |
|---|---|---|
| PIPE-001 | QA PASS, 보안 필수 FAIL | SUCCESS 금지, Issue 전달·수정 분기 |
| PIPE-002 | Build/QA/Security 코드·환경 불일치 | 불일치 기록, 같은 기준 재검증 |
| PIPE-003 | 도구 미실행·0개 검사·결과 누락 | ERROR/NOT_RUN, UNVERIFIED 입력 |
| PIPE-004 | 코드 수정 | 새 Build·QA·Security, 이전 PASS 재사용 금지 |
| PIPE-005 | 코드 수정 3회 소진 | 제품 결함 지속 시 FAIL, 반복 문제는 HUMAN_REVIEW 정책 적용 |
| PIPE-006 | 같은 Issue 2개 수정 Cycle 연속 미해결 | 반복 카운트·HUMAN_REVIEW 후보 정보 전달 |
| PIPE-007 | 요구사항 누락·충돌 | SUCCESS 금지, 충돌은 HUMAN_REVIEW 후 승인된 기준 변경 |
| PIPE-008 | QA·Security 병렬 실행 | 독립 데이터·같은 frozen snapshot |
| PIPE-009 | Developer가 보호 평가 테스트 변경 | 차단 또는 무결성 오류, 비교 무효 |
| PIPE-010 | 외부 배포·계정·개인정보 전송 | 승인 경로 없이 실행 금지 |
| PIPE-011 | 사용자 취소 | ABORTED, finalVerdict null, 증거·종료 이유 보존 |
| PIPE-012 | A2A COMPLETED + 검사 FAIL | Task 완료와 제품 성공 구분 |
| PIPE-013 | Task 실패·필수 업무의 직접 Message 응답 | 검증 미완료 또는 계약 오류, SUCCESS 금지 |
| PIPE-014 | 다른 Agent contextId·자체 만든 Task ID 사용 | 매핑 계약 오류 탐지 |
| PIPE-015 | 입력/권한 오류·제품 실패에 Tool Retry | 금지. 일시 오류만 최대 Retry 2회 |
| PIPE-016 | REQ-008 추적 | 요구·단계·Artifact·Issue·수정·재검증 관계 복원 |
| PIPE-017 | QA/Security 제품 쓰기·경로/symlink 우회 | 서버 권한·읽기 전용 경계에서 차단 |
| PIPE-018 | Host 실행·금지 mount·네트워크·비밀값 노출 | 정책 위반 탐지, 성공 금지 |
| PIPE-019 | archive 해시/Registry 불일치 | 무결성 오류, 검증 결과 재사용 금지 |
| PIPE-020 | 도구 최초 호출+재시도 2회 모두 인프라 실패 | 해당 필수 검증 UNVERIFIED, 종료 정책 확인 |

A2A 요청·Task·Artifact와 MCP 실제 실행 기록을 연결한다. A2A 상태, Workflow 상태, 검증 결과, 최종 Verdict를 각각 보관한다. 프로토콜/SDK 버전·필드 검증은 공통 계약과 설치 lock을 사용하고 평가 담당이 공식 객체를 임의 복제하지 않는다. 실행·권한 구현은 1·2번, 통합 검증은 4번이 협업한다.

## 11. 결과 스키마와 판정

### 11.1 검사 상태와 검증 결과

| 개별 status | 의미 | 검증 집계 |
|---|---|---|
| PASS | 실제 검사·기대값 충족 | PASS 후보 |
| FAIL | 요구 위반 확인 | FAIL 입력 |
| ERROR | 도구·환경·증거 오류 | UNVERIFIED 입력 |
| NOT_RUN | 예정 검사 미실행 | UNVERIFIED 입력 |
| NOT_APPLICABLE | 사전 승인된 비대상, 사유·승인 참조 필수 | 승인 범위만 제외 |

필수 미검증이 있으면 검증 집계는 UNVERIFIED로 두되 확인된 FAIL 목록을 함께 보존하는 평가안이다. 미검증 없이 필수 실패가 있으면 FAIL, 전체 적용 필수가 통과해야 PASS다. 선택 검사는 별도 summary로 표시한다. 적용 대상 0개는 100%가 아닌 null이며 필수 MVP 검사를 전부 제외할 수 없다. Manifest가 없거나 다르면 아무것도 측정하지 못한 것이므로 통과율도 0%가 아닌 null이고, 검사별 errorCode는 MANIFEST_MISSING/MANIFEST_MISMATCH로 남긴다.

### 11.2 보고서 형식 (0.3, 구현 기준)

0.2의 제안 필드표는 실제 구현과 달라 아래 두 형식으로 대체한다. 기본은 독립 평가 보고서이고, 에이전트 호환 보고서는 보조 경로다.

**(1) 독립 평가 보고서 — `independent.build_evaluation_report()`, reportType `INDEPENDENT_EVALUATION`**

| 영역 | 필드 | 설명 |
|---|---|---|
| 식별 | schemaVersion, reportType, evaluationId, sourceRunId, architecture, createdAt | evaluationId(채점 실행)와 sourceRunId(대상 개발 실행)는 서로 다른 UUIDv4. A2A ID는 쓰지 않는다. createdAt은 시간대 포함 ISO 시각 |
| 대상 실행 | sourceRun(workflowStatus, finalVerdict, codeVersion, fixAttempt, startedAt, finishedAt, durationMs, missedDefect) 또는 null | 1번 Orchestrator의 실행 기록(GET /api/v1/runs/{id})을 채점 결과와 나란히 기록. 채점한 코드 버전이 실행의 최종 코드 버전과 같아야 함. missedDefect는 Orchestrator SUCCESS + 보호 테스트 필수 검사 FAIL 확인(다른 검사가 미검증이라 평가 판정이 UNVERIFIED여도 해당). FINISHED·ABORTED만 종료로 보아 finishedAt·durationMs를 기록하고, HUMAN_REVIEW는 재개될 수 있어 null |
| 시험지 | suite.id, suite.version, suite.sha256 | 이름이 같아도 내용이 바뀌었을 수 있으므로 파일 묶음 해시로 같은 시험지임을 증명 |
| 대상 | expectedManifest, observedManifest, manifestCheck | 불일치·누락이면 결과를 쓰지 않고 UNVERIFIED |
| 전체 요약 | evaluationVerdict, summary(required/optional 상태 수), confirmedFailureIds, unverifiedTestIds, optionalConfirmedFailureIds, optionalUnverifiedTestIds, requiredCheckPassRate, requiredRequirementPassRate | evaluationVerdict는 finalVerdict가 아님. 실패·미검증 ID는 필수와 선택(optional…)을 나눠 기록. 검사 통과율과 요구사항 통과율을 따로 계산 |
| 요구사항별 | requirements[].requirementId, checkIds, validationVerdict, summary, confirmedFailureIds, unverifiedTestIds, optionalConfirmedFailureIds, optionalUnverifiedTestIds, requiredPassRate | 한 요구사항에 검사를 여러 개 연결 가능 |
| 관점별 | perspectives.QA, perspectives.SECURITY | 검사가 없는 관점은 null(통과로 표시하지 않음) |
| 검사별 | checks[].testId, required, status, evidenceIds, errorCode, expectedResult, actualResult, reason, normalizedLocation, exclusionApprovalRef, exclusionReason, requirementId, perspective, title | status는 PASS/FAIL/ERROR/NOT_RUN/NOT_APPLICABLE 원래 값 그대로 |

**(2) 에이전트 호환 보고서 — `reporting.build_report()`, dev(`schemas/project`)의 QA_REPORT/SECURITY_REPORT**

| 영역 | 필드 |
|---|---|
| 공통 | artifactId, artifactType, artifactVersion, previousArtifactId, runId, workflowStepId, a2aTaskId, a2aArtifactId, createdBy, createdAt, requirementIds, codeVersion, executionManifest, artifactUri(선택) |
| QA | tests[].testId, requirementId, outcome(PASS/FAIL/UNVERIFIED), title, details, expectedResult, actualResult, normalizedLocation, toolEvidence |
| SECURITY | requirementResults[].requirementId, outcome, details, expectedResult, actualResult, normalizedLocation, toolEvidence / findings[].findingId, severity, disposition, title, description, requirementId, evidenceRef, ruleId, normalizedLocation |

ERROR·NOT_RUN은 outcome UNVERIFIED로 바뀌고 원래 상태·오류 코드·사유·증거 ID는 details(JSON 문자열)에 보존한다. SECURITY는 요구사항당 결과 1개만 허용하며 NOT_APPLICABLE은 표현할 방법이 없어 거부한다. 실제 A2A Task·Artifact ID가 있어야 만들 수 있다.

두 형식 모두 finalVerdict를 만들지 않는다. 내부 기계 ID는 UUIDv4이며, 등록 여부 확인·Registry 저장 경로·보고서 ID 발급 주체는 OPEN-03에서 확정한다. 0.2 제안표의 실행 시간(startedAt, durationMs), 도구 버전, 증거 저장 정보(경로·sha256·producer)는 아직 구현하지 않았으며 결과 어댑터 구현 시 추가를 검토한다.

### 11.3 합성 결과 예시 (독립 평가 보고서)

아래는 `build_evaluation_report()`로 실제 생성한 출력에서 반복되는 부분을 줄인 예시다. 모든 ID와 해시는 가짜이며 실제 서비스 결과가 아니다. 요구사항 2개(QA 1, SECURITY 1) 중 로그 비밀번호 노출 검사가 FAIL인 경우다. 생략한 `requirements[1]`, `perspectives`, 첫 번째 검사 행도 같은 구조다.

```json
{
  "schemaVersion": "1.0",
  "reportType": "INDEPENDENT_EVALUATION",
  "evaluationId": "00000000-0000-4000-8000-000000000002",
  "sourceRunId": "00000000-0000-4000-8000-000000000003",
  "architecture": "MULTI_AGENT",
  "createdAt": "2026-10-06T12:00:00+09:00",
  "suite": {"id": "membership-protected", "version": "0.1", "sha256": "ffff…(64자리)"},
  "expectedManifest": {"repositoryId": "synthetic-repository", "codeVersion": 2, "…": "9개 필드"},
  "observedManifest": {"repositoryId": "synthetic-repository", "codeVersion": 2, "…": "9개 필드"},
  "evaluationVerdict": "FAIL",
  "manifestCheck": {"errorCode": null, "mismatchedFields": [], "metadataMatches": true},
  "summary": {
    "required": {"pass": 1, "fail": 1, "error": 0, "notRun": 0, "notApplicable": 0},
    "optional": {"pass": 0, "fail": 0, "error": 0, "notRun": 0, "notApplicable": 0}
  },
  "confirmedFailureIds": ["00000000-0000-4000-8000-000000000015"],
  "unverifiedTestIds": [],
  "optionalConfirmedFailureIds": [],
  "optionalUnverifiedTestIds": [],
  "requiredCheckPassRate": 0.5,
  "requiredRequirementPassRate": 0.5,
  "requirements": [
    {"requirementId": "00000000-0000-4000-8000-00000000000a",
     "checkIds": ["00000000-0000-4000-8000-000000000014"],
     "validationVerdict": "PASS", "requiredPassRate": 1.0,
     "confirmedFailureIds": [], "unverifiedTestIds": [], "summary": {"…": "위와 같은 구조"}}
  ],
  "perspectives": {
    "QA": {"validationVerdict": "PASS", "requiredPassRate": 1.0, "…": "요구사항별 요약과 같은 구조"},
    "SECURITY": {"validationVerdict": "FAIL", "requiredPassRate": 0.0, "…": "요구사항별 요약과 같은 구조"}
  },
  "checks": [
    {"testId": "00000000-0000-4000-8000-000000000015", "required": true, "status": "FAIL",
     "evidenceIds": ["00000000-0000-4000-8000-000000000079"], "errorCode": null,
     "expectedResult": "서버 로그에 합성 비밀번호 없음",
     "actualResult": "요청 본문 로그에서 [REDACTED] 발견",
     "reason": "요청 본문 전체를 로그로 출력",
     "normalizedLocation": "server.log:request-body",
     "exclusionApprovalRef": null, "exclusionReason": null,
     "requirementId": "00000000-0000-4000-8000-00000000000b",
     "perspective": "SECURITY", "title": "로그 비밀번호 노출"}
  ]
}
```

### 11.4 최종 판정

Orchestrator가 필수 Build·Requirement·QA·Security·Manifest·Trace와 수정/재시도 횟수로 판정한다.

| finalVerdict | 조건 |
|---|---|
| SUCCESS | 같은 snapshot·환경, Build PASS, 필수 8개 요구 충족, 필수 QA·보안 PASS, 차단 결함 0, 필수 미검증 0, 필수 Trace 확보 |
| FAIL | 확인 가능한 필수 제품 결함이 지속되고 수정 3회 소진 또는 복구 불가능 결함 확정 |
| UNVERIFIED | 필수 검증을 도구/인프라 문제로 완료하지 못하고 허용 Tool Retry 소진 |
| HUMAN_REVIEW | 요구 충돌·정책 승인·반복 문제·판단 신뢰 부족으로 사람에게 인계 |

FIX_REQUIRED는 진행 상태이며 즉시 최종 FAIL을 뜻하지 않는다. QA FAIL은 수정 가능하면 FIX_REQUIRED→FIXING→REVALIDATING으로 전달한다. A2A TASK_STATE_COMPLETED는 제품 성공과 무관하다. 사용자 취소는 workflowState=ABORTED, finalVerdict=null, terminationReason 기록이다.

READY/NEEDS_REPAIR/INCOMPLETE를 공통 최종 enum으로 사용하지 않는다. 확인된 제품 FAIL과 필수 UNVERIFIED가 동시에 있고 한도도 소진된 경우 최종 우선순위, HUMAN_REVIEW의 이관/재개 표현, 공통 JSON verdict/finalVerdict 이름은 OPEN-04에서 확정한다. 평가 보고서는 두 근거를 모두 보존하고 SUCCESS를 권고하지 않는다. SUCCESS도 외부 배포 승인을 의미하지 않는다.

## 12. 실패 전달과 재검증

제품 결함은 요구 UUID·test/Issue ID·기대/실제값·재현·Manifest·증거를 포함하여 1번이 Developer에 전달한다. 기획 충돌은 HUMAN_REVIEW로 보내고 승인 후 Planner가 요구를 수정한다. 평가 코드 오류는 4번, 실행 환경 오류는 실행 담당이 처리한다. QA/Security는 제품 코드를 수정하지 않는다.

### 12.1 회차

| 항목 | 규칙 |
|---|---|
| 최초 구현 | attempt=0, codeVersion=1 |
| 수정 | 최대 3회, attempt=1~3, codeVersion=2~4 |
| MCP Retry | 동일 논리 호출에서 최대 2회, 최초 포함 총 3회 |
| 제품 검사 FAIL | Tool Retry 대상 아님, 수정 Cycle로 연결 |

인프라 재시도는 수정 횟수와 별도로 집계하며 총시간·비용에 포함한다. Resource busy·일시적 startup 실패 등만 재시도한다. timeout·transport 단절은 부작용/적용 여부 확인 후 판단한다. 입력 schema 오류·권한 거부·path traversal·미지원 도구·컴파일 오류·QA assertion·보안 결함은 동일 호출 자동 재시도 대상이 아니다. write/patch 결과가 불명확하면 파일 해시부터 확인한다.

같은 Issue fingerprint가 수정 후 재발하면 반복 수를 올린다. 기본 fingerprint 입력은 requirement_id + test_id + category + normalized_location이며 정규화·직렬화·원인 구분은 공통 Issue 계약으로 고정한다. 수정 2개 Cycle 연속 미해결은 HUMAN_REVIEW 후보로 전달하며 자동 인계 임계 정책은 협의한다. 회귀 재발은 기존 fingerprint와 연결한 새 이벤트로 남긴다.

### 12.2 재검증과 인계

새 코드에는 새 Artifact·Manifest와 Build·QA·Security 보고서가 필요하다. 최종 SUCCESS에는 전체 적용 필수 집합이 같은 새 기준에서 검증되어야 한다. 실패 항목만 통과하고 나머지 과거 PASS를 재사용하지 않는다.

기준 자체를 고칠 때 요구·suite 버전을 올리고 사유·승인·이전 결과를 남긴다. 기획 재검토나 환경 재시작으로 전체 예산을 초기화하지 않는 평가 정책을 적용하고 공통 상태 계약에서 확인한다. 한도 소진·판단 보류 시 미해결 요구/Issue, 확인된 FAIL, 미검증 목록, 코드·환경, 실행 증거, 중단 이유를 인계한다.

## 13. Single/Multi 비교 실험

### 13.1 통제 조건

| 조건 | 통제 방식 |
|---|---|
| 모델 | 같은 provider·model ID/revision·temperature·지원 시 seed, 값 기록 |
| 입력 | 같은 사용자 요구·완료 기준 |
| 초기 상태 | 같은 시작 commit/snapshot·데이터·도구 capability 전체 집합·컨테이너 digest·dependency lock·하드웨어 |
| 수정 기회 | 양쪽 모두 검사·수정 가능, 코드 수정 3회·Tool Retry 2회 및 동일 총시간 예산·피드백 정책 |
| 평가 | 고정 공통 테스트·보안 기준·종료 규칙 |
| 프롬프트 | 역할 차이는 허용하되 요구 정보와 책임 동등 |
| 사용량 | Multi의 모든 Agent·재시도 합계 |
| 실행 순서 | 교대안은 팀 승인 후 적용, 캐시·모델 준비 상태 기록 |

동일 조건을 반복하고 모델 응답의 변동성을 인정한다. 최초 제안은 채택된 시나리오별 구조당 5회이며 파일럿 비용을 보고 확정한다. 회원가입은 8개 필수 REQ와 검사 연결을 보존한다. 실행 순서 교대는 승인할 제안이며 매번 fresh workspace를 사용한다. Run Configuration은 시작 시 불변 Artifact로 등록하고 report와 연결한다.

Run Configuration과 측정 기록은 다음 항목을 가진다. 외부 공통 필드 이름에 맞춘 평가 연결안이며 enum·저장 스키마는 1번과 확정한다.

| 기록 | 필수 내용 |
|---|---|
| 계획 | experimentId, 계획 Run 식별, scenarioId, architecture, 반복 번호, 실행 순서 |
| 시작 기준 | startingCommitHash, startingSnapshotSha256, requirementsVersion, suiteVersion, 보호 테스트 해시 |
| 모델·도구 | provider, modelId, modelRevision 또는 미제공 사유, temperature, 지원 시 seed, tool capability 목록, scanner version/rules |
| 실행 환경 | containerImageDigest, dependencyLockHash, hardwareProfile, warm-up/cache 상태, 네트워크·비밀정보 정책 버전 |
| 한도 | maxFixAttempts=3, maxToolRetries=2, 총시간·자원·비용 한도와 피드백 공개 정책 |
| 측정 | runId, 관련 보고서/Trace 참조, workflowState, finalVerdict, 종료 이유, 시작/종료/Verdict 시각, human_wait_ms, 호출·토큰·비용·수정·재시도 수 |

계획 기록은 실행 전 고정하고, 실행 결과는 그 계획을 참조하는 별도 기록으로 추가한다. 실패 실행을 새 성공 실행으로 덮어쓰지 않는다. 제품 성공과 협업 절차 준수는 별도 보조 분석이 가능하지만 공통 최종 Verdict를 대체하지 않는다.

`run_metrics.py`가 위 측정 항목 중 dev가 제공하는 값(상태·판정·수정 횟수·도구 재시도·사건 시각·사용량·비용)을 지표로 바꾸고, `comparison_validity()`가 Run Configuration의 통제 항목과 평가 보고서의 suite 해시·환경 digest를 모든 Run에 대해 대조한다(모델·한도·시작 코드·보호 테스트·suite 해시 불일치는 비교 무효, 환경 digest 불일치는 해당 Run 재실행). 현재 dev는 `architecture`로 `MULTI_AGENT`만 받으므로 Single 실행기는 별도로 필요하다. 기준선 설계는 [single_baseline.md](single_baseline.md)에 있다.

### 13.2 실험 종류

- 전체 개발: 같은 요구로 기획·생성·검증·수정 전 과정 실행.
- 오류 복구: 같은 결함·코드 버전을 제공해 발견·수정 비교.

experiment_type과 defect_origin(natural / injected)을 기록한다. 원본과 주입본을 각각 보존하고 탐지 전 주입 정답을 Agent에게 누설하지 않는다. 수정 단계 피드백은 양쪽에 동등하게 제공한다. 주입 결함과 자연 발생 결함을 섞지 않는다. 알려진 결함 수가 없는 자연 발생 실험에서 결함 발견 재현율을 임의 계산하지 않는다.

### 13.3 지표

| 지표 | 계산·기록 규칙 |
|---|---|
| 요구사항 충족률 | 연결 필수 기준을 모두 충족한 REQ 수 / 사전 확정 적용 REQ 수 |
| 공통 테스트 통과율 | PASS 수 / 사전 확정 적용 검사 수. ERROR·NOT_RUN도 분모 포함 |
| 전체 성공률 | SUCCESS Run 수 / 전체 계획 Run 수 |
| Verified Rate | (SUCCESS + FAIL) / 전체 Run. 계획 분모와의 관계는 OPEN-07에서 확정 |
| Tool Retry | 인프라 재시도 횟수, 코드 수정과 별도 |
| LLM Calls | 모든 Agent·재시도의 모델 호출 수 |
| Coordination Overhead | Agent 전달·대기 시간, 중첩 구간 처리 규칙 고정 |
| Regression Count | 수정 전 PASS였던 같은 공통 테스트가 새 버전에서 FAIL인 수 |
| 보안 결과 | 확인된 고유 결함·해결·미해결·미확인 경고 각각 |
| 오류 발견 수 | 고유 fingerprint 기준. 같은 결함 재보고는 중복 제외 |
| 수정 횟수 | 최초 생성 제외, 수정 코드 제출 회수. 빌드·QA·보안 원인 구분 |
| 첫 실행 가능 시간 | RUN_STARTED부터 첫 Build PASS까지. 실제 서비스 준비 시간은 별도 |
| 최종 판정 시간 | RUN_STARTED부터 VERDICT_CREATED까지, 성공·실패 Verdict 모두 |
| 성공 검증 완료 시간 | SUCCESS 실행의 최종 필수 검증까지, 실패는 null |
| 총 소요시간 | 실패·취소·시간초과 포함 시작부터 종료까지 |
| 사용량·비용 | 입력·출력·캐시 토큰, 단가·기준일·통화, 전체 호출 비용 |

요구사항에 테스트가 여러 개여도 충족률에서는 한 번만 센다. 증거 없는 요구는 충족으로 세지 않는다. N/A는 사전 승인한 적용성 목록에 따라 양쪽에서 동일하게 제외한다. 분모 0이면 null이다.

결함 중복은 시나리오·요구사항·원인·위치·재현 증상으로 판단하고 규칙을 기록한다. 줄 번호 변화만으로 새 결함으로 세지 않는다. 발견 수가 많다고 품질이 우수하다고 해석하지 않고 생성 결함·해결률·미해결 상태를 함께 본다.

전체 경과시간과 Agent 누적 작업시간을 구분한다. 병렬 QA 10초·보안 10초를 전체 20초로 합산하지 않는다. 사람 대기는 human_wait_ms로, 모델 로딩·큐 대기·warm-up/cache 상태도 별도 기록한다. 초기안은 전체 시간에 포함하고 자동 처리시간을 보조 지표로 제공한다.

ABORTED에는 Verdict 시간이 없으므로 null과 종료까지의 시간을 별도 기록한다. 계획·수락·시작·종료 Run 수를 각각 남기고 미시작·취소를 삭제하지 않는다. 성공 건의 시간 중앙값·범위와 성공률·시간초과 수·전체 시간을 함께 보고한다. 전체 계획 Run을 성공률 분모로 유지하고 미시작·취소·환경 실패 유형을 표시한다. 재실험 시 이전 run을 삭제하지 않는다. 제외 분석을 추가하면 원래 전체 집계와 사유도 제공한다.

비용 누락은 0원이 아닌 unknown이다. 로컬 모델의 API 비용과 장비·실행시간 비용을 구분한다. 실제 정산과 실험 추정 비용도 구분한다. 토큰 단가 등 외부 값은 실험 시점에 확인하여 저장한다.

## 14. 로그·증거·안전 경계

1번의 전체 로그에 run → workflow_step → a2a_task → requirement → code/Artifact → test/Issue → fix → 재검증을 연결한다. 4번은 필요한 이벤트·증거·분석 결과를 제공한다. 공통 eventId/runId/workflowStepId/a2aTaskId/actor/attempt, input/outputArtifactIds, issueId, codeVersion, snapshotSha256을 연결한다. Issue에는 detected_by, suspected_cause, cause_status, fixed_by, fix_workflow_step_id, previous/new_code_version, revalidation_result를 제공한다. 원인 추정과 확인을 구분한다.

증거는 run/attempt/suite별로 분리하며 덮어쓰지 않는다. 비밀번호·저장 해시·키·토큰·개인정보는 저장 전 공통 Redaction Layer로 제거한다. Authorization은 scheme만 보존하고 .env 내용·전체 소스는 Trace에 넣지 않는다. stdout/stderr도 마스킹 후 별도 Artifact로 등록한다. 초기 실험은 합성 데이터만 사용한다. 원시 증거의 공유 위치·접근 권한·보존 기간은 18절에서 확정한다. Git에서 원시 결과를 제외하더라도 검수자가 확인할 보고서·증거 참조를 제공한다.

허용 경로·명령·네트워크 대상으로 제한하고 생성 코드·검사를 격리한다. 임의 외부 사이트를 시험 대상으로 삼지 않는다. 외부 배포·실제 개인정보 전송·외부 계정 접근은 승인 경로를 사용하고 검사 PASS로 대체하지 않는다. 관련 제어 구현은 1·2번, 확인 기준은 4번과 분담한다.

## 15. 산출물과 디렉터리

```text
docs/evaluation/evaluation_spec.md        # 이 명세
docs/evaluation/development_progress.md   # 구현 현황·검토 결과·다음 작업
docs/evaluation/environment_setup.md      # 설치·실행 안내
docs/evaluation/scn001_criteria.md        # SCN-001 요구사항별 검사 기준표(D0)
docs/evaluation/share_security_criteria.md  # 팀장 논의용 보안 판정 기준 한 장
docs/evaluation/single_baseline.md        # Single/Multi 비교 기준선(D6)
docs/evaluation/demo_materials.md         # 시연 자료 정리 방법
src/evaluation/                           # 평가 모듈 (15.1)
src/evaluation/contracts/                 # dev 공통 스키마 사본 + 독립 평가 보고서 스키마
src/evaluation/security/                  # SCN-001 Security 정적 판정(D1)
src/evaluation/protected_tests/membership/  # 회원가입 보호 테스트·fixture (예정)
src/evaluation/protected_tests/document/    # 확장 시 추가
src/evaluation/protected_tests/mystery_game/  # 확장 시 추가
tests/evaluation/                         # 평가 모듈 자체 테스트(signup_examples.py = 정상·결함 예제, fixtures/ = 사람이 만든 회원가입 backend 표본)
experiments/                              # 실험 설정·집계 절차 (예정)
```

보호 테스트는 src/evaluation/protected_tests 아래에 둔다. 단, 실행 시에는 Agent 작업공간에서 보이거나 수정될 수 없는 위치에 넣어야 하며 그 방법은 2번과 정한다(OPEN-06). single_agent/multi_agent 실행 연결과 schemas/project·Registry는 담당 구현을 재사용한다. 가상환경·개인 IDE 설정·비밀정보·대용량 원시 증거는 커밋하지 않는다.

최종 제출물은 명세, 요구사항-검사 표, 실제 검사 코드·fixture, 실행·초기화 안내, 스키마, 실제 보고서·증거, 수정 이력, 실험 설정·원본 측정값·집계 코드, 그래프·발표 사례다. 피드백 ID→변경 요구→반영 버전/커밋→재검증 결과의 이력도 남긴다.

### 15.1 구현 현황 (2026-10-16)

이 현황은 확정 정책이나 협의 상태를 바꾸지 않는다. 실제 서비스 검증 완료를 뜻하지 않으며, 모든 예시 출력과 시험은 합성 데이터다.

| 파일 | 구현 범위 |
|---|---|
| src/evaluation/aggregation.py | 검사 계획과 결과 대조, 누락·중복·증거 없는 판정·승인 없는 제외 처리, 필수·선택 분리(판정·통과율·실패/미검증 ID 목록 모두), 진단 정보 보존, 알려진 민감값 치환 |
| src/evaluation/manifest.py | Manifest 형식 검사(UUID·해시 길이·sha256 접두사), 기준·관측 불일치 시 결과 미사용·UNVERIFIED·통과율 null |
| src/evaluation/independent.py | 독립 평가 보고서. 평가 ID·대상 Run 분리, suite 해시, 요구사항 범위 검사, 요구사항별·관점별 요약, 검사·요구사항 통과율 분리, 대상 Run 기록(sourceRun), 스키마 검증 |
| src/evaluation/reporting.py | 에이전트 호환 QA_REPORT/SECURITY_REPORT 변환, ID 대응표·도구 실행 근거·재시도 규칙·dev 스키마 검사 |
| src/evaluation/security/ | SCN-001 Security 정적 판정(D1): 기준 JSON + dev 42번 `assess()`. REQ-003·005·006, Bandit 지원 규칙 판정, dev 승인 로더 통과 |
| src/evaluation/run_metrics.py | dev Run 조회 JSON → 지표(수정 요청과 재검증 PASS로 확인된 해결을 구분), 구조별 요약(평가 보고서의 코드 버전이 Run의 최종 코드 버전과 같을 때만 수용), 비교 가능 여부 검사(평가 보고서의 환경 지문을 그 Run의 동결 환경과도 대조, warm-up 차이 경고), 페이지 조회 도우미 |
| src/evaluation/demo_index.py | 시연 자료(로그·생성 파일·화면·보고서) 목록과 파일별 SHA-256(일반 파일만 받고, 파이프·소켓 등 특수 파일은 열기 전에 거부) |
| src/evaluation/common.py | 구조 이름·시각 파싱 공통 도우미 |
| src/evaluation/__main__.py, report_demo.py | 집계·보고서 합성 예시 |
| tests/evaluation/ | 평가 모듈 자체 테스트(494개, 현황은 development_progress.md). 정상 1 + 결함 18 + 정상 변형 9 예제로 Security 판정 확인 |
| checks/test_setup.py | Python 버전·가상환경·HTTP 라이브러리 확인 2개. 기본 pytest 대상이 아니며 `pytest checks`로 따로 실행 |
| scripts/check_dev_reports.py | dev의 실제 보고서 모델이 합성 보고서 8개를 받는지 확인 |

평가 코드와 테스트에는 파일 맨 위의 역할·용어 설명과 함수·처리 단계별로 초보자도 읽을 수 있는 한국어 주석을 달았다(2026-10-14, 주석만 추가했고 코드 구조(AST)는 같음을 확인했다).

아직 하지 않은 것: 실제 생성물을 실행하는 보호 테스트(D2), 컨테이너 결과 어댑터, 최종 Snapshot 인계와 독립 채점기(D5), 실행 종료 후 자동 채점 연결, 평가 기록 저장, Registry·증거 진위 확인, 수정 전후 Regression 계산, Single 실행기와 실험 코드.

### 15.2 코드 검토에서 확인한 제약과 처리

| 구분 | 내용 | 조치 |
|---|---|---|
| 결정 필요 | 에이전트 호환 경로에서 도구는 정상 종료(PASS)했지만 결과 파싱에 실패해 UNVERIFIED가 된 검사는 "도구·판정 불일치" 오류로 보고서 생성이 멈춘다. dev 계약과 같은 규칙이다. 독립 평가 경로에는 해당하지 않는다. | 처리 방식 결정(OPEN-13) |
| 해소 | 0.3의 SEC-SIGNUP-004~007에 연결할 요구사항이 없던 문제 | 8.1절에서 MVP 범위를 REQ-003·005·006 검사로 줄이고 나머지는 확장 후보로 내려 해소(OPEN-12) |
| 수정 완료 | confirmedFailureIds·unverifiedTestIds에 선택 검사가 섞이던 문제 | 2026-10-06 필수만 담고 선택은 optional… 필드로 분리 |
| 수정 완료 | 가상환경 확인 테스트가 기본 pytest 대상이라 컨테이너에서 실패하던 문제 | 2026-10-06 `pytest.ini` testpaths를 tests로 변경 |
| 수정 완료 | Manifest 불일치 시 통과율이 0.0으로 나오던 문제 | 2026-10-14 null로 바꾸고 검사별 errorCode를 MANIFEST_MISSING/MISMATCH로 기록 |
| 수정 완료 | D1 오탐(정상 코드 FAIL)·미탐(결함 PASS) | 2026-10-14 이름 단어 단위 판정, 검증 오류 처리기·정규화·UNIQUE·로그/응답 노출 인식 확대. Bandit CONFIRMED는 코드 근거가 있을 때만 |
| 수정 완료 | missedDefect가 평가 판정 UNVERIFIED일 때 확인된 FAIL을 놓치던 문제, HUMAN_REVIEW를 종료로 처리하던 문제 | 2026-10-14 반영 |
| 수정 완료 | comparison_validity가 구조당 보고서 하나만 비교해 입력 순서에 결과가 달라지던 문제 | 2026-10-14 모든 Run 대조 |
| 수정 완료 | 외부 점검(2026-10-14)에서 찾은 "결함인데 PASS" 4건: 해시 변수에 원문을 다시 대입하거나 `.hash()` 이름만 같은 메서드, 다른 키에 담은 입력값(`error["input"]`), 복합 UNIQUE, 하드코딩 자격증명의 오탐 해제 | 2026-10-14 승인한 Argon2 도구와 연결된 `.hash()`만 인정, 변수의 모든 대입을 확인, 값의 출처 검사, 이메일 단독 UNIQUE만 인정, Bandit은 변수 이름을 먼저 판단. 재현 사례를 회귀 시험으로 고정 |
| 수정 완료 | 비교 유효성·지표 검사 허점 3건: 구조 이름표·평가 보고서의 Run 번호를 대조하지 않음/필수 조건이 비어도 valid, 동결 구조를 인자로 덮어씀, 계획에 없는 구조의 Run을 요약에서 조용히 버림 | 2026-10-14 모두 거부하거나 위반으로 표시 |
| 수정 완료 | 외부 점검(2026-10-15)에서 찾은 "결함인데 PASS" 4건: ① 함수 매개변수가 전역 해시 도구 이름을 가려도 승인 도구로 오인 ② `PasswordHasher(**{...})`처럼 펼친 약한 설정을 라이브러리 기본값으로 대체 ③ `UniqueConstraint("email", tenant_id)`처럼 글자와 칸 객체가 섞인 복합 UNIQUE ④ `if` 안에서만 `input`을 지우는 반복문을 전체 정리로 인정 | 2026-10-15 이름이 묶이는 모든 곳(대입·매개변수·for·with·import as·정의·덮어쓰기)이 승인 형태일 때만 승인 도구로 인정, 풀어 쓴 인자는 확인 불가(UNVERIFIED), 제약·인덱스의 모든 칸 인자를 세고 읽을 수 없으면 UNVERIFIED, `input` 삭제는 "함수 본문 바로 아래의 반복문 + 삭제문만 있는 본문 + 반복이 끝난 뒤의 사용"일 때만 인정. 같은 종류의 변형을 회귀 시험으로 고정 |
| 수정 완료 | 위 점검의 연장으로 직접 찾아 막은 구멍: ① 해시 함수·검증 오류 처리기의 **함수 이름**을 람다·데코레이터·재대입·같은 이름의 매개변수로 바꿔치기 ② 검증 오류 처리기를 람다·`exception_handlers`·FastAPI 생성자로 등록해 분석을 건너뛰기 ③ 예외 객체(`exc`) 자체(`str(exc)`·`exc.args`·로그)를 쓰는 응답 ④ 비밀번호가 변수·하위 클래스·별칭·요청 본문·해시 변수를 거쳐 로그·응답으로 새기 | 2026-10-15 함수 이름은 정의 외의 묶임(대입·매개변수·with·두 번 import)이나 데코레이터가 있으면 믿지 않음, 등록한 처리기를 소스에서 찾지 못하면 UNVERIFIED, `exc`는 `errors()` 호출 외의 사용을 FAIL, 한 함수 안에서 값을 옮겨 담은 변수를 따라감(비교·논리식·모르는 함수의 결과는 비밀번호 글자가 아니라고 봄). 회귀 시험 63개(정상 코드 오탐 방지 8개 포함) |
| 수정 완료 | 외부 점검(2026-10-16, 5차) 2건: ① `password = payload.email`처럼 이름만 password인 변수에 이메일을 담아 해시해도 통과 ② 비밀번호와 이메일을 받아 이메일만 돌려주는 함수의 결과를 해시해도 통과 | 2026-10-16 변수 이름과 넘긴 인자 이름 대신 해시하는 값의 실제 출처((입력 이름, 필드 경로))를 따라가고, 함수는 실제로 돌려주는 값의 출처를 호출한 쪽 값에 이어 붙임. 같은 종류로 입력 필드를 덮어쓴 뒤 해시(필드 대입·setattr·별칭·vars().update·도우미 함수 안의 대입)와 `self` 필드, 튜플 교환도 막음. 회귀 시험 35개 |
| 수정 완료 | 외부 점검(2026-10-16, 4차) 11건: ① 입력을 버리는 함수(`discard(password)`)·`password *= 0`·잘라내기를 거친 값을 입력의 해시로 인정 ② 이름만 같은 가짜 `os.urandom`을 믿음 ③ 비밀번호 대신 이메일을 해시해도 통과 ④ SQL의 `DEFAULT 'unique'` 문자열을 UNIQUE 제약으로 오인 ⑤ 보관용 같은 다른 계정 표의 UNIQUE가 실제 가입 표의 결함을 가림 ⑥ `response_model=None`이거나 응답 객체를 직접 돌려주는데 걸러진다고 믿음 ⑦ 이전 코드 버전의 평가를 최종 평가로 집계 ⑧ 수정 요청을 수정 완료로 계산 ⑨ WARM/COLD 차이를 비교에서 놓침 ⑩ 로그 폴더의 FIFO를 열다가 자료 수집이 멈춤 | 2026-10-16 해시하는 값은 "입력의 내용을 통째로 간직한 변환"만 인정하는 허용 목록 방식으로 바꾸고 저장하는 곳에서는 비밀번호 이름에서 나온 값인지도 확인, 난수는 진짜 라이브러리(import·미재정의·프로젝트 파일명 충돌 없음·덮어쓰기 없음·8바이트 이상)일 때만 인정, SQL은 실제로 실행하는 글자만 읽고 주석·문자열을 걷어 낸 뒤 칸별로 판정(부분 인덱스·계산식 칸·읽을 수 없는 SQL은 UNVERIFIED), 이메일 UNIQUE는 가입에 쓰는 표(없으면 모든 사용자 표)별로 판정하고 표마다 다르면 UNVERIFIED, `response_model`은 프로젝트의 모델 클래스일 때만 인정하고 응답 객체 직접 반환·`add_api_route`·Flask 경로도 확인, 평가 보고서의 코드 버전 대조, 해결 수는 재검증 PASS만, warm-up 차이는 주의로 표시, 일반 파일만 열기. 회귀 시험 64개와 run_metrics·demo_index 시험 추가 |
| 수정 완료 | 외부 점검(2026-10-16)에서 찾은 "결함인데 PASS" 4건: ① `payload.password or ""`처럼 and·or가 비밀번호 값을 그대로 돌려주는데 True/False만 만든다고 본 문제 ② 입력 비밀번호가 아니라 변수에 담은 상수를 해시해도 통과 ③ 고정 salt(`salt=b"..."`)를 지정해도 통과 ④ `CREATE TABLE other (id INTEGER UNIQUE, email TEXT)`처럼 같은 줄의 다른 칸 UNIQUE를 이메일 중복 방지로 인정 | 2026-10-16 and·or의 피연산자를 따라감, 해시하는 값은 매개변수에서 나온 것이어야 하고(함수 호출은 해시되는 매개변수 자리에 입력이 들어갔는지 짝지어 확인) 아니면 UNVERIFIED, 고정 salt·8바이트 미만 `salt_len`은 FAIL(정책 `uniqueSalt`), `CREATE TABLE`을 칸별로 읽고 비밀번호 칸이 있는 사용자 표의 이메일 칸 자신의 UNIQUE만 인정(클래스·`Table(...)`도 같은 기준, `CREATE UNIQUE INDEX`는 대상 표 확인 필요) |
| 수정 완료 | 위 점검의 연장으로 직접 찾아 막은 구멍: 계산만 하고 버리는 이메일 정리(`value.strip().lower()`), 평문이 저장되는 다른 길(`.values()`·`setattr`·`__dict__`·사전·SQL `execute`), 비밀번호가 나가는 다른 출구(`pprint`·`file.write`·헤더·쿠키·`locals()`·람다/안쪽 함수) | 2026-10-16 정리 결과가 쓰일 때만 인정, 저장 경로와 출구를 넓혀 같은 기준으로 판정. 회귀 시험 60개(정상 코드 오탐 방지 포함) |
| 수정 완료 | 평가 보고서의 환경 지문(컨테이너 이미지 digest·의존성 lock 해시)을 그 Run의 동결 환경과 대조하지 않던 문제. 두 구조의 보고서가 같은 잘못된 환경을 적으면 발견하지 못했다 | 2026-10-15 어긋난 Run과 보고서가 `metadataMatches=false`로 밝힌 Run을 `RERUN_RUN`(해당 Run만 재평가, 비교 전체는 유효)으로 표시 |
| 미해결 | wheel(배포 패키지)에 `security/scn001_security_criteria.json`이 들어가지 않는다. `assess()`는 기준을 요청으로 받아 실행 중에는 이 파일을 읽지 않지만, 이 파일이 기준의 원본이라 소스 없이 wheel만 받은 환경에서는 승인용 기준을 만들 원본이 없다. 소스 체크아웃에서는 영향이 없다. 포함하려면 루트 `pyproject.toml`의 package-data에 `security/*.json`을 더해야 한다 | 팀 공유 파일이라 수정하지 않았다. 주호 님(팀장) 승인 후 반영 |
| 미해결 | 루트 `.python-version`(3.13)은 Git에 올리지 않았다. 저장소를 쓰는 모두의 uv가 고르는 Python 버전을 바꾸기 때문이다. 다른 컴퓨터에서는 `uv sync --frozen --python 3.13`으로 직접 지정해야 한다 | 팀 Python 통일(OPEN-15) 때 파일을 올릴지 `pyproject.toml`을 고칠지 정한다 |
| 참고 | 민감값 치환은 호출자가 알려 준 문자열만, 진단 필드에서만 수행한다. 검사 제목·suite 이름·findings·toolEvidence·context는 치환하지 않는다. | 호출자가 사전 정리 |
| 참고 | `assess()`는 내부 오류를 모두 `None`(미검증)으로 돌려주고 원인을 남기지 않는다(dev 승인 계약상 로그·전역 상태를 쓰지 않음). | 이유 없이 모두 UNVERIFIED면 `evaluate()`를 직접 호출해 예외 확인 |
| 참고 | D1 이름 판정은 설명 단어(text, name, key 등)가 낀 이름을 비밀번호 값이 아닌 것으로 본다. 그런 이름의 변수에 실제 비밀번호를 담아 출력하면 놓친다. 정적 분석은 호출 흐름·동시성·런타임 설정을 보지 않는다. | 실행 검사(SEC-SIGNUP-002·005)가 보완 |
| 참고 | D1은 해시 결과를 담은 변수를 그 변수에 값이 묶이는 곳이 모두 `이름 = 해시 호출(...)`일 때만 따라간다. 분기에서 평문을 대입하거나 매개변수로 받은 변수, 이메일을 해시한 변수는 UNVERIFIED다. | 실제 생성물을 보고 조정 |
| 참고 | `security/*.py`를 고치면 dev 승인 해시가 달라진다. 2026-10-15 수정으로 다시 달라졌다. | 승인은 코드를 동결한 뒤 |
| 참고 | created_at은 3.10에서도 읽히도록 `datetime.fromisoformat` 기준이라 소수점 초 자릿수 등 일부 ISO 표기는 받지 못한다. 개발 환경은 Python 3.13이다(3.10은 dev 테스트와 Security 승인 함수가 실패, 3.14는 보안 승인 테스트 6개 실패). | "YYYY-MM-DDTHH:MM:SS+09:00" 형식 사용, 팀 Python 통일은 OPEN-15 |

## 16. 완료 조건과 자체 검증

| ID | 완료 조건 | 확인 |
|---|---|---|
| DONE-01 | 다른 팀원이 준비·실행·정리 재현 | 깨끗한 환경에서 절차 수행 |
| DONE-02 | 정상·기능 실패·보안 실패·환경 오류 구분 | 각 통제 fixture 결과 대조 |
| DONE-03 | 누락·0개 수집을 PASS 처리하지 않음 | 누락·빈 결과·파싱 실패 입력 |
| DONE-04 | 같은 Build/QA/Security Manifest와 증거 기반 판정 | 코드·환경 불일치·증거 누락·변조 확인 |
| DONE-05 | 보안 실패 → 수정 → Build/QA/Security 재검증 1회 이상 | 실제 A2A Task·MCP 실행·REQ-008 Trace 복원 |
| DONE-06 | 같은 기준으로 Single/Multi 집계 | 알려진 표본의 수기 계산과 대조 |
| DONE-07 | 실패·시간초과·비용 누락 표시 | 해당 실행이 포함된 보고서 |
| DONE-08 | 민감정보 제거·제한·정리 | 합성 비밀값·허용 밖 경로·timeout 확인 |

평가 코드 자체는 실제 오판 가능성이 있는 집계 분모, 버전 불일치, 필수 누락, timeout·cleanup을 검증한다. 샘플 JSON 파싱 성공만으로 평가 시스템 완료를 선언하지 않는다.

## 17. 구현 순서와 현재 위치

완료: 집계·Manifest·독립 평가 보고서·에이전트 호환 보고서, SCN-001 기준표(D0), Security 정적 판정(D1), 정상·결함 예제(D3), 결과 지표·비교 가능 여부 검사·시연 자료 목록(D4, 합성 입력), 공유 문서 3종.

남은 순서(팀장 카톡의 우선순위: 미팅 전에는 기준·준비, 실제 테스트와 비교는 미팅 이후):

1. 19일 주 팀 회의: 보안 판정 기준과 합의 항목(OPEN-01·05·11·14~16)을 [share_security_criteria.md](share_security_criteria.md)로 논의.
2. 세화 시작 코드·API/DB 규격을 받아 공통 QA·보안 평가 테스트(D2)를 연결부만 교체할 수 있게 작성하고, 일부러 고장 낸 버전(평문 저장, 7자 허용, 중복 허용)에서 FAIL을 잡는지 확인.
3. 주호 님(팀장)에게 최종 Snapshot 인계 방법을 받아 독립 채점기(D5)를 만들고, 결과 → CheckResult 정규화 → 독립 평가 보고서까지 연결.
4. 시연 실행 로그·생성 파일·화면을 팀장에게 받아 `demo_index.py`로 정리(12월 발표용).
5. 파일럿 비용·시간으로 반복 횟수·예산을 정하고 Single 실행기 담당이 정해지면 Single/Multi 본 실험(D6). 여유가 되면 협업 과정 검증(10절).
6. 확장 시나리오와 발표 자료 보완.

명세는 팀 검수용 초안이며, 구현 범위는 15.1절과 같다. 실제 서비스 검증·자동 채점 연결·비교 실험은 후속 작업이고 합성 예시는 실행 성공 증거가 아니다.

## 18. 협의 필요 사항

확정된 MVP·수정/도구 재시도 횟수를 다시 미정으로 취급하지 않는다. 아래 항목은 결정일·담당·승인 정책 버전을 남긴다.

| ID | 미정 결정 | 연결 담당 |
|---|---|---|
| OPEN-01 | Argon2id 수치 규칙(이상/정확히 일치, 기준 JSON 초안은 이상)·이메일 정규화 권장안 채택, 파서·길이·최대값·빈값 세부 정책 | 전원·3번 |
| OPEN-02 | API/DB/화면 계약, 동시 요청 수·동기화·정리, 조회 권한 | 3번 |
| OPEN-03 | 평가 전용 report/test/evidence/experiment ID 생성·Registry 매핑, snapshotId 관계, payload 스키마 | 1·2번 |
| OPEN-04 | 혼합 FAIL/UNVERIFIED 최종 우선순위, finalVerdict 키 통일, 한도 소진·취소 공통 상태 전이 | 1번 |
| OPEN-05 | MEDIUM 추가 차단, 반복 Issue 자동 인계 조건, fingerprint의 원인/위치 정규화 | 1·2번 |
| OPEN-06 | testScope/testSuite/scannerProfile·개별 report 계약, Security HTTP/DB 재현 도구 | 2번 |
| OPEN-07 | 계획·미시작·수락 전 거부·취소 Run 표시, Verified Rate 전체 Run 분모, 중첩 대기 집계 | 1번·4번 |
| OPEN-08 | CPU/Memory/PID/timeout/출력 및 총시간 예산, 의존성 준비, Single 실행 진입점 | 1·2번 |
| OPEN-09 | 모델 lock·실험 횟수·실행 순서·단가·비용 예산 | 전원 |
| OPEN-10 | 증거 공유·접근·보존, 확장 시나리오 채택·상세 검사 | 전원 |
| OPEN-11 | 독립 평가 구조 확정: 순환 밖 채점, 결과 비공개, 모든 종료 상태 채점, 중간 버전 기록 방식(4.1절) | 전원 |
| OPEN-12 | (해소) 0.3의 SEC-SIGNUP-004~007은 8.1절에서 확장 후보로 내리고 REQ-003·005·006 검사로 재정의 | — |
| OPEN-13 | 에이전트 호환 경로에서 "도구 정상 종료 + 결과 파싱 실패" 기록 방식 | 1번·2번 |
| OPEN-14 | 생성물 규격(시작 코드·API·DB·로그 위치·의존성), 최종 Snapshot 소스 인계 방법과 종료 후 실행 권한, Single 실행기 담당 | 1·2번(주호), 3번(세화) |
| OPEN-15 | 팀 Python 버전을 3.13으로 통일(3.10은 dev 테스트·Security 승인 함수 실패). `pyproject.toml`의 `>=3.10`과 루트 `.python-version`은 팀 전체에 영향을 주는 파일이라 아직 바꾸거나 올리지 않았다(그동안은 `uv sync --frozen --python 3.13`) | 전원 |
| OPEN-16 | 보호 테스트 위치(10/12 논의 기록: B로 평가하고 A로도 실행해 비교, B′ 추가 제안)와 held-out 시험지 | 주호(팀장)·4번 |

3번 구현(sehwa-dev)과 개발정의서의 차이는 OPEN-01·02에서 함께 정한다. 이메일 앞뒤 공백(정의서는 제거 후 비교, 구현은 형식 오류로 거부), 별도 canonical_email 컬럼(구현은 email을 소문자로 저장해 UNIQUE 적용), Argon2 수치(정의서 m=19456·t=2·p=1, 구현은 pwdlib 기본값), 동시 가입 시 기대 응답 코드, 테스트 DB 경로 분리·초기화·로그 수집 방법이다.

미정 때문에 필수 검증을 수행할 수 없으면 검증 미완료로 표시한다. 편의상 승인·PASS·NOT_APPLICABLE을 만들지 않는다. 확정 정책과 충돌하는 자동 분기는 구현 전에 공통 계약을 정정한다.

## 19. Git 제출과 검수

- 작업 브랜치: seokmin (기존 kangseokmin에서 이름 변경). 기능별 추가 브랜치 대신 개인 브랜치 사용.
- 개인 브랜치는 최초 작성 시 main에서 생성했다. 이후 팀의 dev 운영 기준에 맞춰 최신 변경을 반영한다.
- 명세를 개인 브랜치에 커밋·푸시하고 브랜치·문서 위치·협의 항목을 카카오톡으로 안내한다.
- 팀장이 검수 후 dev 대상 PR을 진행한다. 명세 제출 단계에서 main/dev 병합은 수행하지 않는다.

## 20. 변경 이력

| 버전 | 날짜 | 변경 |
|---|---|---|
| 0.1 | 2026-09-27 | 담당 경계, QA·보안·협업 검증, 결과 계약, 재검증, 실험, 완료 조건·협의 항목 작성 |
| 0.1.1 | 2026-09-28 | 참고 자료 목록 및 자료명·페이지·질문 번호 표기 제거. 기능·판정 정책은 유지 |
| 0.2 | 2026-09-28 | 회원가입 8개 요구, UUID·Manifest·Artifact 계약, 최종 판정·수정/도구 재시도, MCP·Trace 검증, 실험 지표·미정 정책 정합화 |
| 0.3 | 2026-10-06 | 독립 평가 원칙(4.1) 추가, 담당 간 계약을 독립 평가 기준으로 갱신, 보고서 형식을 실제 구현(독립 평가·에이전트 호환)으로 교체하고 0.2 합성 예시 삭제, 모듈 구성·구현 현황·코드 검토 제약(15.1·15.2) 갱신, 구현 순서 재정리, OPEN-11~13 및 3번 구현 차이 추가 |
| 0.4 | 2026-10-14 | 요구사항별 검사 Key를 기준표(D0)로 교체하고 UI 검사·SQLi/XSS 등은 확장 후보로 이동(7·8.1절), 담당 간 계약·구현 순서·구현 현황·검토 제약을 현재 개발 상태로 갱신(2.1·15·17절), 협의 항목 OPEN-14~16 추가와 OPEN-12 해소, sourceRun의 HUMAN_REVIEW 처리 명시 |

### 문서 유지보수 이력

| 날짜 | 변경 | 정책 영향 |
|---|---|---|
| 2026-10-04 | 평가 집계·Manifest 비교 구현 현황 및 dev 해시 형식 반영 내용 기록 | 기존 확정 정책 유지 |
| 2026-10-04 | dev 구현과 ID·상태·증거·정책 차이를 개발진행사항에 연결 | OPEN 승인 상태 유지 |
| 2026-10-04 | 환경/명세서/개발진행사항 구조로 링크 정리 | 문서 역할 구분, 명세 버전 0.2 유지 |
| 2026-10-04 | 공통 보고서 어댑터·실행 근거 정합성·패키지/lock·모델 호환 검사 추가 | OPEN 정책 유지, 실제 실행 연동 전 |
| 2026-10-06 | 독립 평가 보고서 기능 추가 기록. 문서 끝에 따로 붙어 있던 "독립 평가 출력 보완" 절을 4.1·11.2·15절로 통합 | 0.3으로 갱신, 팀 확정 대기 |
| 2026-10-06 | 필수·선택 실패/미검증 ID 분리, 환경 확인 테스트를 기본 pytest 대상에서 분리, 코드 주석 간결화 | 보고서에 optional… 필드 추가 |
| 2026-10-09 | 2차 기업미팅·팀장 오더(실행 정보 연결)에 따라 독립 평가 보고서에 sourceRun 추가 | 보고서·스키마 필드 추가 |
| 2026-10-14 | 기준표·Security 정적 판정·결과 지표·시연 자료 목록·공유 문서 추가, 검수 수정(D1 오탐·미탐, 통과율 null, missedDefect, 비교 가능 검사 순서 의존) 반영, 명세 0.4로 갱신 | 확정 정책 유지, OPEN 항목 갱신 |
