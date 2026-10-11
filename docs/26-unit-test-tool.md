# 26. Unit Test Tool 및 불변 테스트 보고서

작성 기준: 2026-10-08. 범위: 1번+2번의 MCP 테스트 실행·저장·조회. 3번 제품 서비스, 4번 독립 평가/비교 실험 및 해당 통합은 변경하지 않는다.

> `run_unit_tests`와 `read_test_report`를 실제 Snapshot·Container Runtime·불변 SQLite 저장소에 연결했다. 이번 실행기 지원 범위는 Python 표준 `unittest`다. 실제 Docker가 없는 현재 환경에서 제품 테스트 성공을 검증한 것은 아니다.

## 1. 이번에 구현한 것

- Host 전용 `UnitTestConfiguration`/`UnitTestScope`: 승인한 테스트 종류·패턴·디렉터리·이미지·자원·로컬 Docker endpoint 고정.
- `UnitTestInputs`: QA 테스트를 안전하게 읽어 불변 bytes로 캡처하고 Runner/Test/Input Manifest Hash 계산.
- Container용 표준 unittest Runner와 폐쇄형 JSON 보고서 파서.
- `UnitTestTools`: 기존 MCP 입력으로 실제 Source·Sandbox·보고서 저장을 연결.
- `UnitTestOutputStore`: 보고서·정제 출력·Source Manifest·실제 Profile·입력 Hash를 원자적/불변 저장 및 QA 조회.
- 실제 SDK stdio CLI/Client 설정 전달, Tool 등록, 오류·권한·취소 회귀 검증.

생성자·Tool 목록 조회는 제품 코드/테스트 실행이나 Docker/LLM 호출을 시작하지 않는다. Host 파일 읽기는 명시적 Tool 호출에서만 수행한다. Git commit, Run 생성, Source freeze, A2A Task 완료, `QA_REPORT` 발행, 최종 Verdict는 자동 수행하지 않는다. 기본 Agent의 `executionReady=False`는 유지한다.

## 2. 실제 처리 흐름

```text
Host-bound Developer/QA + workspaceId/snapshotId/testScope
→ 승인 Scope/역할과 실제 저장 Source 확인
→ 현재 Run/Step/Code Version/환경 및 QA Source grant 확인
→ 신뢰된 Runner + 필요한 테스트 bytes를 고정하고 Hash 계산
→ Frozen Source와 테스트 입력을 읽기 전용 Container에 준비
→ Host 고정 unittest Runner 실행
→ 원본 JSON/case/count/종료 코드 검증 + 민감 상세 정제
→ 소유 Container/준비 폴더 정리
→ 저장 transaction에서 Run/Step/Source/환경 다시 검사
→ 보고서·출력·Manifest·Profile·Input Hash 함께 저장
→ 기존 MCP Schema의 건수/저장 참조 반환
```

live `source/`를 테스트하지 않는다. `snapshotId`는 저장된 Source Artifact UUID이며 다른 Run·미등록 Source·현재 attempt/Step에 맞지 않는 Snapshot은 실행 또는 저장하지 않는다. QA는 실행 중 QA Step의 `inputArtifactIds`에 전달된 Source만 사용한다.

## 3. 기존 Tool Schema 유지

`run_unit_tests` 입력:

```json
{
  "workspaceId": "HOST_ISSUED_WORKSPACE_UUID",
  "snapshotId": "STORED_SOURCE_ARTIFACT_UUID",
  "testScope": "snapshot-unit"
}
```

위 UUID 문자열은 설명용이다. `testScope`도 Host가 실제 등록한 이름만 가능하다. 모델이 `argv`, `command`, `pattern`, `hostPath`, `role`, 이미지·Docker endpoint를 추가하면 Protocol Error다.

| 출력 | 의미 |
| --- | --- |
| `total` | 실제 보고된 case 수. 원래 요구사항 개수와 다름 |
| `passed` | PASS case 수 |
| `failed` | FAIL case 수 |
| `skipped` | SKIP case 수. PASS로 계산하지 않음 |
| `reportRef` | `artifact://<실행 기록 UUID>/unit-test-report.json` |
| `executionManifestId` | 실제 저장된 Unit 실행 기록 UUIDv4 |

`read_test_report`는 기존 `workspaceId`, `reportRef`를 받아 `{ "testResult": <검증된 unittest-v1 보고서> }`를 반환한다. 보고서는 format/counts와 `tests[{testId,outcome,details?}]`만 포함한다.

`reportRef`는 기존 MCP Artifact-reference 문법을 따르는 **전용 Unit Store의 사설 실행 참조**다. Project Artifact Registry/A2A QA Artifact를 발급한 것이 아니다. Source ID, Sandbox execution ID, receipt ID는 별개이며 임의 URL fetch·Workspace 경로 읽기로 해석하지 않는다. 실제 QA/Build Artifact와의 연결은31/32번이다.

## 4. Scope·역할·테스트 입력 분리

| 종류 | 역할 | 실행하는 테스트 |
| --- | --- | --- |
| `SNAPSHOT` | Developer | 불변 Source의 `/snapshot/tests/...` |
| `QA_TESTS` | QA | `outputs/qa/tests/...`에서 캡처한 bytes를 `/inputs/tests/...`에 Read-only 준비 |
| `PROTECTED` | QA | 신뢰된 Host가 설정으로 제공한 평가 테스트 bytes만 `/inputs/tests/...`에 Read-only 준비 |

QA가 Developer Scope를 실행하거나 Developer가 QA/Protected Scope를 선택하면 거부한다. Planner/Security는 Unit/QA Report Tool 권한을 받지 않는다. Orchestrator의 Report 조회는 기존 Host 저장소 접근 경계이며 새 AgentRole/직접 MCP API를 만들지 않는다.

Protected Scope는 frozen RunConfiguration의 `protected_test_suite_ref`와 정확히 일치해야 한다. 이 참조를 네트워크로 fetch하거나 QA scratch 파일로 대체하지 않는다. Source/QA 파일 Tool을 통해 보호 bytes를 변경할 수 없다. 4번 담당의 보호 테스트 자체를 대신 작성한 것은 아니다.

QA 캡처는 기존 Workspace root의 shared cooperative flock 및 FD/O_NOFOLLOW를 사용한다. symlink/hardlink/비정규 파일·Secret/private staging 경로를 거부하고 파일 inode/크기/시간·directory 목록을 다시 확인한다. 실행 전에 캡처한 bytes는 이후 scratch 파일 변경과 분리된다. 같은 OS 사용자 권한의 악성 프로세스를 격리하는 파일 시스템 transaction이라고 주장하지 않는다.

Input Manifest는 정렬된 `{path,sha256,sizeBytes}` 목록의 canonical JSON SHA-256이다. Runner Hash·외부 Test Files Hash도 별도로 보관한다. Snapshot Scope의 테스트 bytes는 Source archive Hash에 이미 묶이며 외부 테스트 목록은 비어 있다. QA 테스트 bytes의 장기 Artifact 발행/요구사항별 증거 조립은32번이고, 이번 receipt에는 해당 파일 경로/크기/Hash를 보관한다.

## 5. Host 설정과 실행 지원 범위

이미 발급된 Developer binding·동결 Source·승인 Linux 이미지가 준비된 Host 예시:

```python
from dataclasses import replace
from mcp_tools.client import open_mcp_client
from mcp_tools.tools.unit_config import UnitTestConfiguration, UnitTestScope

configuration = replace(
    existing_developer_configuration,
    unit_test_configuration=UnitTestConfiguration(
        scopes=(UnitTestScope(name="snapshot-unit", kind="SNAPSHOT"),),
        python_executable="/usr/local/bin/python",
    ),
)

async def test_source(source_artifact_id):
    async with open_mcp_client(configuration) as client:
        return await client.call_tool("run_unit_tests", {
            "workspaceId": str(configuration.binding.workspace_id),
            "snapshotId": str(source_artifact_id),
            "testScope": "snapshot-unit",
        })
```

Client 전달 필드는 `MCPChildConfiguration.unit_test_configuration`, CLI flag는 `--unit-test-configuration-json`이다. `.env`나 모델 입력으로 실행 명령을 자동 선택하지 않는다. Build/Unit 설정을 함께 전달하면 Docker endpoint는 같아야 한다.

실제 argv는 Host 검증 Python executable 뒤에 `-I -B /inputs/_unit_runner.py --kind <종류> --directory <tests 상대 경로> --pattern <승인 Python 파일 패턴>`으로 고정한다. 디렉터리는 `tests` 하위만, 패턴은 basename Python glob만 허용한다. traversal/절대 경로/leading-dash 패턴은 거부한다. 승인된 중첩/한글 디렉터리와 `test_auth.py`, `test_[ab].py` 같은 패턴을 지원한다.

Python 표준 unittest discovery만 지원한다. pytest 함수·Jest/Vitest·npm test·브라우저 테스트를 실행했다고 표시하지 않는다. application import 기준은 `/snapshot`이며 다른 layout의 지원은 추후 Host runner/profile 설계가 필요하다. 필요한 Python/의존성은 동결 이미지에 미리 준비해야 한다. Network 개방·자동 install/pull·Shell·Host 실행 fallback은 없다.

설정 JSON은 UTF-8 64KiB 이하, 최대32 Scope, closed/중복 필드·비유한 수 검증을 적용한다. 입력은 최대64 테스트 파일, 파일당1MiB, **Runner 포함 전체16MiB**다. Host 보호 bytes도 CLI 설정의64KiB 제한을 함께 받는다. 보고서는1MiB·1000case·ID512byte·detail4096byte 이하이며 canonical receipt metadata는512KiB 이하다. 파서/입력/Runtime/Store가 한도를 재검사한다.

## 6. 실제 테스트 결과와 실행 오류

| 상황 | 처리 |
| --- | --- |
| 정상 실행, assertion PASS | Tool 성공 + 실제 건수. 프로젝트/QA 최종 PASS 아님 |
| assertion/개별 테스트 오류 FAIL, exit1 | Tool 성공 + `failed` 건수와 보고서 |
| Skip/expected failure | SKIP. PASS 아님 |
| unexpected success | FAIL |
| 하위 subtest 실패 | 부모 case에 FAIL 집계 |
| 클래스/모듈 fixture 오류·skip | 해당 fixture의 synthetic FAIL/SKIP case. 실행되지 않은 원래 case를 PASS로 만들지 않음 |
| 테스트0개, discovery/import/framework 오류, 불완전 callbacks, 중복 ID/키, 건수·exit 불일치 | `TEST_RUNNER_ERROR`, 보고서 publication 없음 |
| Docker/이미지/실행기 미준비, OOM, 정리/저장 실패 | `TEST_RUNNER_ERROR`, Host fallback 없음 |
| 시간 초과 | `TIMEOUT` |
| 없는 보고서 | `REPORT_NOT_FOUND` |
| 임의 URI/잘못된 보고서 경로 | `PATH_DENIED` |
| 역할/Source/Workspace 권한 위반 | 기존 Protocol/Permission 오류 |

건수는 case outcome으로 다시 계산하고 exit0/1과 대조한다. stdout prose에서 숫자를 추측하지 않는다. 기존 문장용 redaction을 raw JSON에 먼저 적용하면 escaping/중복 증거가 손상될 수 있어, Host-only decoder가 원본 bytes를 먼저 파싱·정제한 canonical JSON을 Runtime에 반환하도록 했다. 모델은 decoder를 지정할 수 없다. 테스트 Python stdout/stderr는 제한해 버리고 CLI는 직접 FD 출력도 차단한다. 저장되는 detail/stderr는 민감값을 정제한다.

Runner는 동일 Python process의 악성 테스트가 의도적으로 runner를 조작하는 것까지 인증하는 보안 attestation이 아니다. 결과 건수는 요구사항 coverage도 아니다. 보호 테스트/독립 평가와 후속 QA의 case별 근거 검증이 필요하다. [Python 공식 unittest 문서](https://docs.python.org/3.12/library/unittest.html)의 discovery/결과 callbacks를 사용한다.

## 7. 불변 저장·조회와 실행 한계

`unit_test_execution_records`를 같은 Workflow SQLite에 lazy 생성한다. 보고서·정제 stdout/stderr BLOB·각 Hash/크기·실제 Source Manifest·Profile/image identity·Input Manifest를 하나의 transaction에서 저장한다. 저장 시 현재 Run/Workspace/환경, 유일한 실행 중 역할 Step/attempt, requirement/codeVersion 및 Source BLOB/grants를 재검증한다. QA는 실제 input Source를, Developer는 자신의 생성 Step Source를 사용해야 한다.

SQL update/delete/replace와 실행 ID 중복·Source/Build/Unit 등록 ID 충돌을 거부한다. 참조만 먼저 발급해 성공 응답하지 않는다. 조회도 Hash/canonical metadata/Source/lineage를 검사하며 QA는 같은 Run/Workspace의 실제 Source READ_ONLY grant를 재확인한다. 완료된 Run의 과거 보고서 조회에는 현재 QA Step을 요구하지 않지만, 과거 보고서를 현재 코드 PASS로 해석하지 않는다. 신뢰된 Host는 `output_store.get(run_id, execution_manifest_id)`로 전체 receipt를 조회한다.

22/25번의 Container 경계와 취소/정리 동작을 재사용한다. 실행 예산은 `min(Host timeout, max_call_seconds - 2×control_timeout_seconds - 1초)`로 줄이며 기본값은39초다. 환경 검사·Snapshot 준비·Container 실행·종료 확인이 공유하는 deadline이지 테스트 명령에39초를 전부 보장하는 것이 아니다.

동기 I/O/DB busy·외부 Client 종료·프로세스 crash까지 hard wall-clock/정리 완료를 보장하지 않는다. SDK의2초 stdio 종료 유예가 긴 cleanup을 중단할 수 있는 기존 한계도 유지한다. 임의 Container 삭제·자동 광역 정리·누적 Run 예산/Trace 완성은 이번 범위가 아니다. 자동 MCP retry/Fix/Task dispatch는 추가하지 않는다.

## 8. 개발정의서 점검

| 기준 | 이번 결과 |
| --- | --- |
| §2 책임 | Tool은 실행 사실만, 요구사항 판정/수정 지시는 QA/Developer, 최종 Verdict는 Orchestrator |
| §5 동일 Snapshot/환경 | 실제 Source BLOB·Manifest·동결 image/lock 검사, QA input Source 확인 |
| §8 Protocol/Schema | MCP2026-07-28/SDK v2/stdio/JSONSchema2020-12와 기존10 Tool 계약 유지 |
| §8-3/6 결과 | Unit/Report 고정 입출력, assertion FAIL과 실행 오류 분리 |
| §8-7~10 안전 경계 | Workspace FD/Secret 정책, 읽기 전용 Snapshot/Input, Network DENY/Container 제한, Host 생성 코드 실행 없음 |
| §8-11 역할 | Developer/QA Scope·QA Report 조회 실제 강제, 다른 역할 확대 없음 |
| §10 판정 | 테스트 건수만으로 QA PASS·프로젝트 SUCCESS·coverage를 만들지 않음 |
| §11-A 보호 기준 | Host 보호 bytes/Run suite ref 고정, Agent 생성 테스트와 독립 평가 분리 |
| 담당 범위 | 1번+2번만, 3번/4번 모듈·통합 미변경 |

ToolEvidence/Retry는29번, 실제 역할 Executor/Report는30~33번, 기본 Pipeline/누적 예산/Trace는34/37번 후속이다. 현재 단계로 전체 정의서·기업 시연·비교 실험이 완성됐다고 주장하지 않는다. 이전에 사용자가 제외한 비밀번호 보호 정책 보완과 기존 Agent DB 초기 WAL 경쟁도 해결 범위가 아니다.

## 9. 검증

```bash
PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -p 'test_mcp_unit*.py' -q
PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -p 'test_mcp*.py' -q
PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -q
PYTHONPATH=src .venv/bin/python -m compileall -q src/mcp_tools src/orchestrator/sandbox
.venv/bin/pip check
git diff --check
```

최종 검증 결과:

- 신규 Unit 전용 **188개 통과**: 설정22 + 입력29 + Runner35 + 파서24 + 저장52 + Handler/stdio26.
- MCP 관련 **601개 모두 통과**(88.611초).
- 전체 회귀 **1,319개 모두 통과**(134.015초). 기존 Unix socket 검증을 포함하도록 권한을 승인받아 제한 밖에서 실행했다.
- `compileall`, `pip check`, `git diff --check` 통과. 새 의존성/Lock 변경 없음.

실제 Source/QA/Protected 입력 분리, 입력 정확히16MiB/초과 경계, JSON Secret 정제/중복·빈 보고서/exit 일치, 현재 Step 및 실행 중 Run 취소, storage/cleanup/Timeout/OOM 오류, immutable receipt/Hash/권한/ID 충돌, 완료 Run 조회, 실제 SDK stdio 등록·조회·Docker 미준비 처리를 검증했다. 초기 Handler 테스트의 SDK 오류 응답/경로 오류 예상값은 기존 계약에 맞게 바로잡고 최종 전체 실행으로 재검증했다. 회귀 통과를 기존 WAL 경쟁이나 외부 종료 cleanup 한계가 해결된 것으로 해석하지 않는다.

Git/SQLite/Artifact/파일은 실제 임시 fixture이고, Container 통신은 Fake Docker다. Runner 실행 검증은 직접 작성한 안전한 Tool fixture만 사용한다. 실제 SDK stdio로 Handler 등록·보고서 조회·설정/Docker 미준비 오류를 확인하며 generated Source를 Host에서 실행하지 않는다. 현재 `docker` executable이 없으므로 실제 Container 제품 테스트 성공은 미검증이다.

## 10. 변경 파일·다음 작업

구현: `unit.py`, `unit_config.py`, `unit_inputs.py`, `unit_runner.py`, `unit_report.py`, `unit_store.py`. 연결: MCP Client/CLI/오류 코드, Sandbox의 Host-only JSON decoder. Build Store에는 새 Unit 기록과의 ID 충돌 거부만 보완했다. 전용 테스트6개·README·본 문서를 추가했고 새 의존성/Lock 변경은 없다. 기존 팀원 개발 이력인 `development-log.md`는 수정하지 않는다.

다음 작업: **27번 — Browser Test Tool**.

커밋 메시지: `고정 Snapshot 기반 MCP Unit Test Tool과 불변 보고서 구현`

Git commit/push는 직접 수행하지 않는다.
