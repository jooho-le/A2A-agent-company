# SCN-001 회원가입 QA 검사 내용(입력은 시험지 파일이 정한다). 순환용·시범 최종용·공식 최종용이 같은 검사를
# 서로 다른 입력·순서로 부른다. 잘못된 입력은 4xx로 거부해야 하고, 5xx는 처리하지 못한 서버 오류로 본다.
# 거부된 요청은 사용자 표를 바꾸지 않아야 한다(새 계정도, 기존 계정의 값 변경도 없음). 행 비교는 assertTrue로 해서
# 실패 메시지에 저장값(해시)이 실리지 않게 한다.
import signup_link as link


def _signup_first(service):
    # 정상 가입이 먼저 되어야 거부 검사가 의미 있다(모든 요청을 거부하는 서비스가 거저 통과하지 않게).
    if service.signup(link.new_email(), link.new_password()) not in link.SUCCESS:
        raise link.Unverified("PRECONDITION_SIGNUP_FAILED")


def valid_signup(test, data):
    # QA-SIGNUP-001: 유효한 가입마다 성공 응답, 사용자 1명 증가
    def body(service):
        for index, template in enumerate(data["emails"]):
            with test.case(email_case=index):
                email, before = link.new_email(template), service.count()
                status = service.signup(email, link.new_password(data["length"]))
                test.assertIn(status, link.SUCCESS, "유효한 가입 요청이 성공 응답을 받지 못함")
                test.assertEqual(service.count(), before + 1, "사용자 수가 1 늘지 않음")
                test.assertEqual(service.count_canonical(email), 1, "가입한 이메일의 사용자가 1명이 아님")
    test.check(body)


def invalid_email(test, data):
    # QA-SIGNUP-002: 형식 오류 이메일 → 4xx 거부, 사용자 생성 없음
    def body(service):
        _signup_first(service)
        for index, email in enumerate(data["invalid"]):
            with test.case(email_case=index):
                before, rows = service.count(), service.rows()
                status = service.signup(email, link.new_password())
                test.assertIn(status, link.REJECTED, "형식 오류 이메일이 4xx로 거부되지 않음")
                test.assertEqual(service.count(), before, "형식 오류 이메일로 사용자가 생성됨")
                test.assertTrue(service.rows() == rows, "거부된 요청이 사용자 표를 바꿈")
    test.check(body)


def duplicate_email(test, data):
    # QA-SIGNUP-003: 대소문자·공백만 다른 이메일 재가입 → 4xx 거부, 사용자 1명 유지, 기존 계정(비밀번호 해시 등) 그대로
    def body(service):
        email = link.new_email(data["template"])
        if service.signup(email, link.new_password()) not in link.SUCCESS:
            raise link.Unverified("PRECONDITION_SIGNUP_FAILED")
        for form in data["forms"]:
            with test.case(variant=form):
                rows = service.rows()
                status = service.signup(link.variant(email, form), link.new_password())
                test.assertIn(status, link.REJECTED, "같은 canonical 이메일 재가입이 4xx로 거부되지 않음")
                test.assertTrue(service.rows() == rows, "거부된 재가입이 기존 계정을 바꾸거나 계정을 만듦")
        test.assertEqual(service.count_canonical(email), 1, "같은 canonical 이메일의 사용자가 1명이 아님")
    test.check(body)


def password_length(test, data):
    # QA-SIGNUP-004: 7자 이하는 거부, 8자 이상은 가입. 계약은 길이만 정하므로 문자 구성(소문자만·숫자만)이 달라도
    # 8자 이상이면 가입돼야 한다(kinds: (구성, 길이) 목록, 승인되지 않은 복잡성 조건을 찾음).
    def body(service):
        for length in data["reject"]:
            with test.case(length=length):
                before, rows = service.count(), service.rows()
                status = service.signup(link.new_email(), link.new_password(length))
                test.assertIn(status, link.REJECTED, "7자 이하 비밀번호가 4xx로 거부되지 않음")
                test.assertEqual(service.count(), before, "7자 이하 비밀번호로 사용자가 생성됨")
                test.assertTrue(service.rows() == rows, "거부된 요청이 사용자 표를 바꿈")
        for length in data["accept"]:
            with test.case(length=length):
                before = service.count()
                status = service.signup(link.new_email(), link.new_password(length))
                test.assertIn(status, link.SUCCESS, "8자 이상 비밀번호가 정상 처리되지 않음")
                test.assertEqual(service.count(), before + 1, "8자 이상 비밀번호로 사용자가 1명 생성되지 않음")
        for kind, length in data["kinds"]:
            with test.case(kind=kind, length=length):
                before = service.count()
                status = service.signup(link.new_email(), link.new_password(length, kind))
                test.assertIn(status, link.SUCCESS, "8자 이상 비밀번호를 문자 구성 때문에 거부함(계약에 없는 조건)")
                test.assertEqual(service.count(), before + 1, "8자 이상 비밀번호로 사용자가 1명 생성되지 않음")
    test.check(body)


def concurrent_signup(test, data):
    # QA-SIGNUP-005: 같은 이메일 동시 요청에서 중복 생성이 없는지 본다. 경쟁이 안 드러날 수도 있어 여러 번 반복한다.
    def body(service):
        for attempt in range(data["attempts"]):
            with test.case(attempt=attempt):
                email = link.new_email(data["template"])
                statuses = service.concurrent_signups(email, link.new_password(), workers=data["workers"])
                created = service.count_canonical(email)
                successes = sum(1 for status in statuses if status in link.SUCCESS)
                # 아무도 생성되지 않았을 때: 5xx 응답은 관찰한 서버 오류라 FAIL(아래 검사)이다. 모두 4xx면
                # 단독 가입을 다시 해 보고, 단독 가입도 안 되면 요청 규격이 다른 것으로 보고 미검증이다.
                if (created == 0 and successes == 0 and all(status in link.REJECTED for status in statuses)
                        and service.signup(link.new_email(), link.new_password()) not in link.SUCCESS):
                    raise link.Unverified("CONCURRENT_SIGNUP_NOT_CREATED")
                test.assertEqual(created, 1, "동시 요청으로 같은 이메일 사용자가 1명이 아님")
                test.assertEqual(successes, 1, "동시 요청 중 성공 응답이 정확히 1건이 아님")
                test.assertTrue(all(status in link.SUCCESS or status in link.REJECTED for status in statuses),
                                "동시 요청의 중복 실패가 4xx로 거부되지 않음")
    test.check(body)


def response_matches_result(test, data):
    # QA-SIGNUP-006: 요청 순서(steps)대로 보내며 응답과 실제 DB 변화가 맞는지 본다. 거부된 요청이 상태를 남겨
    # 다음 정상 가입을 막는지도 함께 본다(예: 짧은 비밀번호로 거부된 이메일로 바로 정상 가입).
    #   ("new", 이름) 새 이메일 정상 가입 / ("short", 이름) 그 이메일로 7자 비밀번호
    #   ("invalid", 이메일) 형식 오류 / ("duplicate", 이름, 변형) 가입한 이메일의 변형으로 재가입
    def body(service):
        emails = {}
        for index, step in enumerate(data["steps"]):
            kind = step[0]
            if kind == "invalid":
                address, password = step[1], link.new_password()
            else:
                address = emails.setdefault(step[1], link.new_email(data["template"]))
                address = link.variant(address, step[2]) if kind == "duplicate" else address
                password = link.new_password(7 if kind == "short" else 12)
            with test.case(step=index, request=kind):
                before, rows = service.count(), service.rows()
                status = service.signup(address, password)
                created = service.count() - before
                test.assertEqual(status in link.SUCCESS, created == 1, "응답과 실제 가입 결과가 다름")
                if kind == "new":
                    test.assertIn(status, link.SUCCESS, "정상 가입이 성공 응답을 받지 못함")
                else:
                    test.assertIn(status, link.REJECTED, "실패해야 할 요청이 4xx 실패 응답을 받지 못함")
                    test.assertEqual(created, 0, "실패해야 할 요청으로 사용자가 생성됨")
                    test.assertTrue(service.rows() == rows, "실패해야 할 요청이 기존 계정을 바꿈")
    test.check(body)
