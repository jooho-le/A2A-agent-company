"""Read-only, bounded generated test references bound to measured failures.

Never disclose protected suites, mutable workspace tests, runner code, raw
exception locals or request/response bodies. Missing archived inputs stay absent.
"""
import ast
from hashlib import sha256
import json
import re

MAX_CONTEXT_BYTES = 48 * 1024


def _value_shape(node):
    if isinstance(node, ast.Constant):
        if node.value is None:
            return 'NULL'
        if type(node.value) is str:
            return 'EMPTY_STRING' if not node.value else 'NONEMPTY_STRING'
        if type(node.value) is bool:
            return 'BOOLEAN'
        if type(node.value) in (int, float):
            return 'NUMBER'
    return 'DYNAMIC'


def _request_hint(node):
    """Static hints only: never assert that an AST call actually executed."""
    if not isinstance(node, ast.Call):
        return None
    name = node.func.attr if isinstance(node.func, ast.Attribute) else (
        node.func.id if isinstance(node.func, ast.Name) else '')
    method = name.upper()
    args = list(node.args)
    keywords = {k.arg: k.value for k in node.keywords if k.arg is not None}
    if method in ('REQUEST', 'CHECK_HTTP_OUTPUT'):
        if isinstance(node.func, ast.Name) and args:  # module helper's client argument
            args.pop(0)
        value = args.pop(0) if args else keywords.get('method')
        method = value.value.upper() if isinstance(value, ast.Constant) and type(value.value) is str else ''
    if method not in ('GET', 'POST', 'PUT', 'PATCH', 'DELETE'):
        return None
    path = args[0] if args else keywords.get('path')
    if (not isinstance(path, ast.Constant) or type(path.value) is not str
            or not re.fullmatch(r'/[A-Za-z0-9_/{.}-]{0,199}', path.value)):
        return None
    result = {'line': node.lineno, 'method': method, 'path': path.value}
    body = next((k for k in node.keywords if k.arg in ('json', 'data')), None)
    if body is not None:
        result['encoding'] = 'JSON' if body.arg == 'json' else 'FORM'
        result['bodyShape'] = 'OBJECT' if isinstance(body.value, ast.Dict) else 'DYNAMIC'
        if isinstance(body.value, ast.Dict):
            result['fieldShapes'] = [{'fieldName': key.value, 'shape': _value_shape(value)}
                for key, value in zip(body.value.keys, body.value.values)
                if isinstance(key, ast.Constant) and type(key.value) is str
                and re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]{0,63}', key.value)][:32]
    return result


def _file_reference(path, content, failed_ids):
    result = {'path': path, 'sha256': sha256(content).hexdigest()}
    if not path.endswith('.py') or len(content) > MAX_CONTEXT_BYTES:
        return result
    try:
        tree = ast.parse(content.decode('utf-8'))
        nodes = []
        for node in ast.walk(tree):
            nodes.append(node)
            if len(nodes) > 8192:
                return result
        names = {name.rsplit('.', 1)[-1] for name in failed_ids}
        cases = []
        for node in nodes:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in names:
                calls = []
                for child in ast.walk(node):
                    hint = _request_hint(child)
                    if hint is not None:
                        calls.append(hint)
                    if len(calls) == 8:
                        break
                cases.append({'methodName': node.name, 'line': node.lineno, 'endLine': node.end_lineno,
                              'possibleHttpCalls': calls})
                if len(cases) == 16:
                    break
        if cases:
            result['staticCaseHints'] = cases
            result['staticHintsAreExecutionEvidence'] = False
    except (SyntaxError, UnicodeError, RecursionError, ValueError):
        pass  # Invalid or unfamiliar files remain references, never guessed code.
    return result


def generated_reproduction(repository, source, feedback):
    from mcp_tools.tools.generated_recheck import _files, _table
    from mcp_tools.tools.unit_inputs import UnitTestInputs
    from mcp_tools.tools.unit_store import UnitTestOutputStore
    from orchestrator.domain.validation_feedback import measured_test_version

    result, size, context_size, seen = [], 0, 0, set()
    with repository._connection() as db:
        db.execute('BEGIN')
        if not _table(db, 'unit_test_execution_records'):
            return result
        rows = db.execute('''SELECT execution_manifest_id FROM unit_test_execution_records
            WHERE run_id=? AND source_artifact_id=? ORDER BY execution_manifest_id LIMIT 128''',
            (str(source.run_id), str(source.artifact_id))).fetchall()
        for (manifest_id,) in rows:
            row = db.execute('SELECT * FROM unit_test_execution_records WHERE execution_manifest_id=?',
                             (manifest_id,)).fetchone()
            record = UnitTestOutputStore._decode(db, row)
            if (record.scope.kind != 'QA_TESTS' or record.role.value not in ('QA', 'SECURITY')
                    or record.execution_manifest != source.execution_manifest()):
                continue
            version = measured_test_version(record)['testVersion']
            matches = [f for f in feedback if f.get('outcome') == 'FAIL'
                and f.get('role') == record.role.value and f.get('runId') == str(source.run_id)
                and f.get('sourceArtifactId') == str(source.artifact_id)
                and f.get('testVersion') == version
                and f.get('testId', '').startswith('run_unit_tests:' + record.scope.name + ':')]
            cases = {c.test_id for c in record.report.tests if c.outcome == 'FAIL'}
            matches = [f for f in matches if f['testId'].split(':', 2)[2] in cases]
            if not matches or (record.role.value, version) in seen:
                continue
            archived = _files(db, record)
            if not archived:
                continue
            # Constructor authenticates the complete original bytes. Source stays
            # in the private archive for Host replay; do not send Python through
            # the ordinary prose/secret redactor (password=variable is valid code).
            inputs = UnitTestInputs(files=archived, inputs_sha256=record.inputs['inputsSha256'],
                runner_sha256=record.inputs['runnerSha256'], test_files_sha256=record.inputs['testFilesSha256'])
            files = []
            total = sum(len(b) for p, b in inputs.files.items() if p != '_unit_runner.py')
            if size + total > MAX_CONTEXT_BYTES:
                continue  # Never silently truncate Python or substitute another test.
            for path, content in inputs.files.items():
                if path != '_unit_runner.py':
                    files.append(_file_reference(path, content, cases))
            entry = {'role': record.role.value, 'sourceArtifactId': str(source.artifact_id),
                'codeVersion': source.code_version, 'executionManifestId': str(record.execution_manifest_id),
                'testVersion': version, 'testIds': sorted({f['testId'] for f in matches}),
                'files': files, 'readOnly': True, 'sourceDelivery': 'HOST_PRIVATE_ARCHIVE',
                'purpose': 'REPRODUCE_RECORDED_FAILURE_NOT_NEW_REQUIREMENTS'}
            encoded_size = len(json.dumps(entry, ensure_ascii=False, separators=(',', ':')).encode('utf-8'))
            if context_size + encoded_size > MAX_CONTEXT_BYTES:
                continue
            result.append(entry)
            seen.add((record.role.value, version))
            size += total
            context_size += encoded_size
    return result
