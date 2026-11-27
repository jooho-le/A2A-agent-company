# 13. Scenario Registry 및 Developer 수정·재검증 루프

> 상태: SCN-001 기준 검증, Issue 영속화, 자동 수정·새 Snapshot 재검증 구현 완료
> 범위: Planner 기준 고정, QA/Security 역할 분리, 최대 3회 Developer 수정, 반복 Issue 검토 전환
> 현재 계약·지원 API·남은 경계는 [14번 정의서 준수 보완](14-orchestrator-contract-compliance.md)을 따른다. 이 문서의 단계별 미완료 항목·검증 수치는 당시 이력이며, 실제 Agent/MCP 팀 통합은 별도 범위다.

## 목표

Planner가 요구사항을 누락하거나 약화해서 성공 판정을 얻지 못하도록 개발정의서의 첫 MVP를 Orchestrator에 기준 데이터로 등록한다. QA 또는 Security에서 재현 가능한 실패가 나오면 이를 Issue로 저장하고 Developer에 전달해 새 코드 Snapshot을 생성하게 한 뒤 Build와 두 Validator를 같은 새 Snapshot에서 다시 실행한다.

## 기준 시나리오

현재 지원하는 Scenario는 `SCN-001` 회원가입 하나다. Run의 `scenarioId` 내부 UUID와 Planner에게 전달하는 `scenarioKey`를 구분한다.

| Key | UUIDv4 | 검증 담당 | 수락 기준 |
| --- | --- | --- | --- |
| REQ-001 | `f2bf2881-d327-4e67-aa24-45fda73e34a9` | QA | 정상 입력 시 사용자 1건 생성 및 성공 응답 |
| REQ-002 | `b5e5c035-a4d1-43e6-9f97-d51b84f022dd` | QA | 합의한 이메일 파서에서 invalid인 값이 DB에 저장되지 않고 오류 응답 |
| REQ-003 | `c74fdf11-fe18-4664-9e19-db2e43d0127e` | QA, Security | 애플리케이션 사전검사와 별개로 DB UNIQUE 제약이 존재하며 동시 요청에서도 중복 생성 불가 |
| REQ-004 | `c1f65c66-a2b6-47f0-8c76-d7ff06133ce3` | QA | 7자 이하는 거부, 8자 이상 정상 처리 |
| REQ-005 | `e3d8975a-5a3f-433e-8da5-e98b9c559166` | Security | 승인한 알고리즘·파라미터 충족, 평문/일반 SHA 계열 저장 없음 |
| REQ-006 | `df6ac4a0-7f90-4783-939f-4d3b568b5c39` | Security | 지정 출력 전체에서 비밀번호 및 저장 Hash 미노출 |
| REQ-007 | `ac22dde4-83ac-45ab-b6dc-dd51b8984531` | QA | 성공은 성공으로, 검증/중복 실패는 실패로 응답 |
| REQ-008 | `4876a535-23e1-4059-941f-bf49441f2276` | Orchestrator | Trace를 통해 전체 연결 관계 복원 가능 |

Scenario UUID는 `f7f9e5c3-ffc3-4b3f-918b-21e1b956ce76`이다. 로그인, OAuth, 이메일 인증, 비밀번호 찾기, 프로필 관리는 범위에서 제외한다. Registry 정의는 [`scenario_registry.py`](../src/orchestrator/domain/scenario_registry.py)에 있고 `SCENARIO_REGISTRY`는 변경 불가능한 Mapping으로 노출한다.

## 처리 흐름

```text
고정 Scenario Contract → Planner Plan 정확성 확인 → Developer 구현·Build
                                                   ↓ Build PASS
                               QA(REQ-001/002/003/004/007) + Security(REQ-003/005/006)
                                                   ↓ 실패
                               append-only Issue 저장 → Developer 수정 요청
                                                   ↓
                               새 codeVersion/Source Snapshot → Build → QA + Security
```

Planner 입력에 canonical Scenario Contract와 Acceptance Criteria를 포함하고, 결과의 Requirement UUID 집합·Key·설명·수락 기준이 Registry와 정확히 같은지 확인한다. 누락·추가·약화·변경 또는 미지원 Scenario는 Developer에 넘기지 않고 `HUMAN_REVIEW`로 보낸다.

Developer는 모든 canonical Requirement를 입력으로 받는다. 검증 Agent Step은 각 역할에 배정된 Requirement만 받고, QA와 Security가 함께 맡는 REQ-003은 양쪽에 모두 전달한다. REQ-008은 Orchestrator 소유 기준이며 final `SUCCESS` 후보가 되려면 Planner 검증, 현재 버전 Developer Artifact, Build PASS, 같은 버전의 QA/Security 결과, 필요 시 Issue와 수정 이력이 Trace에 확인되어야 한다.

## Issue와 재시도 정책

`issue_records`는 SQLite append-only 테이블이다. 각 Issue에는 Run, fingerprint, 실패 시점 `codeVersion`, Source/Report Artifact 참조, Requirement ID, 보고 Agent, 종류·참조 ID·심각도·설명, 연속 반복 횟수가 들어간다. `ISSUE_CREATED` Trace도 Issue 및 근거 Artifact 참조를 기록한다.

QA 실패 테스트, Security Requirement 실패, Confirmed HIGH/CRITICAL Finding, Build 실패를 Developer 수정 Issue로 전환한다. 검증 보고서와 기존 Acceptance Criteria는 수정 대상이 아니다. 14번 보완부터 Developer에게 마스킹된 제목·설명·기대/실제 결과·실패 위치·근거 참조와 Source/Build/QA/Security Record/URI를 전달한다. Issue 및 Source/Change/Build Artifact ID·버전, 보존해야 할 제약과 출력 계약도 함께 전달한다.

| 정책 | 동작 |
| --- | --- |
| 정상적인 코드 수정 | 초기 Candidate 뒤 최대 3회. 각 수정은 새 Developer Step 및 새 `codeVersion`을 사용한다. |
| 같은 Issue가 연속 반복 | fingerprint가 같은 Issue가 2회 연속 재발하면 추가 자동 수정을 멈추고 `HUMAN_REVIEW`로 전환한다. |
| Build 실패 | QA/Security에 넘기지 않고 Build Issue를 기록한 뒤 수정한다. 수정 한도 도달 시 `FINISHED/FAIL`. |
| QA/Security 실패 | 보고서 저장과 `FIX_REQUIRED` 전환 후 Issue를 기록하고 새 Candidate를 만든다. |
| UNVERIFIED | 필수 Tool의 최초 실행+안전 재시도 2회 소진 입증 시 `FINISHED/UNVERIFIED`, 근거 부족은 `HUMAN_REVIEW`. |
| 의심 Finding, MEDIUM 정책 미확정 | 제품 결함으로 추정하지 않고 `HUMAN_REVIEW`. LOW/INFO는 Report 기록만 한다. |
| Agent/A2A 호출 실패 또는 결과가 불명확 | 중복 실행을 무조건 시도하지 않고 `HUMAN_REVIEW`로 보낸다. |

실제 MCP 호출·재시도는 Agent/MCP 담당 범위다. Orchestrator는 14번에서 구조화된 Tool 실행·Retry 이력 및 Manifest를 검증·영속화하고, 한도 소진의 근거가 있을 때만 `UNVERIFIED`를 최종화한다.

## 상태 및 Artifact 보장

- 초기 Candidate는 `IMPLEMENTING → SNAPSHOT_READY → VALIDATING` 순서로 처리한다.
- 검증 실패는 `FIX_REQUIRED → FIXING → REVALIDATING`으로 잇는다. 수정 Build 실패는 다음 bounded fix를 위해 `FIX_REQUIRED`로 돌아간다.
- 매 수정 Candidate의 Source, Change, Build는 이전 Artifact와 연속 lineage를 가져야 한다. `codeVersion = fixAttempt + 1`이며 보고서 모두 새 Source의 동일 Execution Manifest를 참조한다.
- QA/Security 보고서는 별도 Step UUID를 사용하고, 각 Report의 `requirementIds`는 자기 Step의 담당 범위와 일치해야 한다. 각 새 보고서는 Artifact lineage에서 직전 버전을 참조한다.
- 각 영속화 단위는 SQLite transaction으로 보호한다. Candidate Artifact와 상태 기록, QA/Security Report와 validation decision, Issue와 `ISSUE_CREATED` Trace 및 수정 Step/상태 전환을 각각 원자적으로 커밋한다. Issue 및 Project Artifact 테이블에는 UPDATE/DELETE 금지 Trigger가 있다.

## 개발정의서 대조

| 기준 | 상태 및 증거 |
| --- | --- |
| SCN-001과 REQ-001~008의 고정 수락 기준 | Registry로 구현. Planner가 기준을 약화하는 테스트에서 Developer Step 생성 없이 `HUMAN_REVIEW` 확인 |
| QA/Security 담당 요구사항 분리 | 역할별 Workflow Step Requirement 검증과 Report 테스트 반영 |
| QA/Security 결과가 Developer에게 수정 근거로 전달 | Report 참조 Issue와 `fixRequest` 전달, 동일 오류를 PASS로 바꾸는 통합 테스트에서 새 Snapshot 재검증 성공 |
| 최대 3회 수정 및 동일 Issue 2회 반복 한도 | 서로 다른 Issue 세트로 최대 수정 횟수 종료, Build/QA/Security 동일 Issue 반복 시 사람 검토 전환 테스트 |
| Trace 복원 | Issue, 수정 시작, 새 버전 Developer/Build/QA/Security 이벤트의 Run/Step/Requirement/Artifact/Code 참조 저장 |
| 실제 MCP Build 실행, 실제 서비스 코드/Agent 검증, 단일 vs Multi-Agent 측정 | 팀원 구현 및 연결 전이므로 미완료. 이 문서의 Fake Client 테스트는 프로토콜·오케스트레이션 증거이지 실제 제품 검증 증거가 아님 |
| Archive 원본 bytes 검증 및 Snapshot READ_ONLY 강제 | 실제 Artifact Store·MCP 권한 구현 후 별도 통합 확인 필요 |

정의서의 MVP 완료를 주장하려면 실제 회원가입 서비스에서 MCP Build와 QA/Security가 동작하고, 동일 Snapshot 검증 및 최소 한 번의 실제 수정·재검증, Trace replay까지 확인해야 한다.

## 검증 및 Git 인계

```bash
PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -v
```

커밋 메시지(아직 커밋하지 않음): `시나리오 기준 및 자동 수정 재검증 루프 구현`

14번에서 Human Review 재개·중단 Step 안전 복구와 정의서 준수 보완을 구현했다. 실제 Agent/MCP 서버 연결과 팀 통합은 사용자가 제외한 별도 범위다.
