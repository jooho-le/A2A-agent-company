# 5. Developer Snapshot과 QA/Security 인계

> 상태: 도메인 계약 구현 완료
> 범위: Developer Source Artifact, 불변 Snapshot Manifest, QA/Security read-only 인계, 검증 결과의 동일 Snapshot 확인
> 다음 작업: 8번 — Workflow 저장소와 Run API 연결

## 목적

QA와 Security가 서로 다른 코드나 Developer의 수정 중인 Working Tree를 검사하는 일을 방지한다. Build·QA·Security 결과가 어떤 Source Artifact 및 어떤 실행환경에 근거했는지를 같은 Manifest로 추적한다.

## 구현 위치

| 경로 | 내용 |
| --- | --- |
| `src/orchestrator/domain/snapshot_handoff.py` | Source Artifact, Execution Manifest, read-only Handoff, hash/동일성 검증 |
| `schemas/project/snapshot_execution_manifest.schema.json` | Build·QA·Security 결과에 붙이는 JSON 계약 |
| `tests/test_snapshot_handoff.py` | Artifact 불변성, 접근 권한, hash, 버전 및 동일성 회귀 테스트 |

## Snapshot 식별 정보

`CodeSnapshotArtifact`는 프로젝트 Artifact Registry에 등록할 Developer Source 산출물의 도메인 레코드다. 새 Artifact 버전은 기존 레코드를 덮어쓰지 않고 새 `artifact_id`로 만들며, 두 번째 버전부터 `previous_artifact_id`를 필수로 둔다.

| 필드 | 의미 및 검사 |
| --- | --- |
| `artifact_id` | 프로젝트 전역 Source Artifact UUIDv4 |
| `artifact_version` / `previous_artifact_id` | Artifact Lineage 버전과 직전 레코드 |
| `run_id`, `workflow_step_id` | 실행 및 생성 Step 추적 |
| `a2a_task_id`, `a2a_artifact_id` | 공식 Agent 산출물 참조. 둘 다 제공하거나 둘 다 생략 |
| `requirement_ids` | 관련 요구사항 UUID 목록. 중복 불가 |
| `code_version` | Run의 코드 후보 번호, 1~4 |
| `repository_id` | 소스 Repository 식별자 |
| `commit_hash`, `git_object_format`, `tree_hash` | 전체 Git Object ID와 Root Tree. SHA-1은 40자리, SHA-256은 64자리 |
| `snapshot_sha256` | 정규화해 Artifact Registry에 저장한 Archive의 SHA-256 |
| `artifact_uri` | Registry 위치. 로컬 파일 경로와 `file://` URL은 금지 |
| `container_image_digest`, `dependency_lock_hash` | 실행환경 이미지 및 lock 파일의 SHA-256 Digest |
| `created_by`, `created_at` | Source는 Developer Agent가 생성하며 시각은 timezone 필수 |

코드 후보 번호는 `code_version_for_fix_attempt()`으로 산출한다: 최초 구현은 1, 수정 1~3회는 각각 2~4다. 이는 Artifact Lineage의 `artifact_version`과 별도 카운터다.

## 공통 Execution Manifest

Build, QA, Security 결과에는 다음 9개 필드를 같은 camelCase JSON으로 첨부한다. Manifest에는 프로젝트 Artifact UUID를 `projectArtifactId`로 담고, A2A의 Task-scoped `artifactId`와 혼용하지 않는다.

```json
{
  "repositoryId": "a2a-agent-company",
  "codeVersion": 2,
  "projectArtifactId": "7ba79708-69ec-4334-9fc4-d52232f22597",
  "commitHash": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
  "gitObjectFormat": "sha1",
  "treeHash": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
  "snapshotSha256": "cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc",
  "containerImageDigest": "sha256:dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd",
  "dependencyLockHash": "sha256:eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee"
}
```

JSON Schema는 [`snapshot_execution_manifest.schema.json`](../schemas/project/snapshot_execution_manifest.schema.json)이고, Python 모델은 `ExecutionManifest`다. 모델은 immutable이며 UUIDv4, 후보 버전, Git Object 포맷별 전체 Hash 길이, Snapshot/환경 Digest 형식을 검증한다.

`assert_same_execution_snapshot(build, qa, security)`는 위 Manifest 3개 전체가 같을 때만 공통 Manifest를 반환한다. Snapshot Hash뿐 아니라 Commit, Tree, Artifact, Code Version, Container Image Digest, Dependency Lock Hash까지 비교한다. 하나라도 다르면 `SnapshotMismatchError`이며 전체 성공 판정에 사용할 수 없다.

Artifact Registry에서 받은 Archive bytes는 사용 전에 `verify_snapshot_archive()`로 `snapshot_sha256`과 대조한다. 불일치 시 `SnapshotIntegrityError`를 발생시킨다.

## QA/Security 인계 및 권한

`SnapshotHandoff.from_snapshot()`은 동일한 프로젝트 Artifact와 Manifest를 가리키는 두 Grant를 생성한다.

| 주체 | Frozen Source Snapshot |
| --- | --- |
| Developer | Read-only |
| QA | Read-only |
| Security | Read-only |
| Orchestrator | Metadata/검증 결과 조회 |

QA와 Security Grant는 정확히 하나씩 있어야 하며 권한은 `READ_ONLY`로만 생성 가능하다. QA/Security는 검사 대상 Source를 수정하지 않고 각자의 결과 Artifact만 별도로 만든다. Handoff에는 로컬 경로가 아니라 Artifact Registry URI, 프로젝트 Artifact ID, 동일 Execution Manifest를 넣는다.

## Snapshot 고정 절차

```text
Developer Working Tree
→ Commit 생성
→ Build Candidate / Source Artifact 등록
→ 정규화된 Source Archive 생성 및 Registry 저장
→ snapshot_sha256 계산·검증
→ Snapshot freeze
→ QA read-only Grant ─┐
→ Security read-only Grant ─┴→ 동일 Execution Manifest로 검증 보고
```

수정은 새 Commit·새 Snapshot·새 Artifact Version·새 Code Version으로 시작한다. 기존 Snapshot이나 PASS 보고서를 덮어쓰지 않는다. 수정 후에는 Build, QA, Security가 새 후보의 동일 Manifest를 참조하는지 다시 확인한다.

## 범위 및 후속 연동

5번 당시에는 검증 가능한 도메인 계약과 순수 검증 함수까지만 구현했다. 이후 A2A Client/Task Runner가 [`06-a2a-client.md`](06-a2a-client.md), [`07-task-lifecycle.md`](07-task-lifecycle.md)에 추가됐다. 실제 Git Commit 생성, deterministic archive 제작, Artifact Registry DB/API, 영속 Object Store, URI 권한 ACL, Build/QA/Security 컨테이너 실행은 아직 연결하지 않았다. 특히 `READ_ONLY` Grant를 실제로 강제할 주체는 후속 Artifact Registry/스토리지 계층이다. 현재 Python 모델만으로 외부 파일 권한을 보장한다고 간주하면 안 된다.

## 검증

```bash
PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -v
```

전체 회귀 테스트 31개 통과. Manifest와 Schema 필드, 불변성, Artifact Lineage, QA/Security read-only 인계, 실행환경 불일치 차단, Archive Hash 검증 및 Code Version 매핑을 검사한다.

## 다음 작업

6번의 [`A2A Client`](06-a2a-client.md)와 7번의 [`Task Runner`](07-task-lifecycle.md)가 Handoff/Manifest 전달과 Task 상태 추적을 담당한다. 다음 8번에서는 Run/Step/Context 및 Trace 결과를 영속화한다.
