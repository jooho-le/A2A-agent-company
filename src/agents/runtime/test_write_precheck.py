"""Inspect a proposed Python test write without importing, executing or rewriting it."""
import ast

from agents.runtime.generated_test_inventory import MAX_FILE_BYTES
from mcp_tools.tools.unit_runner import app_fixture_source_issues


def inspect_test_write(arguments):
    path, content = arguments.get('path'), arguments.get('content')
    if not isinstance(path, str) or not path.endswith('.py') or not isinstance(content, str):
        return []  # browser JSON and other scratch formats keep their own contracts
    try:
        if len(content.encode('utf-8')) > MAX_FILE_BYTES:
            return []  # existing size/encoding guards remain authoritative
        ast.parse(content)
    except (SyntaxError, ValueError, UnicodeError, RecursionError) as error:
        line = getattr(error, 'lineno', None)
        column = getattr(error, 'offset', None)
        message = str(getattr(error, 'msg', '')).lower()
        kind = ('INDENTATION' if isinstance(error, IndentationError) else
                'UNTERMINATED_STRING' if 'unterminated string' in message else
                'MISSING_DELIMITER' if 'was never closed' in message or 'does not match' in message else
                'EXPECTED_COLON' if "expected ':'" in message else
                'INVALID_CHARACTER' if 'invalid character' in message else 'INVALID_SYNTAX')
        if '\x00' in content:
            line = content.count('\n', 0, content.index('\x00')) + 1
            column = len(content[:content.index('\x00')].rsplit('\n', 1)[-1]) + 1
            kind = 'NULL_BYTE'
        return [{'fieldPath': '/content', 'reasonCode': 'GENERATED_TEST_SYNTAX_INVALID',
                 'sourcePath': path, 'lineNumber': line if type(line) is int and line > 0 else 1,
                 'columnNumber': column if type(column) is int and 0 < column <= 1_000_000 else 1,
                 'syntaxKind': kind}]
    return [{'fieldPath': '/content', 'reasonCode': item['reasonCode'],
             'sourcePath': path, 'lineNumber': item['sourceLine']}
            for item in app_fixture_source_issues(content)][:16]
