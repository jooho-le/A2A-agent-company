# 44. 자체 LLM/Docker 실제 실행 준비 점검

작성 기준: 2026-10-11. 범위: 1번 Orchestrator + 2번 Agent/MCP. 팀원 제품 연결·독립 평가·Single/Multi 비교 실험은 제외한다.

**현재 상태: 준비 여부 점검만 완료. 실제 실행은 미수행이며 44번 전체가 완료된 것은 아니다.**

## 1. 이번에 확인한 것

- `git status --short` 기준 작업 시작 시 변경 사항이 없었다.
- `configs`를 ignored 파일까지 포함해 확인했으나 승인된 `*.local.json` 파일이 없었다. Runtime·Tool·Source·가격표 예시만으로는 실행할 수 없다.
- 현재 실행 프로세스에 `A2A_PLATFORM_LLM_API_KEY`와 네 역할의 `A2A_PLATFORM_*_TOKEN`이 모두 없었다. `OPENAI_API_KEY`도 없었다. **값은 출력하지 않고 존재 여부만 확인했다.**
- 저장소 루트에 `.env`가 없었다. 기존 Owned Host는 `.env`를 자동으로 읽지 않으므로 파일 생성만으로 실행 자격증명이 전달되는 것도 아니다.
- 현재 shell의 `command -v docker`가 경로를 반환하지 않았다. 이는 이 환경의 PATH에서 Docker CLI를 찾지 못했다는 뜻이며, 다른 설치 경로나 사용자 터미널의 상태까지 미설치로 단정하지 않는다. daemon·이미지 동작은 확인하지 않았다.
- 미승인 예시를 기존 읽기 전용 Host 검사에 전달한 결과 exit `1`, `OWNED_HOST_CONFIGURATION_INVALID`였다. 예상된 승인 거절이며 실행 성공이 아니다.

사용자의 다른 터미널/계정에 Key·Docker·설정이 준비돼 있는지는 이번 확인으로 알 수 없다. 별도 준비한 승인 파일이 있다면 경로를 받아 기존 검사에 연결하면 된다.

## 2. 실제 실행 전에 필요한 준비

1. **모델·자격증명·실행 한도**: 실제 요청 모델/Revision, 지원하는 모델 옵션, 모델/Tool 호출·토큰·시간 한도를 정하고 `owned-runtime.local.json`을 작성한다. LLM API Key와 네 Agent Bearer는 선언한 환경변수로 제공한다. 유료 API를 사용해도 되는 범위도 확인한다.
2. **Docker·Tool 설정**: 사용할 CLI/daemon, 고정 이미지, 실제 dependency lock/hash, Build·Unit·Browser·Scanner 프로파일/버전·자원/시간 한도를 준비해 `owned-tools.local.json`을 작성한다. 다운로드/설치/이미지 Build는 이번에 하지 않았다.
3. **시작 코드**: 자체 최소 실행에 사용할 승인 Reference Repository, 고정 Commit/Snapshot, Lock 경로와 준비 시간 한도를 `owned-source.local.json`에 지정한다. 운영 DB/Workspace/승인 파일과의 기존 경로 분리 검사를 유지한다.
4. **가격표**: 실제 계정/모델·응답 모델·처리 등급·입력 구간에 맞는 가격을 확인하고 `owned-price-book.local.json`에 승인 ID/버전과 함께 작성한다. 가격표는 자동 선택하거나 테스트용 합성 가격으로 대체하지 않는다.
5. **확인 범위**: 기존 등록 시나리오의 작은 Run에서 어디까지 실제 동작을 확인할지 정한다. 보호 QA Selector를 쓰면 대응 승인 케이스가 필요하다. 독립 Security 근거가 없으면 `UNVERIFIED/HUMAN_REVIEW`를 유지하며 실제 호출/Tool 실행 확인을 제품 `SUCCESS`로 바꾸지 않는다.

현재 제공된 정보만으로 모델·요금·이미지·시작 코드·독립 검증 기준을 임의 확정하지 않는다. 특히 기존 요구사항을 줄이거나 Security PASS를 만들어 44번을 완료 처리하지 않는다.

## 3. 준비 후 진행 순서

1. 승인된 로컬 설정 파일을 기존 읽기 전용 검사에 연결한다.
2. Key/역할 Token을 Host process environment에 제공하고 `--check-secrets`로 존재만 확인한다. Key 인증 성공이나 모델 접근 권한까지 확인하는 옵션은 아니다.
3. Docker CLI/daemon와 승인 이미지/Lock/프로파일 준비 상태를 확인한다. 이미지·의존성을 자동으로 변경하지 않는다.
4. 실제 실행 범위와 비용 발생을 확인한 뒤 기존 `--start`로 Host 서버를 시작하고 작은 Run **1개**를 제출한다. 준비 검사나 서버 시작 자체가 실제 모델/Tool 실행의 증거는 아니다.
5. Run/Step/A2A/Source/Build/QA/Scanner의 실제 근거와 `/usage`·`/telemetry`를 확인하고 실제 관측 결과만 기록한다. 값이 불명확하면 `null`/미검증 상태를 유지한다.
6. 실행 결과가 불명확하면 즉시 새 Run으로 재시도하지 않는다. 기존 조회/회수·취소 경계를 사용하고 Host/MCP/Provider 자원을 정리한다.

준비 후 사용할 읽기 전용 명령은 다음과 같다. 현재 해당 로컬 파일이 없으므로 아직 통과할 수 없다.

```bash
PYTHONPATH=src .venv/bin/python -m agents.platform.run_host \
  --runtime configs/owned-runtime.local.json \
  --tools configs/owned-tools.local.json \
  --source configs/owned-source.local.json \
  --price-book configs/owned-price-book.local.json \
  --check-secrets
```

API Key는 채팅·Markdown·Git·Agent Workspace·Tool/Container 환경에 넣지 않는다. 기존 Host 환경변수 바인딩을 사용한다. OpenAI Docs 스킬로 [공식 API 인증 안내](https://developers.openai.com/api/reference/overview#authentication)를 확인했고, 이번 점검에서도 비밀값을 읽어 출력하거나 저장하지 않았다. 런타임 예시의 변수명은 `A2A_PLATFORM_LLM_API_KEY`이며 `OPENAI_API_KEY`를 설정했다고 자동으로 대신 사용하는 계약은 아니다.

호출/토큰/시간 한도와 43번의 비용 기록은 **청구 금액을 정확히 특정 USD에서 강제 중단하는 장치와 다르다**. 실제 청구 한도 보장을 구현/검증한 것으로 설명하지 않는다.

## 4. 개발정의서 준수 점검

| 기준 | 이번 확인 |
| --- | --- |
| §2 역할 경계 | 자신의 플랫폼/Agent 실행 준비만 점검. 역할 3·4 구현/연결 없음 |
| §0·§3·§6·§7 계약/상태 | 코드·Schema·공식 A2A/Workflow 상태 변경 없음 |
| §4 수정/재시도 | Fix/Tool Retry 한도 변경이나 미확인 작업 재실행 없음 |
| §8-7·8-8 비밀정보 | 선언 환경변수의 존재만 확인, 값/인증 헤더/Secret 저장 없음 |
| §8-9·8-10 실행 격리 | Docker가 없다고 Host에서 Build/Test/Scanner를 실행하지 않음. 자동 설치·네트워크 우회 없음 |
| §5·§9·§11-A 재현성 | 승인 모델·고정 이미지/Lock·시작 Commit/Snapshot 없이 실행하지 않음 |
| §10 판정 | 준비 검사·모의 테스트를 실제 실행/제품 SUCCESS로 처리하지 않음 |

기존 보류 항목과 전체 운영 보안 검증을 해결한 단계는 아니다. 이번 변경은 문서뿐이다. 실제 유료 LLM·Docker·HTTP listener·Run 제출·팀원 통합은 모두 미수행이다.

## 5. 검증과 다음 작업

실행한 확인: 승인 로컬 파일 목록, CLI 존재, 비밀값 없는 환경변수 존재 검사, 기존 미승인 예시의 읽기 전용 거절 검사, 문서 `git diff --check`. 새로운 기능 코드를 수정하지 않았으며 기존 43번 회귀 통과를 실제 실행 결과로 재사용하지 않는다.

커밋 메시지: `실제 LLM·Docker 실행 준비 점검 및 미준비 항목 문서화`

다음 작업 번호는 계속 **44번**이다. 위 운영값/환경을 준비하고 실제 최소 Run을 확인한 후에만 45번 최종 인계 문서로 넘어간다. 프로젝트 Git commit/push는 하지 않는다.
