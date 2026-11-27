# 4번 평가 코드·실제 Docker 검증 기록

사용자의 2026-11-15 Docker 테스트 요청에 따라 로컬 Docker Desktop을 시작하고 D5 평가 이미지를 준비했다. 아래 환경은 **합성 서비스로 평가기를 검증한 환경**이며, 공식 Single/Multi 실험 환경으로 자동 채택한 것이 아니다. 유료 모델 호출은 하지 않았다.

## 환경과 실행 범위

| 항목 | 확인값 |
|---|---|
| Docker | 로컬 `desktop-linux`, 서버 29.1.3, Linux/arm64 |
| Base | `python@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90` |
| 평가 이미지 ID | `sha256:5b5cd3fdce6c29e777882b09c104383136d8f90e7e68a3c92b1301c4b98b35f1` |
| 로컬 태그 | `capstone-evaluation:localcheck-20261115` (평가에는 ID 사용) |
| 의존성 lock SHA-256 | `63cc4b1f10bd9002e244aafdcb4de8610597002b2806882459d954c07554475c` |
| 보호 시험 | 0.5.0, 최종 `b60ec485c5f9886c17f0f33fe449f801a0a5c6ad0b685bc8ed48066eedaeaefb` |
| 설치 | Linux wheel 22개, 각 파일 SHA-256 고정, 네트워크 없는 설치·`pip check`·실제 설치 버전 대조 통과 |

전체 패키지 버전과 wheel 지문은 [환경 기록](../../docker/evaluation/verification-2026-11-15-linux-arm64/environment.json), 정확한 잠금 파일은 [requirements.lock](../../docker/evaluation/verification-2026-11-15-linux-arm64/requirements.lock)에 보관한다. 이 잠금 파일은 CPython 3.13/Linux arm64용 wheel 해시를 담는다. 다른 CPU/플랫폼의 공식 환경은 그 환경에서 준비·동결해야 한다.

빌드에는 저장소의 [Dockerfile](../../docker/evaluation/Dockerfile)을 사용했고 임시 빌드 폴더에는 레시피·잠금 파일·wheel만 넣었다. 생성 소스·보호 시험지·API 키를 이미지에 넣지 않았다. 의존성 다운로드 단계 이후 빌드와 채점의 네트워크는 차단했다. 설치 버전 대조와 이미지 안 lock 바이트 대조는 서로 다른 확인이며 둘 다 기록했다.

## 실제 컨테이너 검사

기존 정상 채점 1개와 추가 통합 검사 9개를 준비했다.

| 검사 | 기대 동작 |
|---|---|
| 정상 서비스 최종 채점 | 최종 9개 검사 PASS |
| 정상 서비스 시범 채점 | pilot 9개 검사 PASS |
| 평문 저장·약한 Argon2 설정 | SEC-SIGNUP-002 FAIL |
| DB 초기화 없음 | 가입 검사 FAIL. 전제 조건을 확인하지 못한 나머지 검사는 ERROR/미검증으로 보존 |
| 이미지 lock과 소스 lock 불일치 | 본시험 NOT_RUN, 종합 UNVERIFIED |
| 엄격한 umask 077와 같은 입력 재생 | 컨테이너에서 입력 읽기 성공·재생 일치, 보관 입력 파일 0600·폴더 0700 |
| 정상→평문 저장 버전 비교 | 같은 기록 입력으로 각 버전 2회 실행, PASS/PASS→FAIL/FAIL 회귀 확인 |
| 격리 | UID/GID 10001, 유효 capability 없음, 권한 상승 금지, 외부 경로 없는 네트워크, 읽기 전용 루트, `/work` 임시 쓰기 |
| 시간초과 | 실행 중인 해당 평가 컨테이너 강제 삭제, 고유 이름으로 삭제 완료 확인 |

검사 코드는 [기존 Docker 검사](../../tests/evaluation/test_final_evaluation.py)와 [추가 통합 검사](../../tests/evaluation/test_docker_evaluation_integration.py)에 있다. 네트워크 격리는 비활성 터널 장치의 존재가 아니라 활성 인터페이스·라우팅으로 확인한다. DB 초기화 실패의 종합 상태는 확인된 실패와 미검증이 섞였으므로 UNVERIFIED이며, 이를 PASS로 바꾸지 않는다.

평가 전체(`tests/evaluation`)와 Host·Single/Multi 연동 39개 파일 **2,518개를 한 번에 검사하여 2,517 passed / 1 failed / 0 errors / 0 skipped**, 1228.12초를 확인했다. 실제 Docker 검사 10개는 모두 통과했다. 실패 1개는 새 이미지 lock 사전 검사를 반영하지 않은 기존 강제 종료 시험 대역이었다. 생산 코드는 변경하지 않고 해당 대역만 보완한 뒤 **관련 두 파일 89개가 모두 통과**했다(41.14초, 실제 Docker 정상 채점 재검사 포함). 현재 미해결 실패는 0개다. 전체 실행 이후 바뀐 점검 대상 파일은 해당 테스트 파일 하나뿐임을 SHA-256으로 대조했다. 수정 후 전체 2,518개를 다시 실행했다거나 중복된 검사 수를 합산했다고 표시하지 않는다.

[검증 결과 JSON](../../docker/evaluation/verification-2026-11-15-linux-arm64/test-results.json)에 전체·후속 검사 수, 시간, Docker 검사 이름, 변경 파일과 소스 지문을 따로 남겼다. 전체 JUnit은 `/tmp/capstone-part4-docker-verified-20261115.xml`, 후속 JUnit은 `/tmp/capstone-part4-docker-followup-20261115.xml`다. 전체 실행 로그는 `/tmp/capstone-part4-docker-verified-20261115.log`다. 앞선 예비 통합 실행은 하네스의 환경 복원 누락을 발견해 중단했고, 해당 경로와 Single의 같은 패턴을 수정한 뒤 위 전체 묶음을 새로 실행했다. 이 검사 소요시간은 제품 생성 속도나 실제 Single/Multi 실험 측정값이 아니다.

재실행(현재 로컬 이미지가 있는 환경):

```bash
DOCKER_CONTEXT=desktop-linux \
EVAL_DOCKER_IMAGE=sha256:5b5cd3fdce6c29e777882b09c104383136d8f90e7e68a3c92b1301c4b98b35f1 \
.venv/bin/python -m pytest -q \
  tests/evaluation/test_final_evaluation.py::test_real_container_run \
  tests/evaluation/test_docker_evaluation_integration.py
```

검사 자체는 이미지를 내려받거나 빌드하지 않는다. 환경변수를 주지 않으면 실제 Docker 검사는 건너뛴다. 이미지 ID는 로컬 이미지의 확인값이며, 다른 컴퓨터가 같은 ID를 이미 갖고 있다고 가정하지 않는다.

## 이번 코드 보완

- D5: 실제 이미지 lock 관찰, 고정 `requirements.lock` 경로 확인, umask 077 재생 입력 권한, 누락 커버리지의 미검증 처리, timeout 정리 실패 시 후속 컨테이너 중단.
- Agent 평가표: Run·Snapshot·Manifest 연결, 필수 요구사항·빌드 버전 분모, 중복 검사 ID·잘못된 요구사항 연결 차단, FAIL과 미검증 구분.
- 과정 지표: 같은 수정 주기의 Snapshot·QA 입력 변경 중복 계수 방지. 관찰된 파일 변화만으로 수정 경로 완료를 만들지 않음.
- 정적 보안 보조 판정: 정상 해시 저장 뒤 SQL·동적 필드로 평문을 덮어쓰는 경로와, 사용되지 않는 이메일 정규화가 다른 가입 경로를 PASS시키는 문제 보완. 정적 분석으로 확인할 수 없는 저장 경로는 미검증.
- 검사 하네스: 직접 준비한 비동기 테스트 객체에 정리를 위임해 환경 복원 실패가 숨겨지던 경로를 수정. 초기화된 소유 테스트가 정리하도록 하여 Docker 설정과 임시 폴더가 다음 검사에 섞이지 않게 함. 실제 서비스의 실행 환경을 임의로 복원하는 우회는 추가하지 않음.

## 개발정의서 대비 완료 경계

SCN-001의 기준·보호 시험·예제·지표·최종 채점·비교 도구는 구현되어 있다. 실제 Docker 검증은 평가기의 합성 서비스 실행 증거이며, 아래 항목의 완료 증거로 대체하지 않는다.

1. 공식 공통 시작 커밋, Host/D5 공통 실행 이미지와 도구 설정, 하드웨어·API 인증을 동결하고 사전점검을 통과해야 한다. 이 D5 이미지만으로 Host의 전체 도구가 준비되었다고 판단하지 않는다.
2. 실제 모델로 Single/Multi × Ver1/Ver2 시범을 실행하고 보안 실패→수정→재검증 사례를 확보해야 한다. 시범 한도는 이미 정해졌으며 공식 한도는 시범 자료로 조정한다. 공식은 조합마다 5회, 총 20 Run 및 비교·발표 자료가 남는다.
3. 구현 수정 주기 밖의 독립적인 테스트 수정 경로를 공식 계수하려면 Host가 그 경로의 시작·완료·roundId를 기록해야 한다. 현재는 연결되지 않은 변화를 감지·보존하되 확정 횟수로 세지 않는다.
4. **별도 확장 과제:** SCN-002는 회원가입 MVP 이후의 두 번째 측정 목표다. 전자문서 편집/제출·조회 범위와 API·저장·권한 계약, 시작 코드·시험지를 확정한 뒤 전용 검사를 구현해야 한다. 개발정의서 5쪽의 확정 MVP는 SCN-001이며 35쪽의 다중 시나리오 반복은 예시이므로, SCN-002를 현재 회원가입 MVP 구현의 필수 누락으로 세지 않는다. 후속 확장 목표 자체를 폐기한 것도 아니다.

Docker Desktop을 시작하면서 기존 다른 프로젝트의 컨테이너가 함께 실행된 상태를 관찰했다. 이 작업은 해당 컨테이너·데이터를 수정·삭제하거나 전역 정리를 하지 않았고, 로컬 Docker context를 전역 변경하지 않았다. 평가 컨테이너는 고유 이름과 자동 삭제를 사용한다. 검사 후 평가용 임시 컨테이너가 남지 않았고 기존 두 컨테이너가 유지됨을 확인했다. 검증용 이미지와 환경 기록은 재사용을 위해 남긴다.

## 이후 확인: 공식 실험 이미지 (같은 날)

공식 실험에는 이 검증용 이미지 대신 Host 샌드박스와 D5 채점이 함께 쓰는 공통 이미지 `capstone-sandbox:scn001`(`sha256:c712fdf445fe01217001440459070bf1e2b0a8d94ad3b9103bacfedddf7dae5a`, Playwright·Bandit 포함, wheel 33개 lock)를 쓴다. 보호 시험 0.5.2와 dev `3147711` 병합 상태에서 위 실제 컨테이너 검사 10개와 공식 시작 코드 통과 가능성 검사 3개(`tests/evaluation/test_experiment_image_skeleton.py`: 시작 코드 + 올바른 구현은 시범·공식 모두 9/9 PASS, 시작 코드만은 미검증)가 이 이미지에서 모두 통과했다(13 passed). 준비 방법은 [환경 설정](environment_setup.md)과 [시범 실험 실행 절차](experiment_runbook.md)에 있다.

