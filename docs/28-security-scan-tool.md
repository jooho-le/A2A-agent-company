# 28. Security Scan Tool 및 불변 보안 검사 기록

작성 기준: 2026-10-08. 범위: 1번+2번의 MCP 보안 스캐너 실행·저장·조회. 3번 제품 서비스, 4번 독립 평가/비교 실험과 해당 연결은 변경하지 않는다.

> 쉽게 설명: AI가 만든 코드에서 보안상 수상한 부분을 찾고, 어디가 수상한지 검사 보고서를 남기는 도구를 만들었다.

## 1. 개발 내용과 범위

- `SecurityScannerProfile`/`SecurityScanConfiguration`: Host가 승인한 Bandit 정확한 버전·Rule IDs·Profile 참조·이미지·자원 제한을 고정한다.
- `SecurityScanInputs`: 신뢰된 Runner/Contract/Host JSON만 읽기 전용 입력으로 조립하고 원본 bytes의 Hash를 계산한다.
- Container 전용 Runner: Frozen Source의 모든 `.py` 파일을 Bandit AST 정적 분석으로 검사한다. 제품 코드를 import/실행하지 않는다.
- 폐쇄형 안전 보고서: 실제 검사 파일·선택 Rule·발견 위치/Severity/Confidence만 반환한다. 원본 코드·예외·Scanner 설명은 포함하지 않는다.
- `SecurityScanOutputStore`: Source·Host 정책·실행 Profile·Input Hash·보고서를 함께 원자적/불변 저장하고 권한을 검사하여 읽는다.
- `run_security_scan`, `read_security_report` Handler와 실제 SDK stdio Client/CLI를 연결한다.

첫 실행기는 **Python Bandit만** 지원한다. JavaScript/TypeScript·Semgrep·의존성 CVE·DAST·권한 우회 재현·비밀번호 정책 충족 검증 전체를 대신하지 않는다. 스캐너는 승인 이미지에 미리 준비해야 하며 자동 설치/다운로드나 Host 실행 fallback은 없다. Bandit은 Python Source AST를 검사하는 도구다. [Bandit 공식 사용 문서](https://bandit.readthedocs.io/en/latest/man/bandit.html)

모든 MCP Tool Handler가 연결되었다는 것과 네 역할 Agent가 자동으로 일을 수행한다는 것은 다르다. 기본 Agent의 `executionReady=False`는 유지하며, 실제 역할 Executor와 기본 Pipeline 연결은30~34번 후속이다. Tool 결과를 실제 A2A `security-report.json`으로 조립하는 단계도33번에 남는다.

## 2. 실행 순서

```text
Host-bound Security + workspaceId/snapshotId/scannerProfile
→ 등록 Profile·현재 Security Step·Source/grant/동결 환경 확인
→ frozen RunConfiguration.scannerProfileRef와 승인 Profile 참조 대조
→ 전체 canonical Archive 검증 + Python 파일 존재/안전한 파일 이름 확인
→ 신뢰된 Runner/Contract/Host policy bytes 및 Hash 고정
→ 같은 Frozen Source와 입력을 Read-only Container에 준비
→ 승인 버전/Rule 실제 설치 확인 + 모든 .py 파일 AST 검사
→ 누락/Skip/문법 오류/Plugin 오류/위조 결과 검사
→ Container 종료 확인 및 소유 실행 공간 정리
→ 저장 transaction에서 Run/Step/Source/grant/정책/환경 재검사
→ 보고서·Manifest·실행/Profile/Input Hash 함께 저장
→ 기존 MCP 필드 반환
```

Security는 `VALIDATING`/`REVALIDATING`의 현재 attempt에서 유일하게 실행 중인 Security Step에 전달된 Source만 사용한다. mutable `source/`, 다른 Run·미등록 Snapshot·이전 후보·취소된 실행은 대상이 아니다. QA/Security가 검사하는 Source의 실행 Manifest와 이미지/Lock 기준도 기존 계약대로 확인한다.

## 3. 기존 MCP 입출력 유지

입력 세 필드는 `workspaceId`, `snapshotId`, `scannerProfile`이다. `scannerProfile`은 Host에 등록된 이름이며 모델이 Scanner 설정 파일·URL·명령·Rule 목록을 직접 고르는 기능이 아니다.

```json
{
  "workspaceId": "HOST_ISSUED_WORKSPACE_UUID",
  "snapshotId": "STORED_SOURCE_ARTIFACT_UUID",
  "scannerProfile": "python-security"
}
```

위 UUID 문자열은 설명용이다. command/argv/Rule/version/image/role/timeout/Host Path를 추가하면 기존 Protocol Error다. Planner·Developer·QA는 직접 호출도 거절한다.

| 출력 | 의미 |
| --- | --- |
| `findings` | 안전하게 정제한 실제 Scanner 경고 목록. 모든 항목은 `SUSPECTED` |
| `reportRef` | 실제 저장한 `artifact://<실행 기록 UUID>/security-scan-report.json` |
| `executionManifestId` | 저장된 Security Scan 실행 기록 UUIDv4 |

Finding 필드는 `ruleId`, `testName`, Source 상대 `path`, `line`, `column`, `severity`, `confidence`, `status`뿐이다. Severity/Confidence는 Bandit의 LOW/MEDIUM/HIGH 값이며 CRITICAL·제품 PASS·CONFIRMED 판정을 새로 만들지 않는다. 위치를 이용한 추가 Source 읽기와 재현/오탐 판정은 후속 Security Agent가 수행해야 한다.

`read_security_report`는 같은 workspaceId와 정확한 reportRef를 받아 `{ "securityResult": <bandit-v1 보고서> }`를 반환한다. URI는 내부 사설 실행 Store의 참조이며 Project/A2A Security Artifact나 HTTP 다운로드 주소가 아니다. 임의 URL을 fetch하지 않는다.

보고서에는 `format`, `profileName`, `scanner`, `scannerVersion`, `ruleIds`, `profileRef`, `scannedFiles`, `findings`만 담는다. Rule·파일·Finding의 정렬/중복/구성·실제 Source 위치를 검증하며, 검사 파일 목록이 Source의 전체 Python 파일 목록과 같아야 한다. 0 Python 파일·Skip·불완전 검사 결과를 깨끗한 검사로 표시하지 않는다.

## 4. 승인 Profile과 고정 입력

Host 사용 예시(실제 Run/Step/Source 및 승인 이미지가 준비되었을 때만):

```python
from dataclasses import replace
from mcp_tools.tools.security_config import SecurityScanConfiguration, SecurityScannerProfile

profile = SecurityScannerProfile(
    name="python-security",
    scanner_version=approved_bandit_version,
    rule_ids=approved_rule_ids,
    profile_ref=frozen_configuration.configuration.scanner_profile_ref,
)
configuration = replace(
    existing_security_configuration,
    security_scan_configuration=SecurityScanConfiguration(profiles=(profile,)),
)
```

`MCPChildConfiguration.security_scan_configuration`, CLI의 `--security-scan-configuration-json`로 전달한다. Build/Unit/Browser/Security가 함께 설정되면 같은 로컬 Unix Docker endpoint여야 한다. Host Python executable 기본값은 `/usr/local/bin/python`이다.

정확한 `1.x.x` Bandit 버전, 구체적인 `Bxxx` Rule IDs, Profile 참조는 필수이며 자동 추천/선택하지 않는다. Rule은 최대128개·Profile은 최대32개이고 Profile 이름은 중복될 수 없다. 포괄 식별자 `B001`은 실제 구체적인 검사 Rule이 아니므로 거부한다. 문법상 올바르더라도 승인 이미지에 없는 Rule/버전은 실행 오류이며 자동 fallback하지 않는다.

Profile 참조는 Credential 없는 논리 `artifact://...`/HTTPS URI이며 bytes를 외부에서 가져오지 않는다. frozen `scannerProfileRef`가 없거나 선택 Profile과 다르면 검사하지 않는다. 같은 참조 문자열 자체가 Rule 내용의 암호학적 인증을 의미하지는 않는다. 신뢰된 Host 설정·정확한 Scanner 버전·이미지 Digest·실제 Rule/Policy Hash를 함께 기록하며, 비교 실험 기준의 선택·배포는4번/후속 연결 범위다.

검사 Scope는 `ALL_PYTHON`, `ignore_nosec=True`로 고정한다. Source의 `.bandit`·프로젝트 설정·baseline·exclude/skip·최소 Severity/Confidence 옵션으로 검사를 약화하지 않는다. Bandit 설정/Plugin은 승인 이미지의 설치만 사용하고 생성 Source를 Python 경로나 Plugin 로더에 추가하지 않는다. `# nosec` 무시 옵션과 Rule 선택은 [공식 옵션](https://bandit.readthedocs.io/en/latest/man/bandit.html)에 근거하며, Container 내부 API 호환성은 승인 환경에서 별도 검증한다.

Input은 `_security_runner.py`, `_security_contract.py`, `_security_host.json` 세 파일만이다. Host JSON은 canonical bytes이고 Role/Run/Workspace ID는 모델이 새로 만드는 값이 아니다. 전체 Input·Runner·Contract·Host 정책의 Hash를 각각 기록한다. 생성자/목록/설정 변환만으로 DB/파일/LLM/Docker/Scanner를 실행하지 않는다.

## 5. 권한·개인정보·Container 경계

22번의 비특권 UID10001·Source/Input/Root Read-only·Network none·Capability drop·no-new-privileges·Seccomp·CPU/Memory/PID/시간/출력 제한을 유지한다. Host FS/Secret/Docker Socket Mount·임의 Shell·외부 네트워크·자동 Pull을 추가하지 않는다. Browser 전용 ENV 예외를 Scanner로 확대하지 않는다.

실행 명령은 Host가 고정한 `python -I -B /inputs/_security_runner.py`다. Source를 import/exec하는 것이 아니라 설치된 Bandit이 Source AST를 분석한다. Bandit 자체와 신뢰된 Rule Plugin은 Container에서 실행하므로 Host에 Scanner를 설치하거나 실행하지 않는다. Source의 Plugin/config 파일을 실행하지 않는다.

Runner의 Python/OS 표준 출력·오류는 억제하고 폐쇄형 JSON 하나만 반환한다. Bandit의 `issue_text`·코드 snippet·raw exception/log/Stack Trace·Credential·원본 보고서/코드는 저장/반환하지 않는다. 원본 진단 stderr가 남은 실행은 안전 보고서로 게시하지 않는다. Bandit의 Plugin 예외가 조용히 누락된 경우도 정상 검사로 취급하지 않도록 오류/Skip/검사 파일을 확인한다.

Source는 기존 canonical Archive의 제한(최대1,000파일·파일당1MiB·총16MiB)을 따르며 Host JSON 설정64KiB, 보고서1MiB·Finding 최대1,000개다. 크기 초과·중복 key·비유한 숫자·bool 정수·추가 필드·Path traversal/Secret path·중복/없는 파일·미승인 Rule·잘못된 위치를 거부한다. 출력 한도는 자료를 자르고 성공 처리하는 기준이 아니라 실행 오류 기준이다.

경로는 원래 철자를 유지하되 NFKC 정규화로 숨긴 Secret 이름·traversal·분리자도 검증한다. Scanner 경로 정책은 C1 제어문자·private staging 이름을 추가로 거절하는 보수적인 부분집합이다. 유효한 일부 Snapshot도 이 조건을 위반하면 검사 오류가 될 수 있으며, 안전하지 않은 이름을 바꿔 검사하거나 검사에서 제외하지 않는다. Credential 패턴이 포함된 Python 파일 이름도 보고서에 노출하지 않고 Container 실행 전에 거부한다.

## 6. 정상 결과·오류·불변 저장

| 상황 | 결과 |
| --- | --- |
| 검사 정상 완료, 경고 없음 | Tool 성공 + 빈 findings. 보안 요구사항 전체 PASS가 아님 |
| 검사 정상 완료, 경고 발견 | Tool 성공 + SUSPECTED findings. 제품 결함 확정 아님 |
| 등록 Profile 없음/설정 없음 | `PROFILE_NOT_FOUND` |
| Scanner 버전/Rule/의존성 미준비·문법/파일/Plugin 오류·0 Python·불완전 보고서·OOM·정리/저장 오류 | `SCANNER_ERROR` |
| 전체 Sandbox/Tool 제한 시간 초과 | `TIMEOUT` |
| 모델 필드/직접 역할 우회 | 기존 Protocol Error |
| Source/grant/현재 역할·환경/경로 위반 | 기존 Permission/Path 오류 |
| 없는 보고서 | `REPORT_NOT_FOUND` |

exit0은 실제 경고0개, exit1은 경고가 있는 정상 결과, exit2는 실행 오류다. 종료 코드만으로 깨끗한 검사/제품 PASS를 추정하지 않고 엄격한 보고서 및 Source/Profile과 함께 대조한다.

`security_scan_execution_records`는 보고서·정제된 stdout·빈 stderr·각 Hash/크기 및 Source Manifest·Container/Image·Profile·실행 시간·Host Policy·Input Hash·실제 Python 파일 inventory를 함께 저장한다. 현재 실행 상태·동결 설정·Source bytes·Security READ_ONLY grant·Step의 input Source와 requirements/attempt/codeVersion을 저장 transaction에서 재검사한다.

SQL UPDATE/DELETE/REPLACE와 중복 ID를 거부하고 Source/Project/Run 설정/Build/Unit/Browser/Security 간 Manifest/실행 ID 충돌도 차단한다. 과거 조회 시에도 내용·metadata/Hash·Host 정책·Source/Step/grant/Run/Workspace 관계를 검증한다. 완료된 Run의 기록을 읽을 수 있는 것과 현재 후보의 검사가 성공했다는 것은 다르다.

이 Store는 실제 Tool 실행 기록이지 A2A Security Report/Issue/최종 Verdict가 아니다. Scanner 경고에 대한 취약점 확정·재현·오탐 판단·MEDIUM 수용 정책을 Tool이 대신 결정하지 않는다. 경고0개로 요구사항 충족률100%나 Workflow SUCCESS를 만들지 않는다.

## 7. 개발정의서 준수 점검

| 기준 | 결과 |
| --- | --- |
| §2 역할 책임 | Security만 Scan/Report Tool, Source Write 없음. 최종 판단은 상위 역할/Orchestrator |
| §5 동일 Snapshot/환경 | 실제 Source bytes·Hash·현재 Security Step/입력·grant·image/lock 대조 |
| §8 Protocol/Schema | MCP2026-07-28/SDK v2/stdio/JSONSchema2020-12·기존 Tool 이름과 입력/출력 유지 |
| §8-3/6 오류 | Finding은 정상 Tool 결과, Profile/Scanner/Timeout은 실행 오류로 구분 |
| §8-7~11 안전 | Read-only Snapshot Container·Secret/경로·Network/자원 제한·실제 서버 역할 권한 |
| §10 Security 판정 | Scanner 경고는 SUSPECTED; CONFIRMED/오탐/Final Verdict/coverage를 임의 생성하지 않음 |
| §11-A 고정 Scanner 기준 | frozen Profile 참조·정확한 Scanner 버전·Rule·Host 정책/Input Hash·실행환경 기록 |
| 담당 범위 | 1번+2번만. 제품/독립 평가/팀원 연결 미변경 |

안전 Retry/누적 Run 예산/실행 근거와 Trace 연결은29/34/37번 후속이다. 시간/취소는25~27번의 기존 제한을 유지한다. 기본 Sandbox deadline은 `min(Host timeout,max_call_seconds-2×control_timeout_seconds-1초)`이며 기본39초가 준비·실행·종료 확인을 공유한다. 동기 I/O·외부 SDK 세션 종료/프로세스 crash의 hard wall-clock 및 orphan cleanup 완료까지 보장하지 않는다.

기존 비밀번호 보호 정책 보완·WAL 초기 경쟁을 해결한 것으로 표시하지 않는다. 실제 Container/Bandit 실행 및 제품 시나리오 보안 검증은 아직 확인하지 않았고, 통신/Fixture 테스트를 그 증거로 대체하지 않는다.

## 8. 검증

```bash
PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -p 'test_mcp_security*.py' -q
PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -p 'test_mcp*.py' -q
PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -q
PYTHONPATH=src .venv/bin/python -m compileall -q src/mcp_tools src/orchestrator/sandbox
.venv/bin/pip check
git diff --check
```

28번 신규 테스트 **181개 통과**: Config/Contract40, Inputs20, Runner32, Report19, Store44, MCP Handler/stdio26(22.8초). 기존 기능을 포함한 전체 회귀 테스트 **1,697개 통과**(175.3초). MCP 하위 테스트도 전체 회귀에 포함되어 있으며 별도 재실행 수치로 중복 집계하지 않는다.

`compileall`, `pip check`, `git diff --check` 통과. 전체 회귀의 임시 Unix socket 테스트는 환경 권한 승인을 받아 실행했다. `pip show bandit`과 `command -v docker`로 현재 Host에 Bandit 패키지와 Docker CLI가 없음을 확인했으며, 실제 Scanner/Container 실행 성공을 주장하지 않는다.

정상/경고/실행 오류·검사 누락·Profile/Rule/버전·Snapshot/Step/grant·보고서 위조/위치·취소/시간/OOM/정리·불변 SQL·교차 Store ID 충돌·원문/비밀 미저장·실제 SDK stdio를 검증했다. Git/SQLite/Workspace/Artifact/파일·SDK stdio는 실제 임시 fixture이며, Docker/Bandit은 Fake 실행 fixture다. 실제 생성 Source·Bandit·제품 서비스는 Host에서 실행하지 않는다. 현재 환경에 실제 Docker/Bandit이 준비되어 있지 않으며 이를 자동 설치하지 않는다.

## 9. 인계와 다음 작업

새 Host 의존성/Lock 변경, 팀원 `development-log.md`·Evaluation·웹 서비스 코드 변경은 없다. Git commit/push는 직접 수행하지 않는다.

다음 작업: **29번 — Tool 실행 근거·오류 분류·안전 Retry**.

커밋 메시지: `고정 Snapshot 기반 MCP Security Scan Tool과 불변 보안 검사 기록 구현`

앞으로 각 번호의 완료 설명에는 **쉬운 한 문장 → 구현 요약 → 정의서 점검/검증 한계 → 해당 MD → 한국어 커밋 메시지 → 다음 번호**를 포함한다.
