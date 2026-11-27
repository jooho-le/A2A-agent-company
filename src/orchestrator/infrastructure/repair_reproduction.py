"""Read-only, bounded generated test inputs bound to measured failures.

Never disclose protected suites, mutable workspace tests, runner code, raw
exception locals or request/response bodies. Missing archived inputs stay absent.
"""
from hashlib import sha256

MAX_CONTEXT_BYTES = 48 * 1024


def generated_reproduction(repository, source, feedback):
    from mcp_tools.tools.generated_recheck import _files, _table
    from mcp_tools.tools.unit_inputs import UnitTestInputs
    from mcp_tools.tools.unit_store import UnitTestOutputStore
    from orchestrator.domain.validation_feedback import measured_test_version

    result, size, seen = [], 0, set()
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
            # Constructor checks paths, credential content, complete file hashes
            # and pinned runner identity. The runner is never returned to an agent.
            inputs = UnitTestInputs(files=archived, inputs_sha256=record.inputs['inputsSha256'],
                runner_sha256=record.inputs['runnerSha256'], test_files_sha256=record.inputs['testFilesSha256'])
            files = []
            total = sum(len(b) for p, b in inputs.files.items() if p != '_unit_runner.py')
            if size + total > MAX_CONTEXT_BYTES:
                continue  # Never silently truncate Python or substitute another test.
            for path, content in inputs.files.items():
                if path != '_unit_runner.py':
                    files.append({'path': path, 'sha256': sha256(content).hexdigest(), 'content': content.decode('utf-8')})
            result.append({'role': record.role.value, 'sourceArtifactId': str(source.artifact_id),
                'codeVersion': source.code_version, 'executionManifestId': str(record.execution_manifest_id),
                'testVersion': version, 'testIds': sorted({f['testId'] for f in matches}),
                'files': files, 'readOnly': True, 'purpose': 'REPRODUCE_RECORDED_FAILURE_NOT_NEW_REQUIREMENTS'})
            seen.add((record.role.value, version))
            size += total
    return result
