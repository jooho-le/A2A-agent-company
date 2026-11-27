"""Owner-selected service forks. Frozen evidence, never terminal-history rewrites."""
from dataclasses import dataclass
from hashlib import sha256
import json
import re
import time

MAX_CONTEXT_BYTES = 192 * 1024


def _validate_inventory(inventory):
    from orchestrator.artifacts.git_snapshot import GitSnapshotLimits, _validate_source_path
    from orchestrator.core.source_credentials import validate_source_bytes
    from orchestrator.sandbox.materialization import _check_names
    limits = GitSnapshotLimits()
    if not 1 <= len(inventory) <= limits.max_files:
        raise ContinuationConflict('CONTINUATION_SOURCE_INVALID')
    size = 0
    for path, (content, mode) in inventory.items():
        _validate_source_path(path)
        if (type(content) is not bytes or type(mode) is not int or mode not in (0o644, 0o755)
                or len(content) > limits.max_file_bytes or len(path.split('/')) > 32):
            raise ContinuationConflict('CONTINUATION_SOURCE_INVALID')
        validate_source_bytes(content)
        size += len(content)
    if size > limits.max_total_bytes:
        raise ContinuationConflict('CONTINUATION_SOURCE_INVALID')
    _check_names(inventory)
    return [{'path': name, 'sha256': sha256(content).hexdigest(), 'mode': mode}
            for name, (content, mode) in sorted(inventory.items())]


class ContinuationConflict(ValueError):
    def __init__(self, code='CONTINUATION_UNAVAILABLE'):
        self.code = code
        super().__init__(code)


def requires_previous_work(text):
    """Conservative unresolved references, never choose another project by recency."""
    value = text.strip()
    if re.search(r'(?:앱|사이트|서비스|기능|화면).*만들', value):
        return False
    return bool(re.search(r'^(?:이전|아까|직전|방금|앞선)(?:에서|에|의)?\s*(?:작업|오류|실패|개발|만든|하던|그거|거)', value)
                and re.search(r'고쳐|수정|이어|재시도|완료', value)) or bool(re.search(
                    r'\b(?:previous|earlier|last)\s+(?:run|task|error|code|work)\b.*\b(?:fix|retry|continue|repair)\b', value, re.I))


def ensure_schema(db):
    db.execute('''CREATE TABLE IF NOT EXISTS service_continuations(
        run_id TEXT PRIMARY KEY REFERENCES workflow_runs(run_id), parent_run_id TEXT NOT NULL,
        payload_json TEXT NOT NULL, payload_sha256 TEXT NOT NULL)''')
    db.execute('''CREATE TABLE IF NOT EXISTS service_continuation_files(
        run_id TEXT NOT NULL REFERENCES service_continuations(run_id), path TEXT NOT NULL,
        content BLOB NOT NULL, content_sha256 TEXT NOT NULL, mode INTEGER NOT NULL,
        PRIMARY KEY(run_id,path))''')
    for table in ('service_continuations', 'service_continuation_files'):
        for action in ('UPDATE', 'DELETE'):
            db.execute(f"CREATE TRIGGER IF NOT EXISTS {table}_no_{action.lower()} BEFORE {action} ON {table} "
                       "BEGIN SELECT RAISE(ABORT,'CONTINUATION_IMMUTABLE'); END")


def canonical(data):
    return json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def read_continuation(repository, run_id, *, files=False):
    with repository._connection() as db:
        row = db.execute('SELECT payload_json,payload_sha256 FROM service_continuations WHERE run_id=?',
                         (str(run_id),)).fetchone()
        if row is None:
            return None
        if sha256(row[0].encode()).hexdigest() != row[1]:
            raise ContinuationConflict('CONTINUATION_INTEGRITY_FAILED')
        payload = json.loads(row[0])
        if payload['runId'] != str(run_id):
            raise ContinuationConflict('CONTINUATION_INTEGRITY_FAILED')
        if not files:
            return payload
        entries = db.execute('SELECT path,content,content_sha256,mode FROM service_continuation_files WHERE run_id=? ORDER BY path',
                             (str(run_id),)).fetchall()
    inventory = {path: (bytes(content), mode) for path, content, digest, mode in entries
                 if sha256(bytes(content)).hexdigest() == digest}
    if len(inventory) != len(entries) or _validate_inventory(inventory) != payload['files']:
        raise ContinuationConflict('CONTINUATION_INTEGRITY_FAILED')
    expected = {item['path']: (item['sha256'], item['mode']) for item in payload['files']}
    if ({path: (sha256(content).hexdigest(), mode) for path, (content, mode) in inventory.items()} != expected
            or sha256(canonical(payload['files']).encode()).hexdigest() != payload['seedSha256']):
        raise ContinuationConflict('CONTINUATION_INTEGRITY_FAILED')
    return payload, inventory


def public_continuation(repository, run):
    from orchestrator.domain.scenario_registry import SERVICE_WEB_ID
    if run.scenario_id != SERVICE_WEB_ID:
        return None
    unavailable = None
    if run.status.value not in ('FINISHED', 'ABORTED', 'HUMAN_REVIEW', 'WAITING_INPUT'):
        unavailable = '현재 작업이 진행 중이에요. 자동 수정과 재검사를 먼저 확인해주세요.'
    elif any(s.status.value == 'RUNNING' for s in repository.list_steps(run.run_id)):
        unavailable = '직원이 아직 작업 중이에요. 실행 상태를 확인해주세요.'
    elif requires_previous_work(run.request_text) and read_continuation(repository, run.run_id) is None:
        unavailable = '이 요청에는 원래 작업이 연결되지 않았어요. 내 프로젝트에서 원래 기능을 만들던 작업을 선택해주세요.'
    else:
        with repository._connection() as db:
            active = db.execute('''SELECT 1 FROM service_continuations c JOIN workflow_runs r ON c.run_id=r.run_id
                WHERE c.parent_run_id=? AND r.status NOT IN ('FINISHED','ABORTED','HUMAN_REVIEW','WAITING_INPUT')''',
                (str(run.run_id),)).fetchone()
        if active:
            unavailable = '이 작업에서 이어서 수정하는 실행이 이미 진행 중이에요. 프로젝트의 최신 실행을 확인해주세요.'
    return {'available': unavailable is None, 'parentRunId': str(run.run_id),
            'reason': unavailable, 'preservesHistory': True}


@dataclass(frozen=True)
class ContinuationBundle:
    payload_json: str
    contract_json: str
    files: tuple


def capture_continuation(repository, workspaces, artifacts, source_configuration,
                         *, parent_id, run_id, instruction, configuration):
    """Only the authenticated Host calls this after product ownership checks."""
    from orchestrator.domain.scenario_registry import SERVICE_WEB_ID, service_request_contract
    from orchestrator.domain.validation_feedback import validation_feedback
    from orchestrator.core.security import redact_data
    from orchestrator.sandbox.materialization import _archive_entries
    parent = repository.get_run(parent_id)
    if parent is None or parent.scenario_id != SERVICE_WEB_ID:
        raise ContinuationConflict('CONTINUATION_PARENT_NOT_FOUND')
    if not public_continuation(repository, parent)['available']:
        raise ContinuationConflict('CONTINUATION_PARENT_NOT_READY')
    old = repository.get_run_configuration(parent_id)
    if (old is None or configuration.experiment_id is not None or old.configuration.experiment_id is not None
            or configuration.environment is None or configuration.environment != old.configuration.environment
            or configuration.starting_commit_hash != old.configuration.starting_commit_hash
            or configuration.starting_snapshot_sha256 != old.configuration.starting_snapshot_sha256):
        raise ContinuationConflict('CONTINUATION_BASELINE_CHANGED')
    if (configuration.starting_commit_hash != source_configuration.commit_hash
            or configuration.starting_snapshot_sha256 != source_configuration.snapshot_sha256):
        raise ContinuationConflict('CONTINUATION_BASELINE_CHANGED')
    registered = repository.list_project_artifacts(parent_id)
    sources = sorted((a for a in registered if a.artifact_type == 'SOURCE'), key=lambda a: a.code_version)
    selected = sources[-1] if sources else None
    if selected is not None:
        stored = artifacts.read_registered_source(parent_id, selected.artifact_id)
        inventory = {name: (content, mode) for name, content, mode in _archive_entries(stored.content)}
        kind = 'REGISTERED_SOURCE'
    else:
        # A provider can fail after confirmed writes but before Source READY.
        # Capture only the parent's confined Source, never auth/session files.
        from agents.runtime.developer_workspace import GitDeveloperCheckpoint
        from mcp_tools.tools.file_io import _locked_root
        from orchestrator.workspaces.filesystem import walk_directory
        from orchestrator.domain import AgentRole
        workspace = workspaces.bind(parent.workspace_id, run_id=parent_id, role=AgentRole.DEVELOPER)
        checkpoint = GitDeveloperCheckpoint(workspace,
            baseline_commit_hash=source_configuration.commit_hash,
            repository_id=source_configuration.repository_id, lock_path=source_configuration.lock_path,
            limits=artifacts._git_limits)
        with _locked_root(workspace, write=False) as root_fd:
            with walk_directory(root_fd, ('source',)) as source_fd:
                inventory = checkpoint._inventory(source_fd, time.monotonic()+15)
        kind = 'DRAFT_CHECKPOINT'
    lock = inventory.get(source_configuration.lock_path)
    if not inventory or lock is None or 'sha256:'+sha256(lock[0]).hexdigest() != configuration.environment.dependency_lock_hash:
        raise ContinuationConflict('CONTINUATION_SOURCE_INVALID')
    same_version = [a for a in registered if selected is not None
                    and getattr(a, 'code_version', None) == selected.code_version]
    current = [] if selected is None else [selected]
    for role_report in ('QA_REPORT', 'SECURITY_REPORT'):
        revisions = [a for a in same_version if a.artifact_type == role_report]
        if revisions:
            current.append(max(revisions, key=lambda a: a.artifact_version))
    feedback = validation_feedback(current) if current else []
    feedback = [row for row in feedback if row.get('outcome') != 'PASS'][:32]
    from orchestrator.infrastructure.repair_reproduction import generated_reproduction
    reproduction = generated_reproduction(repository, selected, feedback) if selected is not None else []
    planning = repository.get_planning_artifact(parent_id)
    previous_design = planning.payload.get('serviceDesign') if planning is not None else None
    original = old.scenario_contract
    from orchestrator.domain.service_design import service_policy
    if not service_policy(original):
        raise ContinuationConflict('CONTINUATION_BASELINE_CHANGED')
    submitted = service_request_contract(instruction)
    contract = json.loads(canonical(original))
    for key in ('userRequest', 'requestIdentity'):
        contract['externalContract'][key] = submitted['externalContract'][key]
    # A repair is not new product scope. Keep the exact original requirements;
    # the submitted repair instruction is bounded context, never new criteria.
    contract['requirements'] = json.loads(canonical(original['requirements']))
    prior = original.get('externalContract', {}).get('continuation', {})
    previous_design = previous_design or prior.get('previousServiceDesign')
    context = {'parentRunId': str(parent_id), 'sourceArtifactId': None if selected is None else str(selected.artifact_id),
        'sourceKind': kind, 'sourceVersion': None if selected is None else selected.code_version,
        'originalRequest': prior.get('originalRequest', parent.request_text), 'instruction': instruction,
        'previousServiceDesign': previous_design, 'failedChecks': feedback, 'reproduction': reproduction,
        'instructionPolicy': 'REPAIR_ORIGINAL_SERVICE_KEEP_INTERFACE'}
    context['stepFailures'] = [{'role': s.agent_role.value, 'code': s.failure_code or 'AGENT_EXECUTION_FAILED',
        'status': s.status.value} for s in repository.list_steps(parent_id) if s.status.value == 'FAILED'][-8:]
    builds = [a for a in same_version if a.artifact_type == 'BUILD_REPORT']
    if builds:
        build = max(builds, key=lambda a: a.artifact_version)
        if not build.passed:
            context['failedBuild'] = {'reportArtifactId': str(build.artifact_id), 'exitCode': build.exit_code,
                'diagnostics': [{'ruleId': d.rule_id, 'location': d.normalized_location, 'message': d.message}
                                for d in build.diagnostics][:16]}
    contract['externalContract']['continuation'] = context
    files = _validate_inventory(inventory)
    payload = {'runId': str(run_id), 'parentRunId': str(parent_id),
        'parentUpdatedAt': parent.model_dump(mode='json')['updated_at'],
        'sourceKind': kind, 'sourceArtifactId': context['sourceArtifactId'], 'files': files,
        'seedSha256': sha256(canonical(files).encode()).hexdigest()}
    if len(canonical(contract).encode()) > MAX_CONTEXT_BYTES:
        raise ContinuationConflict('CONTINUATION_CONTEXT_TOO_LARGE')
    if redact_data(contract) != contract:
        raise ContinuationConflict('CONTINUATION_UNSAFE_CONTEXT')
    return ContinuationBundle(canonical(payload), canonical(contract),
                              tuple((name, content, mode) for name, (content, mode) in sorted(inventory.items())))


def insert_continuation(db, run, bundle):
    payload = json.loads(bundle.payload_json)
    row = db.execute('SELECT status,payload_json FROM workflow_runs WHERE run_id=?', (payload['parentRunId'],)).fetchone()
    if (row is None or row[0] not in ('FINISHED', 'ABORTED', 'HUMAN_REVIEW', 'WAITING_INPUT')
            or json.loads(row[1])['updated_at'] != payload['parentUpdatedAt']
            or db.execute("SELECT 1 FROM workflow_steps WHERE run_id=? AND status='RUNNING'", (payload['parentRunId'],)).fetchone()):
        raise ContinuationConflict('CONTINUATION_PARENT_CHANGED')
    if db.execute('''SELECT 1 FROM service_continuations c JOIN workflow_runs r ON c.run_id=r.run_id
                     WHERE c.parent_run_id=? AND r.status NOT IN ('FINISHED','ABORTED','HUMAN_REVIEW','WAITING_INPUT')''',
                  (payload['parentRunId'],)).fetchone():
        raise ContinuationConflict('CONTINUATION_ALREADY_ACTIVE')
    if payload['runId'] != str(run.run_id):
        raise ContinuationConflict('CONTINUATION_BINDING_INVALID')
    inventory = {path: (content, mode) for path, content, mode in bundle.files}
    if (len(inventory) != len(bundle.files) or _validate_inventory(inventory) != payload['files']
            or sha256(canonical(payload['files']).encode()).hexdigest() != payload['seedSha256']):
        raise ContinuationConflict('CONTINUATION_INTEGRITY_FAILED')
    db.execute('INSERT INTO service_continuations VALUES (?,?,?,?)',
        (str(run.run_id), payload['parentRunId'], bundle.payload_json, sha256(bundle.payload_json.encode()).hexdigest()))
    for name, content, mode in bundle.files:
        db.execute('INSERT INTO service_continuation_files VALUES (?,?,?,?,?)',
                   (str(run.run_id), name, content, sha256(content).hexdigest(), mode))
