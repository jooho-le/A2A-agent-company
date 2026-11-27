# 35. 실제 수정·동일 Snapshot 재검증 루프

작성 기준: 2026-10-09. 범위: 1번 Orchestrator + 2번 Agent/MCP의 실제 수정·재검증. 3번 제품 서비스·4번 독립 평가·팀원 통합은 변경하지 않는다.

## 1. 구현 내용

- owned 플랫폼에서도 기존 Orchestrator의 수정 흐름을 활성화한다. 저장된 제품 결함과 `FIX_REQUIRED`가 있어야 새 Developer 수정 Task를 발행한다.
- Developer는 초기 구현뿐 아니라 `FIXING`을 처리한다. 저장된 Issue·직전 Source/Change/Build·해당 후보의 검증 보고서와 실제 생산 Step을 확인하고 기존 Fix Request를 재구성한다.
- 승인된 최초 Git baseline과 수정의 부모 Commit을 구분한다. Run의 `startingCommitHash`를 바꾸지 않고 직전 후보 Commit을 새 수정의 부모로 사용한다. 수정 시작 시 실제 Working Tree가 그 후보와 일치해야 한다.
- 새 Source는 기존 Artifact Store로 동결한다. Source/Change/Build는 새 UUID·증가한 버전·`previousArtifactId`를 사용하며 기존 결과물을 덮어쓰지 않는다.
- QA/Security는 `VALIDATING`과 `REVALIDATING`을 처리한다. 저장된 현재 Source·private Snapshot·읽기 권한·생산 Step·이전 Source/보고서 연결을 검증한다.
- 이전 보고서가 더 과거 후보를 대상으로 하는 경우에도 그 실제 Source와 전체 Manifest를 연결한다. 재검증은 가장 최근 Task뿐 아니라 과거 모든 같은 역할 Task의 ID와 구분하고 기존 역할 Context를 유지한다.
- Build/QA/Security는 모두 현재 후보의 동일 Execution Manifest를 검사한다. 새 검사 실행 기록만 현재 결과 근거로 사용하고 이전 후보의 PASS를 가져오지 않는다.
- 네 역할은 처음 승인한 동일 Run 예산 객체를 계속 사용한다. 수정 단계에서 모델/Tool/토큰 카운터나 Deadline을 재설정하지 않는다.

Agent Card는 수정·재검증 능력을 이름/설명에 반영한다. 기존 `measured-initial-*` Skill ID는 연동 호환성을 위해 그대로 보존하며, 그 ID가 최초 후보만 처리한다는 현재 제한을 뜻하지는 않는다. 기본 Bootstrap 서버는 여전히 실제 실행기를 자동으로 구성하지 않는다.

## 2. 실행 흐름

```text
현재 후보 Build/QA/Security의 검증 가능한 FAIL
→ Orchestrator: Issue 저장 · FIX_REQUIRED
→ FIXING: 새 Developer Step/Task · 저장된 Issue/이전 Artifact 전달
→ 실제 Source 수정 · 직전 후보를 부모로 Git Commit 생성
→ 새 Source 동결 · 새 후보 Build 실행
→ REVALIDATING: 동일 새 후보로 QA/Security 검사
→ Orchestrator: 결과/Issue 재검증 기록 · 기존 규칙으로 판정
```

Build가 실패한 후보에는 QA/Security 보고서가 없을 수 있다. 수정 후 처음 수행하는 QA/Security 보고서는 `artifactVersion=1`, `previousArtifactId=null`이며 `codeVersion`은 실제 새 후보 번호다. Code Version과 개별 보고서 Lineage 번호를 억지로 같게 만들지 않는다. 이전 보고서가 있으면 그 실제 직전 보고서에서 버전을 증가시킨다.

## 3. 상태·횟수·식별자

| 구분 | 규칙 |
| --- | --- |
| 최초 구현 | `fix_attempt=0`, 후보 `codeVersion=1` |
| 수정 | `fix_attempt=1~3`, 새 후보 `codeVersion=2~4` |
| A2A `attempt` | 동일 Step의 입력 재개 횟수. 코드 수정 횟수와 별개 |
| Workflow | Orchestrator만 `FIX_REQUIRED → FIXING → REVALIDATING` 등을 기록 |
| A2A Task | 수정은 새 Step·새 Task. 이전 Task를 다시 실행하지 않음 |
| Agent Context | 기존 역할 Context의 opaque 값을 유지. UUID 변환·재생성하지 않음 |
| Source/Report | 새 Project UUID, 직전 Artifact 연결, 실제 생산 Task/Step 참조 |
| 같은 Issue | 기존 Fingerprint·연속 반복 규칙과 Issue Event 연결을 유지 |
| 수정 한도 | 최초 제외 최대 3회. 소진 후 실제 결과에 따라 FAIL/HUMAN_REVIEW |

모델은 Issue 근거 해결을 제안하고 Source만 수정한다. Requirement·Acceptance Criteria·QA/Security 보고서·보호 테스트·동결 환경을 수정할 권한은 없다. 최종 Verdict는 기존 Orchestrator 규칙으로 계산한다.

## 4. Host 구성

[34번 플랫폼 구성](34-owned-agent-platform.md)의 서비스 팩토리를 그대로 사용한다. 수정마다 Workspace 준비 Hook을 다시 실행하거나 초기 Source를 복사하지 않는다.

- Developer 서비스의 `baseline_commit_hash`는 처음 승인한 `startingCommitHash`를 계속 지정한다. 실행 Context의 검증된 직전 Source로부터 수정 부모를 내부 선택한다.
- QA/Security의 `FrozenSourceSelection`은 매번 실행 Context의 현재 `source.artifact_id`를 지정한다. 최초 Source나 움직이는 Branch/Working Tree를 지정하면 안 된다.
- 승인 Build/Unit/Browser/Scanner Profile, 이미지 Digest, Lock Hash, Network DENY를 유지한다.
- Host는 처음부터 전체 수정 루프를 포함한 충분한 `LLMLimits`와 Run `runtimeBudgetMs`를 명시해야 한다. 소진되면 중단하며 다음 수정에서 한도를 늘리거나 새 예산을 발급하지 않는다.
- 명시적으로 초기 후보만 처리해야 하는 별도 Host는 기존 `PlannerRunDispatcher(..., allow_fix_dispatch=False)` 경계를 사용할 수 있다. owned 기본 구성은 이제 수정 기능을 활성화한다.

## 5. 실패 시 처리와 한계

- Source 또는 이전 Artifact/Issue/Task/Step 불일치, 이전 후보와 다른 Working Tree, 이전/다른 후보의 실행 근거는 거부한다. 최신 파일이나 기본값으로 부족한 데이터를 메우지 않는다.
- 새 Task를 전송한 직후에는 Context가 저장됐어도 Task ID가 아직 반환·관찰되지 않을 수 있다. 이 정상 간격은 현재 Step Task ID도 없는 경우에만 허용하며, 다른 Context 또는 잘못된 non-null Task ID를 허용하는 우회로 사용하지 않는다.
- 불명확한 Source 쓰기를 자동 반복하거나 Git checkout/reset으로 되돌리지 않는다. Docker/Tool/예산 실패를 제품 PASS 또는 꾸며낸 완성 Artifact로 바꾸지 않는다.
- 기존 Security 의미 검증 정책을 유지한다. Scanner 경고 0개·모델 PASS·형식상 Proof만으로 실제 보안 PASS를 보장하지 않는다. 승인된 독립 Host 의미 검증 근거가 없으면 UNVERIFIED다.
- QA FAIL과 Security UNVERIFIED가 섞인 경우 현재 기존 판정은 HUMAN_REVIEW와 실제 QA Issue 보존이다. 모든 실패가 무조건 수정 루프로 들어간다고 주장하지 않는다.
- 실제 LLM·Docker·회원가입 제품 시연, SCN-001 전체 보안 의미 검증기, 4번 비교 실험은 이번 완료 범위가 아니다.
- 실제 입력 재개·취소·Human Review 전체 연결은 36번, LLM/A2A/MCPTrace·사용량 영속 연결은 37번이다. 프로세스 재시작 후 과거 Run의 예산을 임의 복원하지 않는다.

## 6. 개발정의서 준수 점검

| 기준 | 확인 |
| --- | --- |
| §2 책임 | Agent는 역할별 Artifact만 생성. 상태·Registry·Issue·Verdict는 Orchestrator 소유 |
| §3/6/7 A2A | 기존 공식 SDK/요청/Artifact Schema/opaque Task·Context 유지 |
| §4 상태/한도 | 최초 제외 수정 3회, attempt와 fix_attempt 구분, 같은 Issue 반복 시 중단 |
| §5 Snapshot/환경 | 현재 후보의 실제 Source·동일 Manifest·동결 환경·READ_ONLY 검증 |
| §8 MCP/Sandbox | 기존 역할 Tool·승인 Profile·Network DENY. 생성 코드 Host 실행/import 금지 |
| §9 버전 | 새 UUID와 직전 Artifact 연결, Code/보고서 Lineage 번호 구분, 덮어쓰기 없음 |
| §10 Verdict | Task COMPLETED ≠ 제품 PASS, 과거 PASS/근거 없는 보안 PASS 조합 금지 |
| §11 예산/Trace | 동일 Run 예산 유지, 기존 Issue/수정/재검증 Event 유지. 전체 영속화는 37번 |

## 7. 검증 결과

추가한 회귀 테스트 구성은 **56개**다.

| 추가 검사 | 개수 | 주요 확인 |
| --- | --- | --- |
| Developer 수정 | 14 | 저장된 Issue/Artifact·Task 관찰 간격, 실제 Git 부모·Source 변경·코드2~4·공유 예산 |
| QA/Security Context | 22 | 전체 Source/보고서 Lineage·실제 이전 보고서 대상·모든 역사 Task/Context·버전 간격 |
| QA/Security 재실행 | 11 | 현재 후보의 Unit/Browser/Scan 실행·새 receipt·이전 증거/숨긴 FAIL 거부 |
| owned 수정 통합 | 6 | 수정 성공·3회 한도·같은 Issue 반복·Build 실패·예산 소진·보고서 버전 간격 |
| 실제 TCP 수정 통합 | 1 | 로컬 HTTP 서버5개·공식 SDK 새 Task/동일 Context·코드2 재검증·listener 회수 |
| Budget/Card | 2 | 수정 시 예산 재발급 금지·현재 능력 설명·기존 Skill ID 호환 |

집중 실행에서 Developer 신규14개, Context22개, 재실행11개, owned 통합6개가 통과했다. TCP 묶음은 기존 최초 후보1개와 신규 수정1개, 총2개 통과(11.791초)했다.

최종 입력 방어 보완 이후 전체 회귀 **2,504개 모두 통과**(513.216초, exit=0). 위 신규56개와 기존 기능 회귀를 함께 실행한 결과다. compileall·pip check·git diff --check·새 파일 trailing whitespace 검사도 통과했다.

```bash
PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -q
```

34번 전체 실행에서 발생했던 기존 공유 Agent Task DB의 WAL 초기 경쟁 오류는 이번 전체 실행에는 재현되지 않았다. 해당 Store/기존 테스트를 수정하거나 Retry·Timeout을 완화하지 않았으며, 한 번 통과한 것을 그 경쟁 조건을 해결했다고 표현하지 않는다. 기존에 제외한 비밀번호 보호 정책 보완도 이번 범위에 추가하지 않았다.

검사에는 실제 Git·SQLite·Archive·공식 A2A SDK·MCP Dispatcher·private 실행 기록을 사용했다. 전체 수정 통합의 LLM·Docker·Security 의미 검증기는 합성 fixture다. MCP Dispatcher adapter 통과는 실제 stdio + 실제 LLM + Docker 전체 경로를 검증했다는 뜻이 아니다. 테스트의 SUCCESS는 제어/결과물/근거 연결 검증이지 실제 회원가입 기능·보안 성능 증명이 아니다.

추가 독립 검토에서 확인한 Developer의 신규 Task 미관찰 간격, QA 입력 Store의 초기 후보 전용 조건, 이전 보고서 Source 연결 및 과거 Task/Context admission을 보완하고 회귀에 포함했다.

## 8. 커밋·다음 작업

권장 커밋 메시지: `실제 Agent의 Issue 수정과 동일 Snapshot 재검증 루프 구현`

다음 작업: **36번 — 실제 입력 재개·취소·Human Review 제어**.

Git 커밋·Push는 수행하지 않는다.
