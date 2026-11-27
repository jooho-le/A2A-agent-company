"""Collect code changes and registered candidate history for Developer feedback."""
import ast

from agents.runtime.repair_call_history import channel_receipts, current_channel_receipts, recorded_call_edits
from orchestrator.core.security import redact_text
from orchestrator.domain.repair_progress import compare_repair, legacy_practice_versions
from orchestrator.domain.validation_feedback import validation_feedback


def changed_functions(previous, current, paths):
    """Name changed top-level Python functions without sending source text to the model."""
    result = {}
    for path in paths:
        if not path.endswith('.py'):
            continue
        names = []
        for content in (previous.get(path, b''), current.get(path, b'')):
            if len(content) > 128_000:
                break
            try:
                functions = {node.name: ast.dump(node) for node in ast.parse(content).body
                             if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))}
            except (SyntaxError, ValueError, RecursionError):
                break
            names.append(functions)
        if len(names) == 2:
            before, after = names
            changed = sorted(name for name in before.keys() | after.keys()
                             if before.get(name) != after.get(name) and redact_text(name) == name)
            if changed:
                result[path] = changed
    return result


def changed_handler_decorators(current, symbols):
    """Literal exception-handler registrations on changed functions in the submitted source."""
    result = []
    for path, changed in symbols.items():
        source = current.get(path)
        if not source or len(source) > 128_000:
            continue
        try:
            nodes = ast.parse(source).body
        except (ValueError, RecursionError, SyntaxError):
            continue
        for node in nodes:
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) or node.name not in changed:
                continue
            for decorator in node.decorator_list:
                if (isinstance(decorator, ast.Call) and isinstance(decorator.func, ast.Attribute)
                        and decorator.func.attr == 'exception_handler' and len(decorator.args) == 1
                        and isinstance(decorator.args[0], ast.Name)):
                    name = decorator.args[0].id
                    if redact_text(name) == name and len(name) <= 128:
                        result.append({'path': path, 'function': node.name, 'exceptionName': name})
    return result


def candidate_history(services, execution, artifacts, source):
    from orchestrator.sandbox.materialization import _archive_entries

    parent = next((a for a in artifacts if a.artifact_type == 'SOURCE'
                   and a.artifact_id == source.previous_artifact_id), None)
    if parent is None or parent.run_id != source.run_id or parent.code_version + 1 != source.code_version:
        return None
    prior_reports = {}
    for item in artifacts:
        if item.artifact_type in ('QA_REPORT', 'SECURITY_REPORT') and item.execution_manifest == parent.execution_manifest():
            previous = prior_reports.get(item.artifact_type)
            if previous is None or previous.artifact_version < item.artifact_version:
                prior_reports[item.artifact_type] = item
    if not prior_reports:
        return None
    report = next((a for a in artifacts if a.artifact_type == 'CHANGE_REPORT'
                   and a.workflow_step_id == source.workflow_step_id and a.code_version == source.code_version), None)
    paths = sorted(item.path.removeprefix('source/') for item in report.changes) if report else None
    previous_rows = validation_feedback([parent, *prior_reports.values()])
    symbols, handlers, call_edits, after_channels = {}, [], [], set()
    if paths and any(path.endswith('.py') for path in paths):
        def contents(snapshot):
            blob = services._artifacts.read_registered_source(snapshot.run_id, snapshot.artifact_id)
            return {name: data for name, data, _ in _archive_entries(blob.content)}
        old, current = contents(parent), contents(source)
        symbols = changed_functions(old, current, paths)
        handlers = changed_handler_decorators(current, symbols)
        review = report.ready_review
        if (review and review.source_artifact_id == source.artifact_id and review.working_base_commit == parent.commit_hash
                and report.run_id == parent.run_id
                and parent.container_image_digest == source.container_image_digest
                and parent.dependency_lock_hash == source.dependency_lock_hash):
            env = execution.configuration.configuration.environment
            environment = None
            if (env and source.container_image_digest == env.container_image_digest
                    and source.dependency_lock_hash == env.dependency_lock_hash):
                environment = env.model_dump(mode='json', by_alias=True)
            before_channels = channel_receipts(prior_reports.values(), parent, previous_rows)
            call_edits = recorded_call_edits(old, current, previous_rows, review, environment, measured_channels=before_channels)
            if before_channels and call_edits:
                after_channels = current_channel_receipts(artifacts, source)
    return {'previousRows': previous_rows, 'recordedCallEdits': call_edits, 'afterSourceId': str(source.artifact_id),
            'beforeVersion': parent.code_version, 'afterVersion': source.code_version, 'afterChannelReceipts': after_channels,
            'beforeSnapshot': parent.snapshot_sha256, 'afterSnapshot': source.snapshot_sha256,
            'changedFiles': paths, 'changedFunctions': symbols, 'changedHandlerDecorators': handlers,
            'legacyTestVersions': legacy_practice_versions(execution.configuration)}
