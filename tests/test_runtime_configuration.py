"""Step 38 approved settings are inert, strict, secret-free and Host-owned.

All credentials are local fake fixtures. No provider, Docker, listening server,
SQLite creation, generated product Source or external integration is invoked.
"""

from contextlib import redirect_stderr, redirect_stdout
from copy import deepcopy
import io
import json
import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from agents.platform.configuration import (
    RuntimeConfigurationError,
    inspect_runtime_configuration,
    load_runtime_configuration,
    resolve_runtime_configuration,
)
from agents.platform.check_configuration import main as check_configuration
from agents.core.config import AgentSettings
from agents.llm.budget import LLMLimits
from orchestrator.core.config import Settings
from orchestrator.domain.run_configuration import ModelConfiguration
from orchestrator.domain.states import AgentRole


API_KEY_ENV = "A2A_PLATFORM_LLM_API_KEY"
API_KEY = "sk-fixture-runtime-key-not-a-real-credential"
PREFIX = "RUNTIME_CONFIGURATION_"


def configuration_data():
    return {
        "schemaVersion": 1,
        "host": "127.0.0.1",
        "environment": "local",
        "logLevel": "INFO",
        "orchestrator": {"port": 8000, "databasePath": "../.data/orchestrator.sqlite3"},
        "workspaceRoot": "../.data/workspaces",
        "agents": {
            role.value: {
                "port": 8101 + index,
                "databasePath": f"../.data/agents/{role.value.lower()}.sqlite3",
                "bearerTokenEnv": f"A2A_PLATFORM_{role.value}_TOKEN",
            }
            for index, role in enumerate(AgentRole)
        },
        "model": {
            "provider": "openai", "modelId": "fixture-model",
            "modelRevision": None, "temperature": 0, "seed": None,
        },
        "apiKeyEnv": API_KEY_ENV,
        "llmLimits": {
            "max_model_calls": 8, "max_tool_calls": 20, "max_output_tokens": 2048,
            "max_total_tokens": None, "model_timeout_seconds": 60,
            "tool_timeout_seconds": 60, "max_json_bytes": 1_048_576,
        },
        "runtimeBudgetMs": 300_000,
    }


def fake_environment():
    return {
        API_KEY_ENV: API_KEY,
        **{f"A2A_PLATFORM_{role.value}_TOKEN": f"fixture-{role.value.lower()}-bearer-secret"
           for role in AgentRole},
    }


class _ConfigurationFixture:
    def setUp(self):
        self.temporary = TemporaryDirectory(prefix="a2a-runtime-settings-", dir="/tmp")
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name).resolve()
        self.base = self.directory / "config"
        self.base.mkdir()
        self.file = self.base / "owned-runtime.json"
        self.data = configuration_data()
        self.environment = fake_environment()

    def write(self, data=None):
        self.file.write_text(json.dumps(self.data if data is None else data), encoding="utf-8")
        return self.file

    def load(self, data=None):
        return load_runtime_configuration(self.write(data))

    def inspect(self, data=None):
        return inspect_runtime_configuration(self.load(data), base_directory=self.base)

    def resolve(self, data=None, *, environ=None):
        return resolve_runtime_configuration(
            self.load(data), base_directory=self.base,
            environ=self.environment if environ is None else environ,
        )

    def assert_code(self, suffix, operation, *args, **kwargs):
        with self.assertRaises(RuntimeConfigurationError) as caught:
            operation(*args, **kwargs)
        expected = PREFIX + suffix
        self.assertEqual(caught.exception.code, expected)
        self.assertEqual(str(caught.exception), expected)
        self.assertNotIn(str(self.directory), str(caught.exception))
        self.assertNotIn(API_KEY, str(caught.exception))


class RuntimeSchemaTests(_ConfigurationFixture, unittest.TestCase):
    def test_complete_sample_loads_without_creating_runtime_directories_or_databases(self):
        config = self.load()
        self.assertEqual(config.host, "127.0.0.1")
        self.assertFalse((self.directory / ".data").exists())
        self.assertNotIn(API_KEY, repr(config))

    def test_only_four_display_defaults_can_be_omitted(self):
        data = deepcopy(self.data)
        for field in ("schemaVersion", "host", "environment", "logLevel"):
            del data[field]
        config = self.load(data)
        self.assertEqual(config.host, "127.0.0.1")
        for field in ("orchestrator", "workspaceRoot", "agents", "model", "apiKeyEnv",
                      "llmLimits", "runtimeBudgetMs"):
            data = deepcopy(self.data)
            del data[field]
            with self.subTest(field=field):
                self.assert_code("INVALID", self.load, data)

    def test_all_four_agent_roles_are_required_and_unrecognized_roles_rejected(self):
        for role in AgentRole:
            data = deepcopy(self.data)
            del data["agents"][role.value]
            with self.subTest(role=role):
                self.assert_code("INVALID", self.load, data)
        data = deepcopy(self.data)
        data["agents"]["ORCHESTRATOR"] = data["agents"]["PLANNER"]
        self.assert_code("INVALID", self.load, data)

    def test_ports_and_persistent_paths_are_required_not_environment_defaults(self):
        for selector in ("orchestrator", *[role.value for role in AgentRole]):
            for field in ("port", "databasePath"):
                data = deepcopy(self.data)
                block = data["orchestrator"] if selector == "orchestrator" else data["agents"][selector]
                del block[field]
                with self.subTest(selector=selector, field=field):
                    self.assert_code("INVALID", self.load, data)

    def test_required_model_and_all_limit_fields_are_explicit(self):
        for block in ("model", "llmLimits"):
            for field in self.data[block]:
                if block == "model" and field in ("modelRevision", "seed"):
                    continue
                data = deepcopy(self.data)
                del data[block][field]
                with self.subTest(block=block, field=field):
                    self.assert_code("INVALID", self.load, data)

    def test_optional_model_revision_and_unsupported_seed_default_to_none(self):
        data = deepcopy(self.data)
        del data["model"]["modelRevision"]
        del data["model"]["seed"]
        config = self.load(data)
        self.assertIsNone(config.model.model_revision)
        self.assertIsNone(config.model.seed)

    def test_model_json_accepts_only_published_aliases_not_python_field_names(self):
        for public, private in (("modelId", "model_id"), ("modelRevision", "model_revision")):
            data = deepcopy(self.data)
            value = data["model"].pop(public)
            data["model"][private] = value
            with self.subTest(private=private):
                self.assert_code("INVALID", self.load, data)

    def test_closed_schema_rejects_unknown_keys_at_each_configuration_boundary(self):
        for selector in ("root", "orchestrator", "agents", "PLANNER", "model", "llmLimits"):
            data = deepcopy(self.data)
            target = (data if selector == "root" else data["agents"][selector]
                      if selector == "PLANNER" else data[selector])
            target["unapprovedCapability"] = "never log this submitted value"
            with self.subTest(selector=selector):
                self.assert_code("INVALID", self.load, data)

    def test_json_secret_literals_cannot_replace_named_environment_references(self):
        for selector, field in (("root", "apiKey"), ("root", "llmApiKey"),
                                ("model", "apiKey"), ("PLANNER", "bearerToken")):
            data = deepcopy(self.data)
            target = data if selector == "root" else data["agents"][selector] if selector == "PLANNER" else data[selector]
            target[field] = API_KEY
            with self.subTest(selector=selector, field=field):
                self.assert_code("INVALID", self.load, data)

    def test_only_loopback_hosts_are_accepted(self):
        for host in ("localhost", "::1"):
            self.assertEqual(self.load({**self.data, "host": host}).host, host)
        for host in ("0.0.0.0", "192.168.0.1", "public.example.invalid", "", "127.0.0.1\n", True):
            with self.subTest(host=host):
                self.assert_code("INVALID", self.load, {**self.data, "host": host})

    def test_top_level_integer_fields_reject_string_bool_fraction_and_wrong_ranges(self):
        for field, values in (("schemaVersion", (True, "1", 1.5, 2, None)),
                              ("runtimeBudgetMs", (True, "300000", .5, 0, -1, None))):
            for value in values:
                with self.subTest(field=field, value=value):
                    self.assert_code("INVALID", self.load, {**self.data, field: value})

    def test_port_numbers_are_strict_and_bounded_for_both_process_types(self):
        for selector in ("orchestrator", "QA"):
            for value in (True, "8000", 8000.5, 0, -1, 65536, None):
                data = deepcopy(self.data)
                target = data["orchestrator"] if selector == "orchestrator" else data["agents"][selector]
                target["port"] = value
                with self.subTest(selector=selector, value=value):
                    self.assert_code("INVALID", self.load, data)

    def test_integer_llm_limits_do_not_coerce_bool_string_or_fraction(self):
        for field in ("max_model_calls", "max_tool_calls", "max_output_tokens", "max_total_tokens", "max_json_bytes"):
            for value in (True, "20", 20.5):
                data = deepcopy(self.data)
                data["llmLimits"][field] = value
                with self.subTest(field=field, value=value):
                    self.assert_code("INVALID", self.load, data)

    def test_llm_timeout_and_temperature_are_finite_numeric_not_bool_or_string(self):
        for block, field in (("model", "temperature"), ("llmLimits", "model_timeout_seconds"),
                             ("llmLimits", "tool_timeout_seconds")):
            for value in (True, "1", float("nan"), float("inf"), -1):
                data = deepcopy(self.data)
                data[block][field] = value
                with self.subTest(block=block, field=field, value=value):
                    # The strict JSON loader rejects non-finite JSON first.
                    self.assert_code("FILE_INVALID" if isinstance(value, float) and not value < float("inf") else "INVALID",
                                     self.load, data)

    def test_provider_and_unsupported_seed_are_not_accepted_as_configuration_ready(self):
        for provider in ("unsupported", "", " openai ", True):
            data = deepcopy(self.data)
            data["model"]["provider"] = provider
            with self.subTest(provider=provider):
                self.assert_code("INVALID", self.load, data)
        for seed in (0, 42, True, "42"):
            data = deepcopy(self.data)
            data["model"]["seed"] = seed
            with self.subTest(seed=seed):
                self.assert_code("INVALID", self.load, data)

    def test_invalid_environment_names_are_not_shell_fragments(self):
        for value in ("", "contains space", "bad-key", "A2A_KEY\n", 7, None):
            with self.subTest(value=value):
                self.assert_code("INVALID", self.load, {**self.data, "apiKeyEnv": value})

    def test_persistent_paths_cannot_be_empty_memory_or_contain_controls(self):
        for selector in ("workspaceRoot", "orchestrator", "DEVELOPER"):
            for value in ("", ".", ":memory:", "bad\npath", True, None):
                data = deepcopy(self.data)
                if selector == "workspaceRoot":
                    data[selector] = value
                else:
                    block = data["orchestrator"] if selector == "orchestrator" else data["agents"][selector]
                    block["databasePath"] = value
                with self.subTest(selector=selector, value=value):
                    self.assert_code("INVALID", self.load, data)

    def test_config_is_frozen_and_agent_mapping_cannot_be_changed_in_place(self):
        config = self.load()
        with self.assertRaises((ValueError, TypeError, AttributeError)):
            config.host = "localhost"
        with self.assertRaises(TypeError):
            config.agents.by_role[AgentRole.PLANNER] = config.agents.by_role[AgentRole.DEVELOPER]

    def test_nested_approved_model_limits_and_agent_fields_are_frozen(self):
        config = self.load()
        for target, field, value in ((config.agents.planner, "port", 9999),
                                      (config.model, "temperature", 1),
                                      (config.llm_limits, "max_tool_calls", 999)):
            with self.subTest(field=field), self.assertRaises((ValueError, TypeError, AttributeError)):
                setattr(target, field, value)

    def test_production_configuration_requires_each_role_authentication_reference(self):
        data = deepcopy(self.data)
        data["environment"] = "production"
        self.load(data)
        data["agents"]["QA"]["bearerTokenEnv"] = None
        self.assert_code("INVALID", self.load, data)


class RuntimeLayoutTests(_ConfigurationFixture, unittest.TestCase):
    def test_paths_resolve_relative_to_config_directory_without_making_runtime_files(self):
        layout = self.inspect()
        self.assertEqual(layout.orchestrator_database_path, self.directory / ".data/orchestrator.sqlite3")
        self.assertEqual(layout.workspace_root, self.directory / ".data/workspaces")
        for index, role in enumerate(AgentRole):
            self.assertEqual(layout.agent_database_paths[role],
                             self.directory / f".data/agents/{role.value.lower()}.sqlite3")
            self.assertEqual(layout.agent_urls[role], f"http://127.0.0.1:{8101 + index}")
        self.assertFalse((self.directory / ".data").exists())

    def test_absolute_approved_paths_do_not_depend_on_base_directory(self):
        data = deepcopy(self.data)
        data["orchestrator"]["databasePath"] = str(self.directory / "absolute/orchestrator.sqlite3")
        data["workspaceRoot"] = str(self.directory / "absolute/workspaces")
        for role in AgentRole:
            data["agents"][role.value]["databasePath"] = str(self.directory / f"absolute/agents/{role.value.lower()}.sqlite3")
        layout = self.inspect(data)
        self.assertEqual(layout.workspace_root, self.directory / "absolute/workspaces")
        self.assertFalse((self.directory / "absolute").exists())

    def test_ipv6_agent_urls_use_brackets(self):
        layout = self.inspect({**self.data, "host": "::1"})
        self.assertEqual(layout.agent_urls[AgentRole.PLANNER], "http://[::1]:8101")

    def test_duplicate_agent_or_orchestrator_ports_are_rejected(self):
        for selector, port in (("QA", 8101), ("SECURITY", 8000)):
            data = deepcopy(self.data)
            data["agents"][selector]["port"] = port
            with self.subTest(selector=selector):
                self.assert_code("INVALID", self.inspect, data)

    def test_database_path_aliases_are_rejected_before_creation(self):
        for value in ("../.data/orchestrator.sqlite3", "../.data/agents/../orchestrator.sqlite3",
                      "../.data/agents/planner.sqlite3"):
            data = deepcopy(self.data)
            data["agents"]["QA"]["databasePath"] = value
            with self.subTest(path=value):
                self.assert_code("STORAGE_CONFLICT", self.inspect, data)
        self.assertFalse((self.directory / ".data").exists())

    def test_database_and_workspace_ancestor_collisions_are_rejected(self):
        variants = (
            {"workspaceRoot": "../.data"},
            {"workspaceRoot": "../.data/orchestrator.sqlite3/children"},
        )
        for changes in variants:
            with self.subTest(changes=changes):
                self.assert_code("STORAGE_CONFLICT", self.inspect, {**self.data, **changes})
        data = deepcopy(self.data)
        data["agents"]["QA"]["databasePath"] = "../.data/workspaces/qa.sqlite3"
        self.assert_code("STORAGE_CONFLICT", self.inspect, data)
        data["agents"]["QA"]["databasePath"] = "../.data/agents/planner.sqlite3/qa.sqlite3"
        self.assert_code("STORAGE_CONFLICT", self.inspect, data)

    @unittest.skipUnless(hasattr(os, "symlink"), "symlink API required")
    def test_symlink_directory_alias_cannot_hide_shared_agent_database(self):
        real = self.directory / "existing"
        real.mkdir()
        alias = self.directory / "existing-alias"
        alias.symlink_to(real, target_is_directory=True)
        data = deepcopy(self.data)
        data["agents"]["PLANNER"]["databasePath"] = str(real / "same.sqlite3")
        data["agents"]["QA"]["databasePath"] = str(alias / "same.sqlite3")
        self.assert_code("STORAGE_CONFLICT", self.inspect, data)

    @unittest.skipUnless(hasattr(os, "link"), "hardlink API required")
    def test_hardlinked_existing_database_files_cannot_share_identity(self):
        first = self.directory / "first.sqlite3"
        second = self.directory / "second.sqlite3"
        first.write_bytes(b"not opened as a database")
        os.link(first, second)
        data = deepcopy(self.data)
        data["agents"]["PLANNER"]["databasePath"] = str(first)
        data["agents"]["QA"]["databasePath"] = str(second)
        self.assert_code("STORAGE_CONFLICT", self.inspect, data)

    def test_layout_agent_paths_and_urls_are_read_only_mappings(self):
        layout = self.inspect()
        with self.assertRaises(TypeError):
            layout.agent_database_paths[AgentRole.PLANNER] = self.directory / "changed.db"
        with self.assertRaises(TypeError):
            layout.agent_urls[AgentRole.PLANNER] = "http://public.example.invalid"

    def test_frozen_model_copy_is_not_an_admission_proof(self):
        config = self.load().model_copy(update={"host": "0.0.0.0"})
        self.assert_code("INVALID", inspect_runtime_configuration, config, base_directory=self.base)
        self.assert_code("INVALID", resolve_runtime_configuration, config,
                         base_directory=self.base, environ=self.environment)

    def test_existing_directory_as_database_and_file_as_workspace_are_denied(self):
        folder = self.directory / "not-a-database"
        folder.mkdir()
        data = deepcopy(self.data)
        data["orchestrator"]["databasePath"] = str(folder)
        self.assert_code("STORAGE_CONFLICT", self.inspect, data)
        file = self.directory / "not-a-workspace"
        file.write_bytes(b"fixture")
        self.assert_code("STORAGE_CONFLICT", self.inspect, {**self.data, "workspaceRoot": str(file)})

    def test_workspace_with_existing_regular_file_ancestor_is_denied(self):
        parent = self.directory / "existing-regular-file"
        parent.write_bytes(b"not a directory; never execute or import")
        self.assert_code("STORAGE_CONFLICT", self.inspect, {
            **self.data, "workspaceRoot": str(parent / "cannot-exist"),
        })

    def test_sqlite_sidecar_names_are_reserved_against_other_agent_databases(self):
        for suffix in ("-wal", "-shm", "-journal"):
            data = deepcopy(self.data)
            data["agents"]["QA"]["databasePath"] = data["agents"]["PLANNER"]["databasePath"] + suffix
            with self.subTest(suffix=suffix):
                self.assert_code("STORAGE_CONFLICT", self.inspect, data)
        self.assertFalse((self.directory / ".data").exists())

    def test_nonexistent_sqlite_sidecar_names_cannot_be_workspace_roots(self):
        for suffix in ("-wal", "-shm", "-journal"):
            with self.subTest(suffix=suffix):
                self.assert_code("STORAGE_CONFLICT", self.inspect, {
                    **self.data, "workspaceRoot": self.data["orchestrator"]["databasePath"] + suffix,
                })
        self.assertFalse((self.directory / ".data").exists())

    def test_sqlite_sidecar_ancestor_names_cannot_contain_another_database_or_workspace(self):
        for suffix in ("-wal", "-shm", "-journal"):
            sidecar = self.data["orchestrator"]["databasePath"] + suffix
            data = deepcopy(self.data)
            data["agents"]["QA"]["databasePath"] = sidecar + "/qa.sqlite3"
            with self.subTest(suffix=suffix, collision="database"):
                self.assert_code("STORAGE_CONFLICT", self.inspect, data)
            with self.subTest(suffix=suffix, collision="workspace"):
                self.assert_code("STORAGE_CONFLICT", self.inspect, {**self.data, "workspaceRoot": sidecar + "/workspace"})

    @unittest.skipUnless(hasattr(os, "symlink"), "symlink API required")
    def test_existing_sqlite_sidecar_symlink_cannot_alias_another_agent_database(self):
        for index, suffix in enumerate(("-wal", "-shm", "-journal")):
            parent = self.directory / f"sidecar-symlink-{index}"
            parent.mkdir()
            planner, qa = parent / "planner.sqlite3", parent / "qa.sqlite3"
            qa.write_bytes(b"fixture; not opened as SQLite")
            Path(str(planner) + suffix).symlink_to(qa)
            data = deepcopy(self.data)
            data["agents"]["PLANNER"]["databasePath"] = str(planner)
            data["agents"]["QA"]["databasePath"] = str(qa)
            with self.subTest(suffix=suffix):
                self.assert_code("STORAGE_CONFLICT", self.inspect, data)

    @unittest.skipUnless(hasattr(os, "link"), "hardlink API required")
    def test_existing_sqlite_sidecar_hardlink_cannot_alias_another_agent_database(self):
        for index, suffix in enumerate(("-wal", "-shm", "-journal")):
            parent = self.directory / f"sidecar-hardlink-{index}"
            parent.mkdir()
            planner, qa = parent / "planner.sqlite3", parent / "qa.sqlite3"
            qa.write_bytes(b"fixture; not opened as SQLite")
            os.link(qa, Path(str(planner) + suffix))
            data = deepcopy(self.data)
            data["agents"]["PLANNER"]["databasePath"] = str(planner)
            data["agents"]["QA"]["databasePath"] = str(qa)
            with self.subTest(suffix=suffix):
                self.assert_code("STORAGE_CONFLICT", self.inspect, data)

    def test_nonexistent_casefold_or_unicode_equivalent_database_names_are_conservatively_denied(self):
        for first, second in (("PLANNER.sqlite3", "planner.sqlite3"),
                              ("caf\u00e9.sqlite3", "cafe\u0301.sqlite3")):
            data = deepcopy(self.data)
            data["agents"]["PLANNER"]["databasePath"] = "../.data/agents/" + first
            data["agents"]["QA"]["databasePath"] = "../.data/agents/" + second
            with self.subTest(first=first, second=second):
                self.assert_code("STORAGE_CONFLICT", self.inspect, data)
        self.assertFalse((self.directory / ".data").exists())

    def test_casefold_parent_aliases_do_not_hide_database_or_sidecar_collisions(self):
        for value in ("../.data/AGENTS/PLANNER.sqlite3", "../.data/AGENTS/PLANNER.sqlite3-WAL"):
            data = deepcopy(self.data)
            data["agents"]["QA"]["databasePath"] = value
            with self.subTest(path=value):
                self.assert_code("STORAGE_CONFLICT", self.inspect, data)

    def test_casefold_ancestor_aliases_do_not_put_platform_database_inside_workspace(self):
        self.assert_code("STORAGE_CONFLICT", self.inspect, {
            **self.data, "workspaceRoot": "../.DATA/AGENTS",
        })
        self.assert_code("STORAGE_CONFLICT", self.inspect, {
            **self.data, "workspaceRoot": "../.DATA/ORCHESTRATOR.SQLITE3-WAL/children",
        })


class RuntimeSecretTests(_ConfigurationFixture, unittest.TestCase):
    def test_resolver_injects_shared_model_limits_and_correct_per_role_credentials(self):
        resolved = self.resolve()
        self.assertEqual(resolved.orchestrator_host, "127.0.0.1")
        self.assertEqual(resolved.orchestrator_port, 8000)
        self.assertEqual(resolved.runtime_budget_ms, 300_000)
        self.assertEqual(resolved.model.model_id, "fixture-model")
        self.assertEqual(resolved.limits.max_model_calls, 8)
        for role in AgentRole:
            settings = resolved.agent_settings[role]
            self.assertIs(settings.role, role)
            self.assertEqual(settings.llm_api_key.get_secret_value(), API_KEY)
            self.assertEqual(settings.bearer_token.get_secret_value(), self.environment[f"A2A_PLATFORM_{role.value}_TOKEN"])
            self.assertEqual(settings.llm_model_id, "fixture-model")
            self.assertEqual(settings.llm_limits, resolved.limits)
            self.assertEqual(getattr(resolved.orchestrator_settings, f"{role.value.lower()}_agent_url"), settings.agent_base_url)
            self.assertEqual(getattr(resolved.orchestrator_settings, f"{role.value.lower()}_bearer_token").get_secret_value(),
                             settings.bearer_token.get_secret_value())
        for secret in self.environment.values():
            self.assertNotIn(secret, repr(resolved))
            self.assertNotIn(secret, repr(resolved.agent_settings))

    def test_resolved_settings_keep_exact_existing_platform_contract_types(self):
        resolved = self.resolve()
        self.assertIs(type(resolved.orchestrator_settings), Settings)
        self.assertIs(type(resolved.model), ModelConfiguration)
        self.assertIs(type(resolved.limits), LLMLimits)
        for settings in resolved.agent_settings.values():
            self.assertIs(type(settings), AgentSettings)
            self.assertIs(type(settings.llm_limits), LLMLimits)

    def test_explicit_environ_mapping_does_not_fall_back_to_process_credentials(self):
        with patch.dict(os.environ, self.environment, clear=True):
            self.assert_code("SECRET_MISSING", self.resolve, environ={})

    def test_only_named_process_environment_values_are_resolved(self):
        config = self.load()
        with patch.dict(os.environ, self.environment, clear=True):
            resolved = resolve_runtime_configuration(config, base_directory=self.base)
        self.assertEqual(resolved.agent_settings[AgentRole.PLANNER].llm_api_key.get_secret_value(), API_KEY)

    def test_missing_named_api_key_or_role_token_is_a_safe_missing_secret_error(self):
        for name in self.environment:
            environment = dict(self.environment)
            del environment[name]
            with self.subTest(name=name):
                self.assert_code("SECRET_MISSING", self.resolve, environ=environment)

    def test_empty_or_whitespace_credentials_are_rejected_without_printing_values(self):
        for name in (API_KEY_ENV, "A2A_PLATFORM_QA_TOKEN"):
            for value in ("", " ", "\t\n"):
                environment = {**self.environment, name: value}
                with self.subTest(name=name, value=value):
                    self.assert_code("SECRET_MISSING", self.resolve, environ=environment)

    def test_nonempty_credentials_with_controls_whitespace_or_wrong_type_are_invalid(self):
        for name in (API_KEY_ENV, "A2A_PLATFORM_QA_TOKEN"):
            for value in (" leading", "trailing ", "prefix\nsecret", "prefix\x00secret",
                          "prefix\x7fsecret", "prefix\x85secret", 1, True,
                          "x" * 8193):
                environment = {**self.environment, name: value}
                with self.subTest(name=name, value_type=type(value).__name__):
                    self.assert_code("SECRET_INVALID", self.resolve, environ=environment)

    def test_bearer_token_environment_reference_is_optional_not_an_implicit_default(self):
        for representation in ("omitted", "null"):
            data = deepcopy(self.data)
            for role in AgentRole:
                if representation == "omitted":
                    del data["agents"][role.value]["bearerTokenEnv"]
                else:
                    data["agents"][role.value]["bearerTokenEnv"] = None
            with self.subTest(representation=representation):
                resolved = self.resolve(data, environ={API_KEY_ENV: API_KEY})
                for role in AgentRole:
                    self.assertIsNone(resolved.agent_settings[role].bearer_token)
                    self.assertIsNone(getattr(resolved.orchestrator_settings, f"{role.value.lower()}_bearer_token"))

    def test_global_settings_prefixes_cannot_poison_explicit_runtime_settings(self):
        poison = {
            "AGENT_ROLE": "SECURITY", "AGENT_HOST": "localhost", "AGENT_PORT": "9999",
            "AGENT_DATABASE_PATH": "/tmp/unapproved-agent.sqlite3", "AGENT_BEARER_TOKEN": "poison-agent-token",
            "AGENT_LLM_PROVIDER": "unsupported", "AGENT_LLM_MODEL_ID": "poison-model",
            "AGENT_LLM_API_KEY": "poison-api-key", "AGENT_LLM_TEMPERATURE": "1",
            "AGENT_LLM_SEED": "42", "AGENT_LLM_MODEL_REVISION": "poison-revision",
            "ORCHESTRATOR_DATABASE_PATH": "/tmp/unapproved-orchestrator.sqlite3",
            "ORCHESTRATOR_WORKSPACE_ROOT": "/tmp/unapproved-workspaces",
            "ORCHESTRATOR_PLANNER_AGENT_URL": "http://public.example.invalid",
            "ORCHESTRATOR_PLANNER_BEARER_TOKEN": "poison-orchestrator-token",
        }
        with patch.dict(os.environ, poison, clear=True):
            resolved = self.resolve()
        planner = resolved.agent_settings[AgentRole.PLANNER]
        self.assertIs(planner.role, AgentRole.PLANNER)
        self.assertEqual(planner.listen_port, 8101)
        self.assertEqual(planner.host, "127.0.0.1")
        self.assertIsNone(planner.llm_seed)
        self.assertIsNone(planner.llm_model_revision)
        self.assertEqual(planner.llm_temperature, 0)
        self.assertEqual(planner.task_database_path, self.directory / ".data/agents/planner.sqlite3")
        self.assertEqual(Path(resolved.orchestrator_settings.database_path), self.directory / ".data/orchestrator.sqlite3")
        self.assertEqual(Path(resolved.orchestrator_settings.workspace_root), self.directory / ".data/workspaces")

    def test_poisoned_environment_json_cannot_be_parsed_before_explicit_limit_settings(self):
        with patch.dict(os.environ, {"AGENT_LLM_LIMITS": "bad-json-secret-source"}, clear=True):
            resolved = self.resolve()
        self.assertEqual(resolved.limits.max_model_calls, 8)
        self.assertEqual(resolved.limits.max_tool_calls, 20)
        for settings in resolved.agent_settings.values():
            self.assertEqual(settings.llm_limits, resolved.limits)
            self.assertEqual(settings.llm_api_key.get_secret_value(), API_KEY)

    def test_dotenv_cannot_supply_missing_named_credentials(self):
        (self.base / ".env").write_text(f"{API_KEY_ENV}={API_KEY}\nAGENT_LLM_API_KEY=poison\n", encoding="utf-8")
        self.assert_code("SECRET_MISSING", self.resolve, environ={})

    def test_resolved_agent_settings_mapping_is_read_only(self):
        resolved = self.resolve()
        with self.assertRaises(TypeError):
            resolved.agent_settings[AgentRole.PLANNER] = resolved.agent_settings[AgentRole.SECURITY]

    def test_inspection_and_resolution_never_create_databases_or_listening_runtime(self):
        config = self.load()
        with patch("orchestrator.infrastructure.SQLiteWorkflowRepository", side_effect=AssertionError("unexpected DB")), \
             patch("agents.platform.composition.create_platform", side_effect=AssertionError("unexpected platform")):
            inspect_runtime_configuration(config, base_directory=self.base)
            resolve_runtime_configuration(config, base_directory=self.base, environ=self.environment)
        self.assertFalse((self.directory / ".data").exists())


class RuntimeConfigurationFileTests(_ConfigurationFixture, unittest.TestCase):
    def test_missing_directory_and_invalid_utf8_files_are_safe_file_errors(self):
        self.assert_code("FILE_INVALID", load_runtime_configuration, self.file)
        self.assert_code("FILE_INVALID", load_runtime_configuration, self.base)
        self.file.write_bytes(b"\xff\xfe")
        self.assert_code("FILE_INVALID", load_runtime_configuration, self.file)

    def test_malformed_duplicate_keys_and_nonfinite_json_are_rejected(self):
        values = ("{", "[]", "null", '{"schemaVersion":1,"schemaVersion":1}',
                  '{"private-secret":NaN}', '{"private-secret":Infinity}', '{"private-secret":1e9999}')
        for value in values:
            self.file.write_text(value, encoding="utf-8")
            with self.subTest(value=value):
                self.assert_code("INVALID" if value in ("[]", "null") else "FILE_INVALID",
                                 load_runtime_configuration, self.file)

    def test_oversized_file_is_bounded_before_parsing(self):
        self.file.write_bytes(b" " * (9 * 1024 * 1024))
        self.assert_code("FILE_INVALID", load_runtime_configuration, self.file)

    @unittest.skipUnless(hasattr(os, "symlink"), "symlink API required")
    def test_configuration_file_symlink_is_rejected_without_following_source(self):
        self.write()
        alias = self.base / "unapproved-alias.json"
        alias.symlink_to(self.file)
        self.assert_code("FILE_INVALID", load_runtime_configuration, alias)

    @unittest.skipUnless(hasattr(os, "mkfifo"), "FIFO API required")
    def test_named_pipe_is_rejected_without_blocking_open(self):
        os.mkfifo(self.file)
        self.assert_code("FILE_INVALID", load_runtime_configuration, self.file)


class RuntimeConfigurationCLITests(_ConfigurationFixture, unittest.TestCase):
    def invoke(self, arguments, *, environ=None):
        stdout, stderr = io.StringIO(), io.StringIO()
        with patch.dict(os.environ, {} if environ is None else environ, clear=True), \
             redirect_stdout(stdout), redirect_stderr(stderr):
            status = check_configuration(arguments)
        return status, stdout.getvalue(), stderr.getvalue()

    def test_cli_inspects_configuration_without_credentials_or_runtime_startup(self):
        status, stdout, stderr = self.invoke(["--file", str(self.write())])
        self.assertEqual(status, 0)
        self.assertEqual(json.loads(stdout), {
            "status": "RUNTIME_CONFIGURATION_VALID", "credentialCheck": "NOT_REQUESTED", "executionReady": False,
        })
        self.assertEqual(stderr, "")
        self.assertFalse((self.directory / ".data").exists())

    def test_cli_checks_secret_presence_but_does_not_claim_execution_ready(self):
        status, stdout, stderr = self.invoke(["--file", str(self.write()), "--check-secrets"], environ=self.environment)
        self.assertEqual(status, 0)
        body = json.loads(stdout)
        self.assertEqual(body["credentialCheck"], "PRESENT")
        self.assertIs(body["executionReady"], False)
        self.assertEqual(stderr, "")
        for secret in self.environment.values():
            self.assertNotIn(secret, stdout + stderr)
        self.assertNotIn(str(self.directory), stdout + stderr)

    def test_cli_missing_secret_has_no_json_or_credential_value_leakage(self):
        status, stdout, stderr = self.invoke(["--file", str(self.write()), "--check-secrets"])
        self.assertEqual((status, stdout, stderr.strip()), (1, "", PREFIX + "SECRET_MISSING"))
        self.assertNotIn(str(self.directory), stderr)

    def test_cli_invalid_schema_has_only_safe_code_no_submitted_source_or_path(self):
        data = {**self.data, "privateSecret": API_KEY}
        status, stdout, stderr = self.invoke(["--file", str(self.write(data))])
        self.assertEqual((status, stdout, stderr.strip()), (1, "", PREFIX + "INVALID"))
        self.assertNotIn(API_KEY, stderr)
        self.assertNotIn(str(self.directory), stderr)

    def test_cli_missing_file_and_malformed_arguments_are_stable_non_echoing_errors(self):
        status, stdout, stderr = self.invoke(["--file", str(self.file)])
        self.assertEqual((status, stdout, stderr.strip()), (1, "", PREFIX + "FILE_INVALID"))
        for arguments in ([], ["--file"], ["--private-secret", API_KEY], ["--file", str(self.file), "extra-secret"]):
            with self.subTest(arguments=arguments):
                status, stdout, stderr = self.invoke(arguments)
                self.assertEqual((status, stdout, stderr.strip()), (1, "", PREFIX + "INVALID"))
                self.assertNotIn(API_KEY, stderr)

    def test_real_module_cli_is_cwd_independent_and_offline(self):
        file = self.write()
        source = Path(__file__).resolve().parents[1] / "src"
        env = {**os.environ, "PYTHONPATH": str(source)}
        for name in (*self.environment, "OPENAI_API_KEY"):
            env.pop(name, None)
        result = subprocess.run(
            [sys.executable, "-m", "agents.platform.check_configuration", "--file", str(file)],
            cwd=self.directory, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, timeout=20, check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["credentialCheck"], "NOT_REQUESTED")
        self.assertEqual(result.stderr, "")
        self.assertFalse((self.directory / ".data").exists())


if __name__ == "__main__":
    unittest.main()
