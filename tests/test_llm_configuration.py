"""Step 19 operator configuration: no live LLM request or secret loading."""

import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from pydantic import ValidationError

from agents.core.config import AgentSettings
from agents.llm.budget import LLMLimits
from agents.llm.configuration import model_from_settings, provider_from_settings
from agents.llm.contracts import LLMErrorCode, LLMRuntimeError
from orchestrator.domain.run_configuration import ModelConfiguration


class LLMConfigurationTests(unittest.TestCase):
    def setUp(self):
        environment = patch.dict(os.environ, {}, clear=True)
        environment.start()
        self.addCleanup(environment.stop)

    def settings(self, **overrides):
        return AgentSettings(role="PLANNER", _env_file=None, **overrides)

    def configured(self, **overrides):
        fields = dict(llm_provider="openai", llm_model_id="operator-selected-model", llm_temperature=0.2)
        fields.update(overrides)
        return self.settings(**fields)

    def test_selection_and_temperature_have_no_default(self):
        settings = self.settings()
        self.assertIsNone(settings.llm_model_revision)
        self.assertIsNone(settings.llm_temperature)
        self.assertIsNone(settings.llm_seed)
        with self.assertRaises(LLMRuntimeError) as raised:
            model_from_settings(settings)
        self.assertEqual(raised.exception.code, LLMErrorCode.CONFIGURATION)
        self.assertEqual(settings.llm_limits, LLMLimits())

    def test_temperature_required_without_silent_fallback(self):
        with self.assertRaises(LLMRuntimeError):
            model_from_settings(self.configured(llm_temperature=None))

    def test_model_round_trip_reuses_the_existing_frozen_contract(self):
        settings = self.configured(llm_model_revision="pinned-model", llm_seed=42)
        selected = model_from_settings(settings)
        self.assertIsInstance(selected, ModelConfiguration)
        self.assertEqual(selected.model_revision, "pinned-model")
        self.assertEqual(selected.temperature, 0.2)
        self.assertEqual(selected.seed, 42)
        self.assertEqual(model_from_settings(settings, frozen_model=selected), selected)
        for update in (
            {"provider": "other"}, {"model_id": "other"}, {"model_revision": None},
            {"temperature": 0.3}, {"seed": None},
        ):
            with self.subTest(update=update), self.assertRaises(LLMRuntimeError):
                model_from_settings(settings, frozen_model=selected.model_copy(update=update))

    def test_invalid_temperature_revision_or_limits_rejected(self):
        for update in (
            {"llm_model_revision": " "}, {"llm_temperature": -1},
            {"llm_temperature": float("nan")}, {"llm_temperature": float("inf")},
            {"llm_limits": {"max_model_calls": 0}},
            {"llm_limits": {"max_tool_calls": -1}},
            {"llm_limits": {"max_output_tokens": 15}},
            {"llm_limits": {"max_total_tokens": True}},
            {"llm_limits": {"model_timeout_seconds": float("inf")}},
            {"llm_limits": {"unexpected": 123}},
        ):
            with self.subTest(update=update), self.assertRaises(ValidationError):
                self.configured(**update)

    def test_env_json_limits_and_model_fields_can_be_explicitly_set(self):
        with patch.dict(os.environ, {
            "AGENT_LLM_PROVIDER": "openai", "AGENT_LLM_MODEL_ID": "selected-model",
            "AGENT_LLM_MODEL_REVISION": "selected-model-pinned", "AGENT_LLM_TEMPERATURE": "0.4",
            "AGENT_LLM_SEED": "123", "AGENT_LLM_LIMITS": '{"max_model_calls":3,"max_total_tokens":4000}',
        }):
            settings = self.settings()
        self.assertEqual(settings.llm_limits.max_model_calls, 3)
        self.assertEqual(settings.llm_limits.max_total_tokens, 4000)
        self.assertEqual(model_from_settings(settings).seed, 123)

    def test_missing_unsupported_or_blank_auth_is_not_fake_success(self):
        for settings in (self.configured(), self.configured(llm_provider="not-implemented")):
            with self.subTest(provider=settings.llm_provider), self.assertRaises(LLMRuntimeError):
                provider_from_settings(settings)
        with self.assertRaises(ValidationError):
            self.configured(llm_api_key=" ")

    def test_credentials_are_not_exported_with_new_settings(self):
        settings = self.configured(llm_api_key="SYNTHETIC_TEST_KEY_ONLY")
        for rendered in (repr(settings), str(settings), settings.model_dump_json(), repr(settings.model_dump())):
            self.assertNotIn("SYNTHETIC_TEST_KEY_ONLY", rendered)
        self.assertNotIn("llm_api_key", settings.model_dump())

    def test_import_does_not_create_files_start_servers_or_open_network(self):
        root = Path(__file__).resolve().parents[1]
        script = """
from unittest.mock import patch
from pathlib import Path
with patch('socket.socket', side_effect=AssertionError('network opened')), patch('subprocess.Popen', side_effect=AssertionError('subprocess started')):
    import agents.llm.contracts
    import agents.llm.budget
    import agents.llm.configuration
    import agents.llm.engine
    import agents.llm.openai_responses
assert list(Path('.').iterdir()) == []
"""
        with tempfile.TemporaryDirectory() as directory:
            environment = dict(os.environ, PYTHONPATH=str(root / "src"))
            result = subprocess.run([sys.executable, "-c", script], cwd=directory, env=environment, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
