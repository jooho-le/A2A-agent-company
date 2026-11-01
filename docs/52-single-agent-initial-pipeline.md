# 52. Single-Agent 최초 구현·검증 실행 연결

작성일: 2026-10-31. 선행 작업: [51번 MCP 권한](51-single-agent-mcp-permissions.md).

## 1. 구현 내용

[SingleAgentExecutor](../src/agents/single/runtime.py)와 [Single Host factory](../src/agents/single/platform.py)를 추가했다. 요청부터 기획, Source 작성·Build, QA, Security까지 하나의 실제 실행 경로를 연결했다.

| 항목 | 실제 구성 |
| --- | --- |
| Agent 서버·Provider | `SINGLE_AGENT` 서버 하나, Provider 객체 하나 |
| A2A 연결 | 같은 서버 URL, Run당 서버 발급 Context 하나, 단계별 서로 다른 Task |
| LLM 입력 | 같은 System Prompt와 기존 SQLite에 저장된 모든 이전 Task의 메시지·상태·Artifact |
| 업무 선택 | 요청 본문의 역할 지시가 아닌 저장된 WorkflowStep과 동결 Run Configuration |
| 순서 | PLANNING → IMPLEMENTING → QA → SECURITY |
| Source | 기존 Git checkpoint·불변 Snapshot·Build 영수증 경계 재사용 |
| 검증 | QA와 Security를 차례로 실행하며 같은 Source/Manifest를 검사 |
| 예산 | 같은 Run의 deadline·모델/도구 호출·토큰 한도 공유. 단계별 재발급 없음 |

네 Agent 실행기를 생성하거나 네 역할 Prompt를 번갈아 호출하지 않는다. 기존 역할별 입력 검증 함수, 동결 Context loader, Draft validator, Host 실행 증거 서비스만 재사용한다. SQLite의 업무 단계 `agent_role`은 기존 판정·산출물 연결용 범주이며 실제 Agent 정체성은 `single_agent` 하나다.

저장된 작업 기록은 50번의 방식으로 매 모델 호출에 재구성한다. Provider 내부 세션이나 숨겨진 추론의 영구 저장을 뜻하지 않는다. 같은 단계 안에서는 기존 LLM 엔진이 실제 도구 응답을 이어 사용한다. Prompt 크기 한도를 넘으면 기록을 잘라내지 않고 거절한다.

## 2. 실행 연결과 기본 동작

명시적 Host 진입점은 `agents.single.platform.create_single_platform()`이다. 기존 승인 저장소·시작 코드 준비·Build/QA/Security 서비스 factory를 전달한다. 기존 `OwnedAgentPlatform`과 실행 runner를 재사용하며 로컬 엔드포인트는 Orchestrator와 Single Agent 두 개다.

필수 Host 입력:

- 같은 저장소를 사용하는 `repository`, `workspace_registry`, `artifact_store`.
- `agent_settings.role=SINGLE_AGENT`, 승인 Provider·모델·Run 전체 `limits`.
- 같은 Single 서버를 명시한 Orchestrator의 네 단계별 Agent URL·Bearer Token 설정. 입력 설정을 자동으로 덮어쓰지 않는다.
- 신뢰된 `prepare_workspace`와 기존 세 RuntimeServices를 반환하는 서비스 factory.

모델·한도·경로·서버·자격증명 설정이 맞지 않으면 구성/등록을 거절한다. Host가 주입한 네트워크 Client 외 기본 HTTP Client는 기존처럼 `trust_env=False`다.

이 Host의 `/api/v1/runs` 요청에서는 `configuration.architecture`를 `SINGLE_AGENT`로 명시한다. 모델·시작 코드·실행환경·한도 등 기존 동결 설정도 함께 전달한다.

```python
import asyncio
from agents.single.platform import create_single_platform
from agents.platform.runner import run_platform

# approved_host_inputs: 위 필수 입력을 운영자가 준비한 keyword 인자 dict.
platform = create_single_platform(**approved_host_inputs)
asyncio.run(run_platform(platform))
```

위 코드는 Host 조합의 사용 형태다. 승인 정책·이미지·시작 Source·키를 임의로 채우는 기본 설정은 추가하지 않았다. 기존 Multi Host CLI를 Single로 바꾸지 않았으며 전체 운영 실행 안내는 56번에서 정리한다.

| 실행 방식 | 상태 |
| --- | --- |
| 기본 Orchestrator | Single 등록은 기존처럼 503 `SINGLE_AGENT_RUNTIME_NOT_CONFIGURED` |
| `python -m agents.single` | 통신용 Bootstrap, `executionReady=false` |
| `create_single_platform()`으로 구성한 서버 | 최초 구현·검증 실행기 주입, `executionReady=true` |
| Single Host에 Multi 요청 | 저장/작업 전 422 `RUNTIME_ARCHITECTURE_MISMATCH` |

API 밖에서 dispatcher를 직접 호출해도 구조가 맞지 않으면 네트워크 호출 전에 사람 검토로 남긴다. 임의의 주입 실행기나 키 설정만으로 Card가 실행 가능 상태를 광고하지 않는다.

## 3. MCP·Host 실행 증거 경계

모델의 `ToolContext`와 실제 stdio 자식의 `--role`, `--agent-role`은 `SINGLE_AGENT`다. 단계는 불변 `--single-phase`로 전달한다. 실제 모델용 자식 구성에는 보호 테스트 scope를 넣지 않는다.

기존 실행 영수증 검증 API를 재사용하기 위해 `BoundMCPClient`에 Host 전용 `principal_configuration` view를 추가했다. 실제 Single 구성과 기존 업무별 구성의 Run·Workspace·단계·프로파일·Snapshot 일치를 확인한다. 외부 도구 인자로 이 view를 만들거나 변경할 수 없다.

기존 영수증의 `role`은 업무별 검사 범주를 유지하되 **도구 journal의 구성 hash에는 실제 Single 자식의 인자·환경을 사용**한다. 실제 네 Agent가 실행된 것처럼 A2A Context를 만들지 않는다. 전체 Single Trace/Usage 내보내기 표현은 55번 작업이다.

모델은 Multi의 각 업무와 같은 Source 읽기·자체 파일 쓰기·측정 보고서 읽기 범위에서 작업한다. Host는 별도 기존 Capability로 실제 Build·테스트·승인 스캔을 실행하고 영수증을 검증한다. 도구 발견의 전체 합집합과 단계별 제한은 51번 규칙을 유지한다.

QA는 `outputs/qa/tests/`, Security는 `outputs/security/tests/`에 자체 테스트를 작성한다. 검증 단계의 Source는 동결본이다. 보호 테스트는 Host 구성에서 반드시 실행하고 모델에게 시험지 내용이나 수정 권한을 주지 않는다. 최종 독립 채점용 시험지·결과·평가 코드도 전달하지 않는다.

## 4. 석민의 Security 실행 테스트 변경 반영

로컬 `dev`의 공통 Security Draft, RuntimeServices, 검사 정책 지문, Artifact/수집 검증 변경을 필요한 범위만 반영했다. Git 병합·브랜치 변경은 하지 않았다. 51번에서 강화한 Security 쓰기 경로 제한도 보존했다.

- 승인된 Security Unit scope가 있으면 자체 테스트의 `testCases`를 받고, Host가 실제 실행 결과로 Requirement 결과를 계산한다.
- Single Draft의 도구별 selector map을 기존 Security API의 Unit selector tuple에 명시적으로 변환한다.
- 실행 테스트와 정적 의미 proof를 한 Run에 섞는 기존 금지 규칙을 유지한다.
- 정적 스캔 경고가 없거나 모델이 PASS를 제안해도 승인 proof/측정 근거가 없으면 성공으로 만들지 않는다.
- 측정 테스트가 PASS여도 미확정 Scanner 경고를 지우지 않는다.

역할 3 웹 화면, 역할 4 공통 시험지·독립 채점·실험 집계 코드는 새로 만들지 않았다.

## 5. 이번 범위와 다음 단계

이번 범위는 최초 후보(`fixAttempt=0`, `codeVersion=1`)다. 증거가 충족되면 기존 판정기를 통해 SUCCESS가 될 수 있으며, 부족하면 기존 검토/실패 상태를 유지한다. 제품 검사 실패 시 수정이 필요하다는 판정은 보존하되 자동 수정 Task는 아직 발송하지 않는다.

자동 수정·재검증은 53번이다. Single의 `/resume`, `/recover`는 현재 503 `SINGLE_AGENT_CONTINUATION_NOT_CONFIGURED`로 거절해 네 역할 제어 경로로 잘못 재개하지 않는다. Single 전체 제어·취소·재시작 예산 복구는 54번에서 연결·검증한다. 프로세스가 재시작돼 예산을 알 수 없는 Run은 새 예산으로 실행하지 않는다.

사용량의 실제 주체는 Single로 반환하며 미확인 토큰을 0으로 만들지 않는다. 전체 영속 사용량/비용 원장과 Single 대화·Snapshot 평가 인계는 55번이다. 이 단계만으로 Single/Multi 공정 비교가 준비됐다고 선언하지 않는다.

## 6. 검증

신규 테스트 `test_single_agent_pipeline` **7개**가 통과했다. 실제 A2A 실행·Host 구성 검사 6개와 실제 MCP stdio 연결 검사 1개다.

1. 같은 Agent·System Prompt·Context와 이전 Artifact가 기획부터 보안까지 이어짐.
2. 같은 Snapshot의 Build·QA·Security 실행 테스트 증거로 SUCCESS 도달.
3. 보안 근거 부족은 HUMAN_REVIEW 유지.
4. 공통 모델 호출 한도 초과 시 다음 업무가 새 예산으로 실행되지 않음.
5. 구조 불일치 등록 거절, 잘못된 QA 출력 뒤 Security 미발송, 준비 전 재개/복구 차단.
6. 기존 네 서버 URL을 Single Host에 연결하면 실행 전에 거절.
7. 실제 stdio 연결도 Single 권한·기존 private journal을 유지하고, 단계 우회·잘못된 view·종료된 연결 사용을 차단.

Provider와 Docker CLI는 테스트 어댑터다. A2A SDK/HTTP-ASGI, 실제 임시 Git·SQLite·Snapshot·MCP dispatcher·실행 영수증·Artifact 검증 경계는 실제 코드를 사용했다. 별도 stdio 검사는 실제 MCP SDK 자식을 실행했다. 생성 코드와 생성 테스트를 Host에서 실행하지 않았다.

전체 회귀 명령:

```bash
PYTHONPATH=src:tests .venv/bin/python -W error::RuntimeWarning \
  -m unittest discover -s tests -q
```

전체 명령은 **3,347개**를 실행했다. **3,343개가 통과**했고 4개는 샌드박스의 `socket.bind` 제한으로 `PermissionError`가 발생했다. 해당 테스트를 생략하거나 보안 검사를 완화하지 않았으며, 필요한 로컬 소켓 생성만 허용된 실행에서 아래 **4개를 별도로 재검증해 모두 통과**했다.

- `test_owned_controls_tcp.OwnedControlsTCPTests.test_actual_http_resumed_task_can_cancel_without_new_task_or_budget`
- `test_owned_platform_tcp.OwnedPlatformFixTCPTests.test_actual_tcp_initial_failure_fix_and_same_candidate_revalidation`
- `test_owned_platform_tcp.OwnedPlatformTCPTests.test_actual_five_servers_run_initial_cycle_and_release_all_listeners`
- `test_workspace_access.WorkspaceFileAccessTests.test_directory_fifo_and_socket_are_rejected_as_nonregular_files`

별도 실행은 기존 Multi의 임시 loopback HTTP 서버 3건과 임시 Unix 소켓의 파일 접근 차단 검사 1건이다. 실제 LLM API나 Docker daemon을 호출하지 않는다. 전체 실행 뒤 테스트 모듈에 추가한 2건까지 포함한 **신규 7개도 최종 버전으로 재검증해 통과**했다. 이 수치를 전체 수에 중복 합산하지 않았다. `RuntimeWarning`·미대기 coroutine 오류는 없었다.

변경/신규 Python 파일 23개의 Python 3.10 문법, README·47번·52번 문서의 로컬 링크 55개, 신규 파일의 줄 끝·공백, `git diff --check`도 통과했다.

실제 LLM API·Docker daemon·운영 서버·Single/Multi 비교 실험은 실행하지 않았다. Single 실행기는 HTTP-ASGI와 실제 MCP stdio로 확인했으며 Single의 실제 TCP 서버 실행은 별도 확인 대상이다. pytest 기반 평가 테스트는 현재 가상환경에 pytest가 없어 실행하지 않았다. 개인 설정·인증 파일·프로젝트 브랜치·Git 이력은 변경하지 않았다.

## 7. 개발정의서 대조

| 기준 | 이번 구현·경계 |
| --- | --- |
| §2·§11-A 단일 주체 | 한 서버·Provider·Context·System Prompt, 네 실행기 위임 없음 |
| §0·§3·§6·§7 A2A/식별/수명주기 | 기존 공식 SDK·서버 발급 ID·Task Store·직렬 실행 재사용 |
| §5·§8·§9 동일 Source/실행 증거 | 기존 Snapshot·Build·검증 영수증·Manifest·Host 판정 재사용 |
| §4 수정·Retry | 기존 Retry 상한 유지. 실제 Single 수정 기회는 53번까지 미완료 |
| §10 MCP/격리 | 실제 Single stdio 권한·단계·경로 제한, 보호 scope 분리 |
| §11-A 공정 비교 | 동결 모델·시작 코드·환경·한도와 기존 기준 재사용. 비교 완료 선언 없음 |
| §11 기록/비용 | 실제 Single 사용량 주체. 전체 기록/평가 인계는 55번 |

커밋 메시지: `싱글에이전트 최초 구현·검증 실행기와 오케스트레이터 연결`

다음 작업: **53번 — Single-Agent 수정·재검증 루프와 최종 판정 연결**.
