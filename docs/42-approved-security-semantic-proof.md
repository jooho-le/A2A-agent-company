# 42. 승인된 Security 의미 검증기와 신뢰 증거 연결

작성 기준: 2026-10-11. 범위: 1번 Orchestrator + 2번 Agent/MCP의 Host 연결. 팀원 제품 UI/API/DB, 역할 4의 독립 보안 테스트·평가 기준·비교 실험은 구현하거나 연결하지 않는다.

## 1. 이번에 구현한 것

33번의 `SecuritySemanticProof` 확장 지점을 41번의 승인 설정 기반 Host에 연결했다. 승인된 독립 검증기만 현재 Run·Security Step·불변 Source·실제 Scanner 실행 기록을 근거로 판정을 제출할 수 있다.

- 검증기 ID/버전, 승인 기준 실제 JSON과 Hash, 적용 Scenario 계약 Hash, 선언한 구현 파일의 실제 bytes 및 로드된 함수 코드를 확인한다.
- 이 승인 정보를 기존 private Host 정책 Hash에 포함하여 같은 Run의 기준·구현 변경을 거절한다.
- 요청마다 새 UUID와 요청 Hash를 발급한다. 이전 요청 결과, 다른 Source·Step·Scanner 기록, 없는 Requirement/Finding/증거 참조는 받아들이지 않는다.
- 승인·실행 근거를 검증 전후 및 보고서 생성 직전에 다시 검사한다. 결과는 변경할 수 없는 private SQLite Receipt에 저장하고 다시 읽어 확인한다.
- 기존 SecurityReport의 JSON 문자열 `details`/`description`에 의미 검증 Receipt 참조와 승인/요청 Hash를 남긴다. Scanner Tool 성공과 의미 판정 근거를 구분한다.

**실제 회원가입 보안 검증기나 승인된 제품 평가 기준을 제작한 단계는 아니다.** 기본 검증기는 계속 `None`이다. 독립 근거가 없으면 Requirement는 `UNVERIFIED`, Scanner 경고는 `SUSPECTED`로 남고 기존 `HUMAN_REVIEW` 경로를 유지한다. 모델 설명·Scanner 경고 0개·QA PASS만으로 Security PASS를 만들지 않는다.

## 2. 파일

| 파일 | 역할 |
| --- | --- |
| `src/agents/platform/security_approval.py` | 명시적으로 전달한 Host 함수와 승인 JSON의 읽기 전용 검사 |
| `src/agents/platform/security_verifier.py` | 현재 실제 근거로 요청 발급, 결과 소속 확인, 재검증 및 Receipt 연결 |
| `src/agents/platform/security_proof_store.py` | 불변 의미 검증 Receipt 발행과 읽기 전용 과거 조회 |
| `src/agents/platform/host.py` | 승인 descriptor 정책 고정, 제출·준비·실행·재개 경계 연결 |
| `src/agents/runtime/security_services.py` | private Proof 필드와 기존 SecurityReport의 별도 의미 증거 참조 |
| `configs/owned-security-approval.example.json` | 승인되지 않은 설정 형식 예시. 원본은 의도적으로 실패 |
| `tests/test_security_approval.py` / `test_security_proof_store.py` | 승인·Hash·저장 불변성·소유권 회귀 |
| `tests/test_security_verifier.py` / `test_security_host.py` | 요청 재사용 방지·실제 근거 연결·Host 정책 회귀 |

`README.md`와 기존 raw callback 거절 회귀를 갱신했다. 공식 A2A/Artifact/Trace Schema·Task/Workflow 상태·MCP Tool 목록·ACL·의존성·팀원 코드는 변경하지 않았다. 아래 Receipt는 공개 프로젝트 Artifact가 아니다.

## 3. 승인 파일

`configs/owned-security-approval.example.json`은 미승인 필드를 `null`, 구현 목록을 빈 배열로 둔다. 실제 승인 후 운영자가 `configs/owned-security-approval.local.json`에 작성해야 한다. 기존 `configs/*.local.json` 규칙으로 Git에서 제외된다. 자동 승인·기본 보안 PASS 설정은 없다.

| 필드 | 의미 |
| --- | --- |
| `schemaVersion` | 정확한 정수 `1` |
| `verifierId` / `verifierVersion` | 운영자가 승인한 독립 검증기 ID와 버전 |
| `criteriaRef` | 승인 기준을 식별하는 논리 URI. Host 경로·HTTP 주소 아님 |
| `scenarioContractSha256` | 적용할 동결 Scenario 계약의 실제 SHA-256 |
| `criteria` | 독립 판정에 사용할 실제 승인 기준 JSON. 비어 있으면 거절 |
| `implementationSha256` | 실제 함수 코드 + 선언 파일 목록/Hash + Python 런타임의 결합 Hash |
| `implementationFiles` | `name`, `path`, `sha256`만 가진 실제 `.py` 파일 목록 |

추가/누락 필드, bool을 정수로 사용한 버전, 중복 JSON key, 유효하지 않은 Hash, 인식된 Secret을 거절한다. `criteriaRef`는 `urn`, `criteria`, `security-criteria`, `policy`, `artifact`, `approval` 계열 논리 참조만 받는다.

- 승인 파일 최대 256 KiB, 기준 JSON 최대 64 KiB, 구현 최대 32개·파일당 1 MiB·합계 4 MiB다. 빈 `.py` 파일은 허용하지만 실제 함수가 속한 모듈은 반드시 목록에 있어야 한다.
- 상대 구현 경로는 승인 파일 디렉터리 기준이다. 경로 자체와 부모의 symlink, 비정규 파일, 중복 경로/파일 inode를 거절하고 읽기 전후 파일 상태를 확인한다.
- 승인 파일과 구현은 Workspace·시작 Reference Repository 및 DB/sidecar 경로와 분리해야 한다. 생성 코드가 검증기나 기준을 수정하는 경로를 만들지 않는다.
- 실제 파일 bytes뿐 아니라 이미 로드된 함수의 bytecode·상수·중첩 코드·인자·예외 테이블과 Python 구현/version/cache tag도 Hash에 포함한다. 파일을 바꾸거나 로드된 코드를 바꾸면 재검증에 실패한다.

`calculate_implementation_sha256(assessor, files)`는 운영자용 **읽기 전용 Hash 계산 도우미**다. `files`는 `(논리 이름, 절대 Path, 실제 파일 SHA)` tuple들의 tuple이다. 파일을 import하거나 검증기를 실행하거나 승인 JSON을 쓰지 않는다. Hash를 계산했다고 검증기 내용이 승인되는 것은 아니다.

`scenarioContractSha256`는 실제 동결 Scenario 계약과 맞아야 한다. 검증기 기준에 다른 시나리오 Hash를 넣거나 요청 결과에 맞춰 기준을 낮추지 않는다. 비밀번호 Hash 정책·DB UNIQUE/동시성·출력 미노출 같은 실제 의미 판정은 승인된 독립 검증기의 책임으로 남는다.

## 4. Python Host의 명시적 연결

JSON은 Python 모듈/함수를 선택하지 않는다. Host가 신뢰하는 모듈을 직접 로드한 뒤 **인자 하나를 받는 이름 있는 최상위 함수**를 넘겨야 한다. lambda, closure, bound method, callable instance, generator, 기본 인자를 가진 함수는 거절한다. 동기 함수와 async 함수는 지원한다.

연결 순서는 다음과 같다. `assess`와 로컬 승인 파일은 운영자가 별도로 제공해야 하며 이번 작업에 실제 제품 판정 함수는 없다.

```python
from agents.platform.security_approval import load_approved_assessor
from agents.platform.host import create_host

# assess: Host가 별도로 검토·승인한 최상위 독립 함수.
# 이 예시는 assess 구현이나 운영 설정을 제공하지 않는다.
approved = load_approved_assessor(
    "configs/owned-security-approval.local.json", assess,
)
platform = await create_host(
    runtime_file="configs/owned-runtime.local.json",
    tools_file="configs/owned-tools.local.json",
    source_file="configs/owned-source.local.json",
    proof_verifier=approved,
)
# 이후 서버 시작/종료는 기존 Runner·platform.aclose() 소유권을 따른다.
```

검사/조립만으로 assessor·서버·LLM·MCP·Docker가 실행되지 않는다. 기본 `run_host` CLI에는 임의 import나 의미 검증기 선택 옵션을 추가하지 않았다. CLI는 계속 verifier가 없는 보수적 기본값이다. 승인된 검증기로 실행하려면 위와 같은 신뢰된 Python Host 조립이 필요하다.

41번 factory의 raw verifier callback 입력은 이제 거절한다. `None` 또는 `ApprovedSecurityAssessor`만 받으며 실제 서비스에는 `TrustedSecurityProofVerifier`를 전달한다. `None`의 기존 정책 Hash는 유지하지만 승인 descriptor가 달라진 같은 Run은 재개하지 않는다. 41번의 callback **유무만** 고정한 정책은 승인 기준/버전까지 고정한 현재 정책과 같지 않으며 자동 이관하지 않는다.

33번 저수준 `SecurityRuntimeServices`의 명시적 신뢰 Host callback 확장은 기존 호환을 유지한다. 직접 커스텀 조립한 이 경로는 이번 승인/Receipt 보장 대상이 아니다. 공개 모델·API·CLI로 callback을 지정할 수 없으며, 현재 권장 Owned Host 실행은 승인된 adapter 경로만 사용한다. 임의 callback fixture를 운영 보안 검증 완료로 취급하지 않는다.

## 5. 요청과 응답의 근거

adapter는 저장소에서 현재 Run/동결 설정/Step과 실제 Source bytes를 다시 읽는다. 같은 Security Step·Source·환경의 성공한 `run_security_scan` Tool Journal과 불변 Scanner Receipt를 확인한 뒤 다음 정보를 제공한다.

- 현재 Run·Workspace·Security Step·Source UUID, codeVersion/attempt와 Snapshot Hash.
- 승인 기준/descriptor, 동결 Scenario 계약과 RunConfiguration Hash, 동일 실행 Manifest.
- Source 파일의 불변 bytes와 실제 파일별 Hash/크기, 실제 Scanner 보고서와 Journal.
- 현재 Requirement/Finding ID와 현재 근거의 허용 `artifact://` 참조 목록.
- 별도로 표시한 `modelProposal`. 이 내용은 모델 제안이지 신뢰 근거가 아니다.
- 매번 새 `request_id`, 전체 요청의 `request_sha256`, 승인 descriptor의 `approval_sha256`.

검증기에 mutable 저장소·Tool/예산 객체·Host 경로·Credential을 전달하지 않는다. Source bytes는 읽기 전용 Mapping, JSON 데이터는 매번 복사본이다. 입력 Hash에는 실제 Source inventory와 실제 Scan/Journals가 포함된다.

독립 검증기는 `None` 또는 정확한 `SecurityAssessment`를 반환한다. 결과에는 같은 요청 ID/Hash/승인 Hash와 검증한 항목만 담는다. 필수 항목이 생략되면 PASS가 아니라 기존 미검증으로 남는다.

- Requirement 결과: `PASS`, `FAIL`, `UNVERIFIED`.
- Finding 판정: `CONFIRMED`, `FALSE_POSITIVE`, `SUSPECTED`, `UNVERIFIED`.
- `PASS/FAIL/CONFIRMED/FALSE_POSITIVE`에는 현재 허용 근거 참조와 비어 있지 않은 독립 관측 JSON이 필수다. 참조 문자열만으로 증거를 만들 수 없다.
- 응답의 다른 요청 ID/Hash·다른 승인·없는 항목/근거·변경된 실제 입력은 거절한다. 보고서 반영에는 기존 실제 파일 읽기 Journal과 코드 line anchor 검사도 그대로 필요하다.
- 결과를 받기 전후와 보고서 생성 전에 승인 파일/실제 구현/현재 Source/실행 기록을 재검증한다. 오래된 결과를 새 요청에 복사하지 않는다.

실제 보안 체크가 없는 함수가 관측을 꾸며내는지 Python 데이터 타입/Hash만으로 증명할 수는 없다. 독립 검증기의 로직·관측 의미·기준 정확성은 별도의 신뢰/승인 대상이다. 선언하지 않은 전이 의존성·mutable globals까지 attestation하지 않는다. 승인된 함수는 제공된 bytes에 대한 제한된 독립 분석이어야 하며, 생성 프로젝트 코드를 Host에서 실행하거나 외부 Tool/API를 몰래 재시도하는 실행기가 아니어야 한다. 새로운 동적 제품 탐침은 별도 승인·기존 Sandbox 연결이 필요하다.

## 6. 별도 의미 검증 Receipt

private `security_semantic_proof_receipts`에 요청/승인/실제 Source 소속, 판정, 근거 참조, 요청·판정·독립 관측 Hash를 canonical JSON으로 저장한다. Source 원문·관측 설명·Host 경로·Secret은 복사하지 않는다.

발행은 현재 VALIDATING/REVALIDATING Run의 유일한 실행 중 Security Step과 현재 Source에만 가능하다. 요청 ID는 중복되지 않으며 같은 Receipt의 정확히 같은 발행만 idempotent다. UPDATE/DELETE/REPLACE와 불일치 재발행은 차단한다. 저장 후 읽기 전용 조회에서 실제 Source bytes/등록 metadata/권한과 Receipt Hash를 확인한다. 보고서 반영 때는 추가로 현재 요청과 판정 Hash·허용 근거 소속을 대조한다.

Receipt 참조: `artifact://<receipt UUID>/security-semantic-proof.json`.

기존 SecurityReport의 `details`/`description` JSON 문자열에 다음 세 필드가 추가된다.

```json
{
  "semanticProofRef": "artifact://<receipt UUID>/security-semantic-proof.json",
  "semanticProofApprovalSha256": "<actual SHA-256>",
  "semanticProofRequestSha256": "<actual SHA-256>"
}
```

공식 Artifact Schema에 새 필드/Artifact 종류를 추가한 것이 아니다. 기존 `toolEvidence`는 실제 Scanner 실행의 증거이며, 의미 검증은 위의 별도 Receipt를 가리킨다. private Store의 신뢰된 `get(run_id, receipt_id)`만 제공하며 새 HTTP 조회 endpoint는 없다. 조회는 DB/테이블을 생성하지 않으며 완료된 과거 기록도 Source 소속이 유지되면 읽을 수 있다. 과거 조회 가능과 현재 결과 승격 가능은 구분한다.

## 7. 예산·취소와 최종 판정

기존 같은 Run budget/deadline을 사용한다. 동기 Host 분석/파일·DB worker도 취소 때 끝까지 회수한다. 자신의 시간 한도 소진은 `BUDGET`, 독립 함수가 발생시킨 일반 Timeout 등은 안정적인 검증 실패로 구분한다. 새로운 자동 API/Tool 재시도는 없다. 기존 Fix 최대 3회·Tool Retry 2회를 변경하지 않는다.

Receipt가 저장된 직후 취소되면 유효한 Receipt가 남아도 SecurityReport/최종 SUCCESS가 생성됐다는 뜻은 아니다. SQL/파일 I/O 및 동기 callback을 강제로 끊는 격리 Sandbox가 아니므로 종료가 deadline보다 늦을 수 있다. 무제한 작업·Host 코드 실행은 승인 검증기의 허용 계약이 아니다.

QA와 Security는 기존처럼 같은 Snapshot을 병렬로 검증한다. 다른 QA 결과의 PASS를 복사하지 않는다. 모든 필수 Security 항목의 근거와 기존 Build/QA/Requirement/Trace/Manifest 조건이 충족돼야 기존 최종 판정기가 SUCCESS를 결정한다.

## 8. 개발정의서 준수 점검

| 정의서 기준 | 이번 확인 |
| --- | --- |
| §0·§3·§6·§7 A2A/상태 | 공개 Protocol·Card·Task·Message·Artifact·Workflow 상태 유지 |
| §2 역할 경계 | 역할 1·2의 승인/근거 연결만. 역할 3 제품·역할 4 독립 테스트/평가 기준 제외 |
| §4 수정/재시도 | 기존 Fix/Tool 한도·불확실한 물리 실행 자동 재시도 금지 유지 |
| §5·§9 같은 Source | 현재 등록 Source actual bytes/Hash와 동일 Manifest/Scanner Journal 재검사 |
| §8 ACL/Secret/격리 | Security Source 읽기 전용, Host 승인 파일 분리, Secret 검사, Host 제품 실행 없음 |
| §10 Security/최종 판정 | Scanner/모델 제안만으로 승격 금지. 독립 근거 없으면 미검증/사람 확인 유지 |
| §11·11-A 동결/재현성 | 승인 기준·Scenario 계약·함수/파일/런타임 identity를 기존 불변 Host 정책에 포함 |

이는 이번 연결의 구현·자체 회귀 확인이지 전체 프로젝트의 운영 보안 검증 완료 선언은 아니다. 이미 보류한 RunConfiguration URL Credential 보호와 공유 Agent DB 초기화 경쟁은 수정하지 않았다. 실제 SCN-001 독립 검증기·승인 기준, 실제 LLM/Docker/회원가입 시연, 43번 비용·44번 실제 smoke·역할 4 비교 실험은 완료하지 않았다.

## 9. 검증

실제 임시 SQLite/Source/Git과 inert 승인 함수·가짜 Scanner/Container·합성 Provider·in-process HTTP를 사용한다. fixture의 PASS 관측은 `INERT_TEST_FIXTURE`로 표시하며 실제 회원가입 보안 증거가 아니다. 테스트 로그의 URL은 모의 전송이며 외부 API 비용·서버 Listener·실제 Docker/제품 실행은 발생시키지 않았다.

```bash
PYTHONPATH=src:tests .venv/bin/python -m unittest test_security_approval test_security_proof_store test_security_verifier test_security_host -q
PYTHONPATH=src:tests .venv/bin/python -m unittest test_security_agent_services test_security_agent test_security_agent_context test_security_agent_contract test_owned_agent_composition test_owned_host test_owned_host_cli test_host_services test_host_policy_store -q
PYTHONPATH=src:tests .venv/bin/python -m unittest test_owned_agent_pipeline test_owned_agent_fix_pipeline test_agent_platform_budgets test_owned_agent_controls -q
PYTHONPATH=src .venv/bin/python -m compileall -q src tests
.venv/bin/pip check
git diff --check
```

최종 결과는 완료된 검사 기준으로 아래에 기록한다. 중복 재실행을 합산하지 않는다.

- 신규 승인/Receipt/Verifier/Host **127개 / 32.026초 PASS**. 승인 36개·Receipt 36개·Verifier 47개·Host 8개다.
- 기존 Security 실행기/Context/Contract/Services·Owned 조립/Host/CLI/정책 **258개 / 81.164초 PASS**.
- 기존 최초/수정 Pipeline·공유 RunBudget·재개/취소 **43개 / 148.027초 PASS**.
- 합계 **428개 모두 통과**. 실제 제품 보안 체크·LLM API·Docker는 실행하지 않았다.
- `compileall`, `pip check`, `git diff --check`와 신규 파일 trailing whitespace 검사 통과. pip cache 권한 경고는 있었지만 의존성 오류는 없다.
- `git check-ignore`로 로컬 승인 파일 제외를 확인했다. 실제 운영 승인 기준·검증 함수·이미지·비밀 설정을 생성하지 않았다.
- 독립 리뷰와 회귀에서 승인 파일/구현 변경, 다른 Scenario/Source/Step/Scanner, 이전 요청 재사용, 누락/변조 Receipt, Hash가 일치해도 현재 요청에 속하지 않는 근거 참조, deadline/취소, 기본 미검증 유지 경계를 확인했다.

## 10. 커밋 메시지와 다음 작업

커밋 메시지: `승인된 보안 의미 검증기와 요청별 증거 영수증 연결`

다음 작업: **43번 — 승인된 가격표 기반 실제 LLM 비용 계산**. 실제 Usage와 고정 승인 가격표를 연결하며 가격/Usage가 불명확하면 비용은 `null`로 남긴다. 임의 가격·0원·측정 성공을 만들거나 비교 실험을 수행하지 않는다.

프로젝트 Git commit/push는 하지 않는다. Git 생성/Commit은 자동 정리되는 임시 테스트 fixture에 한정한다.
