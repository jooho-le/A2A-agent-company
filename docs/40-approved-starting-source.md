# 40. Run Workspace와 승인된 시작 Source 준비

작성 기준: 2026-10-11. 범위: 1번 Orchestrator + 2번 Agent/MCP의 시작 코드 준비. 팀원의 제품 UI/API/DB와 독립 평가·비교 실험은 변경하지 않는다.

## 1. 구현 내용과 현재 상태

- 운영자가 선택한 로컬 Repository의 **정확한 Commit·Snapshot·Dependency Lock**을 읽기 전용으로 검사한다.
- 시작 코드 설정을 기존 Frozen RunConfiguration의 기준과 대조한 후, 등록된 Run의 빈 Developer Source에만 준비한다.
- 원본의 Working Tree나 `.git` 폴더를 통째로 복사하지 않는다. 선택한 Commit·현재 Tree·Blob만 검증해서 최소 Git 저장소를 구성한다.
- 중간 실패는 `STARTED` 기록을 남기며 자동 재실행·삭제·덮어쓰기를 거절한다. `DONE`도 같은 초기 Run 상태에서 코드가 그대로인지 다시 읽어 검증한다.
- 기존 DeveloperCheckpoint로 수정 후보를 만드는 데 필요한 Git 구조와 호환된다.
- CLI는 검사만 수행하고 Workspace·DB·서버·Docker·LLM을 만들거나 실행하지 않는다.

**준비 기능을 구현한 단계이지 실제 실행 Host를 연결한 단계는 아니다.** 기본 Bootstrap과 `create_platform`은 변경하지 않았다. 승인된 시작 Repository·로컬 설정·이미지를 임의로 만들지 않았고 제품 코드를 구현/실행하지 않았다. 41번에서 기존 준비 callback에 연결한다.

## 2. 파일

| 파일 | 역할 |
| --- | --- |
| `src/agents/platform/starting_source.py` | 닫힌 Host 설정, 시작 Git 검증, 등록된 Source 준비와 private receipt |
| `src/agents/platform/check_source.py` | 읽기 전용 Source/Lock 검사 CLI |
| `configs/owned-source.example.json` | 미승인 값은 `null`로 둔 설정 예시 |
| `src/orchestrator/artifacts/git_snapshot.py` | 현재 Commit/Tree/Blob만 내보내고 순수 검증하는 기능 추가 |
| `tests/test_starting_source.py` | 실제 임시 Git·SQLite·Workspace 기반 준비/실패/권한 회귀 |
| `tests/test_git_object_export.py` | Git object·Archive 동일성, 최소 수입, 변조/상한 회귀 |

`README.md`도 갱신했다. A2A/Artifact/Trace Schema, MCP Tool 목록·계약·ACL, SDK/의존성, 팀원 코드는 변경하지 않았다.

## 3. 운영자 설정

예시를 편집기로 `configs/owned-source.local.json`에 복사하고 승인값을 입력한다. 기존 `configs/*.local.json` ignore 규칙이 적용된다. 원본 예시는 의도적으로 검증에 실패한다.

| 필드 | 기준 |
| --- | --- |
| `schemaVersion` | 정확한 정수 `1`. Host 설정 버전이며 A2A/MCP 버전이 아님 |
| `repositoryPath` | 신뢰된 로컬 Git Working Repository. 상대경로는 설정 파일 폴더 기준 |
| `repositoryId` | 운영자 승인 식별자. UTF-8 256 bytes 이내, 공백/제어문자/감지된 비밀값 금지 |
| `commitHash` | lowercase 전체 Git Object ID, SHA-1 40자리 또는 SHA-256 64자리. `HEAD`/Branch/tag/약식 ID 금지 |
| `snapshotSha256` | 기존 GitSnapshotBuilder 정규 Archive의 SHA-256, lowercase 64자리 |
| `lockPath` | Commit 안의 정규 Source 상대경로. 예시 `requirements.lock`은 실제 존재나 승인값을 뜻하지 않음 |
| `gitTimeoutSeconds` | 유한한 숫자, `0 < 값 <= 300`. 자동 기본값 없음 |

모든 필드는 필수이며 alias 이름만 허용한다. Unknown Field·문자열/bool 숫자·중복 Key·NaN/Infinity·잘못된 Hash·Traversal/Secret 경로는 거절한다. 파일은 최대 64 KiB의 일반 UTF-8 JSON이며 파일 자체의 Symlink·FIFO·Directory는 허용하지 않는다.

`repositoryPath`의 `../`는 운영자 Host 경로로 허용하되 `~`·Shell 변수·URI를 확장하지 않는다. 실제 경로를 Resolve하여 검사하고 Workspace와 중첩되는 배치는 거절한다. 대소문자/NFC 별칭도 보수적으로 거절한다. 설정 디코딩과 준비 객체 생성은 저장소·폴더를 만들거나 Source를 읽는 작업이 아니다.

## 4. 읽기 전용 검사

먼저 39번 Tool 설정과 실제 Lock 파일을 준비한다. 같은 Lock **bytes**가 시작 Commit 안에 있어야 한다.

```bash
PYTHONPATH=src .venv/bin/python -m agents.platform.check_source --file configs/owned-source.local.json --tools configs/owned-tools.local.json
```

성공 출력:

```json
{"status":"SOURCE_BASELINE_VALID","executionReady":false,"workspacePrepared":false}
```

이 검사는 실제 로컬 Git object를 읽어 Commit/Tree/Blob의 Object ID, 정규 Archive Hash, 설정한 Lock 경로의 Hash와 39번 `ExecutionBaseline`의 일치를 확인한다. 원본의 변경 중인 파일, untracked 파일, 현재 HEAD가 검사 대상 Source를 바꾸지 않는다.

검사 통과는 Workspace 준비 완료, 모델/API 인증, Docker/이미지 존재, 실제 Build/Test/Scan 성공, 최종 `SUCCESS`를 뜻하지 않는다. Git commit/push·checkout·clone·fetch·Hook/Filter·의존성 설치·제품 실행은 하지 않는다. 구성 예시의 Hash를 가짜 값으로 채워 실제 기준을 대신할 수 없다.

오류는 exit 1과 안정된 코드만 반환한다. 제출 JSON·Source·Host 경로·Credential 원문은 출력하지 않는다.

| 코드 | 의미 |
| --- | --- |
| `SOURCE_CONFIGURATION_INVALID` | 설정 형식·필수값·Host 입력 오류 |
| `SOURCE_CONFIGURATION_FILE_INVALID` | 파일 종류·접근·크기·UTF-8·읽기 일관성 오류 |
| `SOURCE_BASELINE_MISMATCH` | Git/Archive/Lock 또는 Frozen Run/초기 상태 불일치 |
| `SOURCE_PREPARATION_CONFLICT` | 기존 파일·불명확한 준비 기록·변조된 완료 Source |
| `SOURCE_PREPARATION_TIMEOUT` | Git 시간 한도 또는 전달된 deadline 초과 |
| `SOURCE_PREPARATION_FAILED` | 그 밖의 준비 실패 |

Tool 파일 검사 실패는 기존 `TOOL_CONFIGURATION_*` 코드를 유지한다.

## 5. Host 준비 기능

Host는 `load_source_configuration` → `verify_starting_source(..., environment=tools.baseline)` → `RegisteredSourcePreparer(repository, registry, verified)` 순서로 구성한다. 이 객체는 호출 전에는 Workspace를 생성하지 않는다. 준비 호출은 공개 Run UUID만 받고 Host 경로나 Commit을 모델 입력으로 받지 않는다.

### 초기 Run 검증

기존 `before_dispatch` 위치인 **Planner claim 이후, A2A 전송 이전**에만 허용한다.

- 등록된 Run·Workspace·Step UUID와 Scenario가 일치해야 한다.
- Run은 `PLANNING`, Fix 0회, 아직 Code Version이 없어야 한다.
- 유일한 Planner Step은 `RUNNING`, attempt 0이며 Task/Context/Requirement/Artifact가 없어야 한다.
- Project Artifact, AgentContext, 기존 `A2A_MESSAGE_SENT`가 없어야 한다. Task ID가 없더라도 불명확한 전송 기록이 있으면 거절한다. 이벤트는 페이지 전체를 확인한다.
- Frozen RunConfiguration의 시작 Commit·Snapshot Hash·Image/Lock/Hardware/Network가 승인 Source와 정확히 같아야 한다.

빈 시작 기준을 나중에 채우거나 동결한 환경을 수정하지 않는다. 시작 Source를 Candidate 0/1이나 SOURCE Artifact로 등록하지 않고, Workflow 상태·최종 판정을 바꾸지 않는다. 실제 Developer 수정 결과의 첫 Candidate는 기존 정책대로 1이다.

### 파일과 Git 준비

등록된 Workspace를 provision하고 기존 Developer inode lock 안에서 빈 `source/`에만 기록한다. Descriptor-relative/no-follow/O_EXCL 접근, regular file/nlink 검사, bounded write와 fsync를 사용한다. 기존 파일을 truncate·overwrite하거나 `extractall`/전체 복사를 하지 않는다. Source의 실행 비트와 Unicode 이름을 보존하고 기존 Checkpoint의 경로/깊이/예약 임시 이름 제한을 사전 검사한다.

Git의 object는 종류·길이·내용을 포함한 Hash로 식별하며 압축된 loose object로 저장할 수 있다. 이번 준비도 검증된 bytes만 이 구조로 기록한다. [Git 공식 Object 설명](https://git-scm.com/book/en/v2/Git-Internals-Git-Objects)

새 `.git`에는 검증된 object, 고정된 최소 config, detached HEAD와 빈 refs만 둔다. 원본 remote URL·Hook·config·index·reflog·pack·과거 Commit/object를 복사하지 않는다. [Git 저장소 구조](https://git-scm.com/docs/gitrepository-layout), [SHA-256 object format](https://git-scm.com/docs/hash-function-transition)

기존 Snapshot 상한인 파일 1,000개·파일당 1 MiB·파일 합계 16 MiB·Archive 20 MiB를 유지한다. 추가 Git object 집합은 32 MiB·17,002개 이내이며 History나 불필요한 object가 포함되면 거절한다.

원본 Commit bytes와 부모 Object ID는 유지하되 부모 **object**는 수입하지 않는다. 완전한 clone/history 저장소가 아니므로 `git log` 전체나 `git fsck` 완전성을 지원한다고 주장하지 않는다. 기존 Checkpoint는 현재 Commit/Tree와 private index를 사용해 다음 후보를 생성하므로 이 구조와 호환된다. `.git` 및 private receipt는 기존 Agent File Tool ACL에서 접근할 수 없다.

### STARTED / DONE와 실패 처리

- Source 기록 전에 Workspace root의 `.source-preparation.json`에 private `STARTED`를 저장한다.
- Source와 최소 Git 기록 후 Snapshot을 다시 생성하고, Working Source inventory의 bytes/mode가 승인 Archive와 같은지 확인한다.
- 초기 Run 상태와 receipt를 재검증한 뒤 별도 완료 파일을 atomic replace하여 `DONE`으로 바꾼다.
- receipt는 Run/Workspace/Repository 식별자와 Commit/Tree/Archive/Lock/환경 Hash만 가진다. Source 본문이나 Host 경로는 포함하지 않는다. 공식 Trace/Artifact가 아니다.
- `STARTED`, 외부 기록, 비정상 marker, 기존 untracked 파일, 변조된 Source는 보존하고 거절한다. 삭제·초기화·부분 결과 자동 채택·자동 재시도를 하지 않는다.
- `DONE`의 재호출도 A2A 이전의 같은 초기 상태에서만 읽기 검증한다. Developer가 이미 수정한 Source를 시작 코드로 되돌리지 않는다.

실제 callback이 연결되면 기존 Dispatcher가 준비 실패를 `OWNED_AGENT_PREPARATION_FAILED`/`HUMAN_REVIEW`로 처리한다. 이 단계는 그 기존 경로를 새 상태나 성공 판정으로 대체하지 않는다.

## 6. 한계와 41번 연결 조건

- 아직 실제 Host factory에 주입하지 않았다. 41번은 38·39·40번 설정과 기존 Provider/Runner/MCP 조립을 연결한다.
- `prepare(..., deadline_monotonic=...)`은 전달된 공유 deadline과 Git 시간 한도 중 짧은 것을 사용한다. 단순 `__call__(run_id)`는 Git 한도만 사용하므로 41번 wrapper가 기존 RunBudget의 deadline을 전달해야 한다.
- 동기 작업을 `to_thread`로 실행할 경우 기존 Host처럼 취소 시 worker를 끝까지 회수해야 한다. `cancel`만으로 Git/file write가 정지했다고 가정하면 안 된다. DB/Registry 잠금·OS 파일 I/O는 별도 프로세스 강제 종료로 격리한 것이 아니며, 이 기능만으로 전체 벽시계 시간 한도를 보장하지 않는다.
- 같은 OS 권한을 가진 다른 Host 프로세스의 악의적인 Git metadata 교체나 모든 파일시스템 alias를 완전히 막는 Sandbox가 아니다. 신뢰된 원본 Git과 Host 소유권을 전제로 한다.
- Secret filename/알려진 민감 패턴을 거절하지만 opaque binary나 임의 Source/Commit 내용 전체를 판별하는 DLP는 아니다. 운영자가 승인 baseline에 실제 비밀 파일·값이 없는지 사전 검토해야 한다. 전체 과거 저장소를 가져오지 않는 것도 이 경계를 줄이는 조치다.
- 실제 승인된 시작 코드·Lock·Image/환경의 존재와 Build 가능한 구조는 운영자가 준비해야 한다. 이번 테스트 fixture의 임의 Hash/이미지 값은 실제 준비 증거가 아니다.
- 생성 코드 실행은 기존 일회용 non-root/read-only/Network DENY Container 경로만 사용한다. 준비 기능에 Host Build/Test fallback은 없다.

## 7. 개발정의서 준수 점검

| 정의서 기준 | 확인 |
| --- | --- |
| §0·§3·§6·§7 공식 A2A/상태 | Protocol·Schema·Task 상태·폴링 변경 없음 |
| §2 책임 경계 | 신뢰된 Host 초기 준비만. 제품 UI/API/DB와 독립 평가 개발 제외 |
| §4 Fix/Retry | Fix 3회·MCP Retry 2회 유지. 불명확한 Source write/Send를 자동 재실행하지 않음 |
| §5 Snapshot/Manifest | 실제 Object ID·Tree·정규 Archive·Lock Hash 확인. 기존 QA/Security 불변 Snapshot 전달 유지 |
| §8-7·8-8 경로/Secret | 등록 Workspace·Role ACL 유지. 원본 `.git`/Secret Path 미복사, CLI·receipt에 Host 경로/Source 미노출 |
| §8-9·8-10 설치/실행 격리 | clone/fetch/install/Host 제품 실행 없음. 승인 의존성 준비는 39번 별도 단계 |
| §9 Version/Artifact | 초기 Source는 Candidate/Artifact로 위장하지 않음. 첫 실제 수정 Candidate 1 유지 |
| §10 최종 판정 | 준비 검사·DONE·Git 읽기 성공을 제품 SUCCESS/QA·Security PASS로 바꾸지 않음 |
| §11·11-A 재현성 | 생성 시 동결된 Commit/Snapshot/공통 환경과 대조. 빈 값을 후입력하거나 기존 Run 변경 금지 |

보류한 URL Credential 보호와 기존 공유 Agent DB 초기화 경쟁은 수정하지 않았다. 42번 보안 의미 증거·43번 비용·44번 실제 환경 실행 검증·역할 4의 비교 실험은 완료로 표시하지 않는다.

## 8. 검증

실제 임시 Git/SQLite/파일과 합성 Provider/Container를 사용한 자체 회귀다. 실제 Docker·LLM·제품 Build/Test/Scan·기업 시연 성공을 뜻하지 않는다. 전체 팀원 pytest 평가는 실행하지 않는다.

```bash
PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -p 'test_starting_source.py' -q
PYTHONPATH=src:tests .venv/bin/python -m unittest tests.test_git_snapshot tests.test_git_object_export tests.test_developer_workspace tests.test_snapshot_handoff tests.test_workspace_access tests.test_workspace_registry tests.test_workspace_api tests.test_sandbox_materialization tests.test_runtime_configuration tests.test_tool_configuration -q
PYTHONPATH=src:tests .venv/bin/python -m unittest tests.test_owned_agent_composition tests.test_owned_agent_pipeline tests.test_owned_agent_fix_pipeline tests.test_planner_developer_continuation tests.test_agent_platform_budgets tests.test_dispatch -q
PYTHONPATH=src .venv/bin/python -m compileall -q src tests
.venv/bin/pip check
git diff --check
```

최종 검증: **총 418개 모두 통과**했다. 신규 Source 준비 47개와 Git export 21개를 포함하며 중복 재실행을 합산하지 않았다.

- Source 준비/CLI 신규 **47개 / 27.759초 PASS**: SHA-1/SHA-256, 원본/dirty 파일 보존, 실제 claim 전이, Snapshot/Lock/동결 환경 불일치, 기존 전송 로그의 1,000개 이후 페이지, crash/STARTED, DONE 변조, 경로/권한, 실제 Checkpoint 호환성.
- Git Snapshot/export·Checkpoint·Snapshot 전달·Workspace·Sandbox materialization·38/39번 설정 **275개 / 28.633초 PASS**. 신규 Git export 21개가 이 묶음에 포함된다.
- 기존 자체 플랫폼 조립·초기/수정 Pipeline·Planner/Developer 연속 실행·공유 Budget·Dispatch **96개 / 84.857초 PASS**.
- 두 번째 묶음의 첫 실행에서는 임시 Unix socket 생성 1개가 Sandbox 권한 제한으로 실패했다. 테스트를 생략하거나 구현을 바꾸지 않고 승인된 권한으로 같은 275개를 다시 실행하여 모두 통과했다.
- `compileall`, `pip check`, `git diff --check`, 새 파일 trailing whitespace 검사 통과. pip cache 접근 경고는 있었으나 의존성 오류는 없다.
- `git check-ignore`로 `configs/owned-source.local.json` 제외를 확인했다. 실제 로컬 설정/승인 Repository/이미지는 생성하지 않았다.
- 미승인 Source/Tool 예시를 CLI에 전달하면 exit 1 + `TOOL_CONFIGURATION_INVALID`를 반환한다. Tool 설정을 먼저 검사하기 때문에 이 코드가 우선이며 의도된 결과다.
- 독립 읽기 전용 리뷰에서 발견한 불명확한 A2A 전송, UTF-8 식별자 상한, marker 숫자 타입, 경로 별칭/중첩을 보완하고 회귀를 추가했다. 기존 준비 callback 순서와 최소 Git/Checkpoint 호환성도 재확인했다.

실제 Docker·외부 LLM·제품 코드 실행, 전체 팀원 pytest 평가, 프로젝트 Git commit/push는 수행하지 않았다.

## 9. 커밋 메시지와 다음 작업

커밋 메시지: `승인된 시작 Source 검증과 Run별 Workspace 준비 기능 추가`

다음 작업: **41번 — 실제 실행용 Host factory 조립**. 38·39·40번을 기존 플랫폼에 연결하되 팀원 제품과 평가 모듈 연결은 포함하지 않는다.

프로젝트 Git commit/push는 수행하지 않는다. 테스트의 Git 생성/Commit은 자동 정리되는 임시 fixture에만 한정한다.
