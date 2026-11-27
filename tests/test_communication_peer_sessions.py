"""71: real SDK/ASGI/storage, cached cards, binding isolation and ownership."""

import asyncio
import unittest
from uuid import uuid4

import httpx

from agents.core.config import AgentSettings
from diagnostics.communication.peer_sessions import DiagnosticPeerSessions
from orchestrator.a2a import A2AWorkflowMetadata
import test_agent_server as fixtures


class CountingASGITransport(httpx.ASGITransport):
    def __init__(self, app, *, fail_close=False):
        super().__init__(app)
        self.requests = []
        self.closed = False
        self.fail_close = fail_close

    async def handle_async_request(self, request):
        self.requests.append((request.method, request.url.path, request.headers.get('authorization')))
        return await super().handle_async_request(request)

    async def aclose(self):
        self.closed = True
        await super().aclose()
        if self.fail_close:
            raise ValueError('DUMMY_PRIVATE_CLOSE_ERROR')


def metadata():
    return A2AWorkflowMetadata(runId=uuid4(), workflowStepId=uuid4(), scenarioId=uuid4(), attempt=0)


class PeerSessionTests(unittest.IsolatedAsyncioTestCase):
    setUp = fixtures.AgentServerTests.setUp
    client_for = fixtures.AgentServerTests.client_for

    def pool(self, apps, *, fail_close_role=None):
        transports = {}
        def factory(role):
            self.assertNotIn(role, transports)
            transports[role] = CountingASGITransport(apps[role], fail_close=role == fail_close_role)
            return transports[role]
        pool = DiagnosticPeerSessions(transport_factory=factory)
        self.addAsyncCleanup(pool.aclose)
        return pool, transports

    def hooks(self, tag, log):
        async def request(value):
            await asyncio.sleep(0)
            log.append((tag, 'request', value.url.host, value.url.port))
        async def response(value):
            await asyncio.sleep(0)
            log.append((tag, 'response', value.request.url.host, value.request.url.port))
        return request, response

    async def test_same_peer_reuses_card_and_connection_owner_with_fresh_message_and_task_ids(self):
        token = 'DUMMY_PLANNER_TOKEN'
        settings = AgentSettings(role='PLANNER', bearer_token=token, _env_file=None)
        async with self.client_for(settings=settings, executor=fixtures.CompletedExecutor()) as (app, _):
            pool, transports = self.pool({'PLANNER': app})
            log, tasks, observations = [], [], []
            first_client = None
            for index in range(2):
                fields, message_id = metadata(), uuid4()
                async with pool.borrow('PLANNER', settings.agent_base_url, token, *self.hooks(index, log)) as (client, observed):
                    first_client = first_client or client
                    self.assertIs(client, first_client)
                    await client.resolve_agent_card()
                    task = await client.send_task({'request': f'독립 요청 {index}'}, fields, message_id=message_id)
                    receipt = await client.get_delivery_receipt(str(message_id))
                    self.assertEqual(receipt.message_id, str(message_id))
                    self.assertEqual(receipt.run_id, fields.run_id)
                    self.assertEqual(receipt.task_id, task.id)
                    tasks.append(task)
                    observations.append(observed)
            self.assertFalse(observations[0]['clientReused'])
            self.assertFalse(observations[0]['agentCardCached'])
            self.assertTrue(observations[1]['clientReused'])
            self.assertTrue(observations[1]['agentCardCached'])
            self.assertEqual(observations[0]['sessionId'], observations[1]['sessionId'])
            self.assertNotEqual(tasks[0].id, tasks[1].id)
            self.assertNotEqual(tasks[0].context_id, tasks[1].context_id)
            paths = [path for _, path, _ in transports['PLANNER'].requests]
            self.assertEqual(paths.count('/.well-known/agent-card.json'), 1)
            self.assertEqual(paths.count('/message:send'), 2)
            self.assertEqual(sum(tag == 0 for tag, *_ in log), 6)
            self.assertEqual(sum(tag == 1 for tag, *_ in log), 4)
            self.assertNotIn(token, repr(pool._peers))
            await pool.aclose()
            self.assertTrue(transports['PLANNER'].closed)
            self.assertTrue(pool._peers['PLANNER'].transport.is_closed)

    async def test_parallel_roles_keep_credentials_callbacks_and_receipts_isolated(self):
        qa = AgentSettings(role='QA', bearer_token='DUMMY_QA_TOKEN', _env_file=None)
        security = AgentSettings(role='SECURITY', bearer_token='DUMMY_SECURITY_TOKEN', _env_file=None)
        async with self.client_for(settings=qa, executor=fixtures.CompletedExecutor()) as (qa_app, _), \
                self.client_for(settings=security, executor=fixtures.CompletedExecutor()) as (security_app, _):
            pool, transports = self.pool({'QA': qa_app, 'SECURITY': security_app})
            log = []
            async def send(settings):
                role, token = settings.role.value, settings.bearer_token.get_secret_value()
                message_id, fields = uuid4(), metadata()
                async with pool.borrow(role, settings.agent_base_url, token, *self.hooks(role, log)) as (client, _):
                    task = await client.send_task({'request': role}, fields, message_id=message_id)
                    receipt = await client.get_delivery_receipt(str(message_id))
                    self.assertEqual(receipt.receiver_role, role)
                    self.assertEqual(receipt.run_id, fields.run_id)
                    return task.id
            tasks = await asyncio.gather(send(qa), send(security))
            self.assertEqual(len(set(tasks)), 2)
            for role, port, token in [('QA', qa.listen_port, 'DUMMY_QA_TOKEN'), ('SECURITY', security.listen_port, 'DUMMY_SECURITY_TOKEN')]:
                self.assertTrue(all(entry[3] == port for entry in log if entry[0] == role))
                self.assertTrue(all(value[2] == 'Bearer '+token for value in transports[role].requests))

    async def test_drift_busy_and_closed_sessions_never_silently_rebind(self):
        settings = AgentSettings(role='PLANNER', _env_file=None)
        async with self.client_for(settings=settings, executor=fixtures.CompletedExecutor()) as (app, _):
            pool, _ = self.pool({'PLANNER': app})
            hooks = self.hooks('first', [])
            async with pool.borrow('PLANNER', settings.agent_base_url, 'DUMMY_TOKEN', *hooks):
                with self.assertRaisesRegex(RuntimeError, '^DIAGNOSTIC_PEER_BUSY$'):
                    async with pool.borrow('PLANNER', settings.agent_base_url, 'DUMMY_TOKEN', *hooks):
                        self.fail('same-role overlap must not be admitted')
            for endpoint, token in [(settings.agent_base_url, 'DIFFERENT_DUMMY_TOKEN'), ('https://other.invalid', 'DUMMY_TOKEN')]:
                with self.assertRaisesRegex(RuntimeError, '^DIAGNOSTIC_PEER_BINDING_CHANGED$'):
                    async with pool.borrow('PLANNER', endpoint, token, *hooks):
                        self.fail('binding drift must not be admitted')
            await pool.aclose()
            with self.assertRaisesRegex(RuntimeError, '^DIAGNOSTIC_PEER_SESSION_CLOSED$'):
                async with pool.borrow('PLANNER', settings.agent_base_url, 'DUMMY_TOKEN', *hooks):
                    self.fail('closed session must not reopen')

    async def test_child_inheriting_context_cannot_use_a_released_binding(self):
        settings = AgentSettings(role='PLANNER', _env_file=None)
        async with self.client_for(settings=settings, executor=fixtures.CompletedExecutor()) as (app, _):
            pool, transports = self.pool({'PLANNER': app})
            gate = asyncio.Event()
            async def late_request(client):
                await gate.wait()
                await client.resolve_agent_card()
            async with pool.borrow('PLANNER', settings.agent_base_url, 'DUMMY_TOKEN', *self.hooks('late', [])) as (client, _):
                pending = asyncio.create_task(late_request(client))
            try:
                gate.set()
                with self.assertRaises(Exception):
                    await asyncio.wait_for(pending, timeout=1)
            finally:
                if not pending.done():
                    pending.cancel()
                    await asyncio.gather(pending, return_exceptions=True)
            self.assertEqual(transports['PLANNER'].requests, [])

    async def test_canceled_borrow_releases_role_for_next_owner(self):
        settings = AgentSettings(role='PLANNER', _env_file=None)
        async with self.client_for(settings=settings, executor=fixtures.CompletedExecutor()) as (app, _):
            pool, _ = self.pool({'PLANNER': app})
            entered = asyncio.Event()
            async def borrow():
                async with pool.borrow('PLANNER', settings.agent_base_url, 'DUMMY_TOKEN', *self.hooks('cancel', [])):
                    entered.set()
                    await asyncio.Event().wait()
            work = asyncio.create_task(borrow())
            try:
                await asyncio.wait_for(entered.wait(), 1)
                work.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await work
            finally:
                if not work.done():
                    work.cancel()
                    await asyncio.gather(work, return_exceptions=True)
            async with pool.borrow('PLANNER', settings.agent_base_url, 'DUMMY_TOKEN', *self.hooks('next', [])) as (client, _):
                await client.resolve_agent_card()

    async def test_close_failure_still_closes_other_peers_and_omits_private_error(self):
        settings = AgentSettings(role='PLANNER', _env_file=None)
        async with self.client_for(settings=settings, executor=fixtures.CompletedExecutor()) as (app, _):
            pool, transports = self.pool({'QA': app, 'SECURITY': app}, fail_close_role='QA')
            for role in ('QA', 'SECURITY'):
                async with pool.borrow(role, settings.agent_base_url, 'DUMMY_TOKEN', *self.hooks(role, [])):
                    pass
            with self.assertRaisesRegex(RuntimeError, '^DIAGNOSTIC_PEER_CLOSE_UNVERIFIED$'):
                await pool.aclose()
            self.assertTrue(all(value.closed for value in transports.values()))
