"""Attach already completed Host package records. Never create a completion event during export."""
from hashlib import sha256
import json
import os
from pathlib import Path
import stat


def collect_package_records(root, files, bundle):
    """Read two fixed filenames from an explicitly supplied trusted Host directory."""
    root = Path(root)
    if root.is_symlink():
        raise ValueError('PACKAGE_RECORDS_INVALID')
    root = root.resolve(strict=True)
    result = {}
    for name, limit in (('package-completion.json', 1024 * 1024), ('final-report.json', 16 * 1024 * 1024)):
        if name in files:
            raise ValueError('PACKAGE_RECORDS_INVALID')
        fd = os.open(root / name, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
        with os.fdopen(fd, 'rb') as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size > limit:
                raise ValueError('PACKAGE_RECORDS_INVALID')
            data = stream.read(limit + 1)
        if len(data) > limit:
            raise ValueError('PACKAGE_RECORDS_INVALID')
        result[name] = data
    return _verify_records(result, files, bundle)


def collect_stored_package_records(repository, files, bundle):
    """Read the actual Host producer's immutable records in the caller's transaction."""
    with repository._connection() as connection:
        exists = connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='host_result_packages'").fetchone()
        if not exists:
            return {}
        row = connection.execute('''SELECT r.report,p.completion FROM host_result_packages p
            LEFT JOIN host_final_reports r ON r.run_id=p.run_id WHERE p.run_id=?''',
                                 (bundle['run']['runId'],)).fetchone()
    if row is None:
        return {}
    return _verify_records({'final-report.json': bytes(row[0]), 'package-completion.json': bytes(row[1])}, files, bundle)


def package_production_status(repository, run_id):
    """Read producer progress only. This never creates reports, events or tables."""
    result = {'schemaVersion': 1, 'runId': str(run_id), 'status': 'NOT_CONFIGURED',
              'reason': None, 'eventId': None, 'completedAt': None}
    with repository._connection() as connection:
        if not connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='host_result_packages'").fetchone():
            return result
        row = connection.execute('SELECT completion FROM host_result_packages WHERE run_id=?', (str(run_id),)).fetchone()
        if row is not None:
            evidence = json.loads(row[0])
            return {**result, 'status': 'COMPLETED', 'eventId': evidence['eventId'], 'completedAt': evidence['completedAt']}
        if (not connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='host_package_enrollments'").fetchone()
                or not connection.execute('SELECT 1 FROM host_package_enrollments WHERE run_id=?', (str(run_id),)).fetchone()):
            return {**result, 'reason': 'PACKAGE_PRODUCER_NOT_ENROLLED'}
        row = connection.execute('SELECT status,reason FROM host_package_attempts WHERE run_id=?', (str(run_id),)).fetchone()
    if row is None:
        return {**result, 'status': 'PENDING'}
    return {**result, 'status': 'ERROR' if row[1] == 'PACKAGE_WRITE_FAILED' else row[0], 'reason': row[1]}


def _verify_records(result, files, bundle):
    if set(result) & set(files):
        raise ValueError('PACKAGE_RECORDS_INVALID')
    evidence = json.loads(result['package-completion.json'])
    run = bundle['run']
    if (type(evidence.get('schemaVersion')) is not int or evidence['schemaVersion'] != 1
            or evidence.get('runId') != run['runId'] or evidence.get('codeVersion') != run['codeVersion']
            or evidence.get('producer') != 'HOST_AUTOMATIC' or evidence.get('finalReportPath') != 'final-report.json'):
        raise ValueError('PACKAGE_RECORDS_INVALID')
    expected = {'final-report.json', f"v{run['codeVersion']}/source.tar", f"v{run['codeVersion']}/manifest.json"}
    if set(evidence.get('files', {})) != expected:
        raise ValueError('PACKAGE_RECORDS_INVALID')
    available = {**files, **result}
    for name in expected:
        data = available[name]
        if evidence['files'][name] != {'sha256': sha256(data).hexdigest(), 'sizeBytes': len(data)}:
            raise ValueError('PACKAGE_RECORDS_CHANGED')
    event = next((row for row in bundle['events'] if row['eventId'] == evidence.get('eventId')), None)
    if (event is None or event['eventType'] != 'RESULT_PACKAGE_COMPLETED' or event['actor'] != 'Orchestrator'
            or event.get('snapshotSha256') != evidence.get('snapshotSha256')
            or event.get('codeVersion') != run['codeVersion'] or event['occurredAt'] != evidence.get('completedAt')):
        raise ValueError('PACKAGE_COMPLETION_NOT_RECORDED')
    return result
