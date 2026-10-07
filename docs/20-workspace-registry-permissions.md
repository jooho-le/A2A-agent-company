# 20. 실제 Workspace Registry·경로·역할 권한

작성일: 2026-10-07

> 범위: 1번+2번의 실제 작업 디렉터리 준비 및 파일 접근 기반.
> 기준: 개발정의서 §2·§5-4·§8-7·§8-8, 15번의 확정 후속 번호.
> Snapshot/Artifact 실물 저장·Sandbox·MCP 세션·파일 Tool·역할 Executor·팀원 서비스 연결은 후속 작업이다.

## 1. 이번에 개발한 내용

- 기존 SQLite의 Run/WorkspaceRecord를 신뢰 Registry로 재사용하여 발급된 workspace UUID를 실제 작업 폴더에 연결했다.
- 명시적 Workspace 준비 API와 소유권 Marker, 충돌/부분 준비 재시도 정책을 추가했다.
- 공개 권한 Metadata와 실제 파일 접근이 같은 역할별 정책을 사용하도록 통합했다.
- 절대 경로·Traversal·비밀 경로·외부 Symlink·Symlink Write·Hardlink·비정규 파일 접근을 차단했다.
- 신뢰된 서비스 역할을 바인딩한 파일 descriptor 접근기를 추가했다. 파일 사용 때마다 DB 식별자·소유권·레이아웃을 다시 확인한다.

새 Registry가 별도 DB나 UUID를 만드는 것은 아니다. 기존 Run 생성 흐름, A2A Task 상태, Artifact 계약 및 성공 판정은 변경하지 않았다.

## 2. 등록 Metadata와 실제 준비를 구분

| 동작 | 이번 단계의 처리 |
| --- | --- |
| import / 앱 생성 / Registry 생성 | Workspace 폴더를 만들지 않음 |
| 기존 `POST /api/v1/runs` | 기존처럼 서버 UUID 발급 및 DB Metadata 저장. Workspace 파일 준비는 별도 |
| `GET /api/v1/runs/{runId}/workspace` | ID·역할 권한만 반환. Host Root 비공개, 폴더 생성 없음 |
| 새 `POST /api/v1/runs/{runId}/workspace/provision` | 등록된 Run의 Workspace만 실제 준비 |
| `WorkspaceRegistry.bind(...)` | 준비된 Workspace와 신뢰된 역할을 검증. 미준비이면 오류, 자동 생성 없음 |

준비 API는 경로·역할 입력을 받지 않는다. Run ID로 기존 Workspace ID를 조회하고, 서버의 `ORCHESTRATOR_WORKSPACE_ROOT` 아래 정확히 그 UUID 디렉터리만 사용한다. 임의 ID 발급·Root 변경·다른 Run의 폴더 채택은 하지 않는다.

DB 등록과 파일 준비를 하나의 원자적 트랜잭션이라고 표시하지 않는다. 먼저 등록하고 명시적으로 준비하며, 준비에 실패하면 해당 Workspace 접근을 거부한다. 이후 실제 Agent 연결 단계인 34번에서는 실행 전에 준비를 호출하고 실패를 제어 오류로 처리해야 한다.

성공 응답은 기존 공개 계약에 `provisioned: true`, `layoutVersion: 1`만 추가한다. 이는 폴더 준비 성공이며 Run SUCCESS·Build PASS·검증 PASS가 아니다. 별도의 Run/Task 상태나 가짜 실행 Trace를 만들지 않는다.

## 3. 실제 레이아웃과 소유권

```text
서버에 설정된 Workspace Base/
├── .registry.lock          # Registry 내부 준비 직렬화
└── <발급된 workspace UUID>/
    ├── .workspace.json     # Registry 내부 소유권 Marker
    ├── planning/
    ├── source/
    ├── snapshots/
    └── outputs/
        ├── qa/
        └── security/
```

Marker는 `workspaceId`, `runId`, `layoutVersion`만 저장하며 Host Root나 Secret은 포함하지 않는다. 새 디렉터리/파일 생성 모드는 각각 `0700`/`0600`이다. 이미 존재하는 사용자 파일/디렉터리를 재귀 chmod하거나 덮어쓰지 않는다.

- 알려진 DB Record의 Root가 현재 서버 Base와 다르면 거부한다. Base 변경 후 기존 데이터를 자동 이동하지 않는다.
- `/`, 현재 작업 디렉터리 자체, 사용자 Home 자체 같은 넓은 Base는 거부한다. 전용 하위 디렉터리를 설정해야 한다.
- 소유권 Marker가 없는 비어 있지 않은 폴더는 채택하거나 초기화하지 않는다.
- Marker의 ID/버전/필드, private mode, 파일 종류, Hardlink 여부, 크기와 JSON을 검증한다. 중복 key·비정상 JSON·boolean 버전은 거부한다.
- 올바른 Marker가 있고 레이아웃만 일부 없으면 재호출로 완성할 수 있다. 기존 Source/출력은 보존한다.
- Marker가 깨진 경우 자동 복구·삭제하지 않는다. 운영자가 충돌 원인을 확인해야 한다.
- 프로세스 내 `RLock`으로 최초 Base/Lock 생성까지 직렬화하고, 협력하는 프로세스 간에는 `fcntl.flock`을 사용한다. 잠금 뒤 DB 식별자를 다시 확인한다.

이는 Registry를 사용하는 준비 작업의 직렬화다. 같은 OS 사용자의 임의 Shell 조작을 차단하는 Sandbox나 악성 프로세스에 대한 소유권 인증을 구현한 것은 아니다.

## 4. 역할별 접근 권한

| 역할 | Read | Write |
| --- | --- | --- |
| Planner | `planning/` | `planning/` |
| Developer | `planning/`, `source/`, `snapshots/`, QA/Security 출력 | `source/` |
| QA | `planning/`, `source/`, `snapshots/`, QA/Security 출력 | `outputs/qa/` |
| Security | `planning/`, `source/`, `snapshots/`, QA/Security 출력 | `outputs/security/` |

QA/Security의 Source Read는 개발정의서의 필요 시 Read 권한이다. 실제 검사 대상은 후속 단계에서 동일 Frozen Snapshot으로 고정해야 하며, Working Tree Read만으로 재현 가능한 검증을 달성했다고 표시하지 않는다.

어떤 Agent도 `snapshots/`에 쓸 수 없다. QA/Security는 `source/` 또는 상대 역할 출력에 쓸 수 없다. Planner에게 제품 Source Read 권한을 추가하지 않았다. Orchestrator의 Metadata 관리나 후속 신뢰 Snapshot 생성 작업을 Agent Write 권한으로 바꾸지 않는다.

역할은 신뢰 런처/서버 코드가 `AgentRole`로 선택한다. 모델의 Tool 인자나 Prompt가 역할을 선택하는 구조가 아니다. 실제 MCP Tool allowlist/역할 고정은 기존 선언을 유지하며 23번에서 런타임으로 연결한다.

## 5. 경로·파일 접근 정책

이 라이브러리의 경로는 **Workspace-relative**다. 예를 들어 `source/src/signup.py`이며, 기존 Change Report의 **Source-relative** 경로 `src/signup.py`와 구분해야 한다. 후속 Tool/Executor는 계약별 경로 기준을 명시적으로 변환해야 한다.

| 입력/파일 | 처리 |
| --- | --- |
| POSIX 절대 경로, Windows Drive/UNC, backslash | 거부 |
| `.` / `..`, 빈 component, 반복 slash, trailing slash | 정규화로 숨기지 않고 먼저 거부 |
| NUL/control 문자, URI/ADS colon, 4,096 UTF-8 bytes 초과 | 거부 |
| `.env*`, `.ssh`, `.git`, `.aws`, Secret/credential 경로, Key 확장자 | component별 casefold 검사 후 거부 |
| 내부 Symlink Read | Resolve된 실제 대상이 Workspace 내부이고 같은 역할의 허용/비밀정보 정책을 통과할 때만 허용 |
| Symlink Write | 부모·최종 component 모두 거부 |
| Hardlink 또는 디렉터리/FIFO/Unix Socket 등 비정규 파일 | 거부 |
| 일반 파일 미존재 | `FILE_NOT_FOUND` |

실제 접근은 검사한 `Path`를 호출자에게 반환해서 다시 여는 방식이 아니다. 신뢰된 Root descriptor에서 `dir_fd`로 부모를 열고 각 component에 `O_NOFOLLOW`를 적용한다. Read의 Canonical Resolution 후에도 descriptor로 다시 검증하여 Symlink 교체로 외부 파일을 여는 것을 막는다. 최종 파일은 `fstat`으로 종류와 link 수를 확인하며, 비정규 파일에 블로킹되지 않도록 `O_NONBLOCK`을 사용한다.

쓰기 접근은 검증 전에 `O_TRUNC`로 기존 내용을 지우지 않는다. 기본적으로 파일 생성과 부모 폴더 생성도 암묵적으로 하지 않는다. Context가 descriptor를 닫으므로 호출자는 descriptor를 닫거나 검사한 경로를 다시 열면 안 된다.

이 기반이 파일 내용의 완전한 불변성·동시 수정 CAS·원자적 Patch를 보장하는 것은 아니다. `expectedSha256`, 파일 크기/내용 제한, 실제 Write/Patch Tool과 변경 이력은 24번에서 구현한다.

플랫폼 기능을 확인하고 미지원이면 `WORKSPACE_PLATFORM_UNSUPPORTED`를 반환한다. 현재 검증 환경은 POSIX/macOS이며 Windows를 지원했다고 표시하지 않는다. 사용한 OS 기능과 Canonical Resolution의 공식 근거: [Python os.open/dir_fd](https://docs.python.org/3.11/library/os.html#os.open), [Path.resolve](https://docs.python.org/3.11/library/pathlib.html#pathlib.Path.resolve).

## 6. API와 라이브러리 사용

기존 Orchestrator 서버의 Swagger `/docs`에서 다음 순서로 확인할 수 있다. 실제 Agent URL은 이번 확인에 필요하지 않다.

1. `GET /api/v1/scenarios`에서 등록된 회원가입 시나리오 UUID를 확인한다.
2. `POST /api/v1/runs`에 해당 `scenarioId`와 `requestText`를 넣어 Run을 만든다.
3. 반환된 `runId`로 `POST /api/v1/runs/{runId}/workspace/provision`을 실행한다. 요청 Body·Role·Host Root는 입력하지 않는다.
4. 응답의 발급된 ID, `provisioned`, `layoutVersion`, 역할별 권한을 확인한다. 서버 전용 Base 아래 실제 레이아웃을 확인할 수 있다.
5. 같은 준비 요청을 반복해도 기존 파일을 초기화하지 않는다. `GET .../workspace`는 여전히 Metadata 조회다.

현재 API는 기존과 같은 신뢰된 로컬 MVP 관리 경계다. `workspaceId`를 아는 것이 인증을 대신하지 않으며, 공용 서비스에 그대로 노출해도 안전하다는 의미는 아니다. 외부 배포 전 인증·Run별 관리 권한을 별도로 붙여야 한다.

신뢰된 Host 코드에서 사용하는 연결 지점은 다음과 같다. MCP 호출이나 제품 개발 완료 예제가 아니다.

```python
import os

from orchestrator.domain.states import AgentRole
from orchestrator.workspaces.registry import WorkspaceRegistry

registry = WorkspaceRegistry(repository, settings.workspace_root)
registry.provision(run.workspace_id, run_id=run.run_id)
workspace = registry.bind(
    run.workspace_id, run_id=run.run_id, role=AgentRole.DEVELOPER,
)

# 파일은 별도로 존재해야 한다. 실제 read_project_file Tool은 24번이다.
with workspace.open_read("source/src/signup.py") as file_fd:
    chunk = os.read(file_fd, 4096)
```

`open_write(path, create=False)`와 `ensure_parent(path)`도 신뢰된 역할/경로 정책을 통과해야 한다. 앞으로 파일 Tool은 이 descriptor 경계를 재사용해야 한다. 읽은 Source 전체를 기본 Trace에 직접 기록해서는 안 된다.

## 7. 오류와 개발정의서 준수 점검

내부 오류는 `WorkspaceAccessError.code`로만 전달하며 요청 경로·Host Root·Secret·원래 예외 본문을 오류 문자열에 넣지 않는다. API는 미등록 Run에 404, 준비 충돌/금지된 파일 경로에 409, 잘못된 서버 설정·기타 준비 불가에 503을 반환한다. 이 제어 오류를 제품 결함이나 검증 PASS로 취급하지 않는다.

| 개발정의서 기준 | 이번 구현과 남은 범위 |
| --- | --- |
| Orchestrator Registry 발급 | 기존 Run/Workspace UUID 재사용. 임의 Host Root/모델 ID로 새 Workspace 발급하지 않음 |
| §5-4 Source/출력 권한 | 실제 scoped 파일 접근에서 강제. QA/Security Source Write 금지 |
| §8-7 경로 정책 | Canonical Read + 내부 여부 확인, Symlink Write/Traversal/Secret 경로 차단 |
| §8-8 Secret/로그 | Source/Secret 복사나 전체 Trace 기록을 추가하지 않음. API/오류에 Root 미노출 |
| §5 Frozen Snapshot | Snapshot Write 권한 없음. 실제 Archive/Hash/Artifact grant는 21번, Read-only Mount는 22번 |
| §8 MCP/Sandbox | 선언/규격 유지. 실제 세션/역할 강제23번, OS/Network 격리22번. 이번 ACL을 Sandbox로 표시하지 않음 |
| 상태·성공 판정 | 기존 A2A 상태/Workflow/Verdict 그대로. 준비 성공으로 최종 SUCCESS를 만들지 않음 |
| 규격·수정 제한 | A2A1.0·MCP2026-07-28·Fix 최대3회·MCP Retry 최대2회 변경 없음 |
| 담당 범위 | 1번+2번 기반만 변경. 역할3 서비스/역할4 실험 및 실제 팀원 연결 미작업 |

기존 Bootstrap의 `executionReady=False`와 실제 역할 미구현 REJECTED 경계는 유지한다. 이번 구현만으로 LLM이 파일을 읽고 코드를 개발하는 Agent가 완성된 것은 아니다.

앞서 제외한 `protectedTestSuiteRef`/`scannerProfileRef` Credential URL 정책 보완 및 드문 기존 SQLite 초기 WAL 잠금 문제는 이번에 변경하지 않았다. 아래 테스트 통과를 기존 이슈 해결로 표시하지 않는다.

## 8. 변경 파일과 검증 결과

- `src/orchestrator/workspaces/policy.py`: 역할 정책·경로 문법·비밀 경로·오류 코드.
- `src/orchestrator/workspaces/filesystem.py`: descriptor 기반 디렉터리/일반 파일 접근과 역할 바인딩.
- `src/orchestrator/workspaces/registry.py`: 기존 DB 식별자 검증·준비·Marker·잠금·매 접근 재검증.
- 기존 `domain/workspaces.py`, API 의존성/Run 경로, `main.py`: 공개 권한 통합 및 명시적 준비 연결.
- README/환경 예시와 신규 Workspace Registry/접근/API 테스트 3개 파일.

신규 테스트 **48개**: Registry18 + 파일 접근23 + API7. 전체 회귀 **533개 모두 통과**했다. 실제 Symlink/Hardlink/FIFO/Unix Socket, Canonical Resolve 이후 파일 교체, 역할별 Write 금지, Secret 경로, Marker 훼손, 동시 준비, 반복 준비 시 파일 보존, 준비와 Run 상태 분리를 검사했다.

최초 로컬 Sandbox 실행에서 실제 Unix Socket fixture 생성은 권한으로 차단되어, 승인된 환경에서 같은 검사를 실행하여 통과했다. 최초 동시 준비 검증 중 Base/Lock 파일 생성 오류도 발견되어 프로세스 내 최초 생성 잠금까지 보완했다. 보완 후 동시 준비 반복 검증과 전체 회귀를 통과했다. 기존 SQLite WAL 이슈와는 다른 준비 경로 문제다.

```bash
PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -p 'test_workspace*.py'
PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -q
.venv/bin/python -m compileall -q src/orchestrator/workspaces
.venv/bin/python -m pip check
git diff --check
```

`compileall`·`pip check`·diff check도 통과했다. 현재 환경에는 pytest가 없어 pytest 기반 평가 실험을 검증한 것으로 표시하지 않는다. 실제 LLM 호출·회원가입 제품 실행·Cloud/컨테이너·MCP 연동 검증은 수행하지 않았다.

커밋 메시지: `실제 Workspace Registry와 경로·역할별 접근 권한 구현`

다음 작업: **21번 — 실제 Snapshot/Artifact 저장소·Hash·접근 제어.** 현재의 Metadata를 실제 불변 산출물 저장·내용 Hash 검증·Run/역할별 접근 경계에 연결한다. Git commit/push는 수행하지 않았다.
