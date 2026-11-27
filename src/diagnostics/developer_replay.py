"""Developer-only diagnostic fork. Preparation is offline; execution needs approval.

Original A2A and Git objects remain immutable. A changed implementation is
explicitly recorded as a diagnostic fork, never admitted as a measured replay.
No dispatcher, QA, Security or experiment aggregation is constructed here.
"""
import argparse
import asyncio
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess
import tempfile
from time import monotonic
from uuid import uuid4

from agents.api.validation import parse_workflow_metadata
from agents.llm.budget import ExecutionBudget, LLMLimits
from agents.platform.telemetry import RuntimeTelemetry
from agents.platform.telemetry_store import AgentTelemetryStore
from agents.platform.tool_configuration import load_tool_configuration, _read_regular
from agents.roles.prompts import prepare_role_prompt
from agents.runtime.developer import DeveloperAgentExecutor, _prepare_input
from agents.runtime.developer_context import SQLiteDeveloperContextLoader
from agents.runtime.developer_services import DeveloperRuntimeServices
from agents.runtime.repair_baseline import with_repair_baseline
from agents.semantic_review import ProductSemanticReviewer
from agents.semantic_store import SemanticReviewStore
from diagnostics.stage_replay import (ReplayError, _readonly, _read_json, _write, _json, _code_hashes,
    _copy_inputs, _context, _DiagnosticQueue, _all_records, _live_provider, MAX_INPUT)
from mcp_tools.client import MCPChildConfiguration, open_mcp_client
from mcp_tools.runtime import MCPBinding
from mcp_tools.tools.build_config import decode_build_configuration, encode_build_configuration
from orchestrator.api.schemas.runs import RunStatusResponse
from orchestrator.artifacts.git_snapshot import GitSnapshotBuilder
from orchestrator.artifacts.service import ArtifactStore
from orchestrator.core.security import redact_data
from orchestrator.domain.models import WorkflowRun, WorkflowStep
from orchestrator.domain.snapshot_handoff import CodeSnapshotArtifact
from orchestrator.domain.states import A2ATaskState, AgentRole, WorkflowStatus, WorkflowStepStatus
from orchestrator.infrastructure import SQLiteWorkflowRepository
from orchestrator.sandbox.materialization import _archive_entries
from orchestrator.workspaces.registry import WorkspaceRegistry

PURPOSE = 'DIAGNOSTIC_DEVELOPER_FORK_NOT_FOR_EVALUATION'
INPUTS = ('request.json', 'policy.json', 'diagnostic.sqlite3', 'objects.pack', 'projected-input.json')


def _git(root, *args, data=None):
    executable = shutil.which('git', path='/usr/bin:/bin:/opt/homebrew/bin')
    if executable is None:
        raise ReplayError('REPLAY_GIT_UNAVAILABLE')
    env = {'PATH': '/usr/bin:/bin', 'LANG': 'C', 'LC_ALL': 'C', 'HOME': '/dev/null',
           'XDG_CONFIG_HOME': '/dev/null', 'GIT_CONFIG_NOSYSTEM': '1', 'GIT_CONFIG_GLOBAL': '/dev/null',
           'GIT_NO_REPLACE_OBJECTS': '1', 'GIT_NO_LAZY_FETCH': '1', 'GIT_TERMINAL_PROMPT': '0',
           'GIT_OPTIONAL_LOCKS': '0'}
    result = subprocess.run([executable, '--no-pager', '-C', str(root), '-c', 'protocol.allow=never',
        '-c', 'core.hooksPath=/dev/null', '-c', 'core.fsmonitor=false', '-c', 'gc.auto=0',
        '-c', 'maintenance.auto=false', *args], input=data, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        timeout=30, env=env)
    if result.returncode or len(result.stdout) > MAX_INPUT:
        raise ReplayError('REPLAY_GIT_FAILED')
    return result.stdout


def policy_from_files(tools_file, source_file):
    tools, source = load_tool_configuration(tools_file), _read_json(source_file)
    return {'build': encode_build_configuration(tools.build_configuration),
        'maxCallSeconds': tools.max_call_seconds, 'baselineCommitHash': source['commitHash'],
        'repositoryId': source['repositoryId'], 'lockPath': source['lockPath']}


def _assemble(directory, manifest, *, client_factory=open_mcp_client):
    repository = SQLiteWorkflowRepository(directory / 'diagnostic.sqlite3')
    registry = WorkspaceRegistry(repository, directory / 'workspaces')
    registry.provision(manifest['workspaceId'], run_id=manifest['sourceRunId'])
    artifacts = ArtifactStore(repository, registry)
    context = _context(_read_json(directory / 'request.json'), manifest['sourceTaskId'], manifest['sourceContextId'])
    budget = ExecutionBudget(runtime_budget_ms=manifest['runtimeBudgetMs'],
                             limits=LLMLimits.model_validate(manifest['llmLimits']))
    execution = SQLiteDeveloperContextLoader(repository, lambda _: budget)(context)
    _prepare_input(context, execution)
    if execution.configuration.configuration.architecture != 'MULTI_AGENT':
        raise ReplayError('REPLAY_MULTI_DEVELOPER_ONLY')
    policy = _read_json(directory / 'policy.json')
    source = directory / 'workspaces' / manifest['workspaceId'] / 'source'
    if not (source / '.git').exists():
        _git(source, 'init', '--quiet', '--object-format=' + ('sha256' if len(policy['baselineCommitHash']) == 64 else 'sha1'))
        _git(source, 'index-pack', '--stdin', data=_read_regular(directory / 'objects.pack', MAX_INPUT))
        parent = execution.previous_source.commit_hash if execution.previous_source else policy['baselineCommitHash']
        snapshot = GitSnapshotBuilder(source).build(parent, policy['lockPath'])
        # Never checkout: product attributes/filters/hooks must not run on Host.
        for name, content, mode in _archive_entries(snapshot.archive):
            target = source / name
            target.parent.mkdir(parents=True, exist_ok=True)
            with target.open('xb') as stream:
                stream.write(content)
            target.chmod(mode & 0o777)
    configuration = MCPChildConfiguration(binding=MCPBinding(role=AgentRole.DEVELOPER,
        agent_role=AgentRole.DEVELOPER, run_id=execution.metadata.run_id,
        workspace_id=execution.configuration.workspace_id), database_path=repository.database_path,
        workspace_root=registry.base_path, build_configuration=decode_build_configuration(policy['build']),
        max_call_seconds=policy['maxCallSeconds'])
    services = DeveloperRuntimeServices(repository, registry, artifacts, mcp_configuration=configuration,
        baseline_commit_hash=policy['baselineCommitHash'], repository_id=policy['repositoryId'],
        lock_path=policy['lockPath'], client_factory=client_factory)
    return repository, context, execution, services


def _reproject_host_constraints(directory, manifest):
    """2026-11-18 (docs/live-api/05 25절 #2): the Host's fix-request constraint texts changed. A stored
    request differs from what the current Host projects from the same artifacts only there; replace
    exactly fixRequest.constraints with the current Host text and record the original request hash.
    Any other difference keeps the strict refusal of _prepare_input."""
    from orchestrator.core.security import redact_data
    request = _read_json(directory / 'request.json')
    repository = SQLiteWorkflowRepository(directory / 'diagnostic.sqlite3')
    registry = WorkspaceRegistry(repository, directory / 'workspaces')
    registry.provision(manifest['workspaceId'], run_id=manifest['sourceRunId'])
    context = _context(request, manifest['sourceTaskId'], manifest['sourceContextId'])
    budget = ExecutionBudget(runtime_budget_ms=manifest['runtimeBudgetMs'],
                             limits=LLMLimits.model_validate(manifest['llmLimits']))
    execution = SQLiteDeveloperContextLoader(repository, lambda _: budget)(context)
    expected = redact_data(execution.initial_payload)
    data = request['message']['parts'][0]['data']
    if data == expected or 'fixRequest' not in data or 'fixRequest' not in expected:
        return None
    if {**data, 'fixRequest': {**data['fixRequest'], 'constraints': None}} != {
            **expected, 'fixRequest': {**expected['fixRequest'], 'constraints': None}}:
        return None  # not only the Host constraints: keep the strict refusal
    original = sha256(_json(request).encode()).hexdigest()
    data['fixRequest']['constraints'] = expected['fixRequest']['constraints']
    (directory / 'request.json').unlink()
    _write(directory / 'request.json', request)
    return {'originalRequestSha256': original, 'reprojectedFields': ['fixRequest.constraints']}


def _historical_inputs(connection, step, base_version):
    """Reconstruct the input side of a completed repair in a fresh DB only."""
    earlier = [WorkflowStep.model_validate_json(row[0]) for row in connection.execute(
        'SELECT payload_json FROM workflow_steps WHERE run_id=?', (str(step.run_id),))]
    earlier = [s for s in earlier if s.updated_at <= step.created_at
               and (s.code_version or 0) <= base_version and s.workflow_step_id != step.workflow_step_id]
    steps = {str(s.workflow_step_id) for s in earlier}
    artifacts = {str(a) for s in earlier for a in s.output_artifact_ids}
    if not step.input_artifact_ids or not all(str(a) in artifacts for a in step.input_artifact_ids):
        raise ReplayError('REPLAY_HISTORICAL_INPUT_MISSING')
    issues = {row['issue_id']: row['code_version'] for row in connection.execute(
        'SELECT issue_id,code_version,payload_json FROM issue_records WHERE run_id=? AND code_version<=?',
        (str(step.run_id), base_version)) if json.loads(row['payload_json'])['source_artifact_id'] in artifacts}
    target = str(step.workflow_step_id)
    dispatch_events = [row['sequence'] for row in connection.execute(
        "SELECT sequence,issue_id,payload_json FROM issue_events WHERE run_id=? AND event_type='FIX_REQUESTED'",
        (str(step.run_id),)) if issues.get(row['issue_id']) == base_version
        and json.loads(row['payload_json']).get('fix_workflow_step_id') == target]
    if not dispatch_events:
        raise ReplayError('REPLAY_HISTORICAL_FIX_REQUEST_MISSING')
    dispatch_sequence = max(dispatch_events)

    def project(table, row):
        if table == 'workflow_runs':
            data = json.loads(row['payload_json'])
            data.update(status='FIXING', code_version=base_version, fix_attempt=base_version, verdict=None,
                        termination_reason=None, resume_state=None, updated_at=step.created_at.isoformat())
            row.update(updated_at=data['updated_at'], payload_json=WorkflowRun.model_validate(data).model_dump_json())
        elif table == 'workflow_steps':
            if row['workflow_step_id'] == target:
                opening = step.model_copy(update={'status': WorkflowStepStatus.RUNNING,
                    'a2a_task_state': A2ATaskState.SUBMITTED, 'output_artifact_ids': [],
                    'a2a_artifact_ids': [], 'updated_at': step.created_at})
                row.update(payload_json=opening.model_dump_json(), updated_at=step.created_at.isoformat())
            elif row['workflow_step_id'] not in steps:
                return None
        elif table == 'agent_contexts':
            if row['agent_id'] != 'developer':
                return None
            context = json.loads(row['payload_json'])
            if context['agent_context_id'] != step.agent_context_id:
                raise ReplayError('REPLAY_INPUT_BINDING_MISMATCH')
            context['latest_a2a_task_id'] = step.a2a_task_id
            row['payload_json'] = _json(context)
        elif table in ('project_artifacts', 'artifact_contents', 'snapshot_read_grants'):
            if row['artifact_id'] not in artifacts:
                return None
        elif table in ('issue_records', 'issue_events'):
            version = issues.get(row['issue_id'])
            if version is None:
                return None
            if table == 'issue_events' and row['sequence'] > dispatch_sequence:
                return None
            if table == 'issue_events' and version == base_version:
                if (row['event_type'] != 'FIX_REQUESTED'
                        or json.loads(row['payload_json']).get('fix_workflow_step_id') != target):
                    return None
        return row
    return project, artifacts


async def prepare_replay(evidence_dir, *, policy, limits, runtime_budget_ms, step_id, base_code_version=None):
    evidence_dir = Path(evidence_dir).resolve(strict=True)
    bundle, a2a = _read_json(evidence_dir / 'export/bundle.json'), _read_json(evidence_dir / 'export/a2a-messages.json')
    run_id = bundle['run']['runId']
    if a2a['runId'] != run_id or bundle.get('snapshotConsistent') is not True:
        raise ReplayError('REPLAY_EXPORT_INCONSISTENT')
    with _readonly(evidence_dir / 'orchestrator.sqlite3') as connection:
        row = connection.execute('SELECT payload_json FROM workflow_steps WHERE run_id=? AND workflow_step_id=?',
                                 (run_id, step_id)).fetchone()
        if row is None:
            raise ReplayError('REPLAY_STEP_MISSING')
        step = WorkflowStep.model_validate_json(row[0])
        run = WorkflowRun.model_validate_json(connection.execute('SELECT payload_json FROM workflow_runs WHERE run_id=?',
                                                                (run_id,)).fetchone()[0])
        sources = [CodeSnapshotArtifact.model_validate_json(r[0]) for r in connection.execute(
            "SELECT payload_json FROM project_artifacts WHERE run_id=? AND artifact_type='SOURCE'", (run_id,))]
        if (step.agent_role is not AgentRole.DEVELOPER or step.run_id != run.run_id
                or RunStatusResponse.from_run(run) != RunStatusResponse.model_validate(bundle['run'])):
            raise ReplayError('REPLAY_INPUT_BINDING_MISMATCH')
        project_row = None
        if base_code_version is None:
            if step.output_artifact_ids or step.a2a_artifact_ids or step.code_version != (run.code_version or 0) + 1:
                raise ReplayError('REPLAY_EXPECTS_UNPUBLISHED_DEVELOPER_STEP')
        else:
            if (type(base_code_version) is not int or base_code_version < 1
                    or step.code_version != base_code_version + 1 or run.status is not WorkflowStatus.FINISHED
                    or step.status is not WorkflowStepStatus.SUCCEEDED or step.attempt != 0
                    or step.a2a_task_state is not A2ATaskState.COMPLETED
                    or not step.output_artifact_ids or not step.a2a_artifact_ids):
                raise ReplayError('REPLAY_EXPECTS_COMPLETED_DEVELOPER_REPAIR')
            project_row, input_history = _historical_inputs(connection, step, base_code_version)
            sources = [s for s in sources if s.code_version <= base_code_version and str(s.artifact_id) in input_history]
        commits = {s.commit_hash for s in sources}
    role = a2a['roles']['DEVELOPER']
    receipts = [r for r in role['receipts'] if r['taskId'] == step.a2a_task_id]
    tasks = [t for t in role['tasks'] if t['taskId'] == step.a2a_task_id]
    if len(receipts) != 1 or len(tasks) != 1:
        raise ReplayError('REPLAY_REQUIRES_ONE_INITIAL_REQUEST')
    receipt, task = receipts[0], tasks[0]
    request = receipt['request']
    metadata = parse_workflow_metadata(request['metadata'])
    if (receipt['metadata'] != request['metadata'] or task['metadata'] != request['metadata']
            or metadata.run_id != step.run_id or metadata.workflow_step_id != step.workflow_step_id
            or metadata.code_version != step.code_version or receipt['contextId'] != step.agent_context_id
            or task['contextId'] != step.agent_context_id):
        raise ReplayError('REPLAY_INPUT_BINDING_MISMATCH')
    limits = LLMLimits.model_validate(limits.model_dump())
    if limits.max_total_tokens is None or type(runtime_budget_ms) is not int or runtime_budget_ms <= 0:
        raise ReplayError('REPLAY_BOUNDED_LIMITS_REQUIRED')
    commits.add(policy['baselineCommitHash'])
    if any(re.fullmatch(r'[0-9a-f]{40}|[0-9a-f]{64}', c) is None for c in commits):
        raise ReplayError('REPLAY_COMMIT_INVALID')
    original_source = evidence_dir / 'workspaces' / str(run.workspace_id) / 'source'
    pack = _git(original_source, 'pack-objects', '--stdout', '--revs', data=('\n'.join(sorted(commits))+'\n').encode())
    directory = Path(tempfile.mkdtemp(prefix='capstone-developer-replay-')).resolve()
    manifest = {'schemaVersion': 1, 'purpose': PURPOSE, 'replayId': str(uuid4()), 'status': 'AWAITING_APPROVAL',
        'maxExecutions': 1, 'sourceRunId': run_id, 'sourceStepId': step_id, 'sourceTaskId': step.a2a_task_id,
        'sourceContextId': step.agent_context_id, 'workspaceId': str(run.workspace_id), 'codeVersion': step.code_version,
        'runtimeBudgetMs': runtime_budget_ms, 'llmLimits': limits.model_dump(), 'codeHashes': _code_hashes(),
        'sourceEvidenceDirectory': str(evidence_dir), 'measurementEligible': False, 'overallVerdict': None,
        'policyMode': 'EXPLICIT_CHANGED_IMPLEMENTATION_DIAGNOSTIC_FORK',
        'originalCodeHashes': _read_json(evidence_dir / 'source-version.json')}
    if base_code_version is not None:
        manifest['baseCodeVersion'] = base_code_version
    try:
        _copy_inputs(evidence_dir / 'orchestrator.sqlite3', directory / 'diagnostic.sqlite3', directory / 'workspaces',
            run_id=run_id, step_id=step_id, role=AgentRole.DEVELOPER, project_row=project_row)
        _write(directory / 'request.json', request); _write(directory / 'policy.json', policy)
        with os.fdopen(os.open(directory / 'objects.pack', os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), 'wb') as stream:
            stream.write(pack)
        reprojected = _reproject_host_constraints(directory, manifest)
        if reprojected is not None:
            manifest['hostRequestReprojection'] = reprojected
        expected_archive = None
        if step.code_version > 1:
            expected_archive = _read_regular(evidence_dir / f'export/v{step.code_version - 1}/source.tar', MAX_INPUT)
        return await _seal_preparation(directory, manifest, expected_archive=expected_archive)
    except BaseException:
        _write(directory / 'preparation-failed.json', {'status': 'FAILED', 'apiCalls': 0})
        raise


async def _seal_preparation(directory, manifest, *, expected_archive):
    repository, context, execution, services = _assemble(directory, manifest)
    checkpoint = await services.prepare(execution)  # Git/files only, no model or Docker.
    active = with_repair_baseline(_prepare_input(context, execution), checkpoint)
    # The role prompt and engine redact before the provider sees this envelope.
    # Seal what will actually be sent and retain the local projection's provenance.
    prompt = prepare_role_prompt(AgentRole.DEVELOPER, task_input=active, metadata=execution.metadata)
    wire_task = redact_data(json.loads(prompt.input_json))['taskInput']
    _write(directory / 'projected-input.json', wire_task)
    if execution.previous_source:
        stored = services._artifacts.read_registered_source(execution.metadata.run_id, execution.previous_source.artifact_id)
        if stored.content != expected_archive:
            raise ReplayError('REPLAY_SNAPSHOT_EXPORT_MISMATCH')
    manifest.update(model=execution.model.model_dump(mode='json', by_alias=True),
        repairBaseline=checkpoint.repair_baseline, policySha256=sha256(_json(_read_json(directory / 'policy.json')).encode()).hexdigest(),
        sourceProjectionSha256=sha256(_json(active).encode()).hexdigest(),
        projectedInputSha256=sha256(_json(wire_task).encode()).hexdigest())
    with sqlite3.connect(directory / 'diagnostic.sqlite3') as connection:
        connection.execute('PRAGMA wal_checkpoint(TRUNCATE)')
    manifest['inputHashes'] = {name: sha256((directory / name).read_bytes()).hexdigest() for name in INPUTS}
    manifest['approvalSha256'] = sha256(_json(manifest).encode()).hexdigest()
    _write(directory / 'manifest.json', manifest)
    _write(directory / 'offline-check.json', {'status': 'PASS', 'apiCalls': 0, 'dockerExecutions': 0,
        'originalRequestValidated': 'feedbackOrigin' not in manifest, 'changedImplementationDiagnostic': True, 'measurementEligible': False})
    return directory


def verify_prepared(directory):
    directory = Path(directory).resolve(strict=True)
    roots = {Path(tempfile.gettempdir()).resolve(), Path('/tmp').resolve(), Path('/private/tmp').resolve()}
    if directory.parent not in roots or not directory.name.startswith('capstone-developer-replay-'):
        raise ReplayError('REPLAY_PRIVATE_TEMP_REQUIRED')
    manifest = _read_json(directory / 'manifest.json')
    if (manifest.get('purpose') != PURPOSE or manifest.get('status') != 'AWAITING_APPROVAL'
            or manifest.get('measurementEligible') is not False or manifest.get('maxExecutions') != 1):
        raise ReplayError('REPLAY_MANIFEST_INVALID')
    if manifest.get('approvalSha256') != sha256(_json({k:v for k,v in manifest.items() if k!='approvalSha256'}).encode()).hexdigest():
        raise ReplayError('REPLAY_APPROVAL_PLAN_CHANGED')
    if manifest['codeHashes'] != _code_hashes():
        raise ReplayError('REPLAY_CODE_CHANGED_PREPARE_AGAIN')
    if set(manifest['inputHashes']) != set(INPUTS):
        raise ReplayError('REPLAY_INPUT_HASHES_INVALID')
    for name, digest in manifest['inputHashes'].items():
        if sha256(_read_regular(directory / name, MAX_INPUT)).hexdigest() != digest:
            raise ReplayError('REPLAY_PREPARED_INPUT_CHANGED')
    return manifest


def _build_in_step(events):
    for event in events:
        if 'artifact' not in event:
            continue
        text = json.dumps(event['artifact'], ensure_ascii=False)
        if 'BUILD_REPORT' in text or 'executionOutcome' in text:
            outcome = 'PASS' if '"executionOutcome": "PASS"' in text or '"executionOutcome":"PASS"' in text else (
                'FAIL' if 'FAIL' in text else 'UNKNOWN')
            return {'ran': True, 'outcome': outcome}
    return {'ran': False, 'outcome': None}


class _FirstInputGuard:
    """Codex review 2026-11-18 #3: before the first model call, the input the executor actually rebuilt
    must equal the approved projection (projectedInputSha256). Otherwise nothing is sent."""

    def __init__(self, provider, expected):
        self._provider, self._expected = provider, expected
        self.name = getattr(provider, 'name', None)
        self.checked = self.mismatch = False

    def __getattr__(self, item):
        return getattr(self._provider, item)

    async def complete(self, request):
        if not self.checked:
            try:
                envelope = json.loads(json.loads(request.input_items_json)[0]['content'])
                actual = sha256(_json(envelope['taskInput']).encode()).hexdigest()
            except Exception:
                actual = None
            if actual != self._expected:
                self.mismatch = True
                from agents.llm.contracts import LLMErrorCode, LLMRuntimeError
                raise LLMRuntimeError(LLMErrorCode.CONFIGURATION)
            self.checked = True
        return await self._provider.complete(request)


async def execute_replay(directory, *, approval_id, provider_factory, client_factory=open_mcp_client):
    directory = Path(directory).resolve(strict=True)
    manifest = verify_prepared(directory)
    if not approval_id or approval_id != manifest['approvalSha256']:
        raise ReplayError('REPLAY_EXPLICIT_APPROVAL_REQUIRED')
    try:
        _write(directory / 'execution-claimed.json', {'approvalSha256': approval_id, 'maxExecutions': 1})
    except FileExistsError:
        raise ReplayError('REPLAY_ALREADY_ATTEMPTED_PREPARE_AGAIN') from None
    attempt = directory / 'attempt'; attempt.mkdir(mode=0o700)
    try:
        _copy_inputs(directory / 'diagnostic.sqlite3', attempt / 'diagnostic.sqlite3', attempt / 'workspaces',
            run_id=manifest['sourceRunId'], step_id=manifest['sourceStepId'], role=AgentRole.DEVELOPER)
        for name in ('request.json', 'policy.json', 'objects.pack'):
            with os.fdopen(os.open(attempt / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), 'wb') as stream:
                stream.write(_read_regular(directory / name, MAX_INPUT))
        repository, context, execution, services = _assemble(attempt, manifest, client_factory=client_factory)
    except BaseException:
        _write(attempt / 'result.json', {'purpose': PURPOSE, 'role': 'DEVELOPER', 'error': 'REPLAY_SETUP_FAILED',
            'modelCalls': 0, 'toolCalls': 0, 'measurementEligible': False, 'overallVerdict': None})
        raise
    telemetry_store = AgentTelemetryStore(repository.database_path)
    telemetry, queue = RuntimeTelemetry(repository, telemetry_store), _DiagnosticQueue(attempt)
    provider, error, started = None, None, monotonic()
    try:
        provider = _FirstInputGuard(provider_factory(), manifest.get('projectedInputSha256'))
        executor = DeveloperAgentExecutor(provider=provider, context_factory=lambda _: execution,
            services_factory=lambda _: services, telemetry_factory=telemetry.bind,
            semantic_reviewer=ProductSemanticReviewer(provider, SemanticReviewStore(repository.database_path)))
        await asyncio.wait_for(executor.execute(context, queue), manifest['runtimeBudgetMs'] / 1000)
    except asyncio.CancelledError:
        error = 'REPLAY_CANCELED'; raise
    except TimeoutError:
        error = 'REPLAY_TIME_LIMIT'
    except Exception:
        error = 'REPLAY_EXECUTION_FAILED'
    finally:
        if provider is not None:
            try: await provider.aclose()
            except Exception: error = error or 'REPLAY_PROVIDER_CLOSE_FAILED'
        usage, total = _all_records(telemetry_store.list_usage, execution.metadata.run_id)
        events, count = _all_records(telemetry_store.list_events, execution.metadata.run_id)
        _write(attempt / 'usage.json', {'records': usage, 'total': total})
        _write(attempt / 'telemetry.json', {'events': events, 'total': count})
        _write(attempt / 'artifacts.json', {'artifacts': [e['artifact'] for e in queue.events if 'artifact' in e]})
        if isinstance(provider, _FirstInputGuard) and provider.mismatch:
            error = 'REPLAY_INPUT_CHANGED'
        statuses = [e['status'] for e in queue.events if 'status' in e]
        result = {'purpose': PURPOSE, 'role': 'DEVELOPER', 'replayId': manifest['replayId'], 'error': error,
            'taskStatus': statuses[-1] if statuses else None, 'modelCalls': execution.budget.model_calls,
            'toolCalls': execution.budget.tool_calls, 'totalTokens': execution.budget.total_tokens,
            'knownTotalTokens': execution.budget.known_total_tokens, 'usageComplete': execution.budget.total_tokens is not None,
            'costUsd': None, 'costComplete': False, 'elapsedMs': int((monotonic()-started)*1000),
            'measurementEligible': False, 'overallVerdict': None, 'sourceCodeUnchanged': _code_hashes()==manifest['codeHashes'],
            # Codex review 2026-11-19 #4: the Host build inside this step (published only with a candidate)
            # is distinct from any later diagnostic rebuild of the preserved working copy.
            'hostBuildInStep': _build_in_step(queue.events)}
        _write(attempt / 'result.json', result)
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    prepare = commands.add_parser('prepare', help='Offline preparation only')
    for name in ('evidence-dir', 'tools-file', 'source-file'):
        prepare.add_argument('--'+name, required=True, type=Path)
    prepare.add_argument('--step-id', required=True)
    prepare.add_argument('--base-code-version', type=int, help='Fork a completed repair from this published parent')
    prepare.add_argument('--max-model-calls', type=int, default=50)
    prepare.add_argument('--max-tool-calls', type=int, default=100)
    prepare.add_argument('--max-total-tokens', type=int, default=500000)
    prepare.add_argument('--runtime-seconds', type=int, default=900)
    measure = commands.add_parser('measure-practice', help='Offline recheck of one previously failing Ver1 practice test')
    measure.add_argument('--parent', type=Path, required=True)
    measure.add_argument('--check', required=True)
    focused = commands.add_parser('prepare-focused', help='Offline repair from a published replay and its practice result')
    focused.add_argument('--parent', type=Path, required=True)
    run = commands.add_parser('run', help='Paid execution only after explicit approval')
    run.add_argument('--prepared', required=True, type=Path); run.add_argument('--approval-id', required=True)
    args = parser.parse_args(argv)
    if args.command == 'prepare':
        runtime = _read_json(args.evidence_dir / 'runtime.json')
        limits = LLMLimits.model_validate({**runtime['llmLimits'], 'max_model_calls': args.max_model_calls,
            'max_tool_calls': args.max_tool_calls, 'max_total_tokens': args.max_total_tokens})
        root = asyncio.run(prepare_replay(args.evidence_dir, policy=policy_from_files(args.tools_file, args.source_file),
            limits=limits, runtime_budget_ms=args.runtime_seconds*1000, step_id=args.step_id,
            base_code_version=args.base_code_version))
        print(json.dumps({'status': 'AWAITING_APPROVAL', 'prepared': str(root), 'apiCalls': 0}))
    elif args.command == 'measure-practice':
        from diagnostics.focused_measurement import measure_practice
        result = measure_practice(args.parent, args.check)
        print(json.dumps({'outcome': result['scopes']['practice']['outcome'], 'snapshotSha256': result['snapshotSha256'],
                          'measurementEligible': False, 'apiCalls': 0}))
    elif args.command == 'prepare-focused':
        from diagnostics.focused_replay import prepare_focused
        root = asyncio.run(prepare_focused(args.parent))
        print(json.dumps({'status': 'AWAITING_APPROVAL', 'prepared': str(root), 'apiCalls': 0}))
    else:
        result = asyncio.run(execute_replay(args.prepared, approval_id=args.approval_id, provider_factory=_live_provider))
        print(json.dumps({**{k: result.get(k) for k in ('role', 'error', 'modelCalls', 'knownTotalTokens')},
                          'taskState': (result.get('taskStatus') or {}).get('state')}))
    return 0


if __name__ == '__main__':
    try: raise SystemExit(main())
    except ReplayError as error: raise SystemExit(error.code)
