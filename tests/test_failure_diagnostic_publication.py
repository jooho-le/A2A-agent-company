"""The five task exporters preserve closed facts and legacy empty metadata."""
import asyncio
from importlib import import_module
from unittest.mock import patch

import pytest
from a2a.types import TaskState

from agents.llm.failure_diagnostics import FailureDiagnostic


@pytest.mark.parametrize('module_name', ['agents.runtime.planner', 'agents.runtime.developer',
    'agents.runtime.qa', 'agents.runtime.security', 'agents.single.runtime'])
@pytest.mark.parametrize('diagnosed', [False, True])
def test_all_role_task_status_exporters_keep_closed_failure_details(module_name, diagnosed):
    module = import_module(module_name)
    exporters = [v for v in vars(module).values() if isinstance(v, type)
                 and v.__module__ == module_name and hasattr(v, '_status')]
    assert len(exporters) == 1
    diagnostic = FailureDiagnostic(boundary='HTTP', reason='SERVER_ERROR', httpStatus=500).public() if diagnosed else None

    class Capture:
        message = None
        def new_agent_message(self, **values):
            return values
        async def update_status(self, _state, *, metadata, message):
            self.message = message

    capture = Capture()
    with patch.object(module, 'new_data_part', side_effect=lambda data, **_kw: data):
        asyncio.run(exporters[0]._status(capture, {}, TaskState.TASK_STATE_FAILED, 'LLM_PROVIDER_ERROR',
                                         llm_failure=diagnostic))
    data, = capture.message['parts']
    assert data == ({'code': 'LLM_PROVIDER_ERROR', 'llmFailure': diagnostic} if diagnosed else {'code': 'LLM_PROVIDER_ERROR'})
