# Safe scalar observations only; presentation labels belong to the evaluator.
import json
COLLECTED = {}
PRE = 'precondition'
WAIT = 'NOT_REACHED'

def plan(key, data):
    rows = []

    def add(case, item, value=True, op='eq', kind='assertion'):
        rows.append({'caseId': case, 'itemId': item, 'expected': {'operator': op, 'value': value}, 'kind': kind})

    def status(case, accepted=True, kind='assertion'):
        add(case, 'http_status', [200, 299] if accepted else [400, 499], 'range', kind)

    def delta(case, value):
        add(case, 'user_delta', value)
    add('setup', 'ready', kind=PRE)
    if key == 'QA-SIGNUP-001':
        for i in range(len(data['emails'])):
            case = f'valid_email_{i + 1}'
            status(case)
            delta(case, 1)
            add(case, 'account_count', 1)
    elif key == 'QA-SIGNUP-002':
        status(PRE, kind=PRE)
        for i in range(len(data['invalid'])):
            case = f'invalid_email_{i + 1}'
            status(case, False)
            delta(case, 0)
            add(case, 'rows_unchanged')
    elif key == 'QA-SIGNUP-003':
        status(PRE, kind=PRE)
        for form in data['forms']:
            case = 'variant_' + form
            status(case, False)
            add(case, 'rows_unchanged')
        add('final', 'account_count', 1)
    elif key == 'QA-SIGNUP-004':
        for (mode, values) in (('reject', data['reject']), ('accept', data['accept'])):
            for length in values:
                case = f'{mode}_length_{length}'
                status(case, mode == 'accept')
                delta(case, int(mode == 'accept'))
                if mode == 'reject':
                    add(case, 'rows_unchanged')
        for (kind, length) in data['kinds']:
            case = f'{kind}_length_{length}'
            status(case)
            delta(case, 1)
    elif key == 'QA-SIGNUP-005':
        for i in range(data['attempts']):
            case = f'attempt_{i + 1}'
            add(case, 'account_count', 1)
            add(case, 'success_count', 1)
            add(case, 'response_statuses', data['workers'], 'http_statuses')
    elif key == 'QA-SIGNUP-006':
        for (i, step) in enumerate(data['steps']):
            case = f'step_{i + 1}_{step[0]}'
            add(case, 'response_matches_db')
            status(case, step[0] == 'new')
            if step[0] != 'new':
                delta(case, 0)
                add(case, 'rows_unchanged')
    elif key == 'SEC-SIGNUP-002':
        for i in range(data['accounts']):
            status(f'account_{i + 1}_setup', kind=PRE)
        add('storage', 'no_plaintext')
        add('storage', 'argon2id')
        for i in range(data['accounts']):
            for (name, minimum) in (('m', 19456), ('t', 2), ('p', 1)):
                add(f'account_{i + 1}', name, minimum, 'gte')
        add('storage', 'distinct_salts', data['accounts'])
        for i in range(data['accounts']):
            add(f'account_{i + 1}', 'correct_password')
            add(f'account_{i + 1}', 'wrong_password', False)
    elif key == 'SEC-SIGNUP-003':
        status(PRE, kind=PRE)
        for form in data['forms']:
            case = 'variant_' + form
            status(case)
            add(case, 'canonical')
            add(case, 'unique')
        for (i, pair) in enumerate(data['aliases']):
            for j in range(len(pair)):
                case = f'alias_{i + 1}_address_{j + 1}'
                status(case)
                add(case, 'account_count', 1)
    elif key == 'SEC-SIGNUP-005':
        status(PRE, kind=PRE)
        add('db_error', 'injected', kind=PRE)
        cases = ['success', 'invalid_email', 'short_password', 'duplicate']
        cases += ['malformed_' + kind for kind in data['malformed']] + ['db_error']
        for case in cases:
            # 0.7.3: 받은 응답의 상태 코드(노출 경로 안내용 준비 조건, 합격 기준 아님)
            add(case, 'response_status', [100, 599], 'range', PRE)
        for case in cases + ['application_logs']:
            add(case, 'password_exposed', False)
            add(case, 'hash_exposed', False)
        # 0.7.3: 위 로그 노출 중 처리하지 못한 예외의 traceback에도 있는 것(로그 노출의 일부라 FAIL 조건이 늘지 않음)
        add('application_logs', 'password_in_crash', False)
        add('application_logs', 'hash_in_crash', False)
    else:
        raise ValueError('DETAIL_CHECK_UNKNOWN')
    return rows

def safe_actual(value):
    return type(value) is bool or (type(value) is int and abs(value) <= 10 ** 12) or (type(value) is list and len(value) <= 100 and all((type(item) is int and 0 <= item <= 999 for item in value)))

def matches(expected, actual):
    if not safe_actual(actual):
        raise ValueError('DETAIL_ACTUAL_INVALID')
    (op, value) = (expected['operator'], expected['value'])
    if op == 'eq':
        return type(value) is type(actual) and actual == value
    if op == 'gte':
        return type(actual) is int and actual >= value
    if op == 'range':
        return type(actual) is int and value[0] <= actual <= value[1]
    if op == 'http_statuses':
        return type(actual) is list and len(actual) == value and all((200 <= x < 300 or 400 <= x < 500 for x in actual))
    raise ValueError('DETAIL_OPERATOR_INVALID')

class Recorder:

    def __init__(self, key, data):
        self.rows = [{**row, 'actual': None, 'status': 'NOT_RUN', 'reason': WAIT} for row in plan(key, data)]
        self.index = {(row['caseId'], row['itemId']): row for row in self.rows}
        COLLECTED[key] = self.rows

    def pending(self, case, item):
        row = self.index[case, item]
        if row['reason'] != WAIT:
            raise ValueError('DETAIL_OBSERVATION_DUPLICATE')
        return row

    def observe(self, case, item, actual):
        row = self.pending(case, item)
        row.update(status='UNVERIFIED', reason='OBSERVATION_INTERRUPTED')
        try:
            value = actual() if callable(actual) else actual
            passed = matches(row['expected'], value)
        except BaseException:
            row['reason'] = 'OBSERVATION_UNAVAILABLE'
            raise
        row.update(actual=value, status='PASS' if passed else 'FAIL', reason=None if passed else 'EXPECTATION_MISMATCH')
        return passed

    def finish(self, reason=WAIT):
        for row in self.rows:
            if row['status'] == 'NOT_RUN' and row['reason'] == WAIT:
                row['reason'] = reason

    def block(self, case, items, reason):
        for item in items:
            self.pending(case, item)['reason'] = reason

def payload():
    return json.loads(json.dumps({'schemaVersion': 1, 'checks': COLLECTED}, allow_nan=False))
