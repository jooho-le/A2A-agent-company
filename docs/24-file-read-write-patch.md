# 24. 파일 Read/Write/Patch Tool

> 범위: 1번+2번 중 실제 MCP 파일 처리. 3번 제품 웹 개발·4번 독립 평가/비교 실험 및 해당 연동은 변경하지 않는다.
>
> 23번의 stdio 통신·Schema·역할 권한에 실제 파일 Handler를 연결했다. Build/Test/Scan/Report Handler, 실제 역할 Agent, 기본 Orchestrator 연결은 후속 번호다.

## 1. 구현 결과

| Tool | 실제 처리 |
| --- | --- |
| `read_project_file` | UTF-8 파일 내용·SHA-256·실제 byte 수 반환. QA/Security Source는 선택된 Frozen Snapshot에서 읽음 |
| `write_source_file` | Developer의 `source/` 파일 생성/교체. 선택적 기존 Hash 확인, 실제 변경 여부 반환 |
| `write_test_file` | QA의 `outputs/qa/tests/` 파일만 생성/교체. Source/Report/보호된 기준 변경 금지 |
| `apply_patch` | Developer Source 대상 unified diff. 선택된 기준 Snapshot 및 수정 대상 전체 파일을 확인한 후 여러 파일 수정/생성/삭제 |

실제 CLI도 이 Handler를 등록한다. 따라서 파일 Tool의 implemented metadata는 `true`가 된다. 나머지 미구현 Tool은 계속 `TOOL_NOT_IMPLEMENTED`이며 가짜 Build/Test PASS를 반환하지 않는다. 기본 Agent의 `executionReady=False`와 미구현 작업 거절 상태는 유지한다.

파일 읽기/쓰기는 Host에서 수행하지만 **생성된 코드를 실행하지 않는다.** 파일 Tool에서 Python import, Git commit, Shell, Build/Test/Scan을 실행하는 기능은 없다. 검증용 임시 Git repository에만 Fixture commit을 만들며 사용자의 Git 커밋/푸시는 수행하지 않는다.

## 2. 경로와 역할 경계

Developer의 Source 쓰기는 `source/...`만 허용한다. QA Test 쓰기는 기존 QA Output 안의 `outputs/qa/tests/...`로 더 좁힌다. `outputs/qa/report.json`을 Test Tool로 덮어쓰지 않으며 Report는 별도 Artifact 계약으로 발행해야 한다.

읽기/쓰기 모두 기존 Workspace Registry와 역할 capability를 사용한다. Absolute Path, `..`, Secret Path, Workspace 밖 접근, Hardlink/비정규 파일은 차단한다. 쓰기는 모든 parent/leaf를 FD 기준 `O_NOFOLLOW`로 검사한다. 원본을 검사하기 전에 truncate하지 않는다. 확인한 Path를 나중에 일반 open으로 다시 여는 구조가 아니다.

Developer의 Working Read는 Workspace 안의 허용된 실제 대상으로 resolve/권한 재검사를 하는 내부 symlink 읽기를 유지한다. QA/Security의 일반 Planning/Output 읽기는 **모든 symlink component를 거부**한다. `outputs/qa/link.py → source/signup.py` 같은 경로로 Frozen Source 정책을 우회하지 못하게 하기 위한 보수적 제한이다. 전역 Workspace 권한을 변경한 것은 아니다.

파일 교체에 사용하는 `.mcp-write-...` private staging 이름은 읽기/쓰기 경로에서 예약한다. 다른 경로의 symlink로 staging 파일에 접근하는 시도도 거부한다. 같은 OS 사용자 권한을 가진 임의 악성 프로세스까지 격리하는 Sandbox를 뜻하지 않는다.

## 3. Frozen Snapshot 읽기

QA/Security가 `source/signup.py`를 요청하면 Working Copy가 아니라 Host가 선택한 Source Artifact archive에서 해당 파일을 읽는다. 선택이 없으면 `SNAPSHOT_REQUIRED`이며 Working Copy/latest Snapshot으로 자동 fallback하지 않는다.

`FrozenSourceSelection`은 `project_artifact_id`와 `snapshot_sha256`의 불변 Host 설정이다. `MCPChildConfiguration.frozen_source`에 전달하며 CLI의 `--source-artifact-id`, `--source-snapshot-sha256` 두 인자를 함께 사용한다. 두 값은 Orchestrator/Artifact Store가 발급한 metadata에서 가져와야 하며 모델이 Tool Input으로 지정하는 값이 아니다. 기존 Tool Schema에 새 Source 선택 필드를 추가하지 않았다.

명시적으로 Frozen bytes를 읽을 때는 `snapshots/<선택된 Artifact UUID>/source/signup.py`라는 논리 경로를 사용할 수 있다. 선택된 UUID 외 Artifact는 거부하며 실제 mutable `snapshots/` 디렉터리 내용을 읽지 않는다. Developer도 이 alias로 지정된 기준 Snapshot을 읽을 수 있다.

매 호출마다 다음을 확인한다.

1. 실제 Artifact Store의 Run·Workspace 바인딩과 QA/Security Source grant.
2. 선택된 Artifact ID, Source type, archive media type, Source metadata/Hash.
3. 실제 저장 bytes의 전체 archive SHA-256와 size.
4. **전체** canonical tar 구조·경로·정렬·중복·regular file·권한/크기 규칙.
5. 요청한 파일 존재 여부와 원본 bytes.

Archive를 Host에 풀거나 tar 내부 링크/실행 코드를 실행하지 않는다. 파일 한 개만 안전해 보인다고 나머지 archive 검증을 건너뛰지 않는다. QA/Security에 같은 `FrozenSourceSelection`을 전달하면 Working Copy 수정 이후에도 같은 bytes를 읽는다. 실제 두 Agent에 이 선택을 배정하는 전체 Task 연결은 30~34번이다.

SnapshotReader는 binary bytes도 그대로 유지하지만 `read_project_file`의 content는 문자열이므로 UTF-8이 아닌 파일은 `FILE_ENCODING_ERROR`로 응답한다. 정상 binary Snapshot을 Source 변조라고 판정하거나 손실 디코딩하지 않는다.

## 4. Hash와 파일 쓰기

- `read_project_file.sha256`: 해당 파일의 원본 UTF-8 bytes Hash.
- `expectedSha256`: 선택적 기존 파일 Hash. 이 인자를 제공했을 때 기존 파일이 없거나 Hash가 다르면 `WRITE_CONFLICT`. 인자를 생략하면 호출자가 지정한 기존 Hash 비교 없이 쓰며, transaction 도중 파일 상태 변경 검사는 유지한다.
- `baseSnapshotSha256`: **전체 정규화 Source archive** Hash. 개별 파일 Hash/Git Commit Hash가 아니다.

동일 내용 쓰기는 `changed=false`이며 불필요하게 파일을 교체하지 않는다. 새로운 파일은 mode0600, 기존 파일은 일반 rwx/executable mode를 유지하되 SUID/SGID 비트는 복사하지 않는다.

각 Working Read/Write는 Workspace root inode의 cooperative `flock`을 사용한다. 잠금 충돌은 자동 대기/재실행하지 않고 `WRITE_CONFLICT`다. 쓰기는 같은 parent에 새 bytes를 staging하고 fsync한 후 FD 상대 경로로 교체한다. 이는 단일 파일 rename의 원자성으로, 여러 파일이나 전원 손실까지 하나의 transaction이라는 뜻은 아니다. [Python os.replace 기준](https://docs.python.org/3/library/os.html#os.replace)을 따랐다.

Source-aware 검사를 재사용한다. 정상 `password=request.password` 코드와 줄바꿈/Unicode bytes는 보존하고, 알려진 credential literal은 읽기/쓰기에서 `SECRET_DENIED`로 거부한다. 원본 Source를 마스킹해서 바꾼 문자열에 원래 Hash를 붙이지 않는다. Client도 읽기 content의 실제 Hash와 byte 수를 다시 계산해 결과와 비교한다.

## 5. Patch 처리

다음 순서로 처리하며 모델 입력을 `git apply`나 Shell로 실행하지 않는다.

1. 크기가 제한된 unified diff를 파싱하고 모든 대상 Source 경로를 검사.
2. 선택된 불변 Source archive를 한 번 검증하고 기준 archive Hash 확인.
3. 변경 대상 **전체 파일 내용**이 해당 Frozen base와 같은지 비교. 추가 파일은 실제 부재 여부 확인.
4. hunk 위치·줄 수·기존 내용과 정확히 일치할 때만 결과 bytes 생성. Fuzz/offset fallback 없음.
5. 생성된 전체 결과에도 Secret 검사 수행. 다른 파일의 일반 Working 변경은 유지.
6. 하나의 Workspace write lock 안에서 모든 대상의 예상 Hash/부재 상태를 다시 검사.
7. 모든 경로/내용을 사전 검증·staging한 후 파일별 교체/삭제. 일반 실패는 원본 backup으로 rollback.

실제 수정된 경로는 `changedFiles`, 남아 있는 파일의 Hash는 `newHashes`로 반환한다. 삭제한 파일은 `newHashes`에서 제외한다. 삭제를 빈 파일 쓰기나 빈 bytes Hash로 표현하지 않는다.

지원 범위는 일반 UTF-8 unified diff, 여러 파일, `/dev/null` 추가/삭제, 정확한 hunk, EOF no-newline marker, matching `diff --git`/index 및 일반100644 추가/삭제 metadata다. CRLF body bytes도 보존한다. [공식 Git diff 형식](https://git-scm.com/docs/diff-format) 중 이 제한된 부분을 구현했다.

Quoted/escaped Git 파일명·timestamp header·rename/copy·binary patch·mode 변경·텍스트 hunk가 없는 mode-only patch는 지원하지 않고 `PATCH_FAILED`로 거부한다. 전체 Git patch 형식 호환을 주장하지 않는다. 필요한 비ASCII 파일명은 quoting 없는 UTF-8 경로를 사용한다.

## 6. 오류·시간·복구 제한

오류는 23번의 Protocol/Execution 구분을 유지한다. Schema/역할 외 Tool 호출은 JSON-RPC error, 실제 파일 작업 실패는 `resultType: complete` 및 `isError: true`다. Handler의 실패를 안전한 코드로 전달하도록 `MCPToolExecutionError`를 추가했다.

| 오류 코드 | 의미 |
| --- | --- |
| `FILE_NOT_FOUND` | 요청한 파일 없음 |
| `FILE_TOO_LARGE` | 파일/변경/Archive 자원 제한 초과 |
| `FILE_ENCODING_ERROR` | 문자열 Tool로 읽을 수 없는 비UTF-8 파일 |
| `PATH_DENIED` | 경로·영역·링크·특수 파일 정책 위반 |
| `WRITE_CONFLICT` | 예상 Hash/파일 상태 변경 또는 cooperative 잠금 충돌 |
| `WRITE_FAILED` | 단일 파일 쓰기 실패 |
| `BASE_MISMATCH` | 선택된 기준 Hash 또는 영향받는 Working file 내용 불일치 |
| `PATCH_FAILED` | 지원하지 않는/잘못된 diff, hunk 불일치, Patch 적용/rollback 실패 |
| `SNAPSHOT_REQUIRED` | Host-selected Frozen Source 없음 |
| `SNAPSHOT_INTEGRITY_ERROR` | 실제 Source metadata/Hash/canonical archive 검증 실패 |
| `SECRET_DENIED` | 알려진 credential literal 포함 |

원본 Source·diff·Host 경로·기저 예외 원문을 메시지/Trace에 출력하지 않는다. Bound Client는 현재 안전한 `MCP_CLIENT_TOOL_FAILED`로 실패를 전파한다. 상세 실행 근거·오류 분류·안전 Retry·영속 Trace는 29/37번이다.

파일 1MiB, Patch 입력 1MiB, 한 번의 Patch 64파일/4096hunk, Patch/파일 LF 줄 수 131072, 변경 전·후 bytes 각각 16MiB, parent 깊이 64/신규 directory 64, Source archive 20MiB/1000파일/전체 파일 16MiB 제한을 적용했다. 기존 JSON/stdio 제한과 호출 deadline은 유지한다. 운영 수치를 팀이 실측해 확정했다는 의미는 아니다.

일반 timeout/취소 시 파일 작업 worker를 방치하지 않고 transaction 및 정리가 끝날 때까지 회수한다. 이미 파일 교체가 끝난 뒤 취소되면 그 변경을 자동으로 되돌리는 것은 아니며 안전 근거 없이 다시 호출하지 않는다. 이 제한은 arbitrary Host 작업의 기계적 hard timeout을 보장하지 않는다.

여러 파일 Patch는 **crash-atomic이 아니다.** 프로세스 강제 종료·전원 손실·외부 비협조 writer·지속적인 storage failure에서 일부 교체/복구 실패가 가능하다. rollback 실패 시 성공을 반환하지 않고 private 원본 backup을 보존한다. `.mcp-write-...` 잔여 파일은 임의로 지우지 말고 운영자 복구 대상으로 취급해야 한다.

Snapshot freeze/Git commit은 이 파일 lock에 아직 연결하지 않았다. Host가 파일 transaction이 끝난 다음 Source를 commit/freeze하도록 직렬화해야 한다. 실제 Developer 실행기의 연결은31번에서 계속 구현하며, 지금 동시에 capture해도 안전하다고 표시하지 않는다.

## 7. 사용 예시

다음은 Developer 역할로 고정된 기존 Host-issued binding/configuration이 있는 신뢰된 호출 코드다. 임의 Run/Workspace ID를 만들어 쓰는 standalone 예제가 아니다.

```python
from mcp_tools.client import open_mcp_client

async def edit_source(configuration, workspace_id):
    async with open_mcp_client(configuration) as client:
        written = await client.call_tool("write_source_file", {
            "workspaceId": str(workspace_id),
            "path": "source/example.py",
            "content": "def example():\n    return 1\n",
        })
        return await client.call_tool("read_project_file", {
            "workspaceId": str(workspace_id),
            "path": written["path"],
        })
```

QA/Security Source Read 및 Developer Patch에는 실제 `CodeSnapshotArtifact`에서 받은 `FrozenSourceSelection`을 configuration에 추가한다. 요청의 `workspaceId`/기존 Tool 필드 외에 Role·Run·Host Path·Source 선택 정보를 넣으면 Schema에서 거부한다.

## 8. 변경 파일

| 파일 | 역할 |
| --- | --- |
| `src/mcp_tools/tools/file_io.py` | FD 기반 실제 Working bytes 처리, lock/CAS/staging/rollback |
| `src/mcp_tools/tools/patching.py` | pure unified diff parser·exact byte patch |
| `src/mcp_tools/tools/snapshots.py` | Host selection·실제 Artifact/canonical archive 검증·Frozen Read |
| `src/mcp_tools/tools/files.py` | 네 실제 Handler·역할/Frozen 분기·worker 회수 |
| `src/mcp_tools/runtime.py` | 안전한 파일 Execution Error 전달 |
| `src/mcp_tools/client.py`, `__main__.py` | Host Source selection 전달, 실제 Handler 등록, Client Hash 확인 |
| 신규 `tests/test_mcp_file_io.py`, `test_mcp_patching.py`, `test_mcp_file_snapshots.py`, `test_mcp_files.py` | 실제 파일·Git·DB·stdio 및 오류/경로/복구 검증 |
| 기존 MCP Client/Server 테스트·README | 실제 Handler 등록 후의 기대 동작 및 안내 갱신 |

새 의존성/Lock 변경, 제품 DB/React/backend/evaluation 수정은 없다.

## 9. 개발정의서 점검

| 기준 | 확인 |
| --- | --- |
| §5 동일 Frozen Source/Read-only | Host-selected exact Artifact/Hash, 실제 Source grant와 bytes 확인. 검사 역할 Working fallback/symlink 우회 금지 |
| §8-1/3~5 규격·Tool 계약 | 기존 MCP/SDK/Schema/이름/필드 유지. `write_test_file` output에서 sizeBytes 제외 |
| §8-2 Workspace 발급 | 기존 Run/Workspace Registry binding 재사용. 모델 Host Path/Role/Source 선택 금지 |
| §8-6 오류 형식 | Protocol Error와 Tool Execution Error 분리. 실패를 PASS로 바꾸지 않음 |
| §8-7 파일 접근 | FD/no-follow/상대 경로/Secret/특수 파일/Hardlink 제한. QA Test 영역만 Write |
| §8-8 민감정보 | 원본 code bytes 보존 또는 알려진 credential 거부. Source/diff를 Trace에 직접 저장하지 않음 |
| §8-9/10 실행 환경 | 생성 코드·Shell·Build/Test/Scan Host 실행 없음. Container 실제 Tool 연결은25~28번 |
| §8-11 역할 | Planner0/Developer3/QA2/Security1 실제 파일 Handler. 다른 Tool 계약을 새 권한으로 확대하지 않음 |
| §4 Retry/수정 | 자동 재시도 없음. 기존 수정3회/MCP Retry2회 한도 변경 없음 |
| 담당 범위 | 1번+2번만, 3번·4번 제품/독립 검증 및 연동 없음 |

정의서 전체 완료 선언이 아니다. Product QA/Security 실행·MCP 실행 근거/Retry·네 역할 Agent·기존 Pipeline의 실제 연결은 기존 후속 번호에서 구현한다. 사용자가 이전에 제외한 기존 비밀번호 보호 정책 보완도 이번 범위에 추가하지 않았다.

## 10. 검증

```bash
PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -p 'test_mcp_*.py' -q
PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -q
PYTHONPATH=src .venv/bin/python -m compileall -q src/mcp_tools
.venv/bin/pip check
git diff --check
```

최종 검증 결과:

- MCP 관련 **312개 모두 통과**(22.428초). 신규 파일 I/O 58개·Patch 51개·Snapshot 24개·통합 22개를 포함한다.
- 전체 회귀 **1,030개 모두 통과**(69.241초). Unix socket fixture 권한을 승인받아 제한 밖에서 실행했다.
- `compileall`, `pip check`, `git diff --check` 통과. 새 의존성은 없다.

최초 전체 실행은 테스트 추가 도중 수집된 1,029개 중 기존 테스트 2개 오류였다. 기존 Agent DB 동시 초기화의 `PRAGMA journal_mode=WAL`에서 `database is locked`가 재발했고, Unix socket fixture 생성은 Sandbox 권한에 막혔다. 관련 테스트를 삭제/완화하거나 DB 코드를 변경하지 않고 전체를 다시 실행했다. 재실행은 통과했지만 **기존 간헐 WAL 경쟁은 미해결 이슈**다. A2A SDK의 기존 경고도 남아 있다.

실제 임시 Workspace/SQLite/Git/Snapshot, 다른 프로세스의 flock, 실제 stdio 파일 호출을 검증했다. 제품 코드를 실행하거나 외부 LLM·Docker·회원가입 시연·독립 평가를 완료했다는 뜻은 아니다.

## 11. 다음 작업

다음 작업: **25번 — Build Tool.** 이번에 만든 Source 파일을 기준으로 실제 불변 Snapshot·Container 실행·Build 결과를 연결한다. Host에서 생성 코드를 실행하거나 성공 결과를 임의로 만들지 않는다.

커밋 메시지 제안: `MCP 파일 읽기·쓰기·Patch와 Snapshot 검증 구현`

Git commit/push는 직접 수행하지 않는다.
