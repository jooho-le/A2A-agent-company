# SCN-001 회원가입 QA 검사 내용(입력은 시험지 파일이 정한다). 순환용·시범 최종용·공식 최종용이 같은 검사를
# 서로 다른 입력·순서로 부른다. 잘못된 입력은 4xx로 거부해야 하고, 5xx는 처리하지 못한 서버 오류로 본다.
# 거부된 요청은 사용자 표를 바꾸지 않아야 한다(새 계정도, 기존 계정의 값 변경도 없음). 행 비교는 assertTrue로 해서
# 실패 메시지에 저장값(해시)이 실리지 않게 한다.
import threading

import signup_link as link


def read_user_rows(service):
    # 사용자 표의 모든 행·모든 칸 값(정렬). 거부된 요청이 기존 계정을 바꾸지 않았는지 비교하는 데 쓴다.
    from sqlalchemy import inspect as sa_inspect
    if not service._has_user_table():
        return []
    try:
        mapper = sa_inspect(service.User)
        with service.Session() as db:
            found = [tuple(repr(getattr(row, attribute.key)) for attribute in mapper.column_attrs)
                     for row in db.query(service.User).all()]
    except Exception:
        raise link.Unverified("TEST_DATABASE_READ_FAILED") from None
    return sorted(found)


def observe_status(test, case, measurement, *, required=True):
    def observed():
        code, origin = measurement() if callable(measurement) else measurement
        if 500 <= code <= 599:
            test.detail_recorder.index[case, 'http_status']['statusOrigin'] = origin
        return code
    return test.observe(case, 'http_status', observed, required=required)


def _signup_first(test, service):
    # 정상 가입이 먼저 되어야 거부 검사가 의미 있다(모든 요청을 거부하는 서비스가 거저 통과하지 않게).
    if not observe_status(test, 'precondition', lambda: service.measured_signup(link.new_email(), link.new_password()), required=False):
        raise link.Unverified("PRECONDITION_SIGNUP_FAILED")


def valid_signup(test, data):
    test.prepare("QA-SIGNUP-001", data)
    # QA-SIGNUP-001: 생성물의 실제 DB 연결로 유효한 가입마다 성공 응답, 사용자 1명 증가.
    # 성공 응답 뒤 행이 없을 때: 앱이 평가용 DB를 썼으면 저장하지 않은 것(커밋 누락 등)이라 FAIL이고,
    # 평가용 DB를 전혀 쓰지 않았으면(메모리·다른 저장소) 확인할 수 없어 미검증이다.
    def body(service):
        for index, template in enumerate(data["emails"]):
            case = f'valid_email_{index + 1}'
            with test.case(email_case=index):
                email, before = link.new_email(template), service.count()
                status = service.measured_signup(email, link.new_password(data["length"]))
                # 0.7.1: observe는 응답이 기대 범위(2xx)일 때 True다. 성공 응답일 때만 DB 사용 여부를 본다
                # (처리하지 못한 예외의 500은 send가 used_database를 남기지 않고, http_status가 이미 FAIL이다).
                if observe_status(test, case, status) and service.count_canonical(email) == 0 \
                        and not service.used_database:
                    raise link.Unverified("DATABASE_URL_NOT_USED")
                test.observe(case, 'user_delta', lambda: service.count() - before)
                test.observe(case, 'account_count', lambda: service.count_canonical(email))
    test.check(body)


def _check_rejection(test, service, case, email, password, *, check_count=True):
    rows = read_user_rows(service)
    before = service.count() if check_count else None
    observe_status(test, case, service.measured_signup(email, password))
    if check_count:
        test.observe(case, 'user_delta', lambda: service.count() - before)
    test.observe(case, 'rows_unchanged', lambda: read_user_rows(service) == rows)


def invalid_email(test, data):
    test.prepare("QA-SIGNUP-002", data)
    def body(service):
        _signup_first(test, service)
        for index, email in enumerate(data["invalid"]):
            with test.case(email_case=index):
                _check_rejection(test, service, f'invalid_email_{index + 1}', email, link.new_password())
    test.check(body)


def duplicate_email(test, data):
    test.prepare("QA-SIGNUP-003", data)
    def body(service):
        email = link.new_email(data["template"])
        if not observe_status(test, 'precondition', lambda: service.measured_signup(email, link.new_password()), required=False):
            raise link.Unverified("PRECONDITION_SIGNUP_FAILED")
        for form in data["forms"]:
            with test.case(variant=form):
                _check_rejection(test, service, 'variant_' + form, link.variant(email, form), link.new_password(), check_count=False)
        test.observe('final', 'account_count', lambda: service.count_canonical(email))
    test.check(body)


def password_length(test, data):
    test.prepare("QA-SIGNUP-004", data)
    def body(service):
        for length in data['reject']:
            with test.case(length=length):
                _check_rejection(test, service, f'reject_length_{length}', link.new_email(), link.new_password(length))
        for kind, length in [('accept', n) for n in data['accept']] + list(data['kinds']):
            case = f'{kind}_length_{length}'
            with test.case(length=length, kind=kind):
                before = service.count()
                observe_status(test, case, service.measured_signup(link.new_email(), link.new_password(length, 'mixed' if kind == 'accept' else kind)))
                test.observe(case, 'user_delta', lambda: service.count() - before)
    test.check(body)


def concurrent_signups(service, email, password, workers=5):
    # 같은 이메일 가입을 스레드 여러 개로 동시에 보낸다. 실행 자체가 안 되면 Unverified다.
    # 모든 요청을 같은 client로 보내 같은 lifespan 상태·같은 이벤트 루프에서 처리되게 한다(실제 서버 한 대와 같음).
    barrier = threading.Barrier(workers)
    statuses, unverified = [None] * workers, []

    # 스레드마다 barrier에서 함께 출발해 요청한다.
    def run(index):
        barrier.wait(timeout=30)
        try:
            statuses[index] = service.signup(email, password)
        except link.Unverified as error:
            unverified.append(error.code)

    threads = [threading.Thread(target=run, args=(index,), daemon=True) for index in range(workers)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=120)
    if unverified:
        raise link.Unverified(unverified[0])
    if any(thread.is_alive() for thread in threads) or None in statuses:
        raise link.Unverified("CONCURRENCY_HARNESS_ERROR")
    return statuses


def concurrent_signup(test, data):
    test.prepare("QA-SIGNUP-005", data)
    # QA-SIGNUP-005: 같은 이메일 동시 요청에서 중복 생성이 없는지 본다. 경쟁이 안 드러날 수도 있어 여러 번 반복한다.
    def body(service):
        for attempt in range(data["attempts"]):
            case = f"attempt_{attempt + 1}"
            with test.case(attempt=attempt):
                email = link.new_email(data["template"])
                statuses = concurrent_signups(service, email, link.new_password(), workers=data["workers"])
                created = service.count_canonical(email)
                successes = sum(1 for status in statuses if status in link.SUCCESS)
                # 아무도 생성되지 않았을 때: 5xx 응답은 관찰한 서버 오류라 FAIL(아래 검사)이다. 모두 4xx면
                # 단독 가입을 다시 해 보고, 단독 가입도 안 되면 요청 규격이 다른 것으로 보고 미검증이다.
                if (created == 0 and successes == 0 and all(status in link.REJECTED for status in statuses)
                        and service.signup(link.new_email(), link.new_password()) not in link.SUCCESS):
                    raise link.Unverified("CONCURRENT_SIGNUP_NOT_CREATED")
                test.observe(case, 'account_count', created)
                test.observe(case, 'success_count', successes)
                test.observe(case, 'response_statuses', statuses)
    test.check(body)


def response_matches_result(test, data):
    test.prepare("QA-SIGNUP-006", data)
    # QA-SIGNUP-006: 요청 순서(steps)대로 보내며 응답과 실제 DB 변화가 맞는지 본다. 거부된 요청이 상태를 남겨
    # 다음 정상 가입을 막는지도 함께 본다(예: 짧은 비밀번호로 거부된 이메일로 바로 정상 가입).
    #   ("new", 이름) 새 이메일 정상 가입 / ("short", 이름) 그 이메일로 7자 비밀번호
    #   ("invalid", 이메일) 형식 오류 / ("duplicate", 이름, 변형) 가입한 이메일의 변형으로 재가입
    def body(service):
        emails = {}
        for index, step in enumerate(data["steps"]):
            kind = step[0]
            case = f"step_{index + 1}_{kind}"
            if kind == "invalid":
                address, password = step[1], link.new_password()
            else:
                address = emails.setdefault(step[1], link.new_email(data["template"]))
                address = link.variant(address, step[2]) if kind == "duplicate" else address
                password = link.new_password(7 if kind == "short" else 12)
            with test.case(step=index, request=kind):
                before, rows = service.count(), read_user_rows(service)
                status = service.measured_signup(address, password)
                created = service.count() - before
                test.observe(case, 'response_matches_db', (status[0] in link.SUCCESS) == (created == 1))
                observe_status(test, case, status)
                if kind != "new":
                    test.observe(case, 'user_delta', created)
                    test.observe(case, 'rows_unchanged', lambda: read_user_rows(service) == rows)
    test.check(body)
