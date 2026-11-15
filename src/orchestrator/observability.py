"""Private Host observations: retain identities/counts/findings, never raw output.

This is an operator diagnostic ledger, not model input or a product verdict.
Only bounded, Run-attributed streams are observed; unrelated OS output is not.
"""
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
from functools import wraps
from hashlib import sha256
import json
import re
import sys
from uuid import UUID, uuid4

from orchestrator.core.security import contains_registered_secret, redact_text

MAX_OUTPUT_BYTES = 8 * 1024 * 1024
KINDS = {'A2A_OUTPUT', 'LLM_OUTPUT', 'TOOL_STDOUT', 'TOOL_STDERR', 'HOST_STDOUT', 'HOST_STDERR',
         'HOST_LOG', 'ARTIFACT_FILE_CONTENT'}
_CURRENT = ContextVar('owned_run_output_observer', default=None)
_HASH = re.compile(r'\$argon2(?:id|i|d)\$(?:v=\d+\$)?m=\d+,t=\d+,p=\d+\$[A-Za-z0-9+/.=-]{8,}\$[A-Za-z0-9+/.=-]{16,}'
                   r'|\$2[abxy]\$\d{2}\$[./A-Za-z0-9]{53}'
                   r'|\$pbkdf2(?:-[a-z0-9]+)?\$\d+\$[A-Za-z0-9+/.=-]{4,}\$[A-Za-z0-9+/.=-]{16,}'
                   r'|\$scrypt\$ln=\d+,r=\d+,p=\d+\$[A-Za-z0-9+/.=-]{4,}\$[A-Za-z0-9+/.=-]{16,}')
_MASKED = re.compile(r'\s*(?:\[REDACTED\]|\[MASKED\]|\*+)\s*', re.I)
_PASSWORD_KEY = re.compile(r'^(?:password|passwd|pwd|password_hash|hashed_password|passwordHash)$', re.I)


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def _id(value):
    parsed = UUID(str(value))
    if parsed.version != 4:
        raise ValueError('OBSERVATION_RUN_INVALID')
    return str(parsed)


def ensure_schema(connection):
    # Caller owns the transaction; executescript would implicitly commit it.
    for name in ('run_observation_policies', 'run_output_observations', 'run_output_scopes'):
        connection.execute(f'''CREATE TABLE IF NOT EXISTS {name} (
            identity TEXT PRIMARY KEY, run_id TEXT NOT NULL, payload_json TEXT NOT NULL CHECK(json_valid(payload_json)))''')
        for operation in ('UPDATE', 'DELETE'):
            connection.execute(f'''CREATE TRIGGER IF NOT EXISTS {name}_no_{operation.lower()}
                BEFORE {operation} ON {name} BEGIN SELECT RAISE(ABORT,'immutable observation'); END''')
        connection.execute(f'''CREATE TRIGGER IF NOT EXISTS {name}_no_replace BEFORE INSERT ON {name}
            WHEN EXISTS(SELECT 1 FROM {name} WHERE identity=NEW.identity)
            BEGIN SELECT RAISE(ABORT,'immutable observation'); END''')


def output_receipt(run_id, source_id, kind, value, *, boundary='BEFORE_REDACTION'):
    """Inspect in memory; findings contain stable categories, no matched text."""
    run_id = _id(run_id)
    if kind not in KINDS or not isinstance(source_id, str) or not source_id:
        raise ValueError('OBSERVATION_IDENTITY_INVALID')
    if isinstance(value, bytes):
        raw = value
    elif isinstance(value, str):
        raw = value.encode('utf-8')
    else:
        raw = _json(value).encode('utf-8')
    findings = set()
    status = 'OBSERVED'
    if kind == 'HOST_LOG' and isinstance(value, dict) and value.get('unrenderableFields'):
        status = 'PARTIAL'
    text = raw[:MAX_OUTPUT_BYTES].decode('utf-8', errors='replace')
    if len(raw) > MAX_OUTPUT_BYTES:
        status = 'PARTIAL'
    if _HASH.search(text):
        findings.add('PASSWORD_HASH_FORMAT')
    if redact_text(text) != text:
        findings.add('SECRET_PATTERN')
    if contains_registered_secret(raw):
        findings.add('REGISTERED_SECRET_VALUE')
    try:
        document = json.loads(text)
    except (ValueError, TypeError):
        document = None

    def walk(item, depth=0):
        nonlocal status
        if depth > 64:
            status = 'PARTIAL'
            return
        if isinstance(item, dict):
            for key, child in item.items():
                if (_PASSWORD_KEY.fullmatch(key) and isinstance(child, str)
                        and child.strip() and not _MASKED.fullmatch(child)):
                    findings.add('PASSWORD_FIELD_VALUE')
                walk(child, depth + 1)
                # Responses returns structured JSON as outputText and Tool
                # arguments JSON strings. Decode those explicit wire fields,
                # not arbitrary source/prose strings, before field inspection.
                if key in ('outputText', 'arguments') and isinstance(child, str):
                    try:
                        nested = json.loads(child)
                    except ValueError:
                        continue
                    walk(nested, depth + 1)
        elif isinstance(item, list):
            for child in item:
                walk(child, depth + 1)
    walk(document)
    return {'schemaVersion': 1, 'runId': run_id, 'kind': kind,
            'sourceId': sha256(source_id.encode()).hexdigest(), 'sha256': sha256(raw).hexdigest(),
            'bytes': len(raw), 'status': status, 'boundary': boundary,
            'findings': sorted(findings), 'knownValuesChecked': False}


def append_output(connection, run_id, source_id, kind, value, *, boundary='BEFORE_REDACTION', total_bytes=None):
    receipt = output_receipt(run_id, source_id, kind, value, boundary=boundary)
    if total_bytes is not None and total_bytes > receipt['bytes']:
        receipt.update(status='PARTIAL', observedBytes=receipt['bytes'], bytes=total_bytes,
                       hashScope='OBSERVED_PREFIX')
    identity = sha256((str(run_id) + ':' + kind + ':' + source_id).encode()).hexdigest()
    ensure_schema(connection)
    encoded = _json(receipt)
    previous = connection.execute('SELECT payload_json FROM run_output_observations WHERE identity=?', (identity,)).fetchone()
    if previous:
        if previous[0] != encoded:
            raise ValueError('OBSERVATION_IDENTITY_CHANGED')
    else:
        connection.execute('INSERT INTO run_output_observations VALUES (?,?,?)', (identity, _id(run_id), encoded))
    return receipt


class ObservationStore:
    def __init__(self, repository):
        self.repository = repository

    def pin_policy(self, run_id):
        run = self.repository.get_run(run_id)
        configuration = self.repository.get_run_configuration(run_id)
        if run is None or configuration is None:
            raise ValueError('OBSERVATION_RUN_INVALID')
        policy = {'schemaVersion': 1, 'runId': str(run.run_id),
                  'architecture': configuration.configuration.architecture,
                  'producer': 'OWNED_HOST_OBSERVATION_V1',
                  'automaticRetries': {'A2A': 'DISABLED', 'LLM': 'DISABLED'},
                  'repairRoutes': {'REPLAN': 'UNSUPPORTED', 'TEST_CHANGE': 'SNAPSHOT_AND_QA_INPUTS'},
                  'scope': 'OWNED_AUTOMATIC_SEGMENT',
                  'limitations': ['No automatic A2A/LLM resubmission; polling is not a retry.',
                                  'Output receipts cannot be matched against new secret values after collection.',
                                  'Only Run-attributed Host/Agent Python streams, Tool streams, and registered files are observed.']}
        with self.repository._transaction() as connection:
            ensure_schema(connection)
            old = connection.execute('SELECT payload_json FROM run_observation_policies WHERE identity=?', (str(run_id),)).fetchone()
            if old:
                previous = json.loads(old[0])
                if {k: v for k, v in previous.items() if k != 'pinnedAt'} != policy:
                    raise ValueError('OBSERVATION_POLICY_CHANGED')
                return previous
            # A policy cannot retroactively certify an already executed Run.
            started = connection.execute("SELECT 1 FROM trace_events WHERE run_id=? AND event_type IN ('A2A_MESSAGE_SENT','LLM_MODEL_CALLED')", (str(run_id),)).fetchone()
            if started:
                raise ValueError('OBSERVATION_POLICY_TOO_LATE')
            policy['pinnedAt'] = datetime.now(timezone.utc).isoformat()
            connection.execute('INSERT INTO run_observation_policies VALUES (?,?,?)', (str(run_id), str(run_id), _json(policy)))
        return policy

    def observe(self, run_id, source_id, kind, value, *, boundary='BEFORE_REDACTION', total_bytes=None):
        with self.repository._transaction() as connection:
            return append_output(connection, run_id, source_id, kind, value, boundary=boundary, total_bytes=total_bytes)


class _Tee:
    def __init__(self, stream, kind):
        self._stream, self._kind = stream, kind

    def write(self, value):
        state = _CURRENT.get()
        if state is not None and isinstance(value, str):
            data = value.encode('utf-8')
            previous = state['bytes'][self._kind]
            state['bytes'][self._kind] += len(data)
            if previous < MAX_OUTPUT_BYTES:
                state['streams'][self._kind].append(data[:MAX_OUTPUT_BYTES - previous])
        return self._stream.write(value)

    def flush(self):
        return self._stream.flush()

    def __getattr__(self, name):
        return getattr(self._stream, name)


@contextmanager
def observe_run(repository, run_id, *, scope='HOST_DISPATCH'):
    """Task-local attribution; tee preserves normal stream delivery.

    Observation is evaluation evidence, never part of the observed product
    execution. A receipt that cannot be stored must not fail or change the
    Run: the scope then has no ENDED marker (INCOMPLETE, or STARTED only),
    which scopes_complete() and the evaluator report as unverified coverage.
    """
    for name, kind in (('stdout', 'HOST_STDOUT'), ('stderr', 'HOST_STDERR')):
        if not isinstance(getattr(sys, name), _Tee):
            setattr(sys, name, _Tee(getattr(sys, name), kind))
    state = {'streams': {'HOST_STDOUT': [], 'HOST_STDERR': []},
             'bytes': {'HOST_STDOUT': 0, 'HOST_STDERR': 0}, 'failures': 0,
             'store': ObservationStore(repository), 'runId': str(run_id), 'scope': scope, 'id': uuid4().hex}
    try:
        with repository._transaction() as connection:
            ensure_schema(connection)
            connection.execute('INSERT INTO run_output_scopes VALUES (?,?,?)',
                (state['id'] + ':STARTED', str(run_id), _json({'scopeId': state['id'], 'scope': scope, 'state': 'STARTED'})))
        started = True
    except Exception:
        started = False
    if not started:
        # No STARTED marker: this scope is absent, so the Run's output
        # coverage stays incomplete. The product path runs unobserved.
        yield
        return
    token = _CURRENT.set(state)
    try:
        yield
    finally:
        _CURRENT.reset(token)
        marker = 'ENDED' if not state['failures'] else 'INCOMPLETE'
        try:
            with repository._transaction() as connection:
                for kind, chunks in state['streams'].items():
                    append_output(connection, run_id, scope + ':' + state['id'] + ':' + kind, kind, b''.join(chunks),
                                  total_bytes=state['bytes'][kind])
                connection.execute('INSERT INTO run_output_scopes VALUES (?,?,?)',
                    (state['id'] + ':' + marker, str(run_id),
                     _json({'scopeId': state['id'], 'scope': scope, 'state': marker})))
        except Exception:
            pass  # STARTED without ENDED is incomplete evidence; keep the Run's own outcome.


def observe_log(value):
    state = _CURRENT.get()
    if state is not None:
        try:
            state['store'].observe(state['runId'], 'log:' + uuid4().hex, 'HOST_LOG', value)
        except Exception:
            # A logging call must never raise into product code. The scope is
            # closed as INCOMPLETE so the missing log receipt is not hidden.
            state['failures'] += 1


def output_observation_active():
    return _CURRENT.get() is not None


def scopes_complete(scopes):
    if not isinstance(scopes, list) or not scopes:
        return False
    if any(not isinstance(row, dict) or set(row) != {'scopeId', 'scope', 'state'}
           or not isinstance(row['scopeId'], str) or not re.fullmatch(r'[0-9a-f]{32}', row['scopeId'])
           or row['scope'] not in ('HOST_DISPATCH', 'AGENT_LLM_EXECUTION') or row['state'] not in ('STARTED', 'ENDED')
           for row in scopes):
        return False
    started = {(row['scopeId'], row['scope']) for row in scopes if row['state'] == 'STARTED'}
    ended = {(row['scopeId'], row['scope']) for row in scopes if row['state'] == 'ENDED'}
    return bool(started) and started == ended and len(scopes) == 2 * len(started)


def capture_engine_output(method):
    @wraps(method)
    async def wrapped(self, *args, **kwargs):
        callback = kwargs.get('output_observed_sink')
        bound = getattr(callback, '__self__', None)
        if bound is None or not hasattr(bound, 'repository') or not hasattr(bound, 'binding'):
            return await method(self, *args, **kwargs)
        with observe_run(bound.repository, bound.binding['runId'], scope='AGENT_LLM_EXECUTION'):
            return await method(self, *args, **kwargs)
    return wrapped


def collect_observations(connection, run_id):
    """Read only, usable within the Host/Agent export's pinned transaction."""
    tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    policy = None
    if 'run_observation_policies' in tables:
        row = connection.execute('SELECT payload_json FROM run_observation_policies WHERE run_id=?', (str(run_id),)).fetchone()
        policy = json.loads(row[0]) if row else None
    records = []
    if 'run_output_observations' in tables:
        records = [json.loads(row[0]) for row in connection.execute('SELECT payload_json FROM run_output_observations WHERE run_id=? ORDER BY identity', (str(run_id),))]
    test_inputs = []
    if 'qa_test_input_captures' in tables:
        for row in connection.execute('SELECT capture_id,workflow_step_id,source_artifact_id,tool_name,selector,metadata_json,metadata_sha256 FROM qa_test_input_captures WHERE run_id=? ORDER BY rowid', (str(run_id),)):
            if sha256(row[5].encode()).hexdigest() != row[6]:
                raise ValueError('TEST_INPUT_EVIDENCE_CHANGED')
            files = []
            for name, content, digest in connection.execute('SELECT path,content,content_sha256 FROM qa_test_input_files WHERE capture_id=? ORDER BY path', (row[0],)):
                if sha256(content).hexdigest() != digest:
                    raise ValueError('TEST_INPUT_EVIDENCE_CHANGED')
                if not name.startswith('_'):
                    files.append((sha256(name.encode()).hexdigest(), digest))
            receipt = connection.execute('SELECT execution_manifest_id FROM qa_test_input_receipts WHERE capture_id=?', (row[0],)).fetchone()
            test_inputs.append({'captureId': row[0], 'workflowStepId': row[1], 'sourceArtifactId': row[2],
                                'toolName': row[3], 'selector': row[4],
                                'testFilesSha256': sha256(_json(files).encode()).hexdigest(),
                                'executionManifestId': receipt[0] if receipt else None})
    scopes = []
    if 'run_output_scopes' in tables:
        scopes = [json.loads(row[0]) for row in connection.execute('SELECT payload_json FROM run_output_scopes WHERE run_id=? ORDER BY identity', (str(run_id),))]
    return {'schemaVersion': 1, 'runId': str(run_id), 'policy': policy, 'records': records,
            'recordsTotal': len(records), 'snapshotConsistent': True, 'qaTestInputs': test_inputs,
            'scopes': scopes, 'scopesComplete': scopes_complete(scopes),
            'testInputScope': 'CAPTURED_QA_TOOL_INPUTS'}
