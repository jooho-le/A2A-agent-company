"""Observe before provider submission; durable hash-only data, strict failure boundaries."""
import json
import unittest
from dataclasses import replace
from unittest.mock import Mock

from agents.llm.contracts import LLMRuntimeError
from agents.llm.engine import LLMEngine
from agents.llm.input_evidence import ModelInputEvidence, json_sha256, observe_input
from agents.platform.telemetry_store import TelemetryStoreError
import test_llm_runtime_telemetry as fixtures
import test_llm_runtime as helpers


class ModelInputEvidenceTests(unittest.IsolatedAsyncioTestCase):
    setUp = fixtures.LLMRuntimeTelemetryTests.setUp
    start = fixtures.LLMRuntimeTelemetryTests.start
    usage = fixtures.LLMRuntimeTelemetryTests.usage
    async def observed_run(self, provider, sink):
        return await LLMEngine(role=self.role, provider=provider).run(
            prompt=self.prompt, model=self.model, output=self.output, budget=self.budget,
            call_started_sink=self.start, usage_sink=self.usage, input_observed_sink=sink)

    def persist(self, data):
        self.store.append_model_input(self.fixture.event('MODEL_INPUT_OBSERVED'), data, self.model)

    async def test_hash_observation_precedes_provider_and_excludes_raw_text(self):
        observed = []
        def sink(data):
            observed.append(data)
            self.persist(data)
        def response(request):
            assert len(observed) == 1
            initial = json.loads(json.loads(request.input_items_json)[0]['content'])
            assert observed[0]['metadataSha256'] == json_sha256(initial['metadata'])
            assert observed[0]['taskInputSha256'] == json_sha256(initial['taskInput'])
            return helpers.text_response()
        await self.observed_run(helpers.FakeProvider(response), sink)
        rows, _ = self.store.list_events(self.fixture.run.run_id)
        row = next(r for r in rows if r.get('binding', {}).get('eventType') == 'MODEL_INPUT_OBSERVED')
        assert '회원가입 계획' not in json.dumps(row, ensure_ascii=False)
        with self.assertRaises(TelemetryStoreError):
            self.persist({**observed[0], 'prompt': 'never store this'})
        with self.assertRaises(TelemetryStoreError):
            self.persist({**observed[0], 'sequence': 999})

    async def test_observation_failure_blocks_provider_and_invalidates_budget(self):
        provider = helpers.FakeProvider(helpers.text_response())
        sink = Mock(side_effect=RuntimeError('private details'))
        with self.assertRaises(LLMRuntimeError):
            await self.observed_run(provider, sink)
        assert not provider.requests
        assert self.budget.snapshot.invalidated is True

    async def test_host_feedback_preserves_original_input_but_requirement_changes_do_not(self):
        provider = helpers.FakeProvider(helpers.text_response())
        await self.observed_run(provider, lambda data: None)
        request = provider.requests[0]
        original = observe_input(1, request)

        def changed_request(key, value):
            items = json.loads(request.input_items_json)
            initial = json.loads(items[0]['content'])
            initial['taskInput'][key] = value
            items[0]['content'] = json.dumps(initial)
            return replace(request, input_items_json=json.dumps(items))

        for key in ('semanticReviewFeedback', 'testRunFeedback'):
            changed = observe_input(2, changed_request(key, {'reasonCode': 'TEST_REASON'}))
            self.assertEqual(changed['taskInputSha256'], original['taskInputSha256'])
            self.assertEqual(changed['initialInputSha256'], original['initialInputSha256'])
            self.assertNotEqual(changed['inputItemsSha256'], original['inputItemsSha256'])
            self.assertEqual(set(changed['hostFollowUpSha256']), {key})
        changed = observe_input(2, changed_request('requirements', ['A different requirement']))
        self.assertNotEqual(changed['taskInputSha256'], original['taskInputSha256'])
        self.assertNotEqual(changed['initialInputSha256'], original['initialInputSha256'])
        with self.assertRaises(ValueError):
            ModelInputEvidence.model_validate({**original, 'hostFollowUpSha256': {'requirements': 'a' * 64}})
