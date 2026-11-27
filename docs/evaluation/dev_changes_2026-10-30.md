# dev 코드 변경 공유 (2026-10-30, 4번 강석민 → 팀장 이주호)

10.29 회의록의 기타 개발 사항("석민: Snapshot 인계 방식 등 나머지 테스트에 필요한 사항은 dev 코드에서 수정")에 따라 바꾼 dev 코드를 정리했다. 어떤 테스트가 필요한지(QA·Security 같은 실행 방식, Ver1·Ver2)는 회의 뒤 4번이 정한 평가 방침이다.
`seokmin` 브랜치의 dev 커밋 세 개에 나눠 담았고 아직 푸시하지 않았다: `716f04a`(Security 측정 테스트 경로), `c7a4f85`(Snapshot·A2A 본문 내보내기·실행 환경), `a556629`(실험 Run 규칙·Security 오류 코드). 평가 코드·문서는 각각 바로 다음 커밋이다. 기존 설정(역할을 적지 않은 테스트 범위, 정적 증명을 쓰는 Security)은 그대로 동작한다.

## 1. 왜 바꿨나

- 회의 뒤 4번이 평가를 QA·Security 모두 같은 방식(실행 테스트)으로 맞추기로 했다(회의록에 직접 적힌 결정은 아님). 기존 dev는 Security가 단위 테스트를 실행할 수 없고(`run_unit_tests`는 Developer·QA만), Security 요구사항은 승인된 정적 증명(`assess()`)으로만 PASS/FAIL이 나서 정적 판정을 빼면 항상 UNVERIFIED → HUMAN_REVIEW였다.
- 회의록의 비교 방안 1(순환 + 최종)·2(최종 중심)를 먼저 멀티에이전트 Ver1·Ver2로 실험하려면 최종 Snapshot을 평가 쪽으로 꺼낼 방법이 필요했다(Artifact API는 메타데이터만 준다).

## 2. 무엇을 바꿨나

| 구분 | 범위 | 내용 |
|---|---|---|
| 실행 역할 표시 (`716f04a`) | `mcp_tools/tools/unit_config.py`, `unit_store.py` | `UnitTestScope`에 선택 필드 `role`(값은 `"SECURITY"`만). 적지 않으면 예전처럼 QA 범위. 직렬화·정책 해시는 `role`이 있을 때만 키를 넣어 기존 설정의 해시가 그대로다 |
| Security MCP 도구 (`716f04a`) | `mcp_tools/core/policy.py`, `mcp_tools/tools/files.py`, `file_io.py`, `unit.py`, `unit_inputs.py`, `unit_store.py` | Security 역할 도구 계약에 `write_test_file`·`run_unit_tests` 추가(LLM에는 쓰기 도구만, 실행은 Host가 부름). 쓰기는 `outputs/security/tests` 아래만, 실행 입력도 그 폴더만. 단위 테스트 실행 기록·Snapshot 읽기 권한·보호 범위 확인을 QA·Security 공통 검증 역할 규칙으로 |
| Security 측정 판정 (`716f04a`) | `agents/roles/security_contract.py`, `agents/runtime/security.py`, `security_services.py`, `evaluation_policy_store.py`, `agents/platform/host.py`, `host_services.py`, `tool_configuration.py`, `orchestrator/domain/validation_artifacts.py`, `orchestrator/application/validation_output.py`, `orchestrator/infrastructure/sqlite_workflows.py` | Host가 Security 역할 범위를 주면 Security 에이전트가 자기 테스트를 쓰고 `testCases`로 요구사항에 연결, Host가 자체·보호 테스트를 QA와 같은 단위 테스트 경로로 실행하고 측정 결과로 요구사항을 판정(하나라도 FAIL이면 FAIL, 모두 PASS면 PASS, 나머지 UNVERIFIED). 정적 증명과 함께 쓰지 않음(Host에서 거부). 보호 케이스 파일 하나를 범위의 역할대로 QA·Security에 나눔. Security 보고서·검증 결과 반영·재검증이 `run_unit_tests` 증거를 받음 |
| Snapshot 내보내기·환경 (`c7a4f85`) | `agents/platform/snapshot_export.py`(새 파일), `security_approval.py`, `pyproject.toml`, `uv.lock` | 최종 Snapshot·Manifest 내보내기 명령. 승인 함수의 3.11 전용 `co_qualname`·`co_exceptiontable`을 3.10에서도 동작하게(3.11 이상 해시는 그대로, 3.10에서 안쪽 함수 이름 위조는 `CO_NESTED`로 거부). dev 그룹에 sqlalchemy·argon2-cffi, wheel에 평가 기준·보호 테스트 포함 |
| Security 역할 계약 (`716f04a`) | `agents/roles/contracts.py` | Security 역할 계약에 테스트 방식 책임·금지 문구 추가 |
| A2A 본문 내보내기 (`c7a4f85`) | `agents/platform/a2a_export.py`(새 파일) | 한 Run의 A2A Message 본문을 역할별 에이전트 작업 저장소에서 읽기 전용으로 꺼내는 명령(REQ-006 노출 검사용). 저장소를 쓰기로 열거나 프로세스 잠금을 잡지 않음 |
| 점검 반영 (`c7a4f85`) | `a2a_export.py`, `agents/platform/host.py`, `configs/owned-tools.example.json` | 점검 반영: 저장소 경로를 URI로 인코딩(폴더 이름의 `?`·`#`·`%` 때문에 다른 파일을 열거나 실패하던 것), Host 정책 해시의 `semanticProof`를 Security 역할 범위가 있으면 `MEASURED_TESTS`로 표시(전에는 `UNVERIFIED`로 적혀 41번 문서의 "verifier 유무만 나타냄" 설명과 함께 오해 소지), 예시 설정에 `security-tests` 범위 추가 |
| 실험 Run 규칙 (`a556629`) | `orchestrator/domain/run_configuration.py` | 실험 Run(`experimentId`)의 필수 기준에서 보호 테스트 참조를 뺌. Ver2(순환 안 보호 테스트 없음)는 Host가 참조를 만들지 않아 실험 Run으로 거부되던 것을 고침. Single/Multi 비교에서는 평가 쪽 비교 검사가 두 구조의 보호 테스트 조건(참조)이 같은지 확인한다(둘 다 없는 방안 2 조건도 비교 가능). 나머지 기준(시작 커밋·모델·환경·한도·스캐너 참조·실행 순서·준비 상태)은 그대로 필수 |
| Security 오류 코드 (`a556629`) | `agents/runtime/security_services.py` | 외부 점검 반영: Security 측정 테스트 모드의 오류 코드(`SECURITY_TEST_INPUT_INVALID`·`SECURITY_TEST_BINDING_INVALID`·`SECURITY_TEST_EVIDENCE_INVALID`)가 허용 목록에 없어 모두 `SECURITY_SERVICES_INVALID`로 바뀌던 것을 그대로 남김(입력·ID 연결·실행 근거 문제를 구분) |

## 3. 쓰는 법

**Security를 테스트 방식으로 돌리기** — `owned-tools.local.json`의 `unit.scopes`에 Security 범위를 넣는다(`configs/owned-tools.example.json`에도 넣어 둠). 평가 쪽 `python -m evaluation.experiment configure`가 Ver1·Ver2 설정 쌍을 만들어 준다.

```json
{"name": "security-tests", "kind": "QA_TESTS", "source_directory": "tests", "pattern": "test_*.py", "role": "SECURITY"}
```

Ver1은 여기에 순환용 보호 범위 두 개(QA·SECURITY)와 보호 케이스 파일(`owned-protected-cases.local.json`)을 더한다. Security 범위가 있으면 승인 정적 증명 파일은 주지 않는다(둘 다 주면 Host가 거부). 예시 설정을 그대로 복사해 예전 정적 증명 방식으로 돌리려면 `security-tests` 범위를 지워야 한다.

**최종 Snapshot 꺼내기**

```bash
python -m agents.platform.snapshot_export --database <Host DB> --run-id <Run> --out <폴더> [--all-versions]
```

`v<N>/source.tar`(저장된 tar 바이트 그대로), `v<N>/manifest.json`(Execution Manifest), `run.json`을 쓴다. 저장 내용 해시·tar 해시·`snapshotSha256`이 같을 때만 쓰고, 덮어쓰기는 `--force`일 때만. 프로젝트 소스는 풀거나 실행하지 않는다.

## 4. 확인한 것

- dev 전체 시험(평가 제외) 3,316개 통과(Security 측정 판정까지 넣은 시점). Snapshot 내보내기·Security 역할 계약까지 넣은 시점에 다시 돌려 3,316개 통과·1개 실패(`test_planner_developer_continuation.py`의 `test_auth_after_source_write_preserves_bytes_and_blocks_model_replay`, 10월 15일부터 기록된 간헐 실패로 이번 변경과 무관). 승인 함수 시험 85개가 3.10·3.13 모두 통과.
- 그 뒤 변경(A2A 본문 내보내기·점검 반영·실험 Run 규칙·Security 오류 코드)은 전체 시험을 다시 돌리지 않고 관련 시험만 돌림: 마지막 확인은 실행 설정·Run API·Host·Host 명령·Security Host·파이프라인·검증 재검증 시험 93개 통과. 그 뒤 2026-10-31에 저장소 전체 pytest(평가 포함, 마지막 평가 커밋의 주석·문서를 고치기 전 상태로 실행 코드는 지금과 같음)를 돌려 4,459개 통과·1개 실패(위와 같은 간헐 실패 시험, 따로 돌리면 통과하기도 함)·1개 건너뜀(실제 Docker 시험).
- dev 파이프라인 시험(가짜 LLM·가짜 docker): Security 테스트가 통과하면 Run이 SUCCESS로 끝나고, 실패하면 Security FAIL·Security Issue가 생긴다. 내보낸 tar·Manifest를 평가 D5가 그대로 읽는다.
- 실제 LLM·Docker로는 아직 돌리지 않았다.

## 5. 함께 정할 것

- **Ver2를 실험 Run으로 선언하는 방법**: `RunConfiguration`은 `experimentId`가 있으면 `protectedTestSuiteRef`를 필수로 요구해 Ver2가 거부됐다(재현함). → 4번 결정(10.30): (가) 실험 Run이라도 보호 테스트 참조 없이 허용하도록 dev를 고침(위 표). 확인 부탁드리는 부분은 이 규칙 변경이다. 팀 문서 `docs/14-orchestrator-contract-compliance.md`의 "experimentId를 지정하면 비교 실험 기준을 완전하게 제출해야 한다" 문장에 이 예외(보호 테스트 참조는 비워 둘 수 있음)를 반영해 주셔야 한다(4번은 팀 문서를 고치지 않았다). 실제 Run API(ASGI·임시 SQLite)로 같은 실험의 Ver1·Ver2 요청이 수정 전 422·수정 후 둘 다 201이고, Host가 두 버전을 모두 승인(Ver2에는 보호 범위·참조 없음)함을 시험(`test_runs_api.py`·`test_owned_host.py`)으로 고정했다.
- **스캐너 경고와 HUMAN_REVIEW**: Security 측정 테스트 모드에서는 정적 증명을 함께 쓰지 않아 스캐너 경고를 CONFIRMED·FALSE_POSITIVE로 확정할 경로가 없다. 경고가 하나라도 SUSPECTED·UNVERIFIED로 남으면 실행 테스트가 모두 PASS여도 Run이 HUMAN_REVIEW로 멈춘다(외부 점검이 합성 경고로 재현). 경고 확인 경로를 만들지, HUMAN_REVIEW를 운영 결과로 둘지 정해야 한다. → 4번 결정(10.30): 일단 HUMAN_REVIEW를 결과로 받아들여 두 버전에 같은 규칙으로 집계하고(최종 채점은 그대로), 시범 실행에서 빈도를 본 뒤 다시 정한다.

- 시작 코드 규격(기업 팀장님과 "시작 코드 규격 에이전트" 여부 논의 뒤). 시작 코드에는 평가 코드(`src/evaluation`)가 들어가면 안 된다 — Host가 시작 커밋의 트리 전체를 에이전트 작업 폴더로 복사하기 때문이다.
- 평가 이미지(시작 코드 lock으로 빌드해 해시 고정, Host와 평가 D5 공통).
- 푸시·dev 병합 시점.
