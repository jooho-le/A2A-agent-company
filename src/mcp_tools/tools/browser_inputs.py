"""Freeze browser test JSON and Host runner bytes; never execute on the Host."""

from collections.abc import Mapping
from dataclasses import dataclass, field
import errno
import json
from types import MappingProxyType

from mcp_tools.runtime import MCPExecutionContext
from mcp_tools.tools.browser_config import (
    BrowserConfigurationError, BrowserTestConfiguration, BrowserTestSuite, _copy_configuration, _copy_suite,
    _nonfinite, _object, browser_host_payload, validate_browser_host_payload,
)
from mcp_tools.tools.browser_contract import BrowserContractError, parse_browser_suite
from mcp_tools.tools.file_io import FileOperationError
from mcp_tools.tools.unit_config import MAX_UNIT_FILE_BYTES, MAX_UNIT_FILES, MAX_UNIT_TOTAL_BYTES, UnitTestConfigurationError, _test_path
from mcp_tools.tools.unit_inputs import UnitTestInputsError, _capture_qa, _files_hash, _hash, _validate_content
from orchestrator.domain.states import AgentRole
from orchestrator.workspaces.filesystem import BoundWorkspace
from orchestrator.workspaces.policy import WorkspaceAccessError


_RUNNER = "_browser_runner.py"
_CONTRACT = "_browser_contract.py"
_HOST = "_browser_host.json"
_REQUIRED = frozenset({_RUNNER, _CONTRACT, _HOST})
_CODES = frozenset({
    "PERMISSION_DENIED", "PATH_DENIED", "SECRET_DENIED", "FILE_TOO_LARGE", "FILE_ENCODING_ERROR",
    "WRITE_CONFLICT", "FILE_NOT_FOUND", "TOOL_EXECUTION_FAILED", "TEST_RUNNER_ERROR",
})


class BrowserInputsError(RuntimeError):
    def __init__(self, code):
        self.code = code if isinstance(code, str) and code in _CODES else "TOOL_EXECUTION_FAILED"
        super().__init__(self.code)


@dataclass(frozen=True, kw_only=True)
class BrowserTestInputs:
    files: Mapping[str, bytes] = field(repr=False)
    inputs_sha256: str
    runner_sha256: str
    test_files_sha256: str
    host_configuration_sha256: str
    contract_sha256: str

    def __post_init__(self):
        try:
            if not isinstance(self.files, Mapping):
                raise BrowserInputsError("TOOL_EXECUTION_FAILED")
            copied = dict(self.files)
            if not _REQUIRED <= set(copied) or not 4 <= len(copied) <= MAX_UNIT_FILES + 3:
                raise BrowserInputsError("TOOL_EXECUTION_FAILED")
            total = 0
            for path, content in copied.items():
                if path not in _REQUIRED:
                    try:
                        _test_path(path)
                    except UnitTestConfigurationError:
                        raise BrowserInputsError("PATH_DENIED") from None
                if type(content) is not bytes or len(content) > MAX_UNIT_FILE_BYTES:
                    raise BrowserInputsError("FILE_TOO_LARGE")
                _validate_content(content)
                total += len(content)
            if not copied[_RUNNER] or not copied[_CONTRACT] or total > MAX_UNIT_TOTAL_BYTES:
                raise BrowserInputsError("FILE_TOO_LARGE")
            tests = {path: content for path, content in copied.items() if path not in _REQUIRED}
            for path in tests:
                parts = path.split("/")
                if any("/".join(parts[:index]) in tests for index in range(1, len(parts))):
                    raise BrowserInputsError("PATH_DENIED")
            host = validate_browser_host_payload(json.loads(copied[_HOST].decode("utf-8"),
                object_pairs_hook=_object, parse_constant=_nonfinite))
            canonical_host = json.dumps(host, ensure_ascii=False, allow_nan=False,
                sort_keys=True, separators=(",", ":")).encode("utf-8")
            if copied[_HOST] != canonical_host:
                raise BrowserInputsError("TEST_RUNNER_ERROR")
            if host["suite_path"] not in tests:
                raise BrowserInputsError("FILE_NOT_FOUND")
            parse_browser_suite(tests[host["suite_path"]].decode("utf-8"), host["suite_name"])
            expected = (_files_hash(copied), _hash(copied[_RUNNER]), _files_hash(tests), _hash(copied[_HOST]), _hash(copied[_CONTRACT]))
            if (self.inputs_sha256, self.runner_sha256, self.test_files_sha256,
                    self.host_configuration_sha256, self.contract_sha256) != expected:
                raise BrowserInputsError("TOOL_EXECUTION_FAILED")
            object.__setattr__(self, "files", MappingProxyType(dict(sorted(copied.items()))))
        except UnitTestInputsError as error:
            raise BrowserInputsError(error.code) from None
        except (BrowserConfigurationError, BrowserContractError, UnicodeError, ValueError, TypeError, OverflowError, RecursionError):
            raise BrowserInputsError("TEST_RUNNER_ERROR") from None


def prepare_browser_inputs(context: MCPExecutionContext, suite: BrowserTestSuite,
                           configuration: BrowserTestConfiguration, runner_source: bytes,
                           contract_source: bytes) -> BrowserTestInputs:
    if (
        type(context) is not MCPExecutionContext or not isinstance(context.workspace, BoundWorkspace)
        or context.binding.role is not AgentRole.QA or context.workspace.role is not AgentRole.QA
        or context.binding.run_id != context.workspace.run_id
        or context.binding.workspace_id != context.workspace.workspace_id
    ):
        raise BrowserInputsError("PERMISSION_DENIED")
    try:
        suite, configuration = _copy_suite(suite), _copy_configuration(configuration)
        host = browser_host_payload(configuration, suite)
        if (type(runner_source) is not bytes or not 1 <= len(runner_source) <= MAX_UNIT_FILE_BYTES
                or type(contract_source) is not bytes or not 1 <= len(contract_source) <= MAX_UNIT_FILE_BYTES):
            raise BrowserInputsError("TOOL_EXECUTION_FAILED")
        _validate_content(runner_source)
        _validate_content(contract_source)
        tests = _capture_qa(context.workspace) if suite.kind == "QA_TESTS" else {
            path: content.encode("utf-8") for path, content in suite.protected_files.items()
        }
        if suite.suite_path not in tests:
            raise BrowserInputsError("FILE_NOT_FOUND")
        parse_browser_suite(tests[suite.suite_path].decode("utf-8"), suite.name)
        files = {_RUNNER: runner_source, _CONTRACT: contract_source,
            _HOST: json.dumps(host, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":")).encode("utf-8"), **tests}
        return BrowserTestInputs(files=files, inputs_sha256=_files_hash(files), runner_sha256=_hash(runner_source),
            test_files_sha256=_files_hash(tests), host_configuration_sha256=_hash(files[_HOST]), contract_sha256=_hash(contract_source))
    except (UnitTestInputsError, FileOperationError) as error:
        raise BrowserInputsError(error.code) from None
    except WorkspaceAccessError as error:
        raise BrowserInputsError("FILE_NOT_FOUND" if error.code.value == "FILE_NOT_FOUND" else "PATH_DENIED") from None
    except OSError as error:
        raise BrowserInputsError("FILE_NOT_FOUND" if error.errno == errno.ENOENT else "PATH_DENIED") from None
    except (BrowserConfigurationError, BrowserContractError, UnicodeError, ValueError, TypeError, OverflowError, RecursionError):
        raise BrowserInputsError("TEST_RUNNER_ERROR") from None
