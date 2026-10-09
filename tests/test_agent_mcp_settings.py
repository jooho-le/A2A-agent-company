"""15번 설정 계약 회귀 테스트. 서버·LLM·MCP 실행 없이 설정만 검증한다."""

import json
import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from a2a.utils.constants import (
    A2A_JSON_MEDIA_TYPE,
    PROTOCOL_VERSION_1_0,
    TransportProtocol,
)
from pydantic import SecretStr, ValidationError

from agents.core.config import AgentConfigurationError, AgentSettings
from agents.core.contracts import (
    A2A_MEDIA_TYPE,
    A2A_PROTOCOL_BINDING,
    A2A_PROTOCOL_VERSION,
    AGENT_PORTS,
)
from mcp_tools.core.config import MCPSettings
from mcp_tools.core.policy import (
    MCP_PROTOCOL_VERSION,
    MCP_TRANSPORT,
    ROLE_TOOL_NAMES,
)
from orchestrator.core.config import Settings as OrchestratorSettings
from orchestrator.domain.constants import MAX_MCP_TOOL_RETRIES
from orchestrator.domain.states import AgentRole


class AgentMCPSettingsTests(unittest.TestCase):
    """개발자의 실제 환경변수·.env에 영향받지 않는 설정 검증."""

    def setUp(self) -> None:
        environment = patch.dict(os.environ, {}, clear=True)
        environment.start()
        self.addCleanup(environment.stop)

    def test_each_role_has_its_own_default_port_url_and_database_path(self):
        expected = {
            AgentRole.PLANNER: 8101,
            AgentRole.DEVELOPER: 8102,
            AgentRole.QA: 8103,
            AgentRole.SECURITY: 8104,
        }
        self.assertEqual(dict(AGENT_PORTS), expected)
        for role, port in expected.items():
            with self.subTest(role=role):
                settings = AgentSettings(role=role, _env_file=None)
                self.assertEqual(settings.role, role)
                self.assertEqual(settings.host, "127.0.0.1")
                self.assertIsNone(settings.port)
                self.assertEqual(settings.listen_port, port)
                self.assertEqual(settings.agent_base_url, f"http://127.0.0.1:{port}")
                self.assertEqual(
                    settings.task_database_path,
                    Path(".data") / "agents" / f"{role.value.lower()}.sqlite3",
                )

    def test_explicit_listen_port_overrides_role_default(self):
        for port in (1, 8110, 65535):
            with self.subTest(port=port):
                settings = AgentSettings(role="DEVELOPER", port=port, _env_file=None)
                self.assertEqual(settings.listen_port, port)
                self.assertEqual(settings.agent_base_url, f"http://127.0.0.1:{port}")

    def test_listen_port_outside_tcp_range_is_rejected(self):
        for port in (0, -1, 65536):
            with self.subTest(port=port), self.assertRaises(ValidationError):
                AgentSettings(role="QA", port=port, _env_file=None)

    def test_ipv6_host_is_bracketed_and_localhost_is_supported(self):
        for host, expected in (
            ("::1", "http://[::1]:8103"),
            ("localhost", "http://localhost:8103"),
        ):
            with self.subTest(host=host):
                settings = AgentSettings(role="QA", host=host, _env_file=None)
                self.assertEqual(settings.agent_base_url, expected)

    def test_non_loopback_bind_host_is_rejected(self):
        for host in ("0.0.0.0", "192.0.2.1", "example.com"):
            with self.subTest(host=host), self.assertRaises(ValidationError):
                AgentSettings(role="PLANNER", host=host, _env_file=None)

    def test_both_process_settings_require_an_explicit_role(self):
        for model in (AgentSettings, MCPSettings):
            with self.subTest(model=model.__name__), self.assertRaises(ValidationError):
                model(_env_file=None)

    def test_unknown_role_is_rejected_in_both_processes(self):
        for model in (AgentSettings, MCPSettings):
            for role in ("AUTOMATOR", "planner", ""):
                with self.subTest(model=model.__name__, role=role):
                    with self.assertRaises(ValidationError):
                        model(role=role, _env_file=None)

    def test_model_configuration_starts_unconfigured(self):
        settings = AgentSettings(role="PLANNER", _env_file=None)
        self.assertIsNone(settings.llm_provider)
        self.assertIsNone(settings.llm_model_id)
        self.assertIsNone(settings.llm_api_key)
        self.assertIsNone(settings.bearer_token)
        with self.assertRaises(AgentConfigurationError) as error:
            settings.require_llm_configuration()
        self.assertIn("AGENT_LLM_PROVIDER", str(error.exception))
        self.assertIn("AGENT_LLM_MODEL_ID", str(error.exception))

    def test_partial_model_configuration_reports_only_missing_selection(self):
        for configured, missing in (
            ({"llm_provider": "local-provider"}, "AGENT_LLM_MODEL_ID"),
            ({"llm_model_id": "local-model"}, "AGENT_LLM_PROVIDER"),
        ):
            with self.subTest(configured=configured):
                settings = AgentSettings(
                    role="DEVELOPER", llm_api_key="DUMMY_SELECTION_KEY",
                    _env_file=None, **configured,
                )
                with self.assertRaises(AgentConfigurationError) as error:
                    settings.require_llm_configuration()
                self.assertIn(missing, str(error.exception))
                self.assertNotIn("DUMMY_SELECTION_KEY", str(error.exception))

    def test_blank_model_provider_or_id_is_rejected(self):
        for field in ("llm_provider", "llm_model_id"):
            for value in ("", " ", "\t\n"):
                with self.subTest(field=field, value=value):
                    with self.assertRaises(ValidationError):
                        AgentSettings(role="QA", _env_file=None, **{field: value})

    def test_model_selection_is_trimmed_and_does_not_require_an_api_key_yet(self):
        settings = AgentSettings(
            role="DEVELOPER", llm_provider=" local-provider ",
            llm_model_id=" local-model ", _env_file=None,
        )
        self.assertEqual(settings.llm_provider, "local-provider")
        self.assertEqual(settings.llm_model_id, "local-model")
        self.assertIsNone(settings.require_llm_configuration())
        self.assertIsNone(settings.llm_api_key)

    def test_secret_values_are_usable_but_never_exported_in_repr_dict_or_json(self):
        settings = AgentSettings(
            role="DEVELOPER", llm_api_key="DUMMY_LLM_API_KEY",
            bearer_token="DUMMY_A2A_BEARER_TOKEN", _env_file=None,
        )
        self.assertIsInstance(settings.llm_api_key, SecretStr)
        self.assertIsInstance(settings.bearer_token, SecretStr)
        self.assertEqual(settings.llm_api_key.get_secret_value(), "DUMMY_LLM_API_KEY")
        self.assertEqual(settings.bearer_token.get_secret_value(), "DUMMY_A2A_BEARER_TOKEN")
        for output in (
            repr(settings), str(settings), repr(settings.model_dump()),
            json.dumps(settings.model_dump(mode="json")), settings.model_dump_json(),
        ):
            with self.subTest(output_type=type(output).__name__):
                self.assertNotIn("DUMMY_LLM_API_KEY", output)
                self.assertNotIn("DUMMY_A2A_BEARER_TOKEN", output)
        self.assertNotIn("llm_api_key", settings.model_dump())
        self.assertNotIn("bearer_token", settings.model_dump())
        self.assertNotIn("llm_api_key", json.loads(settings.model_dump_json()))
        self.assertNotIn("bearer_token", json.loads(settings.model_dump_json()))

    def test_blank_credentials_are_rejected_with_generic_error_messages(self):
        for field in ("llm_api_key", "bearer_token"):
            for value in ("", " \t"):
                with self.subTest(field=field, value=value):
                    with self.assertRaises(ValidationError) as error:
                        AgentSettings(role="QA", _env_file=None, **{field: value})
                    self.assertIn("Credential must not be blank", str(error.exception))
                    self.assertNotIn("input_value", str(error.exception))

    def test_validation_error_text_does_not_echo_untrusted_input_or_credentials(self):
        with self.assertRaises(ValidationError) as error:
            AgentSettings(
                role="PLANNER", host="DUMMY_INVALID_HOST_WITH_SECRET",
                llm_api_key="DUMMY_ERROR_API_KEY", bearer_token="DUMMY_ERROR_BEARER",
                _env_file=None,
            )
        message = str(error.exception)
        for secret in (
            "DUMMY_INVALID_HOST_WITH_SECRET", "DUMMY_ERROR_API_KEY", "DUMMY_ERROR_BEARER",
        ):
            self.assertNotIn(secret, message)
        self.assertNotIn("input_value", message)

    def test_environment_and_log_level_have_fixed_allowed_values(self):
        for model in (AgentSettings, MCPSettings):
            defaults = model(role="QA", _env_file=None)
            self.assertEqual(defaults.environment, "local")
            self.assertEqual(defaults.log_level, "INFO")
            for environment in ("local", "development", "test", "production"):
                with self.subTest(model=model.__name__, environment=environment):
                    self.assertEqual(
                        model(role="QA", environment=environment, _env_file=None).environment,
                        environment,
                    )
            for field, value in (("environment", "staging"), ("log_level", "TRACE")):
                with self.subTest(model=model.__name__, field=field):
                    with self.assertRaises(ValidationError):
                        model(role="QA", _env_file=None, **{field: value})

    def test_process_role_and_other_settings_are_immutable(self):
        for model in (AgentSettings, MCPSettings):
            settings = model(role="QA", _env_file=None)
            for field, value in (("role", AgentRole.DEVELOPER), ("environment", "production")):
                with self.subTest(model=model.__name__, field=field):
                    with self.assertRaises(ValidationError):
                        setattr(settings, field, value)
            self.assertEqual(settings.role, AgentRole.QA)
            self.assertEqual(settings.environment, "local")

    def test_environment_prefixes_keep_three_process_configurations_separate(self):
        with patch.dict(os.environ, {
            "AGENT_ROLE": "QA", "AGENT_ENVIRONMENT": "test",
            "AGENT_LOG_LEVEL": "DEBUG", "AGENT_LLM_MODEL_ID": " agent-model ",
            "MCP_ROLE": "SECURITY", "MCP_ENVIRONMENT": "development",
            "MCP_LOG_LEVEL": "WARNING", "ORCHESTRATOR_ENVIRONMENT": "production",
            "ORCHESTRATOR_LOG_LEVEL": "ERROR", "ORCHESTRATOR_APP_NAME": "Isolated Orchestrator",
            "ROLE": "DEVELOPER", "ENVIRONMENT": "invalid-global-value",
        }):
            agent = AgentSettings(_env_file=None)
            mcp = MCPSettings(_env_file=None)
            orchestrator = OrchestratorSettings(_env_file=None)
        self.assertEqual((agent.role, agent.environment, agent.log_level),
                         (AgentRole.QA, "test", "DEBUG"))
        self.assertEqual(agent.llm_model_id, "agent-model")
        self.assertEqual((mcp.role, mcp.environment, mcp.log_level),
                         (AgentRole.SECURITY, "development", "WARNING"))
        self.assertEqual((orchestrator.environment, orchestrator.log_level),
                         ("production", "ERROR"))
        self.assertEqual(orchestrator.app_name, "Isolated Orchestrator")

    def test_mixed_dotenv_is_shared_without_cross_process_setting_leakage(self):
        with TemporaryDirectory() as temporary:
            env_file = Path(temporary) / "mixed.env"
            env_file.write_text(
                "AGENT_ROLE=DEVELOPER\nAGENT_PORT=8199\nAGENT_ENVIRONMENT=test\n"
                "AGENT_LLM_PROVIDER=local-provider\nAGENT_LLM_MODEL_ID=local-model\n"
                "AGENT_LLM_API_KEY=DUMMY_DOTENV_KEY\nAGENT_BEARER_TOKEN=DUMMY_DOTENV_TOKEN\n"
                "MCP_ROLE=SECURITY\nMCP_ENVIRONMENT=development\nMCP_TRANSPORT=stdio\n"
                "MCP_PROTOCOL_VERSION=2026-07-28\n"
                "ORCHESTRATOR_APP_NAME=Shared Env Orchestrator\n"
                "ORCHESTRATOR_ENVIRONMENT=production\nORCHESTRATOR_DATABASE_PATH=.data/shared.sqlite3\n"
                "UNRELATED_TEAM_SETTING=ignored\n",
                encoding="utf-8",
            )
            agent = AgentSettings(_env_file=env_file)
            mcp = MCPSettings(_env_file=env_file)
            orchestrator = OrchestratorSettings(_env_file=env_file)
        self.assertEqual((agent.role, agent.listen_port, agent.environment),
                         (AgentRole.DEVELOPER, 8199, "test"))
        self.assertIsNone(agent.require_llm_configuration())
        self.assertEqual(agent.llm_api_key.get_secret_value(), "DUMMY_DOTENV_KEY")
        self.assertNotIn("DUMMY_DOTENV_KEY", agent.model_dump_json())
        self.assertNotIn("DUMMY_DOTENV_TOKEN", repr(agent))
        self.assertEqual((mcp.role, mcp.environment), (AgentRole.SECURITY, "development"))
        self.assertEqual(orchestrator.app_name, "Shared Env Orchestrator")
        self.assertEqual(orchestrator.environment, "production")
        self.assertEqual(orchestrator.database_path, ".data/shared.sqlite3")

    def test_constructor_values_override_prefixed_environment_values(self):
        with patch.dict(os.environ, {"AGENT_ROLE": "PLANNER", "AGENT_PORT": "8122", "MCP_ROLE": "QA"}):
            agent = AgentSettings(role="SECURITY", port=8191, _env_file=None)
            mcp = MCPSettings(role="DEVELOPER", _env_file=None)
        self.assertEqual((agent.role, agent.listen_port), (AgentRole.SECURITY, 8191))
        self.assertEqual(mcp.role, AgentRole.DEVELOPER)

    def test_mcp_role_tool_allowlists_match_exact_declared_contract(self):
        expected = {
            AgentRole.PLANNER: (),
            AgentRole.DEVELOPER: (
                "read_project_file", "write_source_file", "apply_patch", "run_build", "run_unit_tests",
            ),
            AgentRole.QA: (
                "read_project_file", "write_test_file", "run_unit_tests", "run_browser_tests", "read_test_report",
            ),
            AgentRole.SECURITY: (
                "read_project_file", "run_security_scan", "read_security_report",
            ),
        }
        self.assertEqual(dict(ROLE_TOOL_NAMES), expected)
        for role, tool_names in expected.items():
            with self.subTest(role=role):
                settings = MCPSettings(role=role, _env_file=None)
                self.assertIsInstance(settings.allowed_tool_names, tuple)
                self.assertEqual(settings.allowed_tool_names, tool_names)

    def test_role_tool_policy_cannot_be_overridden_by_input_or_environment(self):
        with patch.dict(os.environ, {
            "MCP_ALLOWED_TOOL_NAMES": '["write_source_file"]', "MCP_MAX_TOOL_RETRIES": "99",
        }):
            settings = MCPSettings(
                role="SECURITY", allowed_tool_names=("write_source_file",),
                max_tool_retries=99, _env_file=None,
            )
        self.assertEqual(settings.allowed_tool_names, ROLE_TOOL_NAMES[AgentRole.SECURITY])
        self.assertNotIn("write_source_file", settings.allowed_tool_names)
        self.assertEqual(settings.max_tool_retries, 2)

    def test_mcp_defaults_reuse_fixed_protocol_transport_and_retry_limit(self):
        settings = MCPSettings(role="PLANNER", _env_file=None)
        self.assertEqual(settings.transport, MCP_TRANSPORT)
        self.assertEqual(settings.transport, "stdio")
        self.assertEqual(settings.protocol_version, MCP_PROTOCOL_VERSION)
        self.assertEqual(settings.protocol_version, "2026-07-28")
        self.assertEqual(settings.max_tool_retries, MAX_MCP_TOOL_RETRIES)
        self.assertEqual(settings.max_tool_retries, 2)

    def test_unsupported_mcp_transport_or_protocol_version_is_rejected(self):
        for field, value in (
            ("transport", "http"), ("transport", "sse"),
            ("protocol_version", "2025-03-26"), ("protocol_version", "latest"),
        ):
            with self.subTest(field=field, value=value):
                with self.assertRaises(ValidationError):
                    MCPSettings(role="QA", _env_file=None, **{field: value})

    def test_port_and_role_policy_mappings_are_read_only(self):
        with self.assertRaises(TypeError):
            AGENT_PORTS[AgentRole.PLANNER] = 9999
        with self.assertRaises(TypeError):
            ROLE_TOOL_NAMES[AgentRole.QA] = ("write_source_file",)
        self.assertEqual(AGENT_PORTS[AgentRole.PLANNER], 8101)
        self.assertNotIn("write_source_file", ROLE_TOOL_NAMES[AgentRole.QA])

    def test_a2a_constants_are_reused_from_the_official_sdk(self):
        self.assertEqual(A2A_PROTOCOL_VERSION, PROTOCOL_VERSION_1_0)
        self.assertEqual(A2A_PROTOCOL_VERSION, "1.0")
        self.assertEqual(A2A_PROTOCOL_BINDING, TransportProtocol.HTTP_JSON.value)
        self.assertEqual(A2A_PROTOCOL_BINDING, "HTTP+JSON")
        self.assertEqual(A2A_MEDIA_TYPE, A2A_JSON_MEDIA_TYPE)

    def test_extra_settings_are_ignored_without_turning_into_configuration(self):
        for model in (AgentSettings, MCPSettings):
            with self.subTest(model=model.__name__):
                settings = model(role="PLANNER", unrelated="ignored", _env_file=None)
                self.assertNotIn("unrelated", settings.model_dump())
                self.assertFalse(hasattr(settings, "unrelated"))

    def test_clean_import_needs_no_model_or_mcp_sdk_and_opens_no_connection(self):
        """새 인터프리터에서 선택적 SDK import·소켓 연결·DB 파일 생성을 막고 설정만 읽는다."""
        script = r'''
import builtins
import importlib
import socket

original_import = builtins.__import__
blocked = {"openai", "anthropic", "litellm", "ollama", "mcp"}

def guarded_import(name, *args, **kwargs):
    if name.split(".")[0] in blocked:
        raise AssertionError("Optional runtime dependency imported: " + name)
    return original_import(name, *args, **kwargs)

def reject_connection(*args, **kwargs):
    raise AssertionError("Settings import attempted a network connection")

builtins.__import__ = guarded_import
socket.create_connection = reject_connection
socket.socket.connect = reject_connection
socket.socket.connect_ex = reject_connection

for name in (
    "agents", "agents.api", "agents.roles", "agents.llm", "agents.core.config",
    "agents.core.contracts", "mcp_tools", "mcp_tools.tools", "mcp_tools.core.config",
    "mcp_tools.core.policy",
):
    importlib.import_module(name)

from agents.core.config import AgentSettings
from mcp_tools.core.config import MCPSettings
agent = AgentSettings(role="PLANNER", _env_file=None)
mcp = MCPSettings(role="PLANNER", _env_file=None)
assert agent.listen_port == 8101
assert str(agent.task_database_path) == ".data/agents/planner.sqlite3"
assert mcp.allowed_tool_names == ()
print("import-only-ok")
'''
        source_root = Path(__file__).resolve().parents[1] / "src"
        with TemporaryDirectory() as temporary:
            result = subprocess.run(
                [sys.executable, "-B", "-c", script],
                cwd=temporary,
                env={"PYTHONPATH": str(source_root), "PYTHONDONTWRITEBYTECODE": "1"},
                capture_output=True, text=True, timeout=30, check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.strip(), "import-only-ok")
            self.assertEqual(list(Path(temporary).iterdir()), [])


if __name__ == "__main__":
    unittest.main()
