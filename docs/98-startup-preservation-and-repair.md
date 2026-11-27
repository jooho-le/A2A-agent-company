# 98. 시작 코드의 앱·DB 초기화 보존과 복구 안내

작성일: 2026-11-23. 97번에서 발견한 DB 초기화 누락을 보완했다. **이번 작업의 OpenAI 호출은 0회**이며, 이전 Run·실험 결과는 변경하지 않았다.

## 원인과 수정

승인된 시작 코드에는 이미 `models` 등록 → `Base.metadata.create_all(bind=engine)` → `FastAPI(lifespan=lifespan)` 연결이 있었다. 그런데 96번 Developer가 `main.py`를 전체 재작성하면서 초기화 함수와 앱의 연결을 모두 빠뜨렸다. 회원가입 코드가 있어도 사용할 테이블이 없어 정상 가입이 실패하는 상태였다.

실제 테이블 누락을 잡는 Host 빌드 검사는 이미 존재했다. 이번에는 **구현 전에 보존할 초기화 위치를 전달하고, 쓰기 직후의 누락 안내와 실제 빌드 실패의 복구 정보를 연결**했다. Host가 생성 코드를 대신 고쳐 주는 기능은 추가하지 않았다.

| 시점 | 이번 변경 | 판정의 한계 |
| --- | --- | --- |
| 구현·수정 시작 | 불변 시작 Snapshot에서 앱·DB·모델 파일과 초기화 호출·lifespan 연결 위치를 `startingSourceStartup`으로 전달한다. 최신 실패 후보와 시작 코드를 구분한다. | AST 관찰 정보이며 실행 성공의 증거는 아니다. |
| Source 쓰기·편집 직후 | 기존 초기화 호출이나 lifespan 연결이 보이지 않으면 `startupPreservationNotice`를 다음 모델 입력에 전달한다. 일반 파일 전체 쓰기도 검사한다. | 안내만 제공한다. 새로운 교정 강제 단계·무제한 재시도·자동 복원은 없다. |
| 실제 Build 실패 후 | 현재 Host가 선택한 `SCN001_USER_TABLE_REQUIRED` Issue의 Source·BuildReport·codeVersion을 대조하고 `activeRepairFocus.brief.startupRepairTargets`를 만든다. | 이전 후보·다른 보고서·인프라 실패·모델의 요약만으로 복구 대상을 만들지 않는다. |
| 복구 지시 | 앱·DB·모델을 함께 읽고 모델 등록, 실제 Base/engine, 원래 `get_db`의 DB 연결을 앱 초기화에서 복구하도록 안내한다. | QA/Test가 대신 테이블을 만들거나 DB 설정을 우회하면 안 된다. |
| 후속 검증 | 기존의 실제 빌드 → 동일 Snapshot의 QA·Security 흐름을 유지한다. 빌드 실패 후보는 검증 통과 후보처럼 전달하지 않는다. | Build PASS는 최종 제품 PASS와 다르다. |

Single의 IMPLEMENTING·FIXING과 Multi Developer에 동일한 안내·검사를 적용한다. 입력·역할 계약 버전은 8로 올렸고, 초기화 보존 정책 V1과 구현 파일의 SHA-256을 Host 실행 정책에 포함했다. A2A·Artifact JSON Schema와 승인된 요구사항은 변경하지 않았다.

## 전달되는 정보

예를 들어 승인된 시작 코드에서 관찰한 공개 좌표는 다음과 같다. Source 전체 내용이나 DB 접속 문자열은 안내 메시지에 복사하지 않는다.

```yaml
application: source/main.py / app
database: source/database.py / get_db
userModel: source/models.py / User
urlEnvironmentName: DATABASE_URL
startingInitializationCall: Base.metadata.create_all
startingBindName: engine
startingApplicationLifespan: lifespan
```

실제 빌드 실패의 수정 대상에는 Issue ID·Source Artifact ID·BuildReport ID·codeVersion도 붙는다. Source의 원시 예외·SQL·비밀값·숨겨진 최종 시험 입력을 새 안내에 넣지 않는다.

관찰 범위는 승인된 앱·DB·모델 모듈이며 파일 크기·AST 노드 수·출력 좌표 수를 제한한다. 다른 모듈로 옮긴 초기화나 동적 구성은 실제 빌드에서 판단한다. 정적 검사에서는 읽기·파싱 불가를 PASS로 바꾸지 않으며, 검사 실패로 별도 모델 재시도를 추가하지 않는다. 올바른 다른 초기화 방식도 실제 시작 검사를 통과할 수 있다.

## 실제 실패 Source 복사본의 진단

96번 Run `330e2984-f6ed-4bac-aa0d-3cf31bce6783`의 Source를 바탕으로 한 97번 진단 복사본에, 이번에는 **앱 소유의 lifespan DB 초기화만 추가**했다. 기존 가입 함수·오류 처리기·DB 모듈·모델·시험 기준은 보존했다. 모든 제품 코드 실행은 기존 격리 Docker 이미지 안에서만 수행했다.

| 검사 | DB 초기화 복구 전 | 복구 후 |
| --- | --- | --- |
| 실제 앱·DB 시작 검사 | `SCN001_USER_TABLE_REQUIRED` | `SCN001_START_CONTRACT_OK` |
| 최종 9개 시험 | 97번 기록: 0 PASS / 4 FAIL / 5 SKIP | **9 PASS / 0 FAIL / 0 SKIP** |
| 유료 모델 호출 | 0 | 0 |

복구 후 통과 항목은 QA-SIGNUP-001~006, SEC-SIGNUP-002·003·005다. 시작 검사는 테스트용 테이블을 대신 만들지 않고, 앱 시작 후 원래 `database.get_db()`가 제공하는 연결에서 실제 매핑 테이블이 존재하는지 확인했다.

**이 결과는 사람이 수정한 진단 복사본의 결과다.** OpenAI 에이전트가 스스로 복구한 성공 표본이나 Single/Multi 성능 비교 결과로 집계하지 않는다. 96번 원본 Run의 HUMAN_REVIEW 상태·기존 측정값은 유지했다. 96번 Source와 97번 복사본의 바이트 해시도 모두 보존됐다.

진단 재현 스크립트·입출력은 Git 제외 경로 `.data/verification/98-startup-preservation-20261123/`에 남겼다. 이전 진단에 추가로 이번 복구를 적용한 것이므로, DB 초기화 수정 하나만으로 96번 원본의 모든 문제가 해결됐다는 뜻은 아니다. 97번의 요청 검증 오류 보호도 함께 필요했다.

## 회귀 검사

- 새 검사에서 불변 시작 코드와 현재 수정 코드의 구분, Source 비실행, 비밀 리터럴 비출력, 선택된 Build 실패의 정확한 참조, 잘못된 후보·보고서·실패 종류 제외를 확인했다.
- Multi, Single IMPLEMENTING, Single FIXING에서 실제 파일 읽기 → 해시에 맞춘 전체 쓰기 → 누락 안내 수신 → 읽기·복구를 확인했다. 모델 응답은 정해진 재현 응답이며, 모델 5회·Tool 4회·출력 교정 0회의 고정 한도 안에서 끝났다.
- 실제 Docker 합성 서비스에서도 초기화 연결 누락 → 실제 Build 실패 → 연결 복구 → 시작 PASS → 동일 최종 9개 PASS를 확인했다. 모델을 호출한 검사는 아니다.
- 기존 Single/Multi 빌드 실패 후 수정·재검증과 Source/Report 버전 연결, Developer·Host·입력 계약을 함께 점검했다.

| 최종 검사 묶음 | 결과 |
| --- | --- |
| 새 초기화 보존 검사·Checkpoint·시작 probe·97번 오류 보호·Source 수정 흐름·Prompt·입력 계약 | **210개 통과** |
| Developer·Fix·Host와 Single/Multi 빌드 실패 이후의 버전 연결·후속 검증 | **128개 통과** |
| 새 실제 Docker 초기화 복구 검사 | **1개 통과**, 내부 최종 시험 9/9 PASS |
| 96번 실제 Source를 바탕으로 한 별도 복사본 진단 | 시작 PASS, 최종 시험 9/9 PASS |

검사 묶음은 일부 겹치므로 합산하지 않는다. Developer·Host 묶음에서는 SDK의 `event_stream` 미설정 경고 128개가 있었으며, 다중 replica 스트리밍을 검증한 결과는 아니다. 새 Source 검사 파일이 없을 때 수집 실패를 먼저 확인했고, 구현 후 추가한 엔진 검사에서는 Multi fixture의 metadata 누락 1건을 바로잡은 뒤 위 최종 묶음을 통과했다.

주요 명령:

```bash
PYTHONPATH=src:tests .venv/bin/python -B -m pytest -q \
  tests/test_startup_preservation.py tests/test_developer_workspace.py \
  tests/test_start_contract_probe_routes.py tests/test_validation_error_repair_guidance.py

A2A_DOCKER_REPAIR_TEST=1 PYTHONPATH=src:tests .venv/bin/python -B -m pytest -q \
  tests/test_startup_preservation_docker.py
```

## 개발정의서 점검

- Orchestrator의 판정·실행 제어와 Agent의 코드 구현 책임을 유지했다.
- Single/Multi에 같은 공개 규격·안내를 적용하고 Ver1/Ver2의 보호시험 제공 차이는 보존했다.
- 해시 기반 파일 쓰기, 불변 Snapshot, 실제 DB 세션 검사, Sandbox·인증·승인·역할별 권한을 완화하지 않았다.
- 모델·Tool·토큰·시간·제품 수정·출력 교정 한도를 늘리지 않았다. 기존 코드와 사용자 설정·비밀 파일은 변경하지 않았다.
- Git 커밋·푸시는 하지 않았다.

**다음 99번:** 보완된 코드로 Multi Ver1을 실제 API로 한 번 실행해 에이전트가 초기화를 보존하거나 스스로 복구하는지 확인한다. 새 후보의 Build·QA·Security와 독립 최종 시험 결과를 구분해 기록한다. 이번 98번에는 포함하지 않았다.

커밋 메시지 제안: `앱·DB 초기화 보존 안내와 빌드 실패 복구 대상 전달 보완`
