"""72: equivalent comparisons and fewer diagnostic-only evaluations."""

import asyncio
from collections import Counter
from copy import deepcopy
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import httpx
from a2a.client.card_resolver import A2ACardResolver
from pydantic import SecretStr

from diagnostics.communication import contracts
from diagnostics.communication.contracts import CheckOptions, ProbeRecord, differences, summarize, summarize_views
from diagnostics.communication.lab import CommunicationLab, running_lab
from diagnostics.communication.live import LiveRuntime
import test_communication_delivery as delivery_fixtures
import test_communication_live as live_fixtures


def nodes(value):
    children = value.values() if isinstance(value, dict) else value if isinstance(value, list) else ()
    return 1 + sum(nodes(child) for child in children)


class ComparisonCostTests(unittest.TestCase):
    def test_nested_json_is_normalized_once_per_input_node(self):
        value = {'payload': [None, False, 0, '한글', 100.0]}
        for _ in range(25):
            value = {'level': value, 'labels': ['a', 'b']}
        original = contracts.normalized
        with patch.object(contracts, 'normalized', wraps=original) as calls:
            result = differences(value, deepcopy(value))
        self.assertEqual(result, [])
        self.assertLessEqual(calls.call_count, 2 * nodes(value))

    def test_all_difference_kinds_order_types_and_escaped_paths_are_preserved(self):
        expected = {'a/b~': {'v': False}, 'list': [1, 2], 'gone': None, 'sameNumber': 100}
        actual = {'a/b~': {'v': 0}, 'list': [1, 3, 4], 'added': 'x', 'sameNumber': 100.0}
        before_expected, before_actual = deepcopy(expected), deepcopy(actual)
        self.assertEqual(differences(expected, actual), [
            {'path': '$/a~1b~0/v', 'kind': 'TYPE_CHANGED', 'expected': False, 'actual': 0},
            {'path': '$/added', 'kind': 'ADDED', 'actual': 'x'},
            {'path': '$/gone', 'kind': 'MISSING', 'expected': None},
            {'path': '$/list', 'kind': 'LENGTH_CHANGED', 'expected': 2, 'actual': 3},
            {'path': '$/list/1', 'kind': 'VALUE_CHANGED', 'expected': 2, 'actual': 3}])
        self.assertEqual((expected, actual), (before_expected, before_actual))

    def test_view_does_not_recompute_the_same_transfer_checks(self):
        row = ProbeRecord('PLANNER', 1, 0)
        with patch.object(contracts, 'compare', wraps=contracts.compare) as compare:
            view = row.view()
        self.assertEqual(compare.call_count, 8)
        self.assertEqual(view['checks']['response']['status'], 'UNVERIFIED')

    def test_next_view_detects_mutation_instead_of_reusing_old_hashes(self):
        row = delivery_fixtures.observed_row()
        row.wire_received = deepcopy(row.wire_received)
        first = row.view()
        self.assertEqual(first['checks']['request']['network']['status'], 'PASS')
        row.wire_received['sample']['value'] = 101
        second = row.view()
        self.assertEqual(second['checks']['request']['network']['status'], 'FAIL')
        self.assertEqual(second['checks']['request']['admission']['status'], 'FAIL')
        self.assertNotEqual(first['checks']['request']['network']['actualHash'], second['checks']['request']['network']['actualHash'])

    def test_precomputed_summary_matches_public_summary_without_changing_views(self):
        rows = [delivery_fixtures.observed_row(), ProbeRecord('SECURITY', 1, 1)]
        views = [row.view() for row in rows]
        original = deepcopy(views)
        self.assertEqual(summarize_views(views), summarize(rows))
        self.assertEqual(views, original)


class ExecutionOwnershipTests(unittest.IsolatedAsyncioTestCase):
    async def test_concurrent_execution_cannot_clear_active_records_or_spend_another_budget(self):
        entered, release = asyncio.Event(), asyncio.Event()
        admitted = []
        runtime = SimpleNamespace(admit=lambda options: admitted.append(options), run_timeout_seconds=2)

        class SlowLab(CommunicationLab):
            async def probe(self, row, *args, **kwargs):
                row.mark('prepare', 'PREPARING')
                entered.set()
                await release.wait()
                row.mark('finish', 'ERROR')
                return None

        lab = SlowLab(runtime)
        options = CheckOptions(architecture='SINGLE_AGENT')
        work = asyncio.create_task(lab.execute(options))
        try:
            await asyncio.wait_for(entered.wait(), 1)
            original_records = dict(lab.records)
            with self.assertRaisesRegex(ValueError, '^DIAGNOSTIC_RUN_BUSY$'):
                await lab.execute(options)
            self.assertEqual(lab.records, original_records)
            self.assertEqual(len(admitted), 1)
            release.set()
            await asyncio.wait_for(work, 1)
            await lab.execute(options)
            self.assertEqual(len(admitted), 2)
        finally:
            if not work.done():
                work.cancel()
                await asyncio.gather(work, return_exceptions=True)

    async def test_invalid_observer_does_not_admit_or_clear_existing_records(self):
        runtime = SimpleNamespace(admit=lambda _: self.fail('observer must be checked before admission'))
        lab = CommunicationLab(runtime)
        lab.records['old'] = object()
        with self.assertRaisesRegex(ValueError, '^DIAGNOSTIC_OBSERVER_INVALID$'):
            await lab.execute(CheckOptions(), emit='not callable')
        self.assertIn('old', lab.records)


class PeerReuseWiringTests(unittest.IsolatedAsyncioTestCase):
    async def test_repeated_runs_reuse_five_cards_and_keep_verification_and_live_events(self):
        seen, views, card_reads = [], Counter(), []
        runtime = LiveRuntime(SecretStr(live_fixtures.KEY), live_fixtures.MODEL,
            max_model_calls=20, transport_factory=live_fixtures.transport_factory(seen))
        original_view, original_card = ProbeRecord.view, A2ACardResolver.get_agent_card

        def observe_view(row):
            views[row.probe_id] += 1
            return original_view(row)

        async def get_card(resolver, *args, **kwargs):
            card_reads.append(1)
            return await original_card(resolver, *args, **kwargs)

        reports = []
        with patch.object(ProbeRecord, 'view', new=observe_view), patch.object(A2ACardResolver, 'get_agent_card', new=get_card):
            async with running_lab(runtime) as lab:
                sessions = lab._peer_sessions
                for architecture in ('MULTI_AGENT', 'MULTI_AGENT', 'SINGLE_AGENT', 'SINGLE_AGENT'):
                    report = await lab.execute(CheckOptions(architecture=architecture))
                    reports.append(report)
                    count = 4 if architecture == 'MULTI_AGENT' else 1
                    self.assertEqual(report['summary']['verifiedRequests'], count)
                    self.assertEqual(report['measurement']['observerMode'], 'FINAL_ONLY')
                    self.assertTrue(all(views[row['id']] == 1 for row in report['rows']))
                    self.assertTrue(all(row['observerCallbackMs'] == 0 for row in report['rows']))
                self.assertEqual(len(card_reads), 5)
                self.assertEqual(len(sessions._peers), 5)
                repeated = await lab.execute(CheckOptions(architecture='SINGLE_AGENT', rounds=2))
                self.assertEqual(repeated['summary']['verifiedRequests'], 2)
                self.assertEqual(len({row['contextId'] for row in repeated['rows']}), 1)
                self.assertEqual(len({row['taskId'] for row in repeated['rows']}), 2)
                self.assertTrue(all(views[row['id']] == 1 for row in repeated['rows']))
                events = []
                live = await lab.execute(CheckOptions(architecture='SINGLE_AGENT'), events.append)
                self.assertEqual(live['measurement']['observerMode'], 'LIVE')
                self.assertEqual(events[0]['type'], 'start')
                self.assertEqual(events[-1]['type'], 'done')
                self.assertTrue(any(event['type'] == 'row' for event in events))
                self.assertGreater(views[live['rows'][0]['id']], 1)
            self.assertIsNone(lab._peer_sessions)
            self.assertTrue(all(peer.transport.is_closed for peer in sessions._peers.values()))
        self.assertEqual(len(seen), 13)
        self.assertEqual(len(card_reads), 5)
        self.assertTrue(all(not row['transport']['clientReused'] for row in reports[0]['rows']))
        self.assertTrue(all(row['transport']['clientReused'] and row['transport']['agentCardCached'] for row in reports[1]['rows']))
        self.assertFalse(reports[2]['rows'][0]['transport']['clientReused'])
        self.assertTrue(reports[3]['rows'][0]['transport']['clientReused'])
        identities = [(row['messageId'], row['taskId'], row['contextId']) for report in reports for row in report['rows']]
        for index in range(3):
            self.assertEqual(len({value[index] for value in identities}), 10)

    async def test_cancellation_preserves_remote_task_and_releases_cached_peer_for_next_run(self):
        entered = asyncio.Event()
        seen = []
        async def respond(request):
            import json
            body = json.loads(request.content)
            seen.append(body)
            if len(seen) == 1:
                entered.set()
                await asyncio.Event().wait()
            return httpx.Response(200, json=live_fixtures.provider_response(body))
        runtime = LiveRuntime(SecretStr(live_fixtures.KEY), live_fixtures.MODEL,
                              transport_factory=lambda: httpx.MockTransport(respond))
        async with running_lab(runtime) as lab:
            work = asyncio.create_task(lab.execute(CheckOptions(architecture='SINGLE_AGENT')))
            try:
                await asyncio.wait_for(entered.wait(), 2)
                work.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await asyncio.wait_for(work, 4)
            finally:
                if not work.done():
                    work.cancel()
                    await asyncio.gather(work, return_exceptions=True)
            row = next(iter(lab.records.values()))
            self.assertEqual(row.phase, 'CANCELED')
            self.assertIsNotNone(row.task_id)
            self.assertEqual(row.cleanup_state, 'TASK_STATE_CANCELED')
            self.assertFalse(lab._peer_sessions._peers['SINGLE_AGENT'].active)
            next_run = await lab.execute(CheckOptions(architecture='SINGLE_AGENT'))
            self.assertEqual(next_run['summary']['verifiedRequests'], 1)
            self.assertTrue(next_run['rows'][0]['transport']['clientReused'])
            self.assertEqual(len(seen), 2)
