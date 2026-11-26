"""Closed failure facts shared with agents; no exception text, SQL or inputs."""
import re

KINDS = frozenset({'ASSERTION', 'EXCEPTION'})
EXCEPTIONS = frozenset({'AssertionError', 'OperationalError', 'IntegrityError', 'ProgrammingError',
    'StatementError', 'DatabaseError', 'DataError', 'RuntimeError', 'ValueError', 'TypeError',
    'AttributeError', 'ImportError', 'ModuleNotFoundError', 'KeyError', 'OTHER'})


def validate_failure_diagnostic(value):
    if (type(value) is not dict or not {'kind', 'exceptionType', 'origin'} <= value.keys()
            or value.keys() - {'kind', 'exceptionType', 'origin', 'cause', 'comparison', 'location'}
            or value['kind'] not in KINDS or value['exceptionType'] not in EXCEPTIONS
            or value['origin'] not in {'TEST', 'PRODUCT', 'LIBRARY'}):
        raise ValueError('UNIT_FAILURE_DIAGNOSTIC_INVALID')
    if 'cause' in value and value['cause'] != 'DATABASE_TABLE_MISSING':
        raise ValueError('UNIT_FAILURE_DIAGNOSTIC_INVALID')
    if 'comparison' in value:
        row = value['comparison']
        if (type(row) is not dict or set(row) != {'actualStatus', 'expectedStatus'}
                or any(type(v) is not int or not 100 <= v <= 599 for v in row.values())):
            raise ValueError('UNIT_FAILURE_DIAGNOSTIC_INVALID')
    if 'location' in value:
        row = value['location']
        if (type(row) is not dict or set(row) != {'sourceSha256', 'line'}
                or type(row['sourceSha256']) is not str or not re.fullmatch('[0-9a-f]{64}', row['sourceSha256'])
                or type(row['line']) is not int or not 1 <= row['line'] <= 1_000_000):
            raise ValueError('UNIT_FAILURE_DIAGNOSTIC_INVALID')
    return value
