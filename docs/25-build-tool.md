# 25. Build Tool 및 불변 실행 기록

작성 기준: 2026-10-08. 범위: 1번+2번의 실제 MCP Build 처리. 3번 제품 웹 개발, 4번 독립 평가 및 해당 통합은 변경하지 않는다.

> `run_build` Handler를 저장된 Source·Container Sandbox·불변 실행 기록과 연결했다. 현재 환경에는 Docker가 없어 실제 Container Build 성공 검증은 미완료다. Host 실행 fallback이나 가짜 PASS는 제공하지 않는다.

## 1. 이번에 개발한 것

- `BuildConfiguration`: 승인된 Container 명령·자원·로컬 Docker endpoint를 고정하는 Host 전용 설정.
- `BuildTools`: 기존 MCP `run_build` 입력을 실제 Artifact Store와 `SandboxRuntime`에 연결.
- `BuildOutputStore`: stdout/stderr, ExecutionManifest, 실제 실행 Profile을 원자적으로 저장하는 SQLite 불변 실행 기록.
- 실제 stdio CLI/Client에서 Host 설정 전달 및 Build Handler 등록.
- 정상 비영 종료, 인프라 실패, 취소·정리·저장 실패의 구분과 회귀 검증.

새 `BuildConfiguration`·`BuildTools`·`BuildOutputStore` 생성자와 Tool 목록 조회는 파일/DB/Docker/LLM 작업을 시작하지 않는다. 명시적인 stdio 부트스트랩에서는 기존 SQLite 저장소를 초기화·접근한다. Run 생성·Git commit·Source freeze·A2A 완료·제품 `BUILD_REPORT` 발행·최종 Verdict는 자동 수행하지 않는다. 기본 Agent의 `executionReady=False`/미구현 거절 경계는 유지한다.

## 2. 실제 실행 순서

```text
Host가 고정한 Developer 역할 + workspaceId/snapshotId
→ 같은 Run의 실제 저장된 Source 및 Workspace 확인
→ 현재 Developer Step·Code Version·환경 확인
→ 실제 Docker/Image/Container 설정 검사
→ Frozen Source만 읽기 전용으로 준비
→ Host가 승인한 고정 명령을 Container에서 실행
→ 실제 종료 코드 확인 + 소유 Container/준비 폴더 정리
→ 저장 시점 Run/Step/Source/환경 재검사
→ 출력 두 개 + Manifest/Profile을 SQLite에 함께 저장
→ 기존 MCP Schema의 종료 코드·시간·저장 참조 반환
```

수정 중인 `source/`나 모델이 지정한 Host 경로를 Build 대상으로 쓰지 않는다. `snapshotId`는 이미 발급된 Source Artifact UUID다. 다른 Run·미등록 Source·취소된 Run·현재 버전이 아닌 Source·일치하지 않는 Developer Step은 실행 또는 저장되지 않는다.

## 3. 기존 Tool 계약 유지

입력은 기존 두 필드만 허용한다.

```json
{
  "workspaceId": "HOST_ISSUED_WORKSPACE_UUID",
  "snapshotId": "STORED_SOURCE_ARTIFACT_UUID"
}
```

위 문자열은 필드 설명용이며 그대로 실행할 UUID 예시는 아니다. 모델 입력에 `command`, `argv`, `profile`, `image`, `dockerEndpoint`, `role`, Host 경로를 추가하면 Protocol Error다.

| 출력 | 실제 의미 |
| --- | --- |
| `exitCode` | 검증한 Container 명령의 실제 종료 코드 |
| `durationMs` | Sandbox가 측정한 실행 시간. 후처리 저장 시간이나 전체 Run 시간과 다름 |
| `stdoutRef` | 저장된 정제 stdout의 `execution://<manifest UUID>/stdout.txt` |
| `stderrRef` | 저장된 정제 stderr의 `execution://<manifest UUID>/stderr.txt` |
| `executionManifestId` | 실제 저장한 불변 Build 실행 기록의 Host-issued UUIDv4 |

기존 `outputSchema`의 필드와 필수 조건을 바꾸지 않고 두 stream ref도 항상 제공한다. Source Artifact UUID, Sandbox `executionId`, 실행 기록 `executionManifestId`는 서로 다른 개념이다. 기록에는 실제 Source Manifest와 Sandbox 실행 ID가 함께 저장된다.

`execution://`는 Host 전용 저장소 참조이며 HTTP 주소·Workspace 파일·기존 Project Artifact URI가 아니다. Model/Agent가 임의 URL fetch로 해석하거나 Project Artifact Registry에 등록된 것처럼 취급하면 안 된다.

## 4. Host 설정과 실행 명령

`MCPChildConfiguration.build_configuration`에 `BuildConfiguration`을 명시한다. Client는 이를 `--build-configuration-json`이라는 Host CLI 인자로만 전달한다. 파일/환경변수를 자동으로 찾아 읽거나 모델 Tool 입력으로 명령을 받지 않는다.

예시는 이미 발급된 Developer binding, 실제 Source, 승인된 Linux Container 이미지가 준비된 Host 호출 코드다. 아래 `scripts/build.py`가 현재 제품에 존재한다는 뜻은 아니다. 실제 제품 Build 명령은 Host가 승인해야 한다.

```python
from dataclasses import replace
from mcp_tools.client import open_mcp_client
from mcp_tools.tools.build_config import BuildConfiguration
from orchestrator.sandbox.contracts import ExecutionProfile

profile = ExecutionProfile(
    name="approved-signup-build",
    tool_name="run_build",
    argv=("/usr/local/bin/python", "-B", "/snapshot/scripts/build.py",
          "--output-dir", "/output"),
)
configuration = replace(
    existing_developer_configuration,
    build_configuration=BuildConfiguration(profile=profile),
)

async def build_source(source_artifact_id):
    async with open_mcp_client(configuration) as client:
        return await client.call_tool("run_build", {
            "workspaceId": str(configuration.binding.workspace_id),
            "snapshotId": str(source_artifact_id),
        })
```

Host JSON은 closed schema로 검증하며 UTF-8 16KiB 이하, 중복/미정의 필드·비유한 숫자·credential·known shell/env dispatcher executable·원격 TCP/SSH Docker endpoint를 거부한다. Profile의 명령 기본값은 없다. Python/Node/npm 실행은 **Container 안에서만** 허용한다.

명령에 추가 argv가 필요하거나 실행 이미지에 의존성이 필요하면 신뢰된 Host 설정/이미지 준비를 변경해야 한다. 모델이 Network를 열거나 임의 `npm install`, Shell Tool, Host fallback을 선택할 수 없다. 이름으로 shell executable을 검사하는 것은 완전한 코드 분석기가 아니며 승인된 Profile/이미지의 신뢰를 대체하지 않는다.

## 5. Container와 역할 경계

22번의 동일 정책을 재사용한다: Frozen Source Read-only, Root FS Read-only, 임시 `/work`·`/output`·`/tmp`만 Write, Network DENY, 비특권 사용자, Capabilities drop, no-new-privileges, Seccomp, CPU/Memory/PID/시간/출력 제한, Docker Socket/Secret Mount 금지.

이미지는 동결 Digest와 실제 local Image Config ID/RepoDigest 관계를 확인한다. Manifest Digest와 Config ID는 동일하다고 간주하지 않는다. 자동 Pull/이미지 태그 교체/ALLOWLIST를 bridge Network로 완화하지 않는다.

실제 MCP Build Handler는 Developer에만 노출한다. Planner/QA/Security에는 노출·직접 호출 권한이 없다. 개발정의서의 Orchestrator Build/Report 조회는 기존 Host 실행 결과 처리 경계로 유지하며, Orchestrator를 새 AgentRole로 추가하거나 Developer로 가장시키지 않는다. 새 Orchestrator 직접 Build 호출 API는 이번 단계에서 만들지 않는다.

## 6. 불변 실행 기록

같은 Workflow SQLite에 전용 `build_execution_records`를 lazy 생성한다. 제품 DB나 기존 Artifact/A2A Schema를 바꾸지 않는다. 기록은 제품 `BUILD_REPORT`가 아니며 아직 존재하지 않는 A2A Task/Artifact ID를 만들어 채우지 않는다.

저장 transaction에서 다음을 재검사한다.

1. 실제 Run/Workspace/Configuration 소유권과 Run 상태.
2. 현재 attempt의 유일한 실행 중 Developer Step, Source 생성 Step, 요구사항·Code Version 일치.
3. 실제 저장 Source의 canonical metadata·BLOB Hash·크기·Read-only grants.
4. 동일 ExecutionManifest·동결 Image/Dependency Lock/Network 정책.
5. 실제 Sandbox 실행 ID·Container/Image ID·종료 코드·정수 시간·Profile 관계.

stdout/stderr는 정제 후 UTF-8 bytes로 저장하고 각각 Hash/크기를 검증한다. 기본 출력 한도는22번을 따르며 저장소의 최대 한도는 각각4MiB다. canonical receipt metadata는64KiB 이하다. 실제 narrowed 명령/Profile/자원도 metadata Hash에 묶고 repr에는 출력하지 않는다.

정제 출력 두 개와 Manifest/Profile은 함께 commit한다. SQL update/delete/replace와 중복 실행 ID 저장은 금지한다. 저장 실패 시 ref만 먼저 발급해 성공했다고 반환하지 않는다. 조회할 때도 Source identity·metadata/stream Hash·소유권을 다시 검사한다.

다음은 신뢰된 Host의 조회 예다. 외부 MCP/HTTP 조회 Tool을 새로 추가한 것이 아니다.

```python
record = output_store.get(run_id, build_result["executionManifestId"])
stdout = output_store.read_output(run_id, build_result["stdoutRef"])
manifest = record.execution_manifest
```

다른 Run의 ref, 파일 경로·외부 URI·Traversal은 거부한다. 정상 종료/취소 이후 과거 기록의 Host 조회는 가능하지만, 조회 성공을 현재 코드의 Build 성공으로 해석하지 않는다.

## 7. 정상 결과와 실행 오류

| 상황 | MCP 처리 |
| --- | --- |
| 실제 명령 정상 완료, exit0 | Tool 성공 + 실제 exit0. 최종 프로젝트 SUCCESS는 아님 |
| 실제 명령 정상 완료, 컴파일 오류/비영 exit | Tool 성공 + 실제 비영 exit. Developer가 수정할 Build 실패 근거 |
| Container 명령 Timeout | `isError=true`, `TIMEOUT` |
| 승인 Profile/Docker/Image 미준비, 설정·정리 실패 | `isError=true`, `SANDBOX_ERROR` |
| 실제 실행/종료 상태 오류, OOM, 저장·publication 실패 | `isError=true`, `BUILD_EXECUTION_ERROR` |
| 역할/Workspace/경로 위반 | 기존 Protocol/Permission/Path 오류 정책 |

원본 Docker 오류·Host 경로·Source·stdout/stderr를 Tool 메시지/Trace에 직접 출력하지 않는다. Tool 성공 출력에는 ref만 들어간다. 기존 Client는 실행 오류를 안전한 `MCP_CLIENT_TOOL_FAILED`로 전파한다. Store/Runtime의 세부 안전 코드 및 정리 실패 execution ID는 Host 내부 진단용이지 제품 PASS 근거가 아니다.

ToolEvidence/Retry chain·누적 Run budget·MCP Trace·실제 Agent Build Report 조립은29/31/34/37번에서 연결한다. 자동 재시도는 없다. 저장/전송 실패 후 실제 명령 실행 여부가 불명확한 경우 임의 재실행하지 않는다. 원인을 확인하지 않은 Timeout을 곧바로 제품 무한루프로 판정하지 않는다.

## 8. 시간·취소·출력의 한계

Build의 Sandbox 실행 deadline을 `min(Host timeout, MCP max_call_seconds - 2×control_timeout_seconds - 1초)`로 줄인다. 종료 후 inspect+rm 제어 예산을 남기기 위한 제한이며 Host timeout을 늘리지 않는다. 기본60초/제어10초라면 실행 deadline은39초다. 환경 검사·Snapshot 준비·Container 생성·실행·종료 상태 확인이 이 예산을 공유하므로 실제 Build 명령에는 준비에 소비한 시간만큼 더 적은 시간이 남는다. 예산이 부족하면 Docker를 시작하지 않는다.

서버 내부 취소·시간 초과는 소유 Container 정리가 끝날 때까지 회수한다. publication worker도 취소 시 방치하지 않는다. 다만 이미 완료된 저장까지 취소가 되돌리는 것은 아니다.

이는 전체 hard wall-clock/프로세스 crash 복구 보장이 아니다. 동기 Host I/O·DB busy·SDK discovery 시간·정리 지연·누적 Run 예산은 별도 경계다. 설치된 SDK의 stdio 종료 유예는2초이므로 임의 Client 취소/세션 종료/Host kill이 긴 Daemon cleanup을 중단시킬 수 있다. 모든 외부 종료에서 Container가 반드시 제거된다고 주장하지 않는다. 소유권 없는 Container 삭제나 광범위 자동 정리는 하지 않으며 orphan 운영 복구/Trace는 후속이다.

Container tmpfs의 Build 산출물은 실행 기록과 다르다. 이번 단계는 제한된 stdout/stderr·Manifest 저장이며 Build binary/dist 디렉터리의 Host export·배포·서비스 자동 실행을 구현한 것이 아니다.

## 9. 개발정의서 점검

| 기준 | 결과 |
| --- | --- |
| §2 책임 경계 | MCP는 실제 실행 결과만 반환, Orchestrator 최종 Verdict/Agent 역할 유지 |
| §5 동일 Snapshot/환경 | live Source 대신 실제 Source BLOB. Manifest/Digest/Lock 일치 확인 및 publication 재검사 |
| §8-1~5 Protocol/Schema | 기존 MCP2026-07-28/SDK v2/stdio/JSONSchema2020-12, run_build 이름·입출력 그대로 |
| §8-6 실행 오류 | 컴파일 비영 종료와 인프라/Timeout/정리·저장 실패 구분 |
| §8-7~10 실행 경계 | 기존 파일/Secret/Network/Container 정책, Host 코드 실행·Socket Mount 없음 |
| §8-11 역할 | Developer만 실제 Build Handler. 타 역할 Tool 권한 확대 없음 |
| §10 성공 판정 | Build 결과만으로 프로젝트 SUCCESS/QA PASS/Security PASS를 만들지 않음 |
| §4 Retry/수정 | 자동 Retry 없음, Fix3회·MCP Retry2회 기존 한도 변경 없음 |
| 담당 범위 | 1번+2번만. 3번/4번 모듈 및 해당 통합 미변경 |

실제 Docker 검증, Unit/Browser/Security Tool, 역할 Executor, 기본 Pipeline 연결, 영속 ToolEvidence/Trace는 후속이다. 사용자가 이전에 제외한 기존 비밀번호 보호 정책 보완 및 기존 Agent DB 초기 WAL 경쟁도 이번 작업으로 해결했다고 표시하지 않는다.

## 10. 검증

```bash
PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -p 'test_mcp_build*.py' -q
PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -p 'test_mcp*.py' -q
PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -q
PYTHONPATH=src .venv/bin/python -m compileall -q src/mcp_tools
.venv/bin/pip check
git diff --check
```

최종 검증 결과:

- 신규 Build 전용 **101개 통과**: Host 설정 28개 + Store 50개 + Handler/stdio 연결 23개.
- MCP 관련 **413개 모두 통과**(40.114초).
- 전체 회귀 **1,131개 모두 통과**(83.562초). 기존 Unix socket 검증을 포함하도록 권한을 승인받아 제한 밖에서 실행했다.
- `compileall`, `pip check`, `git diff --check` 통과. 새 의존성/Lock 변경은 없다.

정상/비영 종료, Frozen Source, 실제 저장/조회와 Hash, 이미지 Config ID/Manifest Digest 관계, 현재 유일 Developer Step, 종료 중 Run 취소, 타 Run 접근, Secret 정제, Protocol/역할 거부, Timeout·OOM·정리·저장 실패와 반복 서버 취소를 확인했다. 초기 통합 테스트의 fixture 취소 상태/cleanup 작성 오류는 수정한 뒤 위 최종 실행으로 재검증했다. 기존 Agent DB 초기 WAL 경쟁을 해결한 것은 아니며 A2A SDK의 기존 경고도 남아 있다.

테스트는 실제 임시 Git/SQLite/Snapshot·역할/상태/Hash·실제 SDK stdio와 Fake Docker 통신을 사용한다. 실제 Docker가 없는 경우 오류로 중단하는 실제 stdio 호출도 확인한다. generated Source를 Host에서 실행하거나 Container Build/회원가입 시연/4번 비교 실험을 완료했다고 주장하지 않는다.

현재 환경에서 실제 Container 검증을 진행하려면 승인된 로컬 Linux Docker 엔진과 동결 Digest 이미지/의존성·제품에 맞는 고정 Build Profile이 필요하다. 엔진 설치나 Network 개방을 자동으로 수행하지 않는다.

## 11. 변경 파일·다음 작업

구현: `src/mcp_tools/tools/build.py`, `build_config.py`, `build_store.py`. 연결: `src/mcp_tools/client.py`, `__main__.py`, `runtime.py`. 전용 테스트3개 및 README·본 문서 추가. 새 의존성/Lock 변경은 없다.

다음 작업: **26번 — Unit Test Tool.** 같은 Frozen Source·Container 정책을 사용해 실제 테스트 실행 결과/Report를 연결한다.

커밋 메시지: `고정 Snapshot 기반 MCP Build Tool과 불변 실행 기록 구현`

Git commit/push는 직접 수행하지 않는다.
