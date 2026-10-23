# SCN-001 평가 기준표와 생성물 연결 규격 초안 (D0)

작성: 2026-10-14 · 갱신: 2026-10-17(전체 점검 반영) · 4번 · 상태: 초안(19일 주 팀 회의에서 합의)
기준 순서: 개발정의서(계약) → dev `caa4144`(실제 동작) → 팀장 카톡·2차 기업미팅(우선순위·범위).

## 1. 판정 공통 규칙

| 상태 | 뜻 |
|---|---|
| PASS | 정한 관찰을 실제로 얻었고 기준을 만족함 |
| FAIL | 정한 관찰을 실제로 얻었고 기준을 어김 |
| UNVERIFIED | 관찰 자체를 얻지 못함(실행 오류, 대상 접근 불가, 분석 불가 구조, 증거 누락) |

- 근거가 없으면 PASS를 만들지 않는다. 실행 오류·증거 누락은 UNVERIFIED다.
- 같은 요구사항에 FAIL과 UNVERIFIED가 섞이면 요약은 UNVERIFIED이고 확인된 FAIL 목록은 따로 보존한다(기존 aggregation 규칙).
- 시험용 비밀번호는 실행 시 생성하고 보고서·로그에 원문을 남기지 않는다(dev 19번).
- 판정 방식: **정적** = 소스를 읽어서(실행·import 금지), **실행** = 생성물을 띄워 요청·DB를 확인.

## 2. 요구사항별 검사

검사 ID 규칙: `QA-SIGNUP-NNN`(기능), `SEC-SIGNUP-NNN`(보안). 기대값은 dev `scenario_registry.py` SCN-001 기준.

| 검사 | 요구 | 방식 | 입력 | 기대 응답 / DB | 증거 | FAIL | UNVERIFIED |
|---|---|---|---|---|---|---|---|
| QA-SIGNUP-001 | REQ-001 | 실행 | 미가입 유효 이메일 + 8자 이상 비밀번호 | 성공 응답, 사용자 1명 생성 | 응답, 요청 전후 사용자 수 | 성공인데 미생성, 실패 응답 | 서비스·DB 접근 불가 |
| QA-SIGNUP-002 | REQ-002 | 실행 | 형식 오류 이메일(@ 없음, 도메인 없음, 빈 값 등) | 실패 응답, DB 변화 없음 | 응답, 전후 사용자 수 | 가입됨 또는 성공 응답 | 〃 |
| QA-SIGNUP-003 | REQ-003 | 실행 | 가입한 이메일을 대소문자·앞뒤 공백만 바꿔 재가입 | 실패 응답, 사용자 1명 유지 | 두 응답, 전후 사용자 수 | 중복 생성 또는 성공 응답 | 〃 |
| QA-SIGNUP-004 | REQ-004 | 실행 | 7자 비밀번호 / 8자 비밀번호 | 7자 거부·미생성, 8자 성공 | 응답, 사용자 수 | 경계 반대 동작 | 〃 |
| QA-SIGNUP-005 | REQ-003 | 실행 | 같은 canonical 이메일 동시 요청(N=5) | 정확히 1명 생성, 나머지 실패 | 응답 목록, 최종 사용자 수 | 2명 이상 생성 | 동시 실행 불가 |
| QA-SIGNUP-006 | REQ-007 | 실행 | 성공·형식 오류·중복 요청 | 응답 성공/실패가 실제 DB 변화와 일치 | 응답, 전후 사용자 수 | 불일치 | 〃 |
| SEC-SIGNUP-001 | REQ-005 | 정적 | 소스 | Argon2id 사용, 수치 기준 충족, 평문·일반 Hash 저장 없음 | 위치·설정값 | 평문 저장, MD5/SHA 등, 수치 미달 | 설정이 상수가 아님, 해시 경로 미확인 |
| SEC-SIGNUP-002 | REQ-005 | 실행 | 가입 후 저장값 조회, 가입 비밀번호와 별도 생성한 다른 비밀번호 | `$argon2id$`, 수치 충족, 같은 비밀번호 두 계정의 salt 다름, 원문과 다름, 가입 비밀번호로 verify 성공·다른 비밀번호로 실패 | 형식·수치·검증 성공/실패(비밀번호·해시 값은 기록 안 함) | 평문·다른 알고리즘·수치 미달·salt 중복·입력 비밀번호 불일치·다른 비밀번호 검증 성공 | DB 접근·검증 불가 |
| SEC-SIGNUP-003 | REQ-003 | 정적 | 소스 | 이메일 UNIQUE 선언 + 정규화(strip·lower). pydantic 설정(`str_strip_whitespace`·`str_to_lower`)·`CREATE UNIQUE INDEX`도 인정 | 위치 | 이메일 컬럼에 UNIQUE 없음, strip·lower 없음 | 모델 구조 미확인 |
| SEC-SIGNUP-004 | REQ-006 | 정적 | 소스 | 응답·로그·검증 오류 응답에 비밀번호·해시 경로 없음 | 위치 | 노출 경로 발견 | 분석 불가 |
| SEC-SIGNUP-005 | REQ-006 | 실행 | 성공·실패 요청 후 응답 본문·서버 로그 | 비밀번호 원문·저장 해시 문자열 없음 | 검색 결과(일치 여부만) | 발견 | 로그 수집 불가 |

- REQ-008(추적 가능성)은 registry에서 검증자가 `ORCHESTRATOR`라 이 표에 없다. 독립 평가 범위가 아니며 D4에서 Trace로 확인할 계획이다. 요구→개발→검증→수정의 연결 확인은 아직 미구현이다.
- 정의서 REQ-003 PASS 기준은 "DB UNIQUE 제약 존재, 동시 요청에서도 중복 생성 불가"다. 무결성 오류(IntegrityError) 처리 여부는 PASS 조건에 없어 정적 검사에서 뺐고, 동시 요청 확인은 QA-SIGNUP-005가 한다.
- 순환 안 QA 보호 테스트(dev PROTECTED)로 연결 가능한 것은 QA 담당 요구사항(REQ-001·002·003·004·007)뿐이다(dev `qa_services.py`).
- SEC 정적 검사(001·003·004)는 D1 `assess()`로 순환 안 Security 판정에 쓴다. SEC 실행 검사(002·005)는 최종 독립 평가용이다.
- REQ-006의 A2A Message/Artifact·Trace 노출은 Orchestrator 데이터라 이 표에서 제외하고 D4에서 별도 확인할 계획이다. 이 노출 검사는 아직 미구현이며 현재 사건 건수·시간 집계와 구분한다.
- 범위 축소(2차 기업미팅): SQL Injection·XSS 등 추가 보안 항목, 다수 화면 테스트는 이번 범위 밖.

### 정적 분석(SEC-SIGNUP-001·003·004)의 판정 원칙

- **PASS는 지원하는 구조에서 증명된 경우에만** 낸다. 지원하는 구조는 FastAPI + pydantic + SQLAlchemy(또는 SQLModel)로 만든 회원가입 서비스이고, 증명하지 못하는 코드(모르는 함수의 결과, 해석할 수 없는 SQL, 소스에서 찾을 수 없는 모델 등)는 UNVERIFIED로 남긴다. 복잡한 분석을 계속 넓히기보다 증명할 수 있는 패턴을 명시하고 나머지는 실행 검사(SEC-SIGNUP-002·005)에 맡긴다.

- 지원하는 코드 패턴에서만 PASS/FAIL을 내고, 모르는 구조는 UNVERIFIED로 남긴다. 정상 코드를 FAIL로 만드는 오탐은 불필요한 수정 순환을 만들기 때문에 이름은 단어 단위로 본다(`passed`·`MIN_PASSWORD_LENGTH`·`PASSWORD_ERROR_MESSAGE`는 비밀번호 값이 아니다).
- 요청 모델을 통째로 로그·출력하거나 `model_dump()`로 펼치면 비밀번호 필드가 함께 나가므로 FAIL이다(`SecretStr` 필드는 제외). 응답에 객체 전체(`__dict__`)를 펼치면 필드를 정적으로 알 수 없어 UNVERIFIED다. 다만 입력 검증 오류(422)는 다르다: pydantic 오류 항목의 `input`에는 `SecretStr`로 바뀌기 전의 원문이 담기고 FastAPI 기본 처리기는 그대로 응답한다(2026-10-17 실제 FastAPI로 확인). 그래서 비밀번호 필드가 `SecretStr`이어도 `input`을 걸러 내는 검증 오류 처리기가 없으면 FAIL이다.
- 검증 오류 응답은 `loc`·`msg`·`type`만 새 dict로 옮기면 안전, `exc.errors()`·`exc.body`를 그대로 돌려주면 FAIL이다. 목록을 훑으며 `input`을 지우는 방식은 **모든 항목에서 반드시 지우는 모양**일 때만 인정한다: 함수 본문 바로 아래(`if`·`try` 안이 아닌 곳)에 있는 `for error in errors:`(`errors`는 `errors = exc.errors()`로 한 번만 대입한 변수)이고, 반복문 본문이 `error.pop("input", None)`·`del error["input"]` 같은 삭제문뿐이며, 그 반복문이 끝난 **뒤에** 목록을 쓸 때다. `if` 안에서만 지우거나 `continue`로 건너뛸 수 있거나 `if` 안에 반복문이 있거나 삭제문 말고 다른 문장이 섞여 있으면 UNVERIFIED, 정리 전에 원본 목록을 쓰거나 일부 항목(`errors[:1]`)만 정리하거나 지운 뒤 `input`을 다시 넣으면 FAIL이다.
- 해시 저장 경로는 "승인한 Argon2 해시 도구(`PasswordHasher(...)`, `PasswordHash((Argon2Hasher(),))` 등)로 만든 값"일 때만 인정한다. `.hash()`라는 이름의 메서드면 무엇이든 인정하지 않고, 변수에 나중에 원문을 다시 대입하거나 매개변수를 그대로 돌려주는 경로가 있으면 PASS가 아니라 UNVERIFIED다.
- 이메일 정리(strip·lower)는 **저장되는 이메일 값이 실제로 정리를 거쳤다고 확인될 때** 인정한다(2026-10-17 결정 A). 근거는 세 가지다: ① 이메일 필드에 등록된 검증기(`@field_validator("email")`·`@validator("email")`, 함수 이름은 상관없음)의 모든 반환 경로, ② pydantic 정규화 설정, ③ 가입으로 사용자를 저장하는 곳(`User(email=값)`, `user.email = 값`)마다 그 값을 거슬러 따라간 결과 — 변수(함수 본문 바로 아래의 차례대로 된 대입은 순서대로, 그 밖은 모든 대입의 공통), 프로젝트 함수의 반환 경로(`normalize(payload.email)`), 프로젝트 안의 모든 호출에서 넘긴 값(`create(db, email.strip().lower())`), 함수 본문 바로 아래에서 먼저 한 번만 되쓴 필드(`payload.email = payload.email.strip().lower()`). 조건 안에서만 정리하거나, 정리한 뒤 원문으로 덮거나, 저장한 뒤에 정리하거나, 함수를 값으로 넘겨(콜백·등록) 보이지 않는 곳에서도 불릴 수 있으면 근거가 아니다. FAIL은 세 경우뿐이다: 등록된 검증기가 정리하지 않은 값을 돌려줌, 빠진 정리 방법이 프로젝트 어디에서도 이메일 값에 쓰이지 않음, 가입 경로 함수(`@router.post` 등)가 받은 입력 필드를 바꾸지 않고 그대로 저장함(정리한 값을 계산만 하고 원래 값을 저장하는 코드 포함, `Depends(...)`로 받은 값은 제외). 정리 코드는 있지만 저장 값과 연결되는지 증명하지 못하면 UNVERIFIED다. 관리자 계정 시드처럼 따옴표 글자로 적은 이메일은 가입 저장으로 보지 않는다.
- 해시 도구라고 믿는 **이름**(예: `HASHER`)은 그 이름에 값이 묶이는 곳이 모두 승인한 도구를 만드는 대입일 때만 인정한다. 같은 이름의 함수 매개변수(`def hash_password(password, HASHER=Identity())`), 다른 값의 대입, `for`·`with`·`import ... as`·`def`·`class`로 덮는 코드가 하나라도 있으면 그 이름을 믿지 않는다(UNVERIFIED). 해시 라이브러리의 메서드를 덮어쓰는 코드(`PasswordHasher.hash = ...`, `setattr`·`object.__setattr__`, `HASHER.__dict__["hash"] = ...`, `vars(HASHER).update(...)`, `HASHER.__class__ = ...`, `globals()["HASHER"] = ...`, 어느 항목인지 모르게 다른 객체의 `vars(...)`에 쓰는 코드)가 있으면 어떤 이름도 믿지 않는다. 자기 자신의 이름 공간을 채우는 관용구(`self.__dict__.update(kwargs)`)는 제외한다. 같은 파일에서 다시 정의한 import 이름(`class PasswordHasher: ...`)도 라이브러리로 보지 않는다.
- 함수 이름도 같은 방식으로 본다. `hash_password`처럼 해시를 만든다고 믿는 **함수 이름**은 그 이름에 함수 정의 말고 다른 값이 묶이는 곳(`hash_password = lambda ...`, 같은 이름의 매개변수, `with ... as hash_password`, 같은 이름을 두 번 import하거나 가져온 이름을 다시 정의)이 없고, 함수에 `@staticmethod`·`@classmethod` 말고 다른 데코레이터가 붙어 있지 않을 때만 인정한다(데코레이터는 함수를 다른 것으로 바꿔 놓을 수 있다). 아니면 UNVERIFIED다.
- 해시하는 값은 **입력(함수 매개변수)의 내용을 통째로 간직한 값**이어야 한다. 변수에 담은 상수(`fixed = "x"; HASHER.hash(fixed)`), 매개변수를 상수로 덮어쓴 뒤 해시, 전역 상수 해시, `password or "x"`처럼 갈래 하나가 상수인 값, 해시 함수에 상수를 넘기는 호출, 해시와 상관없는 매개변수에만 입력을 넘기는 호출은 비밀번호의 해시라고 증명할 수 없으므로 UNVERIFIED다.
  입력을 그대로 두거나 모양만 바꾸는 것만 인정한다: `password`, `payload.password`, `data["password"]`, `.strip()`·`.lower()`·`.upper()`·`.casefold()`(인자 없이), `.encode(...)`·`.decode(...)`(글자를 버리는 오류 처리 제외), `.get_secret_value()`, 표준 `str()`·`bytes(x, "utf-8")`, `unicodedata.normalize("NFKC", x)`, `base64.b64encode(x)`, `pepper + password`, `f"{password}"`, 입력을 그대로 돌려주는 프로젝트 함수. 입력을 지우거나 일부만 남길 수 있는 것은 모두 거부한다: `password * 0`, `password *= 0`, `password[:3]`, `password[0]`, `.replace(...)`, `.strip("abc")`, `f"{password:.0s}"`, 반복문·풀어 받기로 얻은 일부, 입력을 버리는 프로젝트 함수(`discard(password)`), 모르는 함수의 결과. 프로젝트가 `strip` 같은 이름의 메서드를 직접 만들었거나 내장 `str`을 다시 정의했다면 표준 동작이라고 믿지 않는다.
  저장하는 곳에서는 거기에 더해 해시하는 값의 **실제 출처가 비밀번호 필드**인지도 본다. 출처는 (입력 이름, 그 안의 필드 경로)로 따라간다: `payload.password` → (`payload`, `password`), 매개변수 `password` → (`password`, 경로 없음). 변수 이름이나 넘긴 인자의 이름은 믿지 않고, 지역 변수는 그 변수에 담긴 값을 따라가며(`password = payload.email`이면 이름이 password여도 출처는 email이다), 프로젝트 함수는 **실제로 돌려주는 값**의 출처를 호출한 쪽이 넘긴 값에 이어 붙여 본다(비밀번호와 이메일을 받아 이메일만 돌려주는 함수의 결과는 이메일이다). 같은 요청 객체에서 나왔더라도 `payload.email`을 해시해 저장하면 UNVERIFIED다.
  입력 객체의 필드를 이 함수(또는 이 함수가 입력을 넘겨 받은 프로젝트 함수·메서드)가 덮어쓰면 그 필드의 값은 원래 입력이 아니므로 출처에서 뺀다. 쓰는 방식은 필드 대입(`payload.passwd = ...`, `form["passwd"] = ...`, `+=`, `del`)뿐 아니라 `setattr`·`delattr`, `object.__setattr__(x, ...)`·`x.__setattr__(...)`, 이름 공간 쓰기(`x.__dict__["passwd"] = ...`, `vars(x)[...] = ...`, `x.__dict__.update(...)`, `dict.update(vars(x), ...)`, `operator.setitem(vars(x), ...)`), `pop`·`setdefault`·`update`가 모두 해당한다. 바뀌는 항목 이름이 글자로 적혀 있으면(`update({"k": v})`, `pop("k")`, `setattr(x, "k", v)`) 그 항목만, 변수 키나 다른 사전을 통째로 넘기면(`update(extra)`, `setattr(x, key, v)`) 객체 전체가 바뀐 것으로 본다. 별칭을 만드는 방식도 단순 대입(`alias = form`)만이 아니다: 튜플 풀기(`a, b = form, db`), `a or b`·조건식, 리스트·사전에 담았다 꺼내기(`[form][0]`, `{"k": form}["k"]`, `.get("k")`, `list(...)`·`next(iter(...))`), `for`·`with`·`:=`·컴프리헨션·`match`의 변수, 입력을 돌려주는 프로젝트 함수·메서드(`ident(form)`, `self`를 돌려주는 `form.me()`)가 모두 같은 입력을 가리키는 것으로 본다. 입력을 담은 리스트·사전 자체를 바꾸는 것(`items.append(...)`, `holder[0] = ...`), 입력의 하위 객체나 항목만 바꾸는 것(`form.profile.bio = ...`, `for item in form.items: item.x = ...`), 사본(`form.model_dump()`, `dict(vars(form))`)을 바꾸는 것, 다른 필드(`payload.email = payload.email.strip()`)를 바꾸는 것은 비밀번호 필드를 바꾸는 것이 아니라서 상관없다. `self.password`처럼 객체의 필드는 호출한 쪽이 넘긴 입력이 아니라서 인정하지 않는다. 따라가지 못하는 것은 한계로 둔다: 알 수 없는 라이브러리 함수가 입력을 감싸 돌려주는 경우(`w = Wrapper(form)`), 나중에 컨테이너에 담는 경우(`h = []; h.append(form); h[0].passwd = ...`), 람다를 바로 호출해 돌려받는 경우, 프레임워크가 암묵적으로 부르는 검증기(`@model_validator`)가 필드를 바꾸는 경우, 입력을 다섯 단계보다 깊은 도우미 함수 안에서 바꾸는 경우.
  해시 함수도 **실제로 돌려주는** 값만 본다. 비밀번호의 해시를 계산만 하고 버린 뒤 이메일의 해시를 돌려주거나(`HASHER.hash(password); return HASHER.hash(email)`), 두 해시를 튜플·이어 붙인 글로 돌려주거나, 갈래마다 다른 값을 해시하거나, 해시 변수를 이메일의 해시로 덮어쓰면 UNVERIFIED다. 해시를 담은 변수는 거기에 담기는 모든 해시가 비밀번호의 것일 때만 인정하며, 비밀번호 해시와 다른 해시가 번갈아 담기면 순서와 상관없이 UNVERIFIED다(보수적인 판정).
- salt: 시나리오 정책(`uniqueSalt`)에 따라 고정 salt(`salt=b"..."`, 글자만으로 만든 식, 파일 맨 위의 상수 이름)를 넘기면 FAIL이고, `salt_len`을 8바이트 미만으로 정해도 FAIL이다. 출처를 알 수 없는 salt(함수 호출 결과 등)나 `hash(password, **옵션)`처럼 펼친 인자는 UNVERIFIED다. `os.urandom(16)`·`secrets.token_bytes(16)`은 **진짜 라이브러리라고 확인될 때만** 인정한다: 그 파일이 `import os`(또는 `from os import urandom`)로 가져왔고, 같은 이름을 다시 정의하지 않았으며, 프로젝트에 `os.py`·`os/` 같은 같은 이름의 파일이 없고, `os.urandom = ...`·`os.__dict__["urandom"] = ...`·`object.__setattr__(os, "urandom", ...)`처럼 덮어쓴 곳이 없고, 크기가 8바이트 이상일 때이다. 이름만 같은 가짜 객체는 믿지 않는다.
- 저장 경로는 `User(password_hash=...)`만이 아니다. `.values(password_hash=...)`, `setattr(user, "password_hash", ...)`, `{"password_hash": ...}` 사전, `row["password_hash"] = ...`, `db.execute("UPDATE ... password_hash ...", (값,))`에 넣는 값도 같은 기준(원문·상수이면 FAIL, 해시 함수의 결과이면 인정)으로 본다. 사전·SQL은 응답이나 조회에도 쓰이므로 무엇인지 모르는 값은 "확인 못 함"으로 치지 않는다. 무엇인지 확인하지 못한 변수를 저장할 때 이름만으로 원문이라고 단정하는 것은 비밀번호처럼 보이는 이름(`password`, `pwd`)이면서 `hash`·`digest`가 들어 있지 않은 경우뿐이다. `password_digest`처럼 기준의 저장 칸 이름이기도 한 이름은 원문으로 단정하지 않고 UNVERIFIED로 둔다.
- 해시 도구의 설정은 코드에 적힌 숫자(또는 한 번만 대입한 상수)일 때만 읽는다. `PasswordHasher(**{...})`·`PasswordHasher(*[...])`처럼 풀어서 넘긴 값은 약한 설정이 안전한 기본값으로 둔갑하지 않도록 확인 불가(UNVERIFIED)로 처리한다. 상수 글자를 해시한 값(`hash("fixed")`)은 비밀번호의 해시로 보지 않는다.
- 이메일 UNIQUE는 **가입으로 사용자를 저장하는 표**의 것이어야 한다. `User(email=..., password_hash=...)`처럼 사용자 표 클래스를 이메일·저장 칸 값과 함께 만드는 곳이 있으면 그 표들만 본다(보관용 표처럼 가입에 쓰지 않는 표의 UNIQUE는 가입 표의 결함을 가리지 못한다). 어느 표가 가입에 쓰이는지 확인되지 않으면 비밀번호 칸이 있는 모든 표를 보고, 표마다 결과가 다르면 UNVERIFIED다. 요청 모델(pydantic)은 DB 표가 아니라서 제외한다.
- SQL은 **실제로 실행하는 글자**(`execute`·`executescript`·`text()`의 인자, 또는 그 인자로 넘기는 이름에 담은 글자)만 읽는다. 함수 설명 글(docstring)이나 어디에도 쓰이지 않는 글자에 적은 `CREATE TABLE`은 근거가 아니다. 읽을 때는 주석(`--`, `/* */`)과 따옴표 글자의 내용을 먼저 걷어 낸다(`DEFAULT 'unique'`는 제약이 아니다). `CREATE TABLE`은 맨 바깥 쉼표로 칸별로 나눠 읽고, 괄호가 맞지 않거나 `UNIQUE (lower(email))`처럼 계산식 칸이거나 `WHERE`가 붙은 부분 인덱스이거나 `ALTER TABLE`로 바꾸는 경우는 단정하지 않고 UNVERIFIED다. ORM의 `Index(..., postgresql_where=...)`도 부분 인덱스라서 같다.
- 이메일 UNIQUE는 이메일 칸 **하나**만 보호하는 것만 인정한다. `UniqueConstraint("email", "tenant_id")` 같은 복합 제약은 같은 이메일을 허용할 수 있어서 근거가 되지 못한다(SQL `UNIQUE (email, tenant)`·복합 `CREATE UNIQUE INDEX`도 같다). UNIQUE는 **사용자 표**(같은 클래스·`CREATE TABLE`·`Table(...)` 안에 비밀번호 칸이 있는 표)의, 이메일 칸 **자신의** 선언만 인정한다. `CREATE TABLE`은 칸별로 나눠 읽으므로 `id INTEGER UNIQUE, email TEXT`처럼 같은 줄의 다른 칸 UNIQUE는 이메일의 것이 아니다. 비밀번호 칸이 없는 표의 이메일 UNIQUE는 근거가 되지 못하고, `CREATE UNIQUE INDEX`는 대상 표가 사용자 표로 확인될 때만 인정한다(확인할 수 없으면 UNVERIFIED). 칸은 글자뿐 아니라 칸 객체(`UniqueConstraint("email", tenant_id)`, `Index("ix", email, tenant_id, unique=True)`)도 세므로 둘이 섞여도 복합이다. `UniqueConstraint(*칸목록)`·`Index("ix", func.lower(email), unique=True)`처럼 어떤 칸인지 코드로 읽을 수 없는 제약은 "UNIQUE가 없다"고 단정할 수 없어 UNVERIFIED다.
- `response_model`은 **프로젝트 안에 정의된 모델 클래스이고 추가 필드·상속·직렬화 설정을 확인할 수 있을 때만** 응답 필터로 인정한다. `extra="allow"`는 선언하지 않은 비밀번호 필드를 보존할 수 있어 필터 근거가 아니며, 지원하지 않는 상속·사용자 정의 직렬화는 UNVERIFIED다. `response_model=None`은 필터가 꺼진 것으로 보아 민감 객체를 그대로 반환하면 FAIL이다. 소스에서 찾을 수 없거나 설정을 확인할 수 없는 모델은 민감 객체 반환을 UNVERIFIED로 남긴다. `JSONResponse(content=...)`처럼 응답 객체를 직접 만들어 돌려주면 `response_model`이 있어도 걸러지지 않으므로 그 안의 비밀번호를 항상 확인하고, 응답 객체를 만들어 돌려줄 수 있는 도우미 함수의 결과는 UNVERIFIED다. `@router.post`뿐 아니라 `@app.route`(Flask 방식)와 `add_api_route`로 등록한 경로 함수도 같은 기준으로 본다.
- 검증 오류 처리기는 등록 방법(`@app.exception_handler`, `add_exception_handler`, `app.exception_handlers[...] = ...`, `FastAPI(exception_handlers={...})`)과 상관없이 **소스에서 함수를 찾아** 분석한다. 람다이거나 모르는 이름이거나 다른 것으로 바뀔 수 있는 이름이면, 또는 `exception_handler` 말고 다른 데코레이터가 함께 붙어 있으면 실제 실행되는 코드를 알 수 없으므로 UNVERIFIED다. 처리기 안에서 예외 객체(`exc`) 자체를 `str(exc)`·`exc.args`·로그·응답 값으로 쓰면 오류 목록(입력값 포함)이 나가므로 FAIL이다(`exc.errors()` 호출과 `type(exc)`는 제외).
- 비밀번호는 변수를 거쳐서도 새므로, 한 함수 안에서 값을 옮겨 담은 변수를 따라간다: 비밀번호 모델 객체(하위 클래스·별칭 포함)의 복사, 비밀번호를 담은 사전·문자열·`f"..."`·`str()`·`json.dumps()`, `payload.password or ""`·`a and payload.password`(and·or는 피연산자 값을 그대로 돌려준다), 비밀번호를 돌려주는 람다·안쪽 함수를 담은 변수, `payload.password`를 다른 이름에 담은 것, 요청 본문을 통째로 읽은 변수(`request.json()`·`request.get_json()` 등), 해시 함수의 결과를 담은 변수(REQ-006은 해시 노출도 금지한다)가 그렇다. 사전의 키만 꺼내거나 안전한 항목(`data["email"]`)만 쓰는 것, 길이·비교 결과, 모르는 함수의 결과, 전부 대문자인 상수(`Status.PASS`)는 비밀번호로 보지 않는다. 변수 이름 기준으로 따라가므로, 같은 함수 안에서 한 이름을 비밀번호용과 다른 용도로 번갈아 쓰면 오탐할 수 있다. 내보내는 곳은 로그·`print`·`pprint`뿐 아니라 `file.write(...)`·`response.set_cookie(...)`·`response.headers[...] = ...`, `locals()`(함수의 모든 변수를 꺼냄)도 포함한다.
- 검증 오류 응답은 키 이름뿐 아니라 **값의 출처**도 본다. `error["loc"]`·`msg`·`type`만 옮기면 안전하고, `error["input"]`나 `str(error)`처럼 입력값이 섞이면 FAIL, `error["ctx"]`처럼 안을 알 수 없는 값은 UNVERIFIED다.
- Bandit 경고는 코드 근거가 있을 때만 판정한다(개발정의서 10절). **변수 이름이 먼저다**: 비밀번호류 이름에 비어 있지 않은 값을 넣으면 값이 `"password"`든 공백 있는 문장이든 한글이든 CONFIRMED이고, 그 밖의 이름에서 안내 문구·필드 이름 글자·빈 값만 FALSE_POSITIVE, 나머지는 SUSPECTED로 둔다.
- **분석 시간 한도**: 이상하게 쓴 코드가 판정기를 멈추거나 오래 붙잡지 못하게 한다. 입력의 출처·별칭·덮어쓰기를 따라가는 계산에는 작업량 한도(`WORK_LIMIT` 100,000)가 있고, 식이 너무 깊게 중첩되어 파이썬의 재귀 한도를 넘는 경우도 같이 본다. 한도를 넘으면 그 요구사항만 UNVERIFIED(근거 코드 `ANALYSIS_TOO_COMPLEX`)로 답하고 PASS를 지어내지 않는다(다른 요구사항의 판정은 그대로 나온다). 한도는 측정값을 보고 정했다: 정상 예제와 저장소 `.py` 코드 389묶음은 53 이하(364묶음은 0)를 쓰고, 가장 무거운 인공 입력(같은 이름에 2,000번 재대입)은 약 24,000을 쓴다. 코드 길이에 비례하는 시간 안에 끝나는지는 `tests/evaluation/test_security_limits.py`가 별도 프로세스에서 시간 제한을 두고 확인한다(같은 이름에 여러 객체를 번갈아 대입하는 코드, 지수적으로 불어나는 식, 수천 줄의 같은 문장 등 36개 사례. 직전 커밋의 분석기에서는 그중 20개가 20초 안에 끝나지 않거나 오류로 끝났다).
- 정적 분석의 한계: 실제 호출 흐름·동시성·런타임 설정은 보지 않는다. 그래서 실행 검사(QA-SIGNUP-005, SEC-SIGNUP-002·005)가 따로 있다.
- 남아 있는 한계: ① 이메일 UNIQUE는 같은 클래스·표 안에 비밀번호 칸이 있는 표의 것만 인정하므로, 비밀번호가 다른 표에 있는 구조(별도 credentials 표)나 이메일 칸을 믹스인으로 물려받는 구조는 UNVERIFIED가 된다. ② `password = "비밀번호를 입력하세요"` 같은 화면 안내 문구를 비밀번호 이름에 넣은 코드는 CONFIRMED로 판정할 수 있다. ③ 해시 도구를 함수 호출 결과(`get_hasher().hash(...)`)로 받으면 UNVERIFIED가 된다. ④ 해시 결과를 변수에 담았다가 저장하는 코드(`hashed = hash_password(...)` 다음에 `password_hash=hashed`)는 그 변수에 값이 묶이는 곳이 모두 `이름 = 해시 호출(...)`일 때만 따라가서 PASS로 인정한다(분기에서 평문을 대입하거나 매개변수로 받은 변수, 이메일을 해시한 변수는 UNVERIFIED). ⑤ 정리 반복문에 삭제문 말고 다른 문장(예: `error["msg"] = ...`)을 함께 쓰면 UNVERIFIED다. ⑥ f-string으로 조립한 SQL, 여러 파일에 나뉘어 반복문으로 실행하는 SQL, `.sql` 파일은 읽지 않는다(실행하는 글자를 코드에서 직접 볼 수 있을 때만 근거로 쓴다). SQL 글자를 f-string으로 조립해 비밀번호를 끼워 넣는 코드처럼 ORM 밖의 저장 방식은 일부만 본다(`execute("... password_hash ...", (값,))` 형태는 본다). ⑦ REQ-006의 PASS는 "지원하는 노출 경로를 찾지 못했다"는 뜻이지 노출이 없다는 증명이 아니다. 값 따라가기는 한 함수 안에서만 하므로, 다른 함수로 값을 넘겨 그 안에서 출력하거나 바깥 함수의 변수를 안쪽 함수가 쓰는 경우, 모르는 함수를 거친 값은 놓칠 수 있다. 코드를 실행 중에 바꾸는 기법(`exec`, `eval`, `importlib`로 모듈 바꾸기, 프로젝트 밖의 라이브러리 함수가 입력 객체의 필드를 안에서 바꾸는 경우 등)은 모두 추적하지는 못한다. `__dict__`·`vars()`·`setattr` 같은 필드 쓰기는 위 본문대로 추적한다. ①~⑤와 ⑥의 앞부분(읽지 않는 SQL)은 안전한 쪽(PASS를 내지 않는 쪽)으로 틀리는 한계이고, ⑥의 뒷부분(ORM 밖의 저장 방식)과 ⑦은 놓칠 수 있는 한계라서 실행 검사(SEC-SIGNUP-002·005)가 따로 있다.
- 이름 판정의 한계: 설명 단어(text, name, key 등)가 낀 이름은 비밀번호 값이 아닌 것으로 본다. 그런 이름의 변수에 실제 비밀번호를 담아 출력하면 정적 검사가 놓치므로 실행 검사(SEC-SIGNUP-005)로 확인한다.
- 이 분석기의 판정은 합성 예제(정상 1·결함 18·정상 변형 9)와 사람이 만든 회원가입 backend 표본, 외부 점검에서 재현된 우회 사례(1차 12개·2차 5개·3차 4개·4차 11개·5차 2개·6차 2개)와 같은 종류의 변형 318개(정상 코드가 FAIL이 되지 않는지 보는 확인 포함), 자연스러운 회원가입 구현 11가지(서비스 클래스, 이메일을 입력 객체에 되써서 정리, `setattr`로 필드 복사 등)가 그대로 통과하는지 보는 확인, 입력 필드를 덮어쓰는 방법의 조합 시험(별칭을 만드는 19가지 방식 × 쓰는 20가지 방식 등 약 1,100개 조합), 시간·크기 시험 43개로 확인했고, 실제 에이전트가 만든 코드로는 아직 돌려 보지 않았다.

## 3. 합의 필요 항목

| 항목 | 4번 초안 | 근거 |
|---|---|---|
| Argon2 수치 규칙 | registry 값 **이상**(AT_LEAST). 정확히 일치(EXACT)도 기준 JSON에서 선택 가능 | 정의서 "최소 Memory 19MiB" 표현, registry 고정값 |
| REQ-003 Security PASS 조건 | 정적 근거(이메일 칸 하나에 대한 UNIQUE 선언 + 정규화)로 PASS, 동시성 실제 확인은 QA-SIGNUP-005(최종 평가용) | 정적만으로 UNVERIFIED면 Run이 항상 HUMAN_REVIEW |
| REQ-006 Security PASS 조건 | 제품 소스의 노출 경로 부재로 PASS, 실행 확인은 SEC-SIGNUP-005 | 〃 |
| MEDIUM Finding 처리 | 지원 규칙만 CONFIRMED/FALSE_POSITIVE, 나머지는 SUSPECTED 유지 | 정의서 10-3 팀 미결정 |
| 보호 테스트 위치 | B로 평가 + A로도 실행해 비교(팀장 합의), B′ 추가 제안 | 2026-10-12 카톡 |

## 4. 생성물 연결 규격 초안 (시작 코드와 함께 확정)

보호 테스트는 아래 연결 기능만 사용한다. 실제 구현은 시작 코드가 정해진 뒤 어댑터 한 파일로 맞춘다.

| 연결 기능 | 필요한 규격 | 현재 |
|---|---|---|
| 가입 요청 | 앱 진입점, 경로·메서드, 요청 필드, 성공·실패 상태 코드 | 미정 |
| 저장 조회 | DB 종류·위치, 사용자 테이블·컬럼(email, password_hash) | 미정 |
| 초기화 | 테스트 DB 교체 방법(의존성 주입 또는 경로 설정) | 미정 |
| 로그 수집 | 서버 로그 출력 위치 | 미정 |
| 실행 환경 | Python 3.13, lock 파일, 이미지 의존성(네트워크 차단) | Python만 확정 |
| 최종 코드 인계 | 평가할 Snapshot 선택, 소스 전달 방법 | 미정(dev API는 코드 미제공) |

참고: 세화님 backend(`main.app`, `database.get_db`, `models.User`, `POST /api/auth/signup`, 201/409/422)는 플랫폼 웹사이트 코드이며, 생성물 규격으로 채택할지는 팀장님과 결정한다.
