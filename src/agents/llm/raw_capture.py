"""Opt-in private capture of rejected model responses for local diagnosis.

Off unless the operator sets ``A2A_DIAGNOSTIC_RAW_DIR`` to an absolute directory.
Captured files never enter A2A messages, Host exports, logs, Trace or evaluation
results. The directory is created 0700 and each file 0600. Text is passed through
the Host redaction first, so registered secrets and credential values are masked;
paths, field names and code structure stay visible. Capture failures never change
the Run: the original rejection or correction proceeds exactly the same way.
"""
import json
import os
from pathlib import Path
import time
from uuid import uuid4

from orchestrator.core.security import redact_data, redact_text

ENV = 'A2A_DIAGNOSTIC_RAW_DIR'


def enabled() -> bool:
    root = os.environ.get(ENV)
    return bool(root) and Path(root).is_absolute()


def _actor(value):
    return getattr(value, 'value', None) or str(value)


def capture(*, role, phase, sequence, kind, corrected, workspace_id=None, report=None,
            output_text=None, tool_calls=None) -> bool:
    if not enabled():
        return False
    try:
        base = Path(os.environ[ENV])
        base.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(base, 0o700)
        public = None
        if report is not None:
            try:
                public = report.public()
            except Exception:
                public = None
        record = {
            'schemaVersion': 1, 'capturedAt': time.strftime('%Y-%m-%dT%H:%M:%S%z'), 'pid': os.getpid(),
            'role': _actor(role), 'phase': None if phase is None else _actor(phase), 'sequence': sequence,
            'kind': kind, 'corrected': bool(corrected), 'workspaceId': workspace_id, 'report': public,
            'outputText': None if output_text is None else redact_text(str(output_text)),
            'toolCalls': None if tool_calls is None else [
                {'name': call.name, 'callId': call.call_id, 'arguments': redact_text(str(call.arguments_json))}
                for call in tool_calls],
        }
        record = redact_data(record)
        name = f'{time.strftime("%Y%m%dT%H%M%S")}-{os.getpid()}-{int(sequence):04d}-{kind}-{uuid4().hex[:8]}.json'
        descriptor = os.open(base / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, 'w', encoding='utf-8') as stream:
            json.dump(record, stream, ensure_ascii=False, indent=2)
        return True
    except Exception:
        return False
