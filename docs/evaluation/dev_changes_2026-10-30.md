# dev 코드 변경 공유 (2026-10-30, 4번 강석민 → 팀장 이주호)

10.29 팀 회의 결정("Snapshot 인계 방식 등 남은 테스트에 필요한 사항은 dev 코드에서 수정, 4번이 직접")에 따라 바꾼 dev 코드를 정리했다.
모두 `seokmin` 브랜치의 로컬 커밋이고 아직 푸시하지 않았다. 기존 설정(역할을 적지 않은 테스트 범위, 정적 증명을 쓰는 Security)은 그대로 동작한다.

## 1. 왜 바꿨나

- 평가를 QA·Security 모두 같은 방식(실행 테스트)으로 맞추기로 했다. 기존 dev는 Security가 단위 테스트를 실행할 수 없고(`run_unit_tests`는 Developer·QA만), Security 요구사항은 승인된 정적 증명(`assess()`)으로만 PASS/FAIL이 나서 정적 판정을 빼면 항상 UNVERIFIED → HUMAN_REVIEW였다.
- Ver1(순환 안 보호 테스트 + 최종)·Ver2(최종만) 실험을 하려면 최종 Snapshot을 평가 쪽으로 꺼낼 방법이 필요했다(Artifact API는 메타데이터만 준다).

## 2. 무엇을 바꿨나

| 커밋 | 범위 | 내용 |
|---|---|---|
| `6fa6c6a` | `mcp_tools/tools/unit_config.py`, `unit_store.py` | `UnitTestScope`에 선택 필드 `role`(값은 `"SECURITY"`만). 적지 않으면 예전처럼 QA 범위. 직렬화·정책 해시는 `role`이 있을 때만 키를 넣어 기존 설정의 해시가 그대로다 |
| `a0cf489` | `mcp_tools/core/policy.py`, `tools/files.py`, `file_io.py`, `unit.py`, `unit_inputs.py`, `unit_store.py` | Security 역할 도구 계약에 `write_test_file`·`run_unit_tests` 추가(LLM에는 쓰기 도구만, 실행은 Host가 부름). 쓰기는 `outputs/security/tests` 아래만, 실행 입력도 그 폴더만. 단위 테스트 실행 기록·Snapshot 읽기 권한·보호 범위 확인을 QA·Security 공통 검증 역할 규칙으로 |
| `408966f` | `agents/roles/security_contract.py`, `agents/runtime/security.py`, `security_services.py`, `evaluation_policy_store.py`, `agents/platform/host.py`, `host_services.py`, `tool_configuration.py`, `orchestrator/domain/validation_artifacts.py`, `application/validation_output.py`, `infrastructure/sqlite_workflows.py` | Host가 Security 역할 범위를 주면 Security 에이전트가 자기 테스트를 쓰고 `testCases`로 요구사항에 연결, Host가 자체·보호 테스트를 QA와 같은 단위 테스트 경로로 실행하고 측정 결과로 요구사항을 판정(하나라도 FAIL이면 FAIL, 모두 PASS면 PASS, 나머지 UNVERIFIED). 정적 증명과 함께 쓰지 않음(Host에서 거부). 보호 케이스 파일 하나를 범위의 역할대로 QA·Security에 나눔. Security 보고서·검증 결과 반영·재검증이 `run_unit_tests` 증거를 받음 |
| `d2b61e1` | `agents/platform/snapshot_export.py`(새 파일), `security_approval.py`, `pyproject.toml`, `uv.lock` | 최종 Snapshot·Manifest 내보내기 명령. 승인 함수의 3.11 전용 `co_qualname`·`co_exceptiontable`을 3.10에서도 동작하게(3.11 이상 해시는 그대로, 3.10에서 안쪽 함수 이름 위조는 `CO_NESTED`로 거부). dev 그룹에 sqlalchemy·argon2-cffi, wheel에 평가 기준·보호 테스트 포함 |
| `2a446fc` | `agents/roles/contracts.py` | Security 역할 계약에 테스트 방식 책임·금지 문구 추가 |
| `a387bb7` | `agents/platform/a2a_export.py`(새 파일) | 한 Run의 A2A Message 본문을 역할별 에이전트 작업 저장소에서 읽기 전용으로 꺼내는 명령(REQ-006 노출 검사용). 저장소를 쓰기로 열거나 프로세스 잠금을 잡지 않음 |

## 3. 쓰는 법

**Security를 테스트 방식으로 돌리기** — `owned-tools.local.json`의 `unit.scopes`에 Security 범위를 넣는다. 평가 쪽 `python -m evaluation.experiment configure`가 Ver1·Ver2 설정 쌍을 만들어 준다.

```json
{"name": "security-tests", "kind": "QA_TESTS", "source_directory": "tests", "pattern": "test_*.py", "role": "SECURITY"}
```

Ver1은 여기에 순환용 보호 범위 두 개(QA·SECURITY)와 보호 케이스 파일(`owned-protected-cases.local.json`)을 더한다. Security 범위가 있으면 승인 정적 증명 파일은 주지 않는다(둘 다 주면 Host가 거부).

**최종 Snapshot 꺼내기**

```bash
python -m agents.platform.snapshot_export --database <Host DB> --run-id <Run> --out <폴더> [--all-versions]
```

`v<N>/source.tar`(저장된 tar 바이트 그대로), `v<N>/manifest.json`(Execution Manifest), `run.json`을 쓴다. 저장 내용 해시·tar 해시·`snapshotSha256`이 같을 때만 쓰고, 덮어쓰기는 `--force`일 때만. 프로젝트 소스는 풀거나 실행하지 않는다.

## 4. 확인한 것

- dev 전체 시험(평가 제외) 3,316개 통과(`408966f` 시점). 이후 변경(`d2b61e1`·`2a446fc`) 관련 Security·Host·역할 계약·파이프라인 시험 통과. 승인 함수 시험 85개가 3.10·3.13 모두 통과.
- dev 파이프라인 시험(가짜 LLM·가짜 docker): Security 테스트가 통과하면 Run이 SUCCESS로 끝나고, 실패하면 Security FAIL·Security Issue가 생긴다. 내보낸 tar·Manifest를 평가 D5가 그대로 읽는다.
- 실제 LLM·Docker로는 아직 돌리지 않았다.

## 5. 함께 정할 것

- 시작 코드 규격(기업 팀장님과 "시작 코드 규격 에이전트" 여부 논의 뒤). 시작 코드에는 평가 코드(`src/evaluation`)가 들어가면 안 된다 — Host가 시작 커밋의 트리 전체를 에이전트 작업 폴더로 복사하기 때문이다.
- 평가 이미지(시작 코드 lock으로 빌드해 해시 고정, Host와 평가 D5 공통).
- 푸시·dev 병합 시점.
