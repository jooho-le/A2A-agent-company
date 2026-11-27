# SCN-001 시작 코드 골격

Single·Multi가 같은 상태에서 출발하는 회원가입 시나리오의 시작 코드다. 연결 규격은 dev 시나리오 계약의 `externalContract`와 같다(2026-11-14 4번 결정).
요구사항 구현(사용자 모델·가입 API)은 넣지 않았다. 이 README와 `requirements.in`은 팀용이라 시작 저장소에 들어가지 않는다.

## 들어 있는 것

| 파일 | 내용 |
|---|---|
| `main.py` | FastAPI 앱 `app`, 시작할 때 등록된 모델의 테이블 생성, 준비 확인용 `GET /`(200) |
| `database.py` | `DATABASE_URL` 환경 변수(없으면 작업 폴더의 `app.db`), SQLAlchemy 엔진·세션, `Base`, 동기 `get_db` 의존성 |
| `models.py` | 비어 있음. 개발 에이전트가 `User`를 만든다 |
| `tests/test_health.py` | 골격 자체 점검(앱이 뜨고 `GET /`가 200). 요구사항 시험이 아니다 |
| `requirements.in` | 생성 코드가 쓸 수 있는 패키지 후보 |

## 연결 규격 (dev 시나리오 계약 `externalContract`로 에이전트에게 전달됨)

- 앱: `main.py`의 `app`(FastAPI). 서버 실행은 `uvicorn main:app`.
- 준비 확인: `GET /`가 2xx(골격에 있음). dev 브라우저 시험 도구가 이 경로로 서버 준비를 확인한다.
- DB 세션: `database.py`의 `get_db`(동기 함수). 요청 처리의 DB 접근은 모두 `Depends(get_db)`로 한다. async DB는 시험지가 받지 않는다.
- DB 위치: 환경 변수 `DATABASE_URL`. 소스 폴더 옆 고정 경로는 쓰지 않는다(실행 Snapshot은 읽기 전용).
- 사용자 모델: `models.py`의 `User`. `database.Base`를 상속하고 `email`·`password_hash` 칸을 가진다. email의 UNIQUE 제약은 실제 DB에 적용되어야 한다.
- DB 초기화: 골격의 앱 시작(lifespan)에서 등록된 테이블을 만든다. 평가기는 새 `DATABASE_URL`을 제공하되 테이블을 대신 만들거나 `get_db`를 정상 함수로 교체하지 않는다. 이 초기화가 사라져 정상 가입이 실패하면 제품 실패다.
- 가입 API: `POST /api/auth/signup`, JSON `{email, password}`. 성공은 2xx, 입력 오류·중복은 4xx.
- 로그: 표준 `logging`과 표준 출력. 비밀번호·해시를 남기지 않는다.
- 개발자 자체 시험: `tests/test_*.py`.
- 패키지: 실행 이미지에 들어 있는 것만 쓴다(네트워크가 막혀 설치할 수 없다).

평가 쪽 시험(`tests/evaluation/test_starting_skeleton.py`)이 확인하는 것:
- 이 골격에 시험지·평가 코드가 섞이지 않았다.
- 골격만으로는 시험지가 모두 "미검증(User 모델 없음)"이고, FAIL로 잘못 나오지 않는다.
- 골격에 최소 구현(User 모델·가입 API)을 더하면 공식 최종 시험지의 9개 검사가 모두 PASS다. 즉 이 골격과 연결 규격으로 통과 가능한 구현이 있다.

## 시작 저장소 만들기

```bash
python -m evaluation.starting_source build --skeleton examples/starting-source/scn001-signup --lock <실행 이미지 requirements.lock> --out <새 저장소 폴더> --config configs/owned-source.local.json
```

- 연결 규격의 시작 파일(`main.py`, `database.py`, `models.py`, `tests/test_health.py`, `requirements.lock`)만 넣어 고정 작성자·시각으로 커밋한다.
- 출력의 `dependencyLockHash`를 도구 설정에 적는다. dev Host는 시작 커밋 안의 lock 바이트가 도구 설정의 lock과 같은지 검사한다.
- 확인: `python -m agents.platform.check_source --file configs/owned-source.local.json --tools <tools 설정>`

## 정해야 할 것

- (결정, 2026-11-14) 이 연결 규격을 그대로 쓴다. 바꾸면 평가 연결부(`signup_link.py`)·`protected_tests.CONNECTOR`·dev 계약을 함께 고치고 시험지 해시를 다시 고정한다.
- (구현, 2026-11-14) 연결 규격은 동결 시나리오 계약(`externalContract`)으로 모든 역할에 전달된다.
- 패키지 목록과 lock 한 벌. 시작 커밋, dev 실행 이미지(playwright·bandit 포함), 평가 이미지가 같은 lock을 써야 한다. 예를 들어 email-validator를 넣을지도 여기서 정한다.
- 입력 검증 오류 응답이 입력 값을 되돌려 주지 않게 하는 처리를 골격에 넣을지, 에이전트 몫으로 둘지. 노출 검사(REQ-006)에 영향을 준다.
