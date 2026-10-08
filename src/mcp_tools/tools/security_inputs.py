"""Read-only Host runner inputs; no product capture or execution on the Host."""

from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType

from mcp_tools.tools.security_config import (
    SecurityConfigurationError, SecurityScanConfiguration, SecurityScannerProfile,
    _copy_configuration, _copy_profile, security_host_payload,
)
from mcp_tools.tools.security_contract import SecurityContractError, canonical_json, parse_json, validate_security_host_payload
from mcp_tools.tools.unit_config import MAX_UNIT_FILE_BYTES, MAX_UNIT_TOTAL_BYTES
from mcp_tools.tools.unit_inputs import UnitTestInputsError, _files_hash, _hash, _validate_content


_RUNNER = "_security_runner.py"
_CONTRACT = "_security_contract.py"
_HOST = "_security_host.json"
_REQUIRED = frozenset({_RUNNER, _CONTRACT, _HOST})
_CODES = frozenset({"TOOL_EXECUTION_FAILED", "SECRET_DENIED", "FILE_TOO_LARGE", "FILE_ENCODING_ERROR", "SCANNER_ERROR"})


class SecurityInputsError(RuntimeError):
    def __init__(self, code):
        self.code = code if type(code) is str and code in _CODES else "TOOL_EXECUTION_FAILED"
        super().__init__(self.code)


@dataclass(frozen=True, kw_only=True)
class SecurityScanInputs:
    files: Mapping[str, bytes] = field(repr=False)
    inputs_sha256: str
    runner_sha256: str
    contract_sha256: str
    host_configuration_sha256: str

    def __post_init__(self):
        try:
            if not isinstance(self.files, Mapping):
                raise SecurityInputsError("TOOL_EXECUTION_FAILED")
            files = dict(self.files)
            if set(files) != _REQUIRED:
                raise SecurityInputsError("TOOL_EXECUTION_FAILED")
            for content in files.values():
                if type(content) is not bytes or not 1 <= len(content) <= MAX_UNIT_FILE_BYTES:
                    raise SecurityInputsError("FILE_TOO_LARGE")
                _validate_content(content)
            if sum(map(len, files.values())) > MAX_UNIT_TOTAL_BYTES:
                raise SecurityInputsError("FILE_TOO_LARGE")
            host = validate_security_host_payload(parse_json(files[_HOST].decode("utf-8")))
            if files[_HOST] != canonical_json(host).encode("utf-8"):
                raise SecurityInputsError("SCANNER_ERROR")
            expected = (_files_hash(files), _hash(files[_RUNNER]), _hash(files[_CONTRACT]), _hash(files[_HOST]))
            if (self.inputs_sha256, self.runner_sha256, self.contract_sha256, self.host_configuration_sha256) != expected:
                raise SecurityInputsError("TOOL_EXECUTION_FAILED")
            object.__setattr__(self, "files", MappingProxyType(dict(sorted(files.items()))))
        except UnitTestInputsError as error:
            raise SecurityInputsError(error.code) from None
        except (SecurityContractError, SecurityConfigurationError, UnicodeError, ValueError, TypeError,
                OverflowError, RecursionError):
            raise SecurityInputsError("SCANNER_ERROR") from None


def prepare_security_inputs(configuration: SecurityScanConfiguration, profile: SecurityScannerProfile,
                            runner_source: bytes, contract_source: bytes) -> SecurityScanInputs:
    """Pure byte assembly; imports/construction do not touch Source, DB or files."""
    try:
        configuration, profile = _copy_configuration(configuration), _copy_profile(profile)
        host = security_host_payload(configuration, profile)
        if (type(runner_source) is not bytes or not 1 <= len(runner_source) <= MAX_UNIT_FILE_BYTES
                or type(contract_source) is not bytes or not 1 <= len(contract_source) <= MAX_UNIT_FILE_BYTES):
            raise SecurityInputsError("TOOL_EXECUTION_FAILED")
        files = {_RUNNER: runner_source, _CONTRACT: contract_source, _HOST: canonical_json(host).encode("utf-8")}
        return SecurityScanInputs(files=files, inputs_sha256=_files_hash(files), runner_sha256=_hash(runner_source),
            contract_sha256=_hash(contract_source), host_configuration_sha256=_hash(files[_HOST]))
    except (SecurityConfigurationError, SecurityContractError, UnicodeError, ValueError, TypeError, OverflowError, RecursionError):
        raise SecurityInputsError("SCANNER_ERROR") from None
