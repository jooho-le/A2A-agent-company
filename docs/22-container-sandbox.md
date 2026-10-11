# 22. Container Sandbox

작성 기준: 2026-10-08. 범위: 1번+2번 담당의 고정 Snapshot 실행 기반.

> Container 준비·검사·실행·정리 코드를 구현했다. 현재 환경에는 Docker가 없어 실제 Container 격리 검증은 미완료다. 기본 Agent/MCP/Dispatch 연결과 제품 Build/Test/Security PASS는 이번 단계에 포함하지 않는다.

## 1. 이번에 개발한 것

- 검증된 21번 Source Archive를 Workspace 내부 전용 실행 폴더에 안전하게 준비하는 `SnapshotMaterializer`.
- 고정 실행 명령과 자원 제한을 신뢰된 Host가 선택하는 `ExecutionProfile`·`SandboxLimits`.
- 최소 환경·빈 Docker 설정·로컬 Unix Socket만 사용하는 `DockerCLI`.
- 역할·현재 실행 Step·Snapshot·이미지·Container 설정을 검사하는 `SandboxRuntime`.
- 종료 코드/Manifest/정제된 출력 반환, 타임아웃·반복 취소·정리 실패 처리.

파일은 `src/orchestrator/sandbox/`와 전용 테스트에 추가했다. import/생성자는 DB·폴더·Docker/LLM/MCP 실행을 시작하지 않는다. 명시적으로 `await bound.run(...)`을 실행할 때만 준비와 Docker 통신을 한다. 의존성이나 Python 최소 버전은 변경하지 않는다.

## 2. 실행 순서와 권한

```text
신뢰된 Agent 역할 + 실행 중인 Run/Step
→ 같은 Run의 저장된 Source 읽기·ACL/Hash/실행환경 확인
→ 로컬 Linux Docker·Seccomp·동결 이미지 확인
→ Workspace/.sandbox/{executionId}/source 준비
→ Container 생성(자동 Pull 없음)
→ 실제 설정·Source·현재 Run/Step 다시 확인
→ 고정 명령 실행·실제 종료 상태 확인
→ 소유 Container 제거·전용 준비 폴더 정리
→ 실행 결과 반환(제품 PASS/FAIL 판정 없음)
```

Planner는 실행할 수 없다. Developer는 `run_build`/`run_unit_tests`, QA는 `run_unit_tests`/`run_browser_tests`, Security는 `run_security_scan`만 허용한다. 기존 역할별 Tool 정책을 재사용하며, 역할을 Tool 입력이나 Prompt로 선택하지 않는다.

Developer는 IMPLEMENTING/FIXING Run에서 해당 Source를 생성한 실행 중 Step이어야 한다. QA/Security는 VALIDATING/REVALIDATING Run의 실행 중 Step이어야 하고, Artifact 읽기 grant뿐 아니라 해당 Step의 `inputArtifactIds`에도 Source가 있어야 한다. 현재 Fix Attempt·Code Version이 일치하지 않거나 취소/완료된 Run/Step이면 시작하지 않는다. Container 생성 뒤에도 이를 재검사한다.

이것은 Host 라이브러리의 실행 권한 검사다. 실제 MCP `tools/list`/`tools/call` 권한 연결은 23번이다.

## 3. Snapshot 준비와 파일 경계

Source는 live `source/`가 아니라 Artifact Store에서 검증한 immutable bytes만 사용한다. Archive bytes·SHA-256·Run·Artifact URI·크기를 재검사하고, 21번과 동일한 정규화 tar 표현인지도 확인한다. 임의 `extractall()`이나 외부 Archive 다운로드는 하지 않는다.

절대/Traversal/Secret 경로, Symlink/Hardlink/특수 파일, 중복 경로, 대소문자·NFKC 정규화 충돌, 파일/디렉터리 충돌, 숨겨진 추가 Archive/Header를 거부한다. Source/Inputs는 각각 최대 1,000파일·파일당 1MiB·합계 16MiB·디렉터리 4,096개·경로 128개 component이며, Source Archive는 최대 20MiB다.

전용 부모 폴더는 0700, 소유권 Marker는 0600이며 Container에 Mount하지 않는다. Source/Inputs 디렉터리는 0755, 파일은 0444/0555로 준비한다. 선택적 `inputs`는 신뢰된 Host가 제공한 경로→bytes Mapping을 별도 `/inputs`에 Read-only로 넣으며 Source를 덮어쓰지 않는다. 실제 보호 Test/Scanner Profile 입력 조립은 후속 Tool 단계의 책임이다.

시작 전 실제 파일 목록·Hash·Mode·Marker를 다시 확인한다. 정리도 소유 UUID·Marker·실제 파일을 검사한 후 descriptor-relative `os.unlink`/`os.rmdir`로 해당 실행 폴더만 삭제한다. Python 3.10에서 없는 `shutil.rmtree(dir_fd=...)`나 unsafe 경로 삭제 fallback은 사용하지 않는다. 필요한 POSIX API가 없으면 준비 전에 중단한다. Symlink·교체된 inode·변조 상태에서 광범위 삭제로 넘어가지 않는다. 사용자 `source/`·등록 Artifact·다른 실행 폴더는 삭제하지 않는다. 동일 OS 사용자로 이미 실행 중인 악성 Host 프로세스까지 격리한다는 주장은 하지 않는다. [Python 3.10 os API](https://docs.python.org/3.10/library/os.html)를 기준으로 했다.

## 4. Container 정책

| 항목 | 구현 |
| --- | --- |
| 실행 | 일회용 Container, 자동 재시작 없음, 실행 후 명시적 제거 |
| 이미지 | 동결 SHA-256 확인, 검증한 local Image ID로 실행, `--pull never` |
| Source | 준비 폴더만 `/snapshot`에 Read-only bind Mount |
| 입력 | 필요할 때 `/inputs` 별도 Read-only bind Mount |
| Root FS | `--read-only` |
| Write | Container의 `/work`, `/output`, `/tmp` tmpfs만 |
| Network | `--network none`, Host Port/Link/Extra Host 없음 |
| 사용자 | `10001:10001`, Privileged 금지, 모든 Capability drop |
| 권한 상승 | `no-new-privileges`, 기본 Seccomp 지원 엔진만 |
| Namespace | Host PID/IPC/UTS/Network 공유 없음, private cgroup namespace |
| 자원 | CPU·Memory·Swap·PID·tmpfs·시간·출력 제한 |
| 부가 실행 | 이미지 ONBUILD/Volume 거부, Healthcheck·Docker Log Driver 비활성화 |
| Host Mount | 등록 Workspace 내부의 Source/Inputs만, Socket·Secret·추가 Mount 없음 |

생성 요청만 믿지 않는다. 시작 전 `container inspect`에서 실행 ID/Run/Artifact/역할 label, Container 이름/ID·Image·User·명령·환경·Mount·Root/Network/자원 제한을 확인한다. 설정 불일치면 실행하지 않는다. 이 CLI 옵션의 공식 의미는 [Docker container create](https://docs.docker.com/reference/cli/docker/container/create/)와 [Bind mounts](https://docs.docker.com/engine/storage/bind-mounts/)를 기준으로 했다. 실제 사용 엔진의 inspect 표현 및 자원 강제는 Docker 준비 후 별도 검증해야 한다.

### 이미지 Digest의 두 표현

Host Profile의 `image_reference`가 없으면 기존 `containerImageDigest`를 local Image Config ID(`sha256:…`)로 조회하고 `image.Id`와 정확히 비교한다.

Registry Manifest Digest를 동결한 경우 Host가 `repository@sha256:…`를 Profile에 명시해야 한다. suffix가 동결 Digest와 같고 실제 `RepoDigests`에 해당 reference가 있어야 하며, 실행에는 조회된 별도 Config ID를 사용한다. Manifest Digest와 Config ID를 같은 값으로 간주하지 않는다. 태그만 사용하거나 자동 Pull/이미지 교체는 금지한다. [Docker image pull](https://docs.docker.com/reference/cli/docker/image/pull/)의 Digest 고정 방식을 참고했다.

### 로컬 Docker와 환경

POSIX Host와 로컬 Unix Socket·Linux Container만 지원한다. 기본 endpoint는 `unix:///var/run/docker.sock`이며, 신뢰된 Host는 `DockerCLI(endpoint=...)`로 다른 로컬 Socket을 지정할 수 있다. 원격 TCP/SSH 엔진·사용자 Docker Context 자동 선택은 지원하지 않는다.

Docker CLI는 shell 없이 실행하고 stdin을 닫는다. Host API Key·HOME·Proxy·DOCKER 설정·Loader 환경은 상속하지 않으며 각 호출에 빈 전용 `--config`를 사용한다. Host Socket은 제어용으로만 쓰고 Container에 Mount하지 않는다. 이미지 Environment는 제한된 일반 변수만 허용하며, 실행 HOME/Cache/임시 경로는 Container tmpfs로 고정한다. 이미지 자체가 비밀정보를 포함하지 않는지는 신뢰된 이미지 준비 과정에서 보장해야 한다.

`networkPolicy=ALLOWLIST`는 아직 명시적 설정 오류다. DENY를 bridge Network로 바꿔 실행하지 않는다. 의존성은 승인된 고정 이미지/별도 준비 단계로 제공해야 하며, 실행 중 자동 설치를 위해 Network를 열지 않는다.

## 5. 자원·시간·출력 제한

다음은 개발정의서의 팀 확정값이 아니라 제한 없는 실행을 방지하는 임시 Host 기본값이다. 실제 Build 측정 후 Tool별 Profile에서 확정한다.

| 항목 | 임시 기본값 |
| --- | --- |
| CPU | 1 CPU |
| Memory | 512MiB, Memory Swap 총량도 동일 |
| PID | 128 |
| 실행 전체 Deadline | 60초 |
| 개별 제어 명령 | 최대 10초, 남은 Deadline 이하 |
| tmpfs | `/work`, `/output`, `/tmp` 각각 64MiB |
| stdout/stderr | 각각 1MiB |

bool, 정수 한도 항목의 비정수, 범위 초과·NaN·무한대는 거부한다. CPU·시간에는 범위 내 유한 소수 값을 허용한다. Container/제어 명령의 공통 Deadline은 Run의 `runtimeBudgetMs` 이하로 제한하지만, 동기 Snapshot 준비·안전 정리를 포함한 호출 전체 wall-clock 상한과 영속 누적 Run 예산을 보장하지 않는다. 앞선 호출까지 합친 영속 Run 예산은 34/37번에서 연결해야 한다. Snapshot 파일 준비는 크기가 제한된 동기 작업이므로 Host 디스크 I/O까지 즉시 중단하는 hard real-time 보장은 아니다. Container 실행 Deadline이 지나면 종료·정리하고, 안전 정리는 실행 Deadline 밖에서도 수행한다.

## 6. 종료·취소·오류

Docker CLI 타임아웃/출력 초과/취소는 해당 CLI 프로세스 그룹을 종료·회수한다. CLI 종료만으로 Daemon의 Container가 사라지는 것은 아니므로 Runtime이 생성한 이름으로 다시 조회한다. 실행 label·이름·ID의 소유권이 일치할 때만 그 ID를 `rm --force`한다. `create`가 반환한 임의 ID를 그대로 삭제 대상으로 신뢰하지 않는다. 반복 취소에도 정리를 끝낸 뒤 취소를 전달한다.

Container 소유권을 확인할 수 없거나 제거 실패하면 `SANDBOX_CLEANUP_FAILED`와 안전한 `executionId`를 반환하고 준비 폴더를 보존한다. 운영자는 해당 실행 ID의 label/상태를 신뢰된 관리 환경에서 확인해야 한다. 자동 Retry·자동 복구·광범위 Container 삭제는 하지 않는다. Host 자체 종료/전원 장애 이후 orphan 복구 서비스는 이번 단계에 포함하지 않는다.

정상 종료는 실제 Container `State.ExitCode`와 attached CLI 종료 코드를 확인한다. 종료 코드가 0이 아니어도 제품 명령의 실제 결과일 수 있으며, 시작 실패·OOM·Daemon 상태 오류는 실행 오류로 구분한다. 이것을 Build/QA/Security PASS/FAIL로 판정하지 않는다. `SandboxResult`에는 저장 Source의 `ExecutionManifest`, 실제 Image/Container ID, 종료 코드·시간·정제된 stdout/stderr를 담는다. [Docker container start](https://docs.docker.com/reference/cli/docker/container/start/)의 attach 실행을 사용한다.

출력은 길이를 제한하고 기존 `redact_text` 정책으로 정제하며 repr에는 넣지 않는다. Source 전체·Host Root·원본 CLI 오류를 Trace에 자동 저장하지 않는다. 임의 파일 내용 속 모든 비밀정보를 찾아내는 완전한 Secret Scanner로 표시하지 않는다.

Container Output 파일은 tmpfs에만 존재하고 이번에는 Host로 export하지 않는다. tmpfs는 Container가 멈추면 사라지므로 종료 후 `docker cp`로 보고서 파일을 회수하는 구조라고 설명하지 않는다. 현재 전달 경계는 제한된 stdout/stderr이며, 구조화된 보고서 회수·Artifact publication·MCP 오류 형식/ToolEvidence/Retry는 25~29번에서 이어간다. [Docker tmpfs mounts](https://docs.docker.com/engine/storage/tmpfs/)의 수명 기준을 따른다.

주요 오류: `SANDBOX_UNAVAILABLE`, `SANDBOX_PERMISSION_DENIED`, `SANDBOX_CONFIGURATION_REQUIRED`, `SANDBOX_IMAGE_MISMATCH`, `SANDBOX_INTEGRITY_ERROR`, `PATH_DENIED`, `SANDBOX_TIMEOUT`, `SANDBOX_OUTPUT_LIMIT`, `SANDBOX_EXECUTION_ERROR`, `SANDBOX_CLEANUP_FAILED`.

## 7. 신뢰된 Host 사용 예

아래는 준비된 Workspace·실행 중인 Developer Step·실제 동결 Source·승인된 로컬 이미지가 있는 상황의 라이브러리 연결 예다. 현재 프로젝트에 `scripts/build.py`가 구현되어 있다는 의미가 아니며, 실제 명령은 25번 Host Profile에서 선택한다. 모델/MCP 입력에서 Profile의 `argv`를 직접 받으면 안 된다.

```python
from orchestrator.domain.states import AgentRole
from orchestrator.sandbox.contracts import ExecutionProfile
from orchestrator.sandbox.runtime import SandboxRuntime

runtime = SandboxRuntime(repository, workspace_registry, artifact_store)
developer = runtime.bind(run.run_id, role=AgentRole.DEVELOPER)
profile = ExecutionProfile(
    name="approved-build",
    tool_name="run_build",
    argv=("/usr/local/bin/python", "-B", "/snapshot/scripts/build.py",
          "--output-dir", "/output"),
)
result = await developer.run(source.artifact_id, profile)
# 실제 exit_code와 정제된 stdout/stderr일 뿐 제품 PASS 판정은 아님.
```

## 8. 개발정의서 준수 점검

| 개발정의서 기준 | 구현/남은 범위 |
| --- | --- |
| §2 역할 경계 | Orchestrator 판정/Agent 역할 보존. 제품 코드 Host 실행·MCP 성공 판정 없음 |
| §5 동일 Frozen Snapshot | 실제 Artifact/Hash/Manifest·현재 Version·QA/Security handoff 검사 |
| §8-7 Filesystem | Workspace 내부 전용 준비 폴더, 링크·Traversal·Secret 경로·Socket Mount 차단 |
| §8-8 Secret/Trace | 최소 CLI 환경·출력 정제·원문 Source 미기록. 이미지/내용 전수 Secret 검사는 별도 |
| §8-9 Network | DENY 강제. ALLOWLIST/Dependency Preparation은 미지원으로 중단 |
| §8-10 Sandbox | Ephemeral/Read-only/자원/PID/Timeout/비특권 정책 구현. 실제 Docker 검증은 미완료 |
| §8-11 역할 Tool | 실행 Profile allowlist 적용. MCP 노출·입력 Schema 연결은 23번 |
| §10 성공 판단 | 실행 결과를 제품 PASS/SUCCESS로 표시하지 않음 |
| 기존 규격/한도 | A2A 1.0·MCP 2026-07-28·Fix 최대3·MCP Retry 최대2 변경 없음 |
| 담당 범위 | 1번+2번만. 3번 서비스·4번 평가 모듈·팀원 연결 미변경 |

기본 Agent `executionReady=False`/미구현 REJECTED 경계는 유지한다. 실제 역할 Executor 30~33번, 본인 Agent의 기존 Orchestrator 연결 34번, Trace 연결 37번은 아직 후속 작업이다. 기존 `protectedTestSuiteRef`/`scannerProfileRef` Credential URL 정책과 드문 Agent DB 초기 WAL 잠금 문제는 수정하지 않았다.

## 9. 검증·커밋·다음 작업

신규 검증은 실제 tempfile Git·SQLite·파일·역할 권한과 Fake Docker CLI/응답을 사용한다. 실제 컨테이너 또는 생성된 제품 코드를 Host에서 실행하지 않는다.

신규 전용 테스트 **90개 모두 통과**했다(14.432초): Docker CLI24 + Materialization29 + Runtime37. 안전한 Source/Inputs 준비·실제 내용 변조·경로/링크·역할/Step/동일 Source·Container 설정 변조·이미지 ID/Digest 구분·시간/출력 제한·프로세스 생성 중 취소·큰 출력 pipe 정리·반복 취소·소유권 없는 Container 삭제 금지·정리 실패 시 보존을 확인했다. Python 3.10에 없는 `shutil.rmtree(dir_fd=...)` 의존과 프로세스 핸들 전달 전 취소/대용량 pipe 회수 경계를 최종 리뷰에서 보완했다.

```bash
PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -p 'test_sandbox_*.py' -q
PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -q
.venv/bin/python -m compileall -q src/orchestrator/sandbox
.venv/bin/python -m pip check
git diff --check
```

최초 전체 실행은 기존 Unix Socket fixture가 Sandbox 권한에 막혀 710개 중 1개 오류였다. 해당 테스트를 삭제/완화하지 않고 동일 명령을 승인된 환경에서 재실행해 **710개 모두 통과**했다(39.952초). 최종 추가 회귀까지 포함한 다음 전체 실행은 **718개 중 기존 테스트1개 오류**였다(40.959초): `test_agent_task_store.test_concurrent_initial_roles_cannot_share_a_new_database`의 기존 `PRAGMA journal_mode=WAL` 동시 초기화에서 `sqlite3.OperationalError: database is locked`가 재발했다. 신규90개는 모두 통과했고 기존 Agent DB 코드는 변경하지 않았다. 재실행이 통과하더라도 기존 간헐 잠금 문제가 해결되었다고 표시하지 않는다.

최종 전체 재실행은 **718개 모두 통과**했다(39.807초). 위 WAL 잠금 재발은 그대로 기존 미해결 이슈로 남긴다.

`compileall`·`pip check`·`git diff --check`도 통과했다. 현재 Python 3.11.9에서 검사하며, Python 3.10 지원 API를 유지하되 별도 3.10 인터프리터 실행을 완료했다고 주장하지 않는다. pytest 기반 4번 평가 실험을 검증한 것으로 표시하지 않는다.

실제 Docker 검증은 별도 준비가 필요하다: 승인된 로컬 Linux 엔진·고정 이미지, 실제 inspect 호환성, Source/Root 쓰기 거부, tmpfs 쓰기, 외부 Network 차단, 자원 제한, 타임아웃/취소 시 Container 제거를 확인해야 한다. 현재 Docker가 없을 때는 `SANDBOX_UNAVAILABLE`로 중단하며 Host fallback이나 가짜 성공을 반환하지 않는다. 실제 LLM/MCP/회원가입 시연/평가 실험 검증은 수행하지 않았다.

커밋 메시지: `고정 Snapshot 기반 Container Sandbox와 실행 제한·안전 정리 구현`

다음 작업: **23번 — MCP stdio 서버·공통 Schema·역할별 Tool 노출·권한 강제.** 실제 Build/Test/Scan Tool 구현은 25~28번으로 이어진다. Git commit/push는 직접 수행하지 않는다.
