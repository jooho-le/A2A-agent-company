# 4번 담당 상세 명세: Evaluation / QA / Security / 비교 실험

| 항목 | 내용 |
|---|---|
| 담당자 | 강석민 |
| 작성일 / 버전 | 2026-09-28 / 0.2 |
| 상태 | 팀 검수용 설계 초안. 구현·실험 완료 보고서가 아님 |
| 담당 범위 | 검증 시스템, QA·보안 평가 기준, 비교 실험, 결과 분석·발표 근거 |
| 첫 구현 대상 | 회원가입 MVP 확정, 표시 Key SCN-001 |

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
| QA 기준 | 요구사항별 기대 결과·경계값·독립 평가 테스트 | 2번 QA Agent의 테스트 생성과 병행 |
| 보안 기준 | 검사 대상·재현·적용 여부·판정 근거 | 2번 Security Agent의 분석·도구 사용과 연결 |
| 검증 실행 | 보호 평가 테스트·도구 결과 변환·보고서·집계 | MCP 실행·컨테이너·권한은 2번, 평가 어댑터는 4번. 함수 연결은 협의 |
| 최종 판단 | 항목별 결과와 전체 평가 권고 | 최종 상태·재시도·종료 결정은 1번 |
| 로그 | 검사 증거 생성·필드 정의·분석 | 전체 이벤트 저장·Task 상태 DB는 1번 |
| 대상 서비스 | API·DB·환경에 맞춘 검사 | 서비스·화면·인증·DB 구현은 3번 |
| 비교 실험 | 조건·반복 계획·공통 평가·집계 | Single/Multi 실행 진입점은 1·2번과 연결 |
| 발표 근거 | 실패·수정·재검증 사례·그래프·한계 | 팀 전체 발표 구성과 연결 |

4번은 A2A 서버, Agent 프롬프트 전체, MCP 서버 전체, 서비스 화면, Orchestrator를 중복 구현하지 않는다. QA·Security Agent는 제품 코드를 직접 수정하지 않고 결함을 반환한다. 테스트·판정 코드의 결함은 4번이 수정하고 기준 변경 이력을 남긴다.

### 2.1 담당 간 계약

| 담당 | 제공받을 정보 | 전달할 정보 |
|---|---|---|
| 1번 | run/Workflow Step/A2A ID 매핑, Artifact Registry, Execution Manifest, 실행 한도 | 결과 스키마·종료 권고·증거·수정 회차 |
| 2번 | 기획 Artifact, Agent 검사 결과, MCP 호출·응답 형식 | 검사 진입점·입력 검증·정규화 규칙 |
| 3번 | API 규격, 기동·준비 확인·종료, 테스트 DB·계정·조회 수단 | 케이스·테스트 데이터·실패 재현 절차 |

실행기가 실제 시간·종료 코드를 기록하고, 모델 호출 담당이 사용량을 제공해야 한다. 4번이 Agent 설명으로 시간·토큰 수·실행 결과를 추정하지 않는다.

## 3. 구현 범위와 단계

### 3.1 초기 범위

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

초기 제안은 공통 평가 피드백을 양쪽에 동일하게 제공하고 예산 내 수정을 허용하는 방식이다. 비공개 최종 테스트를 도입하면 공개 범위·수정 기회를 양쪽에 동일하게 적용하고 실험 조건에 명시한다.

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

개별 검사 결과는 reportRef/traceRefs에서 test ID·요구사항·기대/실제값·증거를 읽어 정규화한다. 합계 숫자만으로 항목별 PASS를 만들지 않는다. skipped는 사전 승인된 적용 제외인지 NOT_RUN인지 판단한다. Manifest·보고서 누락은 검증 미완료다.

MCP protocol error, isError=true인 도구 실행 오류, 정상 도구 실행 후 제품 FAIL을 구분한다. QA assertion 실패나 scanner finding 자체는 도구 장애가 아니다. testScope/testSuite/scannerProfile과 보안 HTTP·DB 재현 실행 경로, 보고서 세부 스키마는 2번과 확정한다.

### 5.3 평가 모듈 제안

validation은 입력·ID·버전, adapters는 MCP 결과 변환, reporting은 검사·Issue·증거, evaluation은 요구사항 집계와 판정 입력, experiments는 실행 조건·측정값·통계를 담당한다. 최종 Verdict 계산은 1번 Orchestrator가 담당하며 4번은 판정 규칙 검증용 fixture와 결과를 제공한다. 실제 실행 명령과 함수 API는 구현 전 확정한다.

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

MVP scenario_key는 SCN-001이다. 내부 scenario_id와 requirement_id는 Registry UUIDv4를 사용한다. 아래 REQ와 QA Key는 사람이 읽는 표시값이다.

| 요구 Key | 요구사항 | 검증 연결 |
|---|---|---|
| REQ-001 | 유효한 이메일·비밀번호로 계정 생성 | QA-SIGNUP-001 |
| REQ-002 | 합의된 이메일 파서의 invalid 입력 거부 | QA-SIGNUP-002 |
| REQ-003 | canonical email 중복 금지, DB UNIQUE 및 동시 요청 보호 | QA-SIGNUP-003/007, Security 구조 확인 |
| REQ-004 | 비밀번호 최소 8자 | QA-SIGNUP-004/005 |
| REQ-005 | 승인된 Password Hash 알고리즘·파라미터 | SEC-SIGNUP-001 |
| REQ-006 | 비밀번호·저장 해시가 API/A2A/로그/Trace에 미노출 | SEC-SIGNUP-002 |
| REQ-007 | 실제 가입 결과와 사용자 응답 일치 | QA-SIGNUP-006-API/UI |
| REQ-008 | 요구→개발→검증→Issue→수정→재검증 추적 | PIPE-016, 1번 Trace와 연계 |

이전 요구 버전의 REQ-006은 새 REQ-007에 해당하므로 과거 결과를 새 ID 의미로 재해석하지 않는다. 요구사항과 suite 버전을 함께 갱신하고 기존 실행은 이전 기준으로 보존한다.

| 검사 Key | 입력·절차 | 기대 결과 |
|---|---|---|
| QA-SIGNUP-001 | 미등록 user@example.com·유효 비밀번호 가입, DB/응답 확인 | 계정 1건, 필드·성공 응답 일치 |
| QA-SIGNUP-002 | userexample.com 등 합의된 invalid 입력, 요청 전후 DB 비교 | 거부, 계정 미생성, 오류 응답 |
| QA-SIGNUP-003 | 기존 canonical email과 동일한 값 재가입 | 기존 계정 유지, 총 1건, 중복 응답 |
| QA-SIGNUP-004 | 합성 7자 비밀번호 A1b2c3d | 거부, DB 무변경 |
| QA-SIGNUP-005 | 합성 8자 비밀번호 A1b2c3d4 | 다른 조건 충족 시 성공 |
| QA-SIGNUP-006-API | 성공·형식 오류·중복 요청별 DB/응답 대조 | 실제 처리 결과와 API 응답 일치 |
| QA-SIGNUP-006-UI | 대표 성공·오류 흐름의 화면과 API 대조 | 실제 결과와 안내 일치 |
| QA-SIGNUP-007 | 같은 canonical email 동시 요청, DB 제약·최종 건수 확인 | DB UNIQUE 존재, 1건만 생성, 나머지 실패 |

각 검사는 사전조건, fixture 버전, 요청·DB 확인·정리 단계, 기대값, 마스킹된 증거를 가진다. 동시 요청 수·동기화 방식과 API 상태/키·DB 조회 인터페이스는 3번과 확정한다. 단위 검사는 입력 검증, 통합 검사는 API→DB, 화면 검사는 실제 사용자 흐름을 다룬다.

이메일 상세 정책 권장안: 앞뒤 공백 제거, domain/local-part lowercase, Gmail dot/plus나 provider별 특수 변환 없음. 채택 여부와 파서를 확정한 후 대소문자·공백 동등성 및 dot/plus 구별 검사를 추가한다. DB UNIQUE·동시 중복 방지 요구는 확정이다. 필수값·빈값·최대 길이·유니코드 길이 해석의 미정 기준은 임의 FAIL 근거로 쓰지 않는다.

## 8. 보안 상세 기준

### 8.1 보안 검사

| 검사 Key | 확인 대상·기대 결과 | 요구 연결·적용 |
|---|---|---|
| SEC-SIGNUP-001 | 승인된 해시 알고리즘·파라미터·고유 salt·검증 동작. 평문/MD5/SHA-1/단독 SHA-256·512 금지 | REQ-005 필수 |
| SEC-SIGNUP-002 | 합성 비밀번호와 저장 해시가 API 응답·A2A Message/Artifact·일반 로그·Trace·Tool 로그에 노출되지 않음 | REQ-006 필수 |
| SEC-SIGNUP-003 | DB UNIQUE 존재 및 입력 조작에도 중복 제한 유지 | REQ-003, QA 동시성 결과와 연결 |
| SEC-SIGNUP-004 | SQL 삽입 입력의 쿼리 의미 변경·비인가 데이터 접근 없음 | SQL 사용 시 적용, 보안 요구 등록 필요 |
| SEC-SIGNUP-005 | 사용자 입력이 화면에서 스크립트로 실행되지 않음 | 해당 렌더링이 있을 때 적용 |
| SEC-SIGNUP-006 | UI 우회 직접 API에서도 합의한 입력 제약 유지 | 관련 기능 요구와 연결 |
| SEC-SIGNUP-007 | 소스·설정·출력에 실제 비밀정보 없음 | 대상 소스 검사, 합성 값으로 검증 |

해시 세부 권장안은 Argon2id, m=19456 KiB, t=2, p=1, 라이브러리가 생성한 고유 salt와 encoded hash다. 승인 알고리즘·파라미터 정책의 채택을 OPEN-01에서 확정하고 정책 버전을 남긴다. 원문과 저장값이 다르다는 이유만으로 안전 판정하지 않는다.

해시 검사는 격리된 검증 경로에서 수행하고 일반 결과에는 알고리즘·파라미터 충족 여부와 마스킹된 증거만 남긴다. 원문/해시를 증거에 복사하여 노출 검사를 통과했다고 보고하지 않는다. 보안 HTTP·DB 재현 도구 및 scannerProfile의 지원 범위는 2번과 확정한다.

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

필수 미검증이 있으면 검증 집계는 UNVERIFIED로 두되 확인된 FAIL 목록을 함께 보존하는 평가안이다. 미검증 없이 필수 실패가 있으면 FAIL, 전체 적용 필수가 통과해야 PASS다. 선택 검사는 별도 summary로 표시한다. 적용 대상 0개는 100%가 아닌 null이며 필수 MVP 검사를 전부 제외할 수 없다.

### 11.2 보고서 계약 제안

공통 식별·Manifest를 재사용하고 다음 평가 payload를 구체화한다. 보고서 생성 주체·공통 Registry 저장 경로는 OPEN-03에서 확정한다.

| 영역 | 필드 |
|---|---|
| 식별 | schemaVersion, reportId, runId, workflowStepId, a2aTaskId, agentContextId, scenarioId, scenarioKey, architecture |
| 기준 | requirementsVersion, suiteVersion, attempt, suite, executionManifestId |
| 실행 | startedAt, finishedAt, durationMs, tool(name/version/exitCode), mock |
| results | testId, testKey, requirementIds, required, status, expected, actual, errorCode, evidenceIds, durationMs |
| findings | issueId, issueFingerprint, category, severity, findingState, resolutionState, requirementIds, testIds, reproduction, evidenceIds |
| 증거 | evidenceId, 저장소 상대경로, sha256, kind, producer, redacted |
| 요약 | validationVerdict, 필수/선택별 상태 수, confirmedFailureIds, unverifiedTestIds |

보고서는 Registry의 QA_REPORT/SECURITY_REPORT 등에 등록하고 run·workflowStep·생성자·요구사항·소스 버전·artifact_uri·생성시각을 연결한다. A2A artifactId와 프로젝트 artifact_id는 별도 필드로 보존한다.

reportId/evidenceId/testId 생성 계약은 협의하되 내부 기계 ID는 UUIDv4 원칙을 따른다. 등록된 ID만 수락한다. 개별 보고서는 finalVerdict를 생성하지 않는다. 예전 completion_recommendation 필드는 제거한다. 최종 결과와 단일 검사 권고를 혼동하지 않는다.

### 11.3 합성 결과 예시

아래는 부분 검사 형식 예시이며 실제 결과·증거가 아니다. 요구사항 UUID는 Registry에서 REQ-003으로 연결된다는 가정이다. 생략된 전체 검사를 통과한 것으로 해석하지 않는다.

```json
{
  "schemaVersion": "0.2",
  "reportId": "39c287b5-b677-4a46-a2a3-c169a355c2a1",
  "runId": "9f4cb335-772c-4607-93fb-f3534839cc7b",
  "workflowStepId": "90787967-e7c5-4af2-b128-48d2c3e41436",
  "a2aTaskId": "example-qa-server-task",
  "agentContextId": "example-qa-context",
  "scenarioId": "99ee3b44-3ec4-4be5-a23f-22acf65dff66",
  "scenarioKey": "SCN-001",
  "architecture": "MULTI_AGENT",
  "requirementsVersion": "membership-0.2",
  "suiteVersion": "evaluation-0.2",
  "attempt": 0,
  "suite": "qa",
  "executionManifestId": "c792dc7c-e476-4f84-b1d6-ab1371800ca9",
  "startedAt": "2026-09-28T00:00:00Z",
  "finishedAt": "2026-09-28T00:00:01Z",
  "durationMs": 1000,
  "tool": {"name": "example-runner", "version": "example", "exitCode": 1},
  "mock": true,
  "results": [{
    "testId": "131c17a2-a9a0-48aa-babf-745e9b8c9d0b",
    "testKey": "QA-SIGNUP-003",
    "requirementIds": ["3b839b52-9daa-4e9c-9cb1-1d7d98d8deba"],
    "required": true,
    "status": "FAIL",
    "expected": {"accountCount": 1},
    "actual": {"accountCount": 2},
    "errorCode": null,
    "evidenceIds": ["ffcc7fe8-0e73-4d10-9615-e1878038b294"],
    "durationMs": 1000
  }],
  "findings": [],
  "evidence": [{
    "evidenceId": "ffcc7fe8-0e73-4d10-9615-e1878038b294",
    "path": "examples/duplicate-account.json",
    "sha256": "0000000000000000000000000000000000000000000000000000000000000000",
    "kind": "request-response-and-db-check",
    "producer": "example-runner",
    "redacted": true
  }],
  "validationVerdict": "FAIL",
  "summary": {"required": {"pass": 0, "fail": 1, "error": 0, "notRun": 0, "notApplicable": 0}, "optional": {}},
  "confirmedFailureIds": ["131c17a2-a9a0-48aa-babf-745e9b8c9d0b"],
  "unverifiedTestIds": []
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

## 15. 산출물과 제안 디렉터리

```text
docs/evaluation_spec.md          # 이 명세
evaluation/                     # 집계·합성 데모 로컬 초안, 나머지 기능 예정
tests/evaluation/               # 집계 코드 자체 검사 로컬 초안
evaluation/protected_tests/membership/       # 공통 평가·fixture (예정)
evaluation/protected_tests/document/     # 확장 시 추가
evaluation/protected_tests/mystery_game/         # 확장 시 추가
experiments/                    # 실험 설정·집계 절차 (예정)
docs/evaluation_examples/       # 합성 결과 예시 (예정)
```

보호 평가는 evaluation/protected_tests 아래에 두는 공통 권장 구조를 따른다. single_agent/multi_agent 실행 연결과 schemas/project·Registry는 담당 구현을 재사용한다. 세부 구조·스택 합의 후 확정한다. 2026-10-04 기준 집계 코드·합성 데모·자체 테스트의 로컬 초안이 존재하며, 보호 평가·실제 실행 연결·실험 코드는 아직 구현하지 않았다. 가상환경·개인 IDE 설정·비밀정보·대용량 원시 증거는 커밋하지 않는다.

최종 제출물은 명세, 요구사항-검사 표, 실제 검사 코드·fixture, 실행·초기화 안내, 스키마, 실제 보고서·증거, 수정 이력, 실험 설정·원본 측정값·집계 코드, 그래프·발표 사례다. 피드백 ID→변경 요구→반영 버전/커밋→재검증 결과의 이력도 남긴다.

### 15.1 로컬 구현 현황 (2026-10-04)

이 현황은 설계 v0.2의 확정 정책이나 협의 상태를 변경하지 않는다. 평가 초기 구현의 범위를 기록하며 실제 서비스 검증 완료를 뜻하지 않는다.

| 파일 | 구현 범위 |
|---|---|
| evaluation/aggregation.py | 고정 검사 계획과 정규화된 결과의 집계, 누락·중복·증거 참조 누락 처리, 필수·선택 분리 |
| evaluation/__main__.py | 정상·기능 실패·보안 실패·환경 오류·실패와 누락의 합성 입력 데모 |
| tests/evaluation/test_aggregation.py | 집계 동작과 잘못된 입력의 자체 검사 |
| checks/test_setup.py | Python 가상환경과 HTTP 라이브러리 확인 |
| evaluation/manifest.py | 기대 Manifest와 보고서의 코드·환경 메타데이터 비교, 누락·불일치 시 미검증 처리 |
| tests/evaluation/test_manifest.py | Manifest 형식·누락·불일치와 다른 버전 결과 혼합 방지 검사 |
| evaluation/reporting.py | 명시적 검사 ID 대응, 공통 보고서 변환, Tool 실행 근거 정합성 및 dev 스키마 검사 |
| evaluation/report_demo.py | QA/Security × PASS/FAIL/ERROR/NOT_RUN 합성 보고서 8개 |
| evaluation/pyproject.toml / uv.lock | 독립 평가 패키지와 고정 의존성 |

집계 코드는 11.1절 평가안을 시험하는 내부 초안이다. 반환값은 전체 보고서 계약이 아닌 부분 집계이며 finalVerdict를 계산하지 않는다. UUID 형식과 증거 ID 유무를 검사하지만 Registry 등록 여부, 증거 내용·해시·마스킹, 요구사항별 완전성, Manifest 일치, 보안 차단 Finding을 검증하지 않는다. 필수 여부와 적용 제외 승인은 신뢰된 계획에서 제공해야 한다. 실제 서비스 검증에 사용하려면 이 선행 검증과 계약 연결이 필요하다. 정규화 결과를 공통 QA/Security 보고서로 변환하는 로컬 어댑터는 아래 범위로 추가 구현했다.

별도 `aggregate_report` 진입점은 Manifest 메타데이터 일치를 확인한다. dev `2b4fc60`의 공통 스키마에 맞춰 containerImageDigest와 dependencyLockHash는 `sha256:` + 64자리 소문자 16진수 형식을 검사한다. 실제 소스·환경 해시, Registry 조회, 증거 진위 확인은 아직 연결하지 않았다.

`build_report`는 필수 검사 계획과 명시적 ID 대응을 사용하며 ERROR/NOT_RUN을 UNVERIFIED로 변환하고 details에 내부 상태·사유를 보존한다. N/A는 공통 정책 합의 전 거부한다. Security는 요구사항별 사전 집계 결과 1개만 지원하며 여러 증거를 임의 병합하지 않는다. 증거 진위·마스킹·A2A 포장·실제 도구 실행은 연결 전이다.

현재 dev 구현과 본 명세의 ID·상태·증거·정책 차이는 [개발진행사항](development_progress.md)에 기록했다. 이 검토는 OPEN-01/03/04/06의 팀 승인을 대신하지 않는다.

합성 예시의 PASS는 실제 서비스 통과나 16절 완료 조건 충족을 의미하지 않는다. 실행 방법과 환경 상태는 [로컬 환경 안내](environment_setup.md)에서 관리한다.

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

## 17. 구현 순서와 이번 검수 범위

1. 확정 회원가입 MVP·8개 요구·입출력과 남은 정책 검수.
2. API·Artifact에 맞춰 대응표와 결과 형식 확정.
3. 실행기·변환·판정 구현, 통제된 정상·실패·오류 확인.
4. 첫 서비스 실제 QA·보안 증거 확보.
5. 실패 전달·수정·재검증·중단 통합.
6. 파일럿 비용·시간으로 반복 횟수·예산 확정 후 본 실험.
7. 확장 시나리오와 발표 자료 보완.

명세는 팀 검수용 초안이며, 집계·Manifest 비교·자체 테스트는 15.1절 범위까지 구현했다. Agent 통합·실제 서비스 검증·비교 실험은 후속 작업이고 합성 예시는 실행 성공 증거가 아니다.

## 18. 협의 필요 사항

확정된 MVP·수정/도구 재시도 횟수를 다시 미정으로 취급하지 않는다. 아래 항목은 결정일·담당·승인 정책 버전을 남긴다.

| ID | 미정 결정 | 연결 담당 |
|---|---|---|
| OPEN-01 | Argon2id 권장 수치·이메일 정규화 권장안 채택, 파서·길이·최대값·빈값 세부 정책 | 전원·3번 |
| OPEN-02 | API/DB/화면 계약, 동시 요청 수·동기화·정리, 조회 권한 | 3번 |
| OPEN-03 | 평가 전용 report/test/evidence/experiment ID 생성·Registry 매핑, snapshotId 관계, payload 스키마 | 1·2번 |
| OPEN-04 | 혼합 FAIL/UNVERIFIED 최종 우선순위, finalVerdict 키 통일, 한도 소진·취소 공통 상태 전이 | 1번 |
| OPEN-05 | MEDIUM 추가 차단, 반복 Issue 자동 인계 조건, fingerprint의 원인/위치 정규화 | 1·2번 |
| OPEN-06 | testScope/testSuite/scannerProfile·개별 report 계약, Security HTTP/DB 재현 도구 | 2번 |
| OPEN-07 | 계획·미시작·수락 전 거부·취소 Run 표시, Verified Rate 전체 Run 분모, 중첩 대기 집계 | 1번·4번 |
| OPEN-08 | CPU/Memory/PID/timeout/출력 및 총시간 예산, 의존성 준비, Single 실행 진입점 | 1·2번 |
| OPEN-09 | 모델 lock·실험 횟수·실행 순서·단가·비용 예산 | 전원 |
| OPEN-10 | 증거 공유·접근·보존, 확장 시나리오 채택·상세 검사 | 전원 |

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


### 문서 유지보수 이력

| 날짜 | 변경 | 정책 영향 |
|---|---|---|
| 2026-10-04 | 평가 집계·Manifest 비교 구현 현황 및 dev 해시 형식 반영 내용 기록 | 기존 확정 정책 유지 |
| 2026-10-04 | dev 구현과 ID·상태·증거·정책 차이를 개발진행사항에 연결 | OPEN 승인 상태 유지 |
| 2026-10-04 | 환경/명세서/개발진행사항 구조로 링크 정리 | 문서 역할 구분, 명세 버전 0.2 유지 |
| 2026-10-04 | 공통 보고서 어댑터·실행 근거 정합성·패키지/lock·모델 호환 검사 추가 | OPEN 정책 유지, 실제 실행 연동 전 |
