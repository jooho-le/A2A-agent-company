# 47. Single-Agent 실행 계약과 후속 구현 계획

작성 기준일: 2026-10-31. 담당: 주호(역할 1·2).

상태: **개발정의서와 현재 코드를 대조한 설계 문서**. 47번은 실행 계약을 정리하는 단계이며, Single-Agent 서버·실행기·설정 Schema는 아직 구현하지 않았다. 다음 작업은 48번 실행 설정 확장이다.

## 1. 근거와 현재 구현 상태

사용자가 제공한 개발정의서 §0·§2·§4·§5·§8·§9·§10·§11·§11-A를 기준으로 한다. Single-Agent의 근거는 §11-A의 “기획 + 구현 + 검증 + 수정”, 동일한 전체 Tool Capability, 동일한 수정 기회다. 이 문서의 실행 단계·정체성 분리·구현 순서는 그 계약을 구현하기 위한 프로젝트 설계다.

| 대조 대상 | 확인한 상태 |
| --- | --- |
| 작업 브랜치 `jooho` / `6e62ac3` | 기존 네 Agent와 공통 플랫폼 구현. `architecture`는 `MULTI_AGENT`만 허용 |
| 로컬 `dev` / `a99c59c` | 석민의 PR #9와 주호의 PR #10이 반영된 코드. 이 문서 작성 중 원격 재조회·브랜치 변경·병합은 하지 않음 |
| `dev`의 `docs/evaluation/single_baseline.md` | Single 실행기는 주호, 평가 기준·집계는 석민 담당. 실행기는 아직 없음 |
| `dev`의 `docs/evaluation/dev_changes_2026-10-30.md` 및 실제 코드 | Security 실행 테스트, Snapshot/A2A 내보내기, Ver2 실험 Run 허용을 확인 |
| `dev`의 `src/evaluation/experiment.py` | 실험 설정·결과 수집·독립 채점·보관·비교 도구. Single 실행기는 아님 |

현재 작업 트리에 없는 `dev` 전용 파일은 위 표의 경로와 커밋을 기준으로 읽었다. 후속 구현에서는 이 변경과 호환성을 대조하고, 예전 `jooho` 동작으로 되돌리지 않는다. 브랜치를 병합하거나 평가 코드를 이 문서에 복제하지 않는다.

과거 [45번 인계](45-owned-platform-handoff.md)의 Single 기준선 담당 설명은 당시 기록이다. 이후 사용자 지시와 10.29 회의 기록에 따라 **Single 실행 경로는 역할 1·2, 공통 시험지·실험 집계는 역할 4**로 구분한다.

## 2. Single-Agent의 정의와 책임

한 Run에서 **하나의 Agent 정체성과 이어지는 대화 기록**으로 요구사항 분석·기획·제품 코드 작성·자체 테스트 작성·보안 검사·수정을 수행한다. 모델 호출은 여러 번 할 수 있고, 수정 기회도 Multi-Agent와 같다.

기획·개발·QA·Security는 처리 단계다. 단계를 바꿀 때 네 독립 Agent를 호출하거나 대화 기록을 초기화하는 방식으로 Single을 구성하지 않는다. 모델의 숨겨진 추론을 수집하는 것이 아니라, 다음 단계에 필요한 메시지·구조화된 산출물·도구 결과를 보존한다.

| 구성요소 | 책임 |
| --- | --- |
| Single-Agent | 고정 요구사항 분석, 계획·코드·자체 테스트 작성, 실제 도구 결과 해석, 결함 수정 |
| Orchestrator / 신뢰된 Host | Run·Step 생성, 단일 Agent 호출, 단계·권한·예산 강제, Snapshot/증거 검증, 재시도·종료·최종 Verdict 계산 |
| MCP / Sandbox | 승인된 도구 실행, 파일 접근 제한, 실행 증거와 보고서 생성 |
| 역할 4 Evaluation | 동일한 공통 시험지로 별도 채점, 비교 가능 여부와 지표·결과 집계 |

요구사항·Acceptance Criteria의 권위는 동결한 Scenario 계약에 있다. Single이 요구사항을 정리해도 필수 항목을 삭제하거나 보안 조건을 완화할 수 없다. 모델의 “완료” 응답은 제품 성공의 근거가 아니다.

## 3. 실행 설정과 통신

- 공개 Run 설정의 `architecture`에 `SINGLE_AGENT`를 추가한다. 기존 `MULTI_AGENT` 값과 기본값, 기존 요청·저장 데이터의 의미는 유지한다. Python 모델·프로젝트 JSON Schema·Host 승인·실행 경로가 같은 값을 처리해야 한다.
- 시작 시 RunConfiguration을 불변 Artifact로 저장한다. 실행 도중 구조·모델·시작 코드·도구 정책을 바꿔 같은 Run을 이어가지 않는다.
- 단일 Agent의 프로젝트 식별자와 현재 업무 단계를 분리한다. 네 역할의 기존 enum을 무조건 순회하는 서버·DB·권한·Trace 코드는 후속 단계에서 점검한다. Single을 네 역할로 가장해 기록하지 않는다.
- A2A는 개발정의서의 **1.0 HTTP+JSON/REST**를 따른다. `POST /message:send`와 `returnImmediately=true`, 서버가 발급한 Task ID 저장, `GET /tasks/{id}` 폴링을 재사용한다.
- 같은 Run의 단계·수정·재개는 단일 Agent의 Context와 기록을 이어 쓴다. 단계가 달라 새 Task가 필요해도 서버의 발급 규칙을 따르며, Task 개수와 Agent 개수를 혼동하지 않는다.
- 프로젝트 내부 ID는 UUIDv4, A2A Task/Context/Artifact ID는 서버의 opaque 값을 유지한다. `SINGLE_AGENT`를 A2A 공식 Role enum에 추가하지 않는다.
- MCP는 개발정의서의 **2026-07-28, 로컬 stdio**와 현재 고정된 의존성을 사용한다.

이 절은 후속 구현 계약이다. 47번에는 새 API·CLI·포트·Schema 필드 구현을 추가하지 않았다.

## 4. 처리 흐름과 출력

```text
사용자 요청 + 동결한 Scenario/RunConfiguration
  ↓
같은 Single-Agent: 요구사항 분석·계획
  ↓
같은 Single-Agent: 최초 구현 (fixAttempt=0)
  ↓
불변 Source Snapshot 생성 (codeVersion=1) + Build
  ↓ Build PASS
같은 Single-Agent: QA 검사 → Security 검사
  ↓
Host: 같은 Snapshot/환경의 실제 근거 확인 → Orchestrator 판정
  ├─ 필수 검증 PASS → SUCCESS
  ├─ 제품 결함 → 같은 Agent에 수정 요청 → 새 Snapshot → Build/QA/Security 재검증
  └─ 검증 불가·판단/승인 필요 → 기존 UNVERIFIED/HUMAN_REVIEW 규칙
```

Single의 QA·Security 작업은 같은 Agent가 순서대로 수행한다. 별도 Agent 병렬 호출을 Single 경로에 넣지 않는다. 도구 자체의 실행 방식은 양쪽에 같은 승인 프로파일을 적용한다.

Build가 제품 코드 오류로 실패하면 성공 검증 단계로 넘기지 않고 수정 대상으로 처리한다. 검증 중 제품 Source를 바꿔야 하면 현재 후보의 검사를 중단하고 수정 단계로 돌아간다.

| 산출물 | 계약 |
| --- | --- |
| 계획·요구사항 | 기존 Requirement ID·Acceptance Criteria와 연결. 필수 항목 누락·변경 거부 |
| Source·변경 보고서 | 새 코드 후보·변경 파일·수정 내용·이전 버전과 연결 |
| Build·QA·Security 보고서 | 실제 도구 실행 근거·요구사항·테스트/검사 ID·Execution Manifest와 연결 |
| Issue·수정 요청 | 원인·증거·현재 코드 버전·이전 Issue와 연결 |
| 최종 결과 | Orchestrator Verdict와 근거. 별도 독립 평가의 `evaluationVerdict`와 구분 |

기존 Artifact/보고서 구조를 재사용한다. Single 정체성 때문에 프로젝트 Schema 확장이 필요하면 생산·저장·조회·검증을 함께 수정하고, 기존 Multi 데이터의 유효성을 보존한다. 출처를 네 독립 Agent인 것처럼 만들거나 Schema 검사를 생략하지 않는다.

## 5. MCP 기능과 보호 경계

Single은 Multi 네 역할의 **전체 기능 합집합**을 제공받는다. 실제 호출 방식과 Host 전용 실행 책임은 비교하는 Multi 설정과 같게 유지한다. 예를 들어 보호 테스트 실행을 Host가 담당하는 구성에서는 Single 모델에도 보호 실행 권한을 직접 주지 않는다.

| 기능 | 사용할 기존 도구·경계 |
| --- | --- |
| 코드 읽기 | `read_project_file`, 승인된 Run Workspace/Source 범위 |
| 제품 코드 작성·수정 | `write_source_file`, `apply_patch`; 구현·수정 단계의 Working Source만 |
| 빌드 | `run_build`; 승인된 Build 프로파일과 현재 후보 |
| 자체 테스트 작성 | `write_test_file`; QA·Security 출력 경로를 구분하고 제품 코드나 보호 시험지에 쓰지 않음 |
| 기능·보안 실행 테스트 | `run_unit_tests`, `run_browser_tests`; 승인된 범위·정책·실행 주체 유지 |
| 보안 스캔·결과 읽기 | `run_security_scan`, `read_security_report` |
| 테스트 결과 읽기 | `read_test_report`; 같은 Run·후보의 실제 실행 기록 |

도구의 집합이 넓어진다고 파일 접근 범위나 신뢰 경계가 넓어지는 것은 아니다. Host가 단일 Agent 정체성·Run·현재 단계·출력 경로를 검증한다. 모델이 전달한 역할명·Metadata·임의 경로로 권한을 바꾸지 못한다.

공식 검증 단계의 Frozen Snapshot은 읽기 전용이다. Agent가 작성한 테스트와 Host가 생성한 실행 증거·확정 보고서는 구분한다. 동결 요구사항, 보호 테스트, 실행 증거·Registry·Host DB는 Agent가 수정할 수 없다. Docker 격리·네트워크·Secret 정책은 Multi와 동일하게 적용한다.

## 6. Snapshot과 최종 판정

Build·QA·Security의 Manifest에는 기존 [Execution Manifest Schema](../schemas/project/execution_manifest.schema.json)의 `repositoryId`, `codeVersion`, `projectArtifactId`, `commitHash`, `gitObjectFormat`, `treeHash`, `snapshotSha256`, `containerImageDigest`, `dependencyLockHash`를 보존한다.

세 검사 모두 **같은 코드 후보·Snapshot·실행환경**이어야 한다. 수정하면 새 Artifact와 코드 버전을 만들고 필요한 검증을 다시 수행한다. 이전 후보의 PASS를 새 후보에 복사하거나 기존 불변 Artifact를 덮어쓰지 않는다.

최종 Verdict는 기존 `SUCCESS`·`FAIL`·`UNVERIFIED`·`HUMAN_REVIEW` 규칙을 재사용한다. `FIX_REQUIRED`는 진행 상태, `ABORTED`는 취소 상태이며 별도 성공/실패 Verdict로 추가하지 않는다. A2A `TASK_STATE_COMPLETED`는 전체 제품 `SUCCESS`와 다르다.

## 7. 수정·재시도·입력·취소·예산

| 항목 | 계약 |
| --- | --- |
| 최초 구현 | `fixAttempt=0`, `codeVersion=1` |
| 코드 수정 | 최대 3회. 최초 후보를 포함해 최대 4개 후보 |
| 도구 재시도 | 동일한 논리 호출당 최초 1회 + 재시도 최대 2회 |
| 제품 검사 실패 | 도구 재시도로 감추지 않고 제품 결함으로 분류 |
| 쓰기·전송 결과 불명확 | 적용 여부 확인 전 자동 재실행 금지 |
| 같은 Issue 반복 | 기존 fingerprint·반복 판정과 HUMAN_REVIEW 규칙 적용 |
| 입력·인증·사람 검토 후 재개 | 같은 작업 기록·확인된 결과·후보·수정 횟수·남은 예산 유지 |
| 취소 | 실제 작업 종료/취소 확인 후 ABORTED 기록. 종료 여부가 불명확하면 임의 성공 처리하지 않음 |
| 재시작 복구 | 저장된 Context·Task·Trace·예약된 사용량을 확인하고 기존 복구 정책 적용 |
| 전체 예산 | 기획·개발·검증·수정·대기·재개에 같은 Run deadline과 모델/도구 호출·토큰 예산 적용 |

단계마다 예산을 새로 지급하지 않는다. 토큰·호출 상한도 비교하는 Multi와 같은 Run 단위 기준으로 맞춘다. 실제 모델·단가·실행시간·자원 수치·반복 횟수는 임의로 정하지 않고 승인 설정을 사용한다.

## 8. 공정한 비교와 석민의 변경 반영

| 양쪽에서 같게 유지할 조건 | 기록·확인 기준 |
| --- | --- |
| 시작 코드·Snapshot | 동일한 전체 Commit ID·Snapshot SHA-256, Run별 새 Workspace |
| 요구사항·Acceptance Criteria | 같은 Scenario와 동결 계약 |
| 모델 | 같은 Provider·Model ID·Temperature, Revision과 Seed는 지원 범위에서 일치·기록 |
| 도구 | 동일한 전체 Capability, 같은 승인 프로파일·실행 정책 |
| 수정·재시도·예산 | Fix 3회·Tool Retry 2회, 같은 Run 전체 한도 |
| 실행환경 | 같은 이미지 Digest·Dependency Lock·Hardware·Network·Secret 정책 |
| 스캐너 | 같은 Version/Rule |
| 평가 | 같은 비교 방식·결과 공개 정책·공통 채점 시험지와 그 지문 |
| 실행 순서·준비 상태 | `executionOrder`·`warmupState` 기록. 교대 순서와 반복 횟수는 실험 계획에서 결정 |

### Ver1·Ver2는 구조 선택과 별개

| 평가 방식 | 개발 순환 | 최종 독립 채점 |
| --- | --- | --- |
| Ver1 | 에이전트 자체 검사 + 순환용 보호 테스트 | 별도 공통 시험지 |
| Ver2 | 에이전트 자체 검사, 순환용 보호 테스트 없음 | 별도 공통 시험지 |

Single-Ver1과 Multi-Ver1, Single-Ver2와 Multi-Ver2처럼 같은 방식 안에서 구조를 비교한다. Ver1·Ver2 자체를 Single·Multi로 부르지 않는다.

최종 채점용 시험지(pilot/final)와 그 결과를 개발·수정 Agent에 전달하지 않는다. Ver1 순환용 보호 테스트의 피드백은 양쪽에 같은 공개 정책을 적용한다. 최종 평가 코드·시험지·결과 파일을 시작 Source나 Agent가 읽을 수 있는 Source에 넣지 않는다.

`dev`에 반영된 다음 사항을 후속 구현에서 유지한다.

1. `experimentId`가 있어도 Ver2는 `protectedTestSuiteRef` 없이 등록할 수 있다. 나머지 필수 실험 기준은 유지하고, 비교 시 양쪽의 순환용 보호 테스트 조건과 최종 채점 시험지를 확인한다.
2. Security 실행 테스트 구성을 Single도 처리한다. 자체·보호 테스트는 신뢰된 실행 결과로 판정하고, 측정 테스트 방식과 승인 정적 의미 검증 방식을 동시에 선택하지 않는 기존 Host 검사를 유지한다.
3. 미확정 Scanner 경고를 테스트 PASS만으로 지우지 않는다. 측정 테스트 방식의 HUMAN_REVIEW 정책을 양쪽에 동일하게 적용한다.
4. `agents.platform.snapshot_export`의 원본 tar·Manifest·`run.json` 인계 형식과 무결성 검사를 재사용한다. 실제 Single Run으로 호환성을 확인하는 작업은 55번이다.
5. `agents.platform.a2a_export`는 현재 네 역할의 저장소와 누락 역할을 기준으로 한다. Single 정체성과 대화 기록 수집을 별도로 지원해야 하며, 기존 네 역할 기록을 만든 것처럼 꾸미거나 Single의 정상 구성을 누락으로 오판하지 않도록 55번에서 확장한다.

평가 도구가 이미 `SINGLE_AGENT` 보고서를 받는다는 사실만으로 Single 실행이 구현된 것은 아니다. 최종 공통 시험지·채점·집계는 역할 4의 기존 기능을 사용하고, 실행기에서 점수 규칙을 새로 만들지 않는다.

## 9. 기록·지표 인계

Run·Step·실제 Agent 정체성·업무 단계·Task/Context·Artifact·Issue·도구 실행 근거를 연결한다. 같은 Agent의 QA 단계와 독립 QA Agent 실행을 기록에서 구별할 수 있어야 한다.

Run 시작, 첫 Build PASS, 판정·종료, 사람 대기 시간을 기록한다. 전체 시간은 wall clock으로 측정하며 단계 시간을 더해 대체하지 않는다. 모델 호출·입출력 토큰·비용·수정·재시도는 Run 전체를 집계한다. 미확인 사용량/비용은 `null` 또는 불완전 상태로 표시하고 0으로 만들지 않는다.

기존 조회 API와 Snapshot/Manifest 인계가 Single도 처리하도록 확장한다. SUCCESS뿐 아니라 FAIL·UNVERIFIED·HUMAN_REVIEW·취소/중단 기록도 보존한다. 마지막 Snapshot이 없는 실행은 그 사실과 실패 원인을 기록하며 평가 가능한 결과물로 꾸미지 않는다.

## 10. 47~56번 구현 순서

| 번호 | 작업 | 완료 기준 |
| --- | --- | --- |
| 47 | 실행 계약·공정성·경계 정리 | 원문 및 `dev` 변경과 대조한 이 문서, README 안내 |
| 48 | 실행 설정 확장 | Python 모델·프로젝트 Schema에 구조 선택 반영, 기존 Multi와 Ver2 실험 기준 보존 |
| 49 | 단일 Agent 서버 기반 | 단일 정체성·Agent Card·A2A Task·전용 저장소, 기존 네 역할과 충돌 없는 구성 |
| 50 | 통합 Prompt·작업 기록 | 기획·개발·QA·Security·수정의 같은 Agent 기록 유지와 출력 계약 |
| 51 | MCP 기능·권한 | 도구 합집합 제공, 단계/경로 검증, Snapshot·보호 기준·증거 수정 차단 |
| 52 | 최초 구현·검증 실행 | 요청부터 계획·코드·Build·QA·Security까지 단일 실행 경로 연결 |
| 53 | 수정·재검증·판정 | 최대 3회 수정, 새 후보의 동일 Manifest 검사, 기존 Verdict·Issue 정책 |
| 54 | 제어·복구·예산 | 입력/인증·사람 검토·취소·재시작과 Run 전체 예산 보존 |
| 55 | 기록·평가 인계 | 시간/사용량/비용·Snapshot 내보내기·단일 Agent 대화 기록 인계 |
| 56 | 전체 점검·실행 안내 | Single 흐름과 Multi 회귀 확인, 정의서 대조표·실행 안내와 미검증 경계 정리 |

매 단계에 개발 문서, 변경에 맞는 검증 결과, 개발정의서 대조, 한국어 커밋 메시지와 다음 번호를 남긴다. 56번까지 코드 구현·내부 검증을 수행하고, 승인된 API 키·Docker를 사용하는 실제 환경 실행 확인과 비교 실험은 별도 결과로 기록한다.

## 11. 개발정의서 대조 및 47번 검증

아래는 **설계 대조 결과**이며 구현 테스트 통과를 의미하지 않는다.

| 원문 기준 | 이 문서의 반영 |
| --- | --- |
| §0·§3·§6·§7 통신/식별자 | §3: 기존 A2A/MCP·ID 유지, 프로젝트 정체성과 공식 A2A Role 구분 |
| §2 책임 경계 | §2·§5: Single은 §11-A에 따른 통합 업무 주체, Host/MCP/독립 평가 경계 보존 |
| §4 상태·수정·재시도 | §6·§7: Fix 3회·Retry 2회, A2A/Workflow/Verdict 분리, 제어·복구 |
| §5·§9 동일 Snapshot·Artifact | §4·§6: 불변 후보·Manifest·증거·새 버전 재검증 |
| §8 도구/격리 | §5: 도구 합집합과 보호된 경로·기준·증거, 같은 Sandbox/Secret 정책 |
| §10 최종 판정 | §2·§6: 실제 검증 근거로 Orchestrator 판정, 자기 선언·Task COMPLETED로 PASS 금지 |
| §11 추적 | §9: 요구사항→검사→Issue→수정→재검증, 실제 정체성·사용량·불완전성 기록 |
| §11-A 공정성 | §7·§8·§9: 동일 조건·수정 기회·공통 평가, 실패 Run 보존·지표 인계 |
| 원문 이후 평가 변경 | §1·§8: Ver2 참조 생략·Security 실행 테스트·내보내기는 `dev`의 후속 변경으로 명시 |

47번 검증 결과:

- 원문과 명시한 커밋의 코드·문서를 대조해 위 설계 기준과 후속 변경을 구분했다.
- 새 문서와 README 추가 부분의 로컬 Markdown 링크 **3개가 모두 존재**함을 확인했다.
- 문서 절 1~11, 로드맵 47~56의 번호 순서와 새 문서의 whitespace를 확인했다.
- `git diff --check`가 통과했다. 변경 범위는 이 문서와 README뿐이다.

실행 코드·의존성·설정·Schema를 수정하지 않아 런타임 테스트는 실행하지 않았다. 기존 검증 수치를 이번 결과로 인용하지 않으며, 실제 LLM·Docker·Single Run·비교 실험 완료를 선언하지 않는다.

커밋 메시지: `싱글에이전트 실행 계약과 공정 비교 기준 정리`

다음 작업: **48번 — SINGLE_AGENT 실행 설정과 프로젝트 JSON Schema 확장**.
