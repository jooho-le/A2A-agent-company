"""A delayed prior SDK status cannot turn an admitted continuation into a 500."""

import unittest
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

from a2a.server.request_handlers import DefaultRequestHandler
from a2a.types import AgentCard, TaskState
from a2a.utils.errors import InvalidAgentResponseError
from google.protobuf.json_format import MessageToDict

from agents.api.handler import ProjectRequestHandler
from agents.api.validation import parse_workflow_metadata
import test_agent_task_store as store_fixture


class ContinuationResponseTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = store_fixture.AgentTaskStoreTests.asyncSetUp
    request = store_fixture.AgentTaskStoreTests.request
    claim = store_fixture.AgentTaskStoreTests.claim
    write_state = store_fixture.AgentTaskStoreTests.write_state

    async def interrupted(self, state=TaskState.TASK_STATE_AUTH_REQUIRED):
        first = await self.claim()
        previous = (await self.write_state(first.task.id, state)).task
        metadata = self.metadata.model_copy(update={"attempt": 1})
        request = self.request(metadata=metadata, task_id=previous.id, context_id=previous.context_id)
        handler = ProjectRequestHandler(task_store=self.store, agent_executor=MagicMock(), agent_card=AgentCard())
        self.addAsyncCleanup(handler.aclose)
        return handler, request, previous, metadata

    async def test_delayed_interrupted_response_reads_current_admission_without_resubmitting(self):
        for state in (TaskState.TASK_STATE_AUTH_REQUIRED, TaskState.TASK_STATE_INPUT_REQUIRED):
            with self.subTest(state=state):
                handler, request, previous, metadata = await self.interrupted(state)
                with patch.object(DefaultRequestHandler, 'on_message_send', new_callable=AsyncMock,
                                  return_value=previous) as sdk:
                    current = await handler.on_message_send(request, self.context)
                    self.assertEqual((current.id, current.context_id), (previous.id, previous.context_id))
                    self.assertEqual(current.status.state, TaskState.TASK_STATE_SUBMITTED)
                    self.assertEqual(parse_workflow_metadata(MessageToDict(current.metadata)), metadata)
                    repeated = await handler.on_message_send(request, self.context)
                    self.assertEqual(repeated, current)
                    self.assertEqual(sdk.await_count, 1)
                self.assertEqual(len(await self.store.revisions(current.id)), 3)

    async def test_other_identities_or_noninterrupted_prior_responses_still_fail(self):
        for change in ('task', 'context', 'run', 'step', 'future_attempt', 'older_attempt', 'working', 'completed'):
            with self.subTest(change=change):
                handler, request, previous, _ = await self.interrupted()
                result = store_fixture.task_copy(previous)
                if change == 'task':
                    result.id = str(uuid4())
                elif change == 'context':
                    result.context_id = str(uuid4())
                elif change in ('run', 'step'):
                    result.metadata.update({'runId' if change == 'run' else 'workflowStepId': str(uuid4())})
                elif change in ('future_attempt', 'older_attempt'):
                    result.metadata.update({'attempt': 2 if change == 'future_attempt' else 0})
                    if change == 'older_attempt':
                        # Move the legitimate prior admission to attempt 1,
                        # then resume attempt 2 while presenting stale 0.
                        claim = await self.store.claim_message(request, self.context)
                        await self.write_state(claim.task.id, TaskState.TASK_STATE_AUTH_REQUIRED)
                        request = self.request(metadata=self.metadata.model_copy(update={'attempt': 2}),
                            task_id=previous.id, context_id=previous.context_id)
                else:
                    result.status.state = (TaskState.TASK_STATE_WORKING if change == 'working'
                                           else TaskState.TASK_STATE_COMPLETED)
                with patch.object(DefaultRequestHandler, 'on_message_send', new_callable=AsyncMock,
                                  return_value=result):
                    with self.assertRaises(InvalidAgentResponseError):
                        await handler.on_message_send(request, self.context)

    async def test_current_store_response_must_match_every_admitted_identity(self):
        for change in ('missing', 'task', 'context', 'run', 'attempt'):
            with self.subTest(change=change):
                handler, request, previous, metadata = await self.interrupted()
                async def stale_sdk(_params, _context):
                    return previous
                # Admission uses the transaction directly; only the fallback
                # get is replaced, preserving a real durable request receipt.
                current = store_fixture.task_copy(previous)
                current.metadata.update(metadata.to_a2a_json())
                if change == 'task':
                    current.id = str(uuid4())
                elif change == 'context':
                    current.context_id = str(uuid4())
                elif change == 'run':
                    current.metadata.update({'runId': str(uuid4())})
                elif change == 'attempt':
                    current.metadata.update({'attempt': 2})
                stored = None if change == 'missing' else MagicMock(task=current)
                with patch.object(DefaultRequestHandler, 'on_message_send', side_effect=stale_sdk), \
                        patch.object(self.store, 'get', new_callable=AsyncMock, return_value=stored):
                    with self.assertRaises(InvalidAgentResponseError):
                        await handler.on_message_send(request, self.context)
