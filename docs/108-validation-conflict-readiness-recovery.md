# 108. 오류 응답 보호·파일 충돌 복구·준비 경로 보존

작성일: 2026-11-23. 사용자가 선택한 기존 보완 대상 **1·2·3번을 함께 수정**했다. 새 기능이나 평가 기준을 추가한 작업이 아니다. **이번 작업의 실제 OpenAI 호출과 새 실험 Run은 0회**다.

## 변경 요약

| 대상 | 확인한 원인 | 보완한 동작 |
| --- | --- | --- |
| 1. 비밀번호 노출 | `dict`형 입력의 프레임워크 검증 오류를 기존 Source 검사가 놓침 | READY 전에 해당 경로를 찾고 앱의 `RequestValidationError` 보호를 수정하도록 연결 |
| 2. 파일 수정 중단 | 쓰기 충돌 복구에서 이미 반영된 내용을 최신 해시로 다시 써도 ‘변경 없음’으로 중단 가능 | 앞서 같은 파일을 수정한 기록과 최신 CAS 영수증이 정확히 일치하는 경우에만 재확인으로 인정 |
| 3. 서버 준비 확인 실패 | `GET /` 앞의 전체 경로 마운트가 요청을 먼저 받아 준비 응답을 가림 | 확인 가능한 마운트 순서 오류를 사전 검사하고 준비 경로 보존·실제 Build 수정 안내 강화 |

## 1. 오류 응답에서 비밀번호가 되돌아오는 경로

107번 Single Ver1의 마지막 등록 Source v3은 `data: dict` 입력을 받았다. `HTTPException`과 일반 `Exception` 처리기가 있었지만, 요청을 함수에 넘기기 전에 FastAPI가 만드는 `RequestValidationError` 응답은 보호하지 못했다. 목록처럼 잘못된 전체 입력 구조에서 비밀번호가 드러난 최종 시험 결과와 연결되는 경로다.

기존 검사가 주로 `BaseModel`의 `password: str`를 찾던 범위를 다음과 같이 보완했다.

- `dict`·`list` 및 해당 제네릭 타입, 명시적인 `Body` 입력과 keyword-only 인자를 확인한다.
- 비밀번호 필드가 `SecretStr`여도 전체 요청 구조의 검증 실패를 별도로 검토한다.
- `Depends` 등 다른 입력 지정자를 단순히 본문으로 오인하지 않는다.
- 예외 처리기에서 `exc.body`·`exc.args`를 응답으로 직접 전달하는 경로도 검토한다.
- 보호 코드는 실제 앱 진입점의 `repairSourcePath`로 안내하고, 기존 HTTP 업무 오류 처리를 보존한다.

일반 오류 처리기만 추가하거나 엔드포인트 내부에서 예외를 잡는 방식이 이 문제를 해결한다고 안내하지 않는다. 값 없는 검증 오류 응답을 사용하도록 구체적으로 안내한다. 새 `validationErrorContext`도 기존의 닫힌 필드 검사·비노출 필터를 통과한 값만 전달한다.

Single의 IMPLEMENTING·FIXING과 Multi Developer 모두 기존의 같은 검사와 출력 교정 한도를 사용한다. 경고 뒤에는 실제 파일 읽기·수정·다시 검사하는 절차를 유지한다. 정적 경고가 사라졌다는 이유만으로 제품 Security PASS를 선언하지 않는다.

## 2. 충돌 복구에서 이미 적용된 수정 인정

107번 Multi Ver1 Trace에는 실제 편집 후 `WRITE_CONFLICT`, 새 읽기, 성공한 쓰기, `NO_NET_SOURCE_CHANGE` 중단 순서가 남아 있다. 같은 단계에서 이미 수정한 파일의 현재 내용을 재확인하는 쓰기까지 일괄 중단할 수 있던 경로를 수기 fixture로 재현하고 보완했다. 원래 모델의 전체 Tool 인자를 완전히 재생한 것은 아니다.

다음 조건을 **모두** 만족할 때만 `RECOVERY_CONTENT_ALREADY_CURRENT`로 확인한다.

1. 부작용이 없다고 확인된 `WRITE_CONFLICT` 복구다. 결과 불명 쓰기는 해당하지 않는다.
2. 같은 Engine 단계에서 해당 파일을 실제로 수정한 성공 기록이 있다.
3. 충돌 후 그 파일을 새로 읽었고, 쓰기의 `expectedSha256`이 이 최신 해시와 일치한다.
4. 성공한 `write_source_file` 영수증의 경로·해시가 일치하고 `changed=false`다.
5. 모델이 제출한 전체 내용의 SHA-256도 그 해시와 같다.
6. 전체 작업 소스는 원래 기준에서 실제로 변경된 상태다.

이 경우 새로운 변경을 만들었다고 기록하지 않으며, 의미 없는 추가 편집을 요구하지 않는다. 이후 READY 검사·Build·QA·Security는 그대로 수행한다.

이전에 쓰지 않은 파일, 다른 파일만 고친 경우, 해시·내용·영수증이 불일치하는 경우, 원래 기준으로 되돌린 경우, 부작용이 불명인 경우는 통과시키지 않는다. 재확인 뒤 Source 경고가 남으면 READY도 거절된다. CAS·역할 권한·파일 접근·모델/도구 예산과 재시도 한도는 완화하지 않았다.

## 3. `GET /`가 있어도 가려지는 시작 규격

107번 Single Ver2의 등록 Source v1에는 `GET /`가 있었다. 다만 앞에서 `app.mount('/', signup_app)`를 등록했고, 해당 자식 앱에는 준비 경로가 없어 요청을 먼저 받은 뒤 정상 준비 응답을 주지 못했다. 따라서 함수나 데코레이터의 존재만 확인하는 방식으로는 Build 실패를 막지 못했다.

`READY_ROUTE_SHADOWED`는 직접 만든 FastAPI 앱·알려진 자식 앱·확인 가능한 등록 순서에서 이 패턴을 찾는다. 수정 안내는 준비 응답을 전체 경로 마운트보다 먼저 등록하거나 호환되는 APIRouter를 연결하면서 기존 회원가입 경로와 초기화를 유지하도록 한다.

실제로 먼저 응답하는 다른 라우트, 자식 앱의 정상 준비 경로, 외부 라우터·미들웨어·동적인 라우팅은 같은 결함으로 단정하지 않는다. 확정할 수 없는 구성은 기존 실제 Build에서 판단한다.

추가로 시작 코드 안내에 준비 경로·등록 위치를 포함했다. 기존 경로가 사라지거나 마운트에 가릴 수 있으면 `NOT_EVALUATED` 안내를 전달한다. 실제 `SCN001_READY_ENDPOINT_REQUIRED` 실패에는 **같은 Source·버전·Build Report에 묶인** 수정 목표를 제공한다. 오래된 결과나 다른 후보의 실패를 현재의 수정 지시로 쓰지 않는다.

## 검증

| 검사 | 결과 |
| --- | --- |
| QA·Security·Single 파이프라인·Host 전체 흐름 | **149 PASS** |
| 최종 오류 응답·충돌·Source 교정·시작 규격·MCP·Host 관련 검사 | **309 PASS** |
| 과거 Single Ver1 v3·Single Ver2 v1의 불변 Source 읽기 | 새 검사에서 각 원인 탐지, 원본 DB 변경 없음 |
| `git diff --check` | 통과 |

149개 전체 흐름 점검 뒤 정상 라우팅의 오판 방지와 정책 지문 등록을 마무리하고, 관련 309개를 최종 실행했다. 두 묶음은 일부 검사가 겹치므로 고유 검사 수로 합산하지 않는다. SDK의 스트리밍 미설정 경고와 기존 테스트 클래스 수집 경고는 남았고 실패는 없었다.

검증에서는 다음을 구분했다.

- 직접 작성한 FastAPI fixture에서 잘못된 목록 입력의 노출을 재현하고, 값 없는 전용 검증 오류 처리 뒤 노출이 사라지는지 확인했다. 정상 요청과 기존 업무 오류 처리는 보존한다.
- 실제 MCP 파일 도구의 최신 해시를 제공한 동일 내용 쓰기가 `changed=false`·같은 경로·같은 해시를 돌려주는지 확인했다.
- Single·Multi의 충돌 복구에서 정상 재확인만 허용하고, 이전 변경 없음·다른 경로·위조 영수증·다른 내용·기준 복귀·결과 불명·미해결 Source 경고를 거절하는지 검사했다.
- 직접 작성한 시작 코드에 **기존 Host Build probe 그대로** 적용했다. 마운트가 먼저면 `SCN001_READY_ENDPOINT_REQUIRED`, 준비 경로가 먼저면 기존 회원가입 경로를 유지하며 `SCN001_START_CONTRACT_OK`다.
- 과거 Agent Source는 AST로만 읽었다. 저장된 생성 앱·시험을 호스트 Python에서 import·실행하지 않았다. 근거는 `.data/verification/108-offline-source-evidence-20261123.json`에 값 없이 보존했다.

```bash
.venv/bin/python -B -m pytest \
  tests/test_qa_agent.py tests/test_security_agent.py tests/test_test_data_pipeline.py \
  tests/test_single_agent_pipeline.py tests/test_validation_artifacts.py \
  tests/test_loop_validation_feedback.py tests/test_per_test_setup_recovery.py \
  tests/test_owned_host.py -q -x

.venv/bin/python -B -m pytest \
  tests/test_ready_route_preservation.py tests/test_startup_preservation.py \
  tests/test_validation_error_body_recovery.py tests/test_source_quality_repair_flow.py \
  tests/test_source_conflict_recovery.py tests/test_mcp_files.py \
  tests/test_signup_quality_precheck.py tests/test_start_contract_diagnostics.py \
  tests/test_start_contract_probe_routes.py tests/test_owned_host.py -q -x
```

## 개발정의서 점검·다음 작업

- Single·Multi에 같은 Engine·정책을 적용하며 역할·A2A Task·Artifact·Source Snapshot·Run 상태 계약을 유지했다.
- DRAFT 사전 검사 V8, Source 검토 V6, 시작 사전 검사 V6, 충돌 복구 안내 V5, 시작 코드 보존 V2다. 변경 모듈과 시작 사전 검사 파일의 SHA-256도 Host 실행 정책에 반영했다.
- 인증·승인·역할 권한·CAS·Sandbox와 보호시험·독립 최종 시험을 유지했다. 제품 채점을 정적 검사 결과로 대신하지 않았다.
- `.env`·사용자 설정·기존 실험 DB·Source·성능 표는 바꾸지 않았다. 프로젝트 Git 커밋·푸시는 하지 않았다.

**1·2·3번의 코드 보완과 오프라인 재현 검증을 마쳤다.** 실제 모델이 새 교정 절차로 네 조건을 끝까지 수행하는지는 아직 재실행하지 않았다. 107번의 미평가·보류 결과를 성공으로 고치지 않는다.

**다음 109번:** 남아 있는 기존 4번, API 공급자 오류·시간 초과의 원인 구분과 확인. 이번 작업에 공급자 재시도 확대나 유료 재실행을 포함하지 않았다.

커밋 메시지 제안: `회원가입 오류 응답 보호와 파일 충돌 복구 및 준비 경로 보존 보완`
