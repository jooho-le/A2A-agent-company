# 21. 실제 Snapshot/Artifact 저장소·Hash·접근 제어

작성일: 2026-10-07

> 범위: 1번+2번의 실제 산출물 내용 저장 및 신뢰된 접근 라이브러리.
> 기준: 개발정의서 §5·§8-7/8-8·§9·§10, 15번의 확정 후속 번호.
> 실제 Container Mount22번, MCP23번, 역할 Executor30~33번, 기존 Dispatch 연결34번 및 3/4번 담당은 이번 범위에 포함하지 않는다.

## 1. 이번에 개발한 내용

- 발급된 Workspace의 실제 Git Commit/Tree/Blob에서 정규화한 Source Archive와 SHA-256을 만든다.
- 기존 SQLite에 실제 Archive/보고서 JSON bytes를 저장하는 불변 BLOB 테이블을 추가한다.
- 내용·Metadata Hash, 크기·media type·Run/Step 식별자를 읽을 때 재검증한다.
- Source 후보와 QA/Security의 명시적인 READ_ONLY grant를 한 트랜잭션으로 저장한다.
- 신뢰된 Run/서비스 역할을 바인딩한 Artifact 접근기를 제공한다. Artifact URI를 Host 경로나 외부 URL로 해석하지 않는다.
- 기존 Artifact Registry에 등록된 보고서를 작성 역할만 내용 저장할 수 있게 하고, 기존 Metadata를 임의 변경하지 않는다.

여기서 Artifact 내용 저장과 기존 `project_artifacts` Workflow 등록은 서로 다른 단계다. 실제 내용이 없는 기존 Metadata를 준비 완료로 취급하거나 자동으로 가짜 Archive를 만들지 않는다.

## 2. Git에서 실제 Snapshot 만들기

신뢰된 Host가 등록된 Run/Developer Step, **full Commit Object ID**, Repository 식별자와 **Source-relative Lock 경로**를 전달한다. Host Source Root는 20번 Registry에서만 조회한다. 브랜치/HEAD/임의 업로드 tar·모델 주장 Hash는 Snapshot 근거로 받지 않는다.

1. 일반 디렉터리인 등록 Source 및 내부 Git Metadata를 확인한다.
2. 실제 Git Object Format과 Commit 객체, Root Tree를 확인한다.
3. `ls-tree -r -t -z --full-tree`로 정확한 경로와 객체를 조회한다.
4. Commit/Tree/Blob bytes에서 실제 Git Object ID를 다시 계산한다.
5. 허용된 일반 파일을 UTF-8 경로 정렬 순으로 tar에 담는다.
6. 실제 tar bytes의 SHA-256 및 Commit에 포함된 Lock 파일의 SHA-256을 계산한다.
7. Lock Hash가 동결 Run 환경과 일치해야 Source를 저장한다.

`git archive`의 export-ignore/export-subst에 의해 코드가 빠지거나 바뀌는 것을 피하려고 실제 Tree/Blob bytes를 사용한다. Working Tree의 미커밋 변경·파일 시간·브랜치 이동은 이미 고정한 Snapshot에 영향을 주지 않는다. 실행 파일 mode는 `0755`, 일반 파일은 `0644`, uid/gid/mtime은 0, 사용자·그룹명은 비운다. UTF-8 긴 경로에는 표준 PAX 형식을 사용하며 임의 업로드 Archive를 추출하지 않는다.

Git은 고정 argv·`shell=False`·정리된 환경에서 읽기 전용으로 실행한다. Replacement Object·외부 Config include·Alternates·Worktree/외부 gitdir·Partial Clone의 Lazy Fetch·Hook/프로토콜 실행 경로를 제한한다. Symlink/Hardlink/비정규 Git Metadata, Source의 링크/gitlink, Traversal/비밀 경로, 정규화 경로 충돌은 거부한다. Git stdout/stderr 크기와 전체 deadline을 제한하며 오류 본문/Host 경로를 반환하지 않는다.

이 Git Metadata 검사는 Container 보안 경계가 아니다. 같은 OS 사용자가 검사 도중 폴더를 악의적으로 바꾸는 상황까지 격리하지 않으며 22번 Sandbox에서 신뢰된 Git Metadata와 실행 환경을 보호해야 한다.

기본 한도: 파일1,000개, 파일당1MiB, 전체 파일16MiB, tar20MiB, 전체 생성30초. `GitSnapshotLimits`는 신뢰된 Host가 설정하는 제한이며 모델 인자로 완화하지 않는다. 저장소는 내용당 최대20MiB로 제한한다.

## 3. 실제 저장과 불변성

| 항목 | 저장/검증 |
| --- | --- |
| `artifact_contents` | Artifact/Run/Step ID, 타입·Code Version, Metadata JSON+Hash, 내용 bytes+Hash·크기·media type |
| `snapshot_read_grants` | Source Artifact ID별 QA/Security READ_ONLY |
| Source 내용 | 정규화된 실제 `source.tar`, `application/x-tar` |
| 보고서 내용 | 기존 Registry Metadata와 일치하는 정제된 canonical JSON, `application/json` |
| URI | `artifact://<UUID>/source.tar` 및 기존 보고서 URI의 정확한 내부 참조 |

내용과 grant는 기존 Workflow DB의 같은 트랜잭션으로 저장한다. 파일시스템과 DB가 원자적으로 같이 저장된다고 주장하지 않는다. 이번 실제 내용 저장은 BLOB이며, 20번 `snapshots/` 폴더에 자동 추출하거나 Host 파일 경로를 반환하지 않는다. 22번은 이 검증된 bytes로 읽기 전용 Mount를 준비해야 한다.

지원 내용 타입은 SOURCE·REQUIREMENT·CHANGE_REPORT·BUILD_REPORT·QA_REPORT·SECURITY_REPORT다. RUN_CONFIGURATION은 기존 별도 불변 JSON 테이블/Metadata API를 유지하며 이 BLOB 저장소로 중복 등록하지 않는다.

UPDATE/DELETE/같은 ID의 INSERT OR REPLACE는 SQLite Trigger로 금지한다. 동일 ID/정확히 동일한 내용·Metadata는 idempotent하게 확인할 수 있지만, 다른 bytes·Metadata·버전으로 덮어쓸 수 없다. Source 수정은 새 UUID와 다음 Code/Artifact Version, 이전 Source UUID를 사용한다. 같은 Run·Repository·Git Format의 직전 버전만 predecessor가 될 수 있다.

Hash는 무결성 비교값이지 DB 관리자에 대한 암호학적 서명이 아니다. DB 파일 자체를 변경하거나 Trigger를 제거할 수 있는 권한까지 방어했다고 표시하지 않는다. Source bytes는 Artifact Store에만 저장하며 기본 Trace에 직접 넣지 않는다.

## 4. 역할과 Run 접근 권한

| 역할 | Source 내용 Read | 보고서 Read | 내용 저장 |
| --- | --- | --- | --- |
| Planner | 금지 | Requirement만 | 등록된 Requirement JSON만 |
| Developer | 같은 Run READ_ONLY | 같은 Run 보고서 | Source Freeze, 등록된 Change/Build 보고서 |
| QA | 같은 Run + 실제 QA grant | 같은 Run 보고서 | 등록된 QA 보고서 |
| Security | 같은 Run + 실제 Security grant | 같은 Run 보고서 | 등록된 Security 보고서 |
| Orchestrator Host | 신뢰된 Source 식별/내용 검증 | Registry 관리 | 관리 경계에서 연결 |

역할은 서버/런처가 바인딩한다. URI/Tool 입력에서 role을 선택하는 구조가 아니다. 매 Agent 접근에서 등록된 Run/Workspace 준비 상태를 확인하고, 실제 내용/Metadata/명시적 grant를 재검증한다. 다른 Run의 Artifact ID나 변형된 URI·외부 URL·file URI는 거부한다.

`verify_handoff()`는 QA/Security에 전달된 Run/Artifact/URI/ExecutionManifest가 실제 저장된 Source와 정확히 같은지 확인한다. 모델이 보낸 grant나 Hash만으로 권한을 부여하지 않는다. 반환된 immutable bytes가 Read-only 콘텐츠 capability이며, OS Read-only Mount 완성은 아직 아니다.

## 5. Build 이전 후보와 A2A 완료 등록의 구분

```text
신뢰된 Developer 실행 Context
→ 실제 Commit 고정
→ 실제 Source Freeze + 내용/권한 저장
→ 신뢰된 ExecutionManifest
→ Build Tool 실행 [25/31번]
→ 실제 A2A 완료 Artifact 조립 [31번]
→ 기존 Orchestrator의 결과 등록 [34번]
```

Source Freeze는 IMPLEMENTING/FIXING Run의 실행 중/완료 Developer Step에서 가능하며 해당 Fix Attempt·Requirement ID·동결 실행환경을 보존한다. 실행환경이 없거나 실제 Lock Hash가 다르면 거부한다. Container Image Digest는 기존 Host의 동결 입력을 보존하는 것이며, 이번에 Docker Image를 조회/실행해 확인한 값은 아니다.

최초 Code Version1, Fix 최대3회에 따른 Version2~4만 허용한다. 같은 후보의 재호출은 동일 Source를 반환하며, 같은 Version에 다른 Commit을 덮어쓸 수 없다. 저장 트랜잭션에서도 Run/Step을 다시 확인해 취소/상태 변경 후 후보를 등록하지 않도록 한다.

Freeze는 Run/Task 상태·Final Verdict·Build PASS·`SNAPSHOT_FROZEN` Trace를 생성하지 않는다. 기존 Metadata 기반 Dispatch도 이번에 자동 교체하지 않았다. 31/34번은 실제 staged Source로 Manifest를 조립하고 `verify_candidate()`로 Source 식별/내용/환경/Lineage를 확인한 뒤 기존 완료 등록을 이어가야 한다. 최종 A2A 참조 검증은 기존 Task 결과 경계를 유지한다.

## 6. 신뢰된 라이브러리 사용 예

현재 신규 HTTP 다운로드/업로드 API나 MCP Tool은 없다. 아래는 준비된 Workspace의 실제 Git 저장소·완전한 Run 환경을 사용하는 Host 연결 지점이며 기본 Bootstrap Agent 실행 예제가 아니다.

```python
from orchestrator.artifacts.service import ArtifactStore
from orchestrator.domain.snapshot_handoff import SnapshotHandoff
from orchestrator.domain.states import AgentRole

store = ArtifactStore(repository, workspace_registry)
developer = store.bind(run.run_id, role=AgentRole.DEVELOPER)
source = developer.freeze_source(
    workflow_step_id=developer_step.workflow_step_id,
    commit_hash=full_commit_id,
    repository_id="a2a-agent-company",
    lock_path="requirements.lock",  # Source-relative, Host-selected
)
handoff = SnapshotHandoff.from_snapshot(source)
qa = store.bind(run.run_id, role=AgentRole.QA)
verified = qa.verify_handoff(handoff)
# verified.content를 다음 단계 Sandbox에 전달. Trace에 전체를 저장하지 않음.
```

생성자/import는 DB 테이블·Git 실행·Workspace를 만들지 않는다. 첫 명시적 저장소 작업이 내부 테이블을 준비한다. Report publication은 등록된 Artifact ID만 받으며 임의 보고서 bytes나 외부 URI를 업로드/다운로드하지 않는다. 기존 Metadata 조회 API는 내용의 존재/검증 완료를 보장하지 않는다.

## 7. 개발정의서 준수 점검

| 기준 | 이번 구현과 남은 범위 |
| --- | --- |
| §5 동일 Snapshot | 실제 Commit/Tree/Archive Hash, 공통 Manifest와 grant 검증 |
| §5-4 권한 | Developer Read, QA/Security grant Read-only, Source Write capability 없음 |
| §8-7 경로 | Registry Root만 사용, Source/Metadata의 링크·Traversal·비밀 경로 제한 |
| §8-8 Secret/Trace | 원문 Source는 전용 Store에 저장, 보고서는 기존 정제 정책, 오류/repr에 bytes/Host Root 미노출 |
| §9 불변 Artifact/Lineage | 새 UUID·직전 Source 버전, 덮어쓰기 금지, 실제 내용 Hash 재검증 |
| §10 성공 판정 | Freeze/Hash 검증을 제품 PASS나 SUCCESS로 표시하지 않음 |
| 역할 경계 | 1번+2번만, 3번 서비스/4번 평가 및 실제 팀원 연결 미작업 |
| 기존 규격/제한 | A2A1.0·MCP2026-07-28·Fix3/MCP Retry2 유지 |

경로의 비밀 파일 차단은 임의 코드/binary 안의 모든 Credential을 발견하는 완전한 Secret Scanner가 아니다. Source를 변조해 Hash를 바꾸는 redaction은 하지 않는다. 실제 MCP 내용 제한·Secret 검사와 Security Scan은 해당 후속 단계에서 이어간다.

기존 `protectedTestSuiteRef`/`scannerProfileRef` Credential URL 보완 및 드문 SQLite 초기 WAL 잠금 문제는 이번에 변경하지 않았다. 기본 Agent의 `executionReady=False`/미구현 REJECTED 경계도 유지한다.

## 8. 검증·커밋·다음 작업

신규 테스트는 실제 tempfile Git/SQLite를 사용한다. Source 재현성·실제 Lock/Git Object Hash·위험 경로·호출 한도, 저장 불변성·내용/grant 변조·원자적 publication, Run/역할/Manifest/URI 검증을 확인한다. 테스트에서 만든 Git Commit은 임시 저장소에만 존재하며 프로젝트 Git commit/push는 하지 않는다.

신규 테스트 **95개**: 실제 Git Snapshot22 + SQLite 내용 저장34 + 서비스/ACL39. 각 파일의 전용 검증은 모두 통과했다. 최종 리뷰에서 반복 Freeze의 Step Code Version 불일치와 Git 생성 중 취소 경계를 보완하고 회귀 테스트도 추가했다.

전체 첫 실행은 **628개 중 기존 테스트1개 오류**였다. `test_agent_task_store.test_concurrent_initial_roles_cannot_share_a_new_database`의 기존 `PRAGMA journal_mode=WAL` 동시 초기화에서 `sqlite3.OperationalError: database is locked`가 재발했다. 이번 변경은 해당 Agent DB 코드를 수정하지 않았으며, 신규95개는 이 전체 실행에서도 모두 통과했다. 재실행이 통과하더라도 기존 드문 잠금 문제의 해결로 표시하지 않는다.

최종 전체 재실행은 **628개 모두 통과**했다(31.051초). `compileall`·`pip check`·`git diff --check`도 통과했다. 기존 실제 Unix Socket fixture를 포함한 전체 회귀는 승인된 실행 환경에서 수행했다. 현재 환경에는 pytest가 없어 pytest 기반 평가 실험을 검증한 것으로 표시하지 않는다. 실제 LLM API/Cloud·제품 Build/Test/Security Scan·Container·MCP·팀원 서비스 연결은 수행하지 않았다.

```bash
PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -p 'test_git_snapshot.py'
PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -p 'test_artifact_blob_store.py'
PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -p 'test_artifact_service.py'
PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -q
.venv/bin/python -m compileall -q src/orchestrator/artifacts
.venv/bin/python -m pip check
git diff --check
```

커밋 메시지: `실제 Snapshot·Artifact 내용 저장과 Hash 검증·역할별 접근 제어 구현`

다음 작업: **22번 — Container Sandbox.** 실제 Snapshot의 안전한 Materialization·Read-only Mount·출력 Write 영역·실행 자원/Network 격리를 구현한다.

## 9. 공식 근거

- 정확한 NUL 경로/Full Tree 목록: [git-ls-tree](https://git-scm.com/docs/git-ls-tree)
- 실제 객체의 종류/크기/내용: [git-cat-file](https://git-scm.com/docs/git-cat-file)
- export-ignore/export-subst와 Archive Metadata 차이: [git-archive](https://git-scm.com/docs/git-archive)
- SQLite bytes/BLOB와 트랜잭션: [Python sqlite3](https://docs.python.org/3/library/sqlite3.html)
