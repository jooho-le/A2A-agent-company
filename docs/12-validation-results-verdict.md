# 12. QA/Security 결과 검증 및 Verdict

> 상태: 12번 작업 당시의 QA/Security Report 검증·저장 및 제한된 Verdict 판정 구현 기록
> 후속: Scenario Registry와 Developer 수정·재검증 루프는 [13번 작업](13-scenario-registry-fix-revalidation.md)에서 구현됨

## 목적

QA/Security Agent의 A2A Task가 `TASK_STATE_COMPLETED`인 사실만으로 제품이 성공했다고 판단하지 않는다. Task의 Report Artifact를 계약대로 검증하고, Build·QA·Security 결과가 같은 Source Snapshot과 실행환경을 가리키는지 확인한 뒤에만 상태를 전이한다.

현재 Orchestrator는 고정된 Scenario/Requirement Registry를 아직 조회하지 않는다. 따라서 Agent가 반환한 Planner Requirement만 통과한 결과를 최종 `SUCCESS`로 단정할 수 없다. 검증 자체가 전부 PASS여도 현재 연결 경로는 `HUMAN_REVIEW`로 보류한다.

> 이 문서의 “현재” 및 미완료 표는 12번 구현을 마쳤을 때의 상태를 보존한다. 13번 이후 기준은 링크한 후속 문서를 참조한다.

## 구현 위치

| 경로 | 내용 |
| --- | --- |
| `src/orchestrator/domain/validation_artifacts.py` | 불변 QA/Security Report 및 test/finding 모델 |
| `src/orchestrator/application/validation_output.py` | A2A 출처·metadata·Schema·Requirement·Manifest 검증과 Verdict 정책 |
| `src/orchestrator/application/dispatch.py` | 두 Agent 결과 취합, Report 검증, 최종화 호출 |
| `src/orchestrator/infrastructure/sqlite_workflows.py` | Report Artifact, Step output, Verdict/Trace 원자 저장과 SQLite Schema migration |
| `schemas/project/qa_report.schema.json` | QA Report Data Part 계약 |
| `schemas/project/security_report.schema.json` | Security Report Data Part 계약 |
| `schemas/project/execution_manifest.schema.json` | Build/QA/Security 공통 Execution Manifest |

## A2A Report 계약

각 Agent Task는 완료 후 역할에 맞는 Artifact를 정확히 하나 반환한다. Artifact는 A2A `artifactId`와 프로젝트 UUIDv4 `artifactId`를 분리하며, `application/json` Data Part 하나와 아래 프로젝트 metadata를 가진다.

| Agent | A2A Artifact 이름 | Data Part |
| --- | --- | --- |
| QA | `qa-report.json` | [`qa_report.schema.json`](../schemas/project/qa_report.schema.json) |
| Security | `security-report.json` | [`security_report.schema.json`](../schemas/project/security_report.schema.json) |

공통 metadata는 `runId`, `workflowStepId`, `projectArtifactId`, `artifactVersion` 네 필드뿐이다. Data Part에는 Run/Step/Task/A2A Artifact ID, 생성 역할, Requirement IDs, `codeVersion`, 공통 Execution Manifest, 생성시각과 결과가 들어간다. Schema에 없는 필드·필수 필드 누락·중복 결과 Artifact는 거부한다.

QA는 각 Planner Requirement에 하나 이상의 `tests[]` 결과를 제공하고, 각 결과는 `PASS`, `FAIL`, `UNVERIFIED` 중 하나다. Security는 각 전달받은 Requirement별 `requirementResults[]`와 `findings[]`를 제공한다. Finding의 심각도는 `CRITICAL/HIGH/MEDIUM/LOW/INFO`, 판정은 `CONFIRMED/SUSPECTED/FALSE_POSITIVE`다.

## 검증과 최종 판정

Report 등록 전 아래 조건을 확인한다.

- 완료된 A2A Task ID·상태, 역할별 Workflow Step, Step Requirement IDs 및 A2A Artifact ID와 일치한다.
- Artifact 이름/개수, JSON Data Part 개수·media type, metadata 키·값 및 엄격한 payload Schema가 맞다.
- QA는 모든 전달 Requirement를 테스트로 덮고, Security는 모든 전달 Requirement에 결과를 제공한다.
- Report `codeVersion`/Manifest가 Source Snapshot 및 Build PASS와 같고, 특히 project Source Artifact ID·commit/tree/snapshot hash·container digest·lock hash가 모두 일치한다.
- Source/Build가 같은 Run에 등록되어 있고 성공 Build임을 Registry에서 재확인한다.

| 검증 근거 | Run 처리 |
| --- | --- |
| QA 필수 Test 또는 명시 Requirement 실패, Confirmed CRITICAL/HIGH Finding | 수정 한도 전 `FIX_REQUIRED`; 수정 한도 소진 시 `FINISHED/FAIL` |
| 필수 결과 `UNVERIFIED`이나 retry 기록/자동 Tool 재시도가 없음 | `HUMAN_REVIEW`; 제품 FAIL로 오인하지 않음 |
| Confirmed MEDIUM/LOW 또는 SUSPECTED Finding | 정책/재현 확인 전 `HUMAN_REVIEW` |
| 잘못된/누락 Report, 다른 Snapshot, Agent Task 실패·timeout 등 | `HUMAN_REVIEW`; 결과 일부를 Registry에 남기지 않음 |
| 모든 QA/Security 결과 PASS, blocking finding 없음, 기준 Requirement가 권위 있는 Registry에서 확인됨 | `FINISHED/SUCCESS` 후보 |

현재 마지막 행의 전제인 권위 있는 Requirement Registry가 연결되지 않았으므로 실제 dispatcher에서는 PASS 보고서도 `HUMAN_REVIEW`로 끝난다. 이는 Planner가 요구사항을 낮추거나 생략해도 성공으로 처리하는 것을 막기 위한 의도적인 차단이다. `decide_verdict`에는 이후 Registry 연결 시 성공 판정을 허용할 입력이 있지만 현재 dispatcher는 그 값을 승인하지 않는다.

Confirmed CRITICAL/HIGH는 명시 Requirement 결과가 PASS여도 SUCCESS를 막고 수정 요구로 보낸다. MEDIUM/LOW 정책은 개발정의서에서 팀 미결정이므로 현재 자동 성공·실패시키지 않고 사람 검토로 보낸다. FALSE_POSITIVE는 blocker로 보지 않는다. `UNVERIFIED`를 전체 `UNVERIFIED`로 확정하려면 정의서대로 Tool/Infrastructure 재시도 한도 소진을 알아야 하지만, 현재 Report/A2A 연결에는 retry count와 자동 재시도 경로가 없으므로 성급한 최종 Verdict를 만들지 않는다.

Build가 수정 한도에 도달한 뒤에도 실패하면 `FINISHED/FAIL`로 기록한다. QA/Security의 수정 한도 내 실패는 `FIX_REQUIRED`까지 기록하지만, 자동 Developer 수정 호출이나 새 Snapshot 재검증은 아직 연결하지 않았다.

## 영속화와 로그

QA/Security Report 두 건, 해당 WorkflowStep의 `outputArtifactIds`, Run 상태/Verdict와 검증 Trace를 SQLite transaction 하나로 커밋한다. 검증 또는 DB 기록 중 하나라도 실패하면 Report Artifact를 부분 등록하지 않고 `HUMAN_REVIEW`로 남긴다. Trace에는 `QA_REPORT_VALIDATED`, `SECURITY_REPORT_VALIDATED`, `VALIDATION_DECISION_{상태}`를 기록하고 Run/Step/Task/Requirement/Artifact/Code/Snapshot 참조를 연결한다.

기존 `project_artifacts`의 CHECK 제약에는 QA/Security 타입이 없어서, 초기화 때 기존 행을 보존하며 허용 타입을 확장하는 transactional table migration을 수행한다. Artifact append-only UPDATE/DELETE Trigger도 다시 만든다. 새 테이블은 `QA_REPORT`, `SECURITY_REPORT`를 기존 Source/Change/Build Registry와 같은 목록/API 경로로 조회한다.

## 개발정의서 대조 및 미완료 사항

| 항목 | 상태 |
| --- | --- |
| A2A `COMPLETED`와 검증 PASS를 구분 | 반영 |
| QA/Security 역할별 Report Schema, source/Step/Task/Artifact 참조 검증 | 반영 |
| Requirement별 QA 테스트·Security 결과 coverage 확인 | 반영; 현재 Planner가 준 Requirement 집합 기준 |
| Build/QA/Security 동일 Manifest 검증 | 반영; Archive bytes의 실제 hash 확인·READ_ONLY ACL 강제는 Artifact Store 미연동으로 미완료 |
| CRITICAL/HIGH blocker, Fix 한도 후 FAIL, 미확정 결과 사람 검토 | 반영 |
| 고정 Scenario/Requirement/Acceptance Criteria 원본과 Security Requirement 분류 | 미완료; 권위 baseline 없으므로 자동 SUCCESS 차단 |
| Issue Registry에 Finding 저장 및 반복 Issue fingerprint 판단 | 미완료; Finding은 Security Report Artifact에 보존 |
| `FIX_REQUIRED → FIXING → REVALIDATING` 자동 Developer 요청, 새 Candidate, Build/QA/Security 재호출 | 미완료; 다음 구현 작업 |
| MCP/Infrastructure retry count 및 한도 후 전체 `UNVERIFIED` 처리 | 미완료; 지금은 retry를 위조하지 않고 `HUMAN_REVIEW` |
| 중단 후 preclaimed QA/Security Step 자동 재개 | 미완료; 재시작/외부 효과 안전 재개 정책 필요 |

개발정의서의 MVP 성공 기준에는 실제 MCP Tool 수행, 권위 있는 공통 평가 기준, 수정 루프 검증 및 Trace 복원이 포함된다. 이번 구현은 그 전체 완료를 의미하지 않으며, Agent 실서비스와 Artifact Store/MCP 권한 구현은 통합 후 별도 검증해야 한다.

## 검증

```bash
PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -p 'test_validation_artifacts.py' -v
PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -p 'test_dispatch.py' -v
PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -v
```

검증 범위: 엄격한 Report Schema 및 모델 불변성, QA 실패/Fix limit 판정, Security blocker와 미결 정책 Finding, UNVERIFIED의 사람 검토, QA/Security 결과 동시 저장, 기존 Source/Build 보존, Trace 연결, 잘못된/누락 Report 거부, Build/Manifest 동일성이다.

## 다음 작업

13번은 고정 Scenario/Requirement Registry와 Security Requirement 분류를 먼저 연결하고, 이후 `FIX_REQUIRED`에서 실패 Report/Finding을 Developer Fix Request로 전달해 새 codeVersion의 Source/Build 및 QA/Security 재검증까지 안전하게 잇는다. Tool retry와 Run 재시작 복구는 Fix Attempt와 분리된 정책으로 다룬다.
