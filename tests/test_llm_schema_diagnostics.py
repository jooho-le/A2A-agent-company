"""Offline wire/engine regressions; no real provider or generated-code execution."""
import copy
from dataclasses import replace
import json
import unittest
from uuid import uuid4

import httpx
from pydantic import SecretStr

from agents.llm.contracts import JsonSchema, LLMErrorCode, LLMRuntimeError, ToolCall, ToolDefinition, json_text
from agents.llm.engine import LLMEngine
from agents.llm.openai_responses import OpenAIResponsesProvider
from agents.llm.tool_wire import strict_tool_schema, project_tool_arguments, STRICT_TOOL_ARGUMENTS
from agents.llm.validation import diagnose
from mcp_tools.core.catalog import TOOL_CONTRACTS
from orchestrator.domain.run_configuration import ModelConfiguration
from orchestrator.domain.states import AgentRole
import test_llm_runtime as runtime_fixture
from test_llm_runtime import FakeProvider, FakeToolExecutor, tool_response, text_response, object_schema
from test_openai_responses import response_payload


class ToolWireSchemaTests(unittest.TestCase):
    def test_all_real_mcp_input_schemas_compile_without_changing_project_contracts(self):
        for name, contract in TOOL_CONTRACTS.items():
            with self.subTest(tool=name):
                original = contract.input_schema_json
                project = JsonSchema(original)
                wire = strict_tool_schema(project)
                wire.require_openai_strict()
                self.assertEqual(set(wire.to_dict()['required']), set(wire.to_dict()['properties']))
                self.assertEqual(project.schema_json, original)

    def test_optional_null_round_trip_preserves_project_admission(self):
        project = JsonSchema(TOOL_CONTRACTS['write_source_file'].input_schema_json)
        wire = strict_tool_schema(project)
        data = {'workspaceId':str(uuid4()),'path':'source/models.py','content':'x = 1\n','expectedSha256':None}
        with self.assertRaises(LLMRuntimeError): project.validate(data)
        wire.validate(data)
        actual = project_tool_arguments(project, data)
        project.validate(actual)
        self.assertNotIn('expectedSha256', actual)
        self.assertIn('expectedSha256', data)
        for value in ('', 'invalid', 2, False):
            with self.subTest(value=value), self.assertRaises(LLMRuntimeError):
                wire.validate({**data, 'expectedSha256':value})
        sha = 'a' * 64
        self.assertEqual(project_tool_arguments(project, {**data, 'expectedSha256':sha})['expectedSha256'], sha)

    def test_missing_optional_wire_field_is_not_silently_accepted(self):
        project = object_schema({'path': {'type':'string'}, 'expectedSha256':{'type':'string'}}, required=['path'])
        with self.assertRaises(LLMRuntimeError): strict_tool_schema(project).validate({'path':'source/main.py'})
        project.validate({'path':'source/main.py'})

    def test_existing_nullable_and_falsy_values_keep_their_meaning(self):
        project = object_schema({'path': {'type':'string'}, 'content':{'type':['string','null']},
                                 'total': {'type':'integer'}, 'changed': {'type':'boolean'}}, required=['path'])
        data={'path':'', 'content':None, 'total':0, 'changed':False}
        strict_tool_schema(project).validate(data)
        self.assertEqual(project_tool_arguments(project, data), data)

    def test_nested_optional_schemas_fail_explicitly_instead_of_weakening(self):
        project = object_schema({'content':{'type':'object','properties':{'path':{'type':'string'}},
                                          'required':[], 'additionalProperties':False}})
        with self.assertRaises(LLMRuntimeError): strict_tool_schema(project)

    def test_unknown_required_field_is_not_removed_by_wire_conversion(self):
        project = object_schema({'path':{'type':'string'}}, required=['path','unknown'])
        with self.assertRaises(LLMRuntimeError): strict_tool_schema(project)


class LLMValidationDiagnosticTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.f = runtime_fixture.LLMRuntimeTests('runTest')
        self.f.setUp()

    async def execute(self, provider, tools=(), executor=None, output=None, observed=None, sink=None):
        return await LLMEngine(role=self.f.role, provider=provider, tools=tools, tool_executor=executor).run(
            prompt=self.f.prompt, model=self.f.model, output=output or self.f.output, budget=self.f.budget(),
            workspace_id=self.f.workspace_id, output_observed_sink=observed, validation_failed_sink=sink)

    async def test_optional_null_rejection_records_field_without_executing_or_retrying(self):
        schema = object_schema({'workspaceId':{'type':'string'},'path':{'type':'string'},
                                'expectedSha256':{'type':'string'}},required=['workspaceId','path'])
        provider = FakeProvider(tool_response(self.f.call(expectedSha256=None)))
        executor = FakeToolExecutor()
        rows=[]
        with self.assertRaises(LLMRuntimeError) as raised:
            await self.execute(provider, (self.f.tool(input_schema=schema),), executor, sink=lambda r: rows.append(r) or True)
        error=raised.exception
        report=error.public_validation()
        self.assertEqual(error.code, LLMErrorCode.SCHEMA)
        self.assertEqual(report['stage'], 'TOOL_ARGUMENTS')
        self.assertEqual(report['issues'], [{'fieldPath':'/expectedSha256','reasonCode':'TYPE_INVALID',
                                            'actualType':'null','reason':'필드의 자료형이 규격과 다릅니다.'}])
        self.assertTrue(report['traceRecorded'])
        self.assertEqual(len(error.records),1)
        self.assertEqual(len(provider.requests),1)
        self.assertEqual(executor.calls,[])
        self.assertIsNone(error.__context__)
        self.assertIsNone(error.__cause__)
        self.assertEqual(len(rows),1)

    async def test_tool_policy_boundaries_have_closed_value_free_reasons(self):
        cases = [
            (self.f.call(name='private-unoffered-tool'), 'TOOL_NOT_OFFERED', '/toolName'),
            (self.f.call(workspaceId='private-wrong-workspace'), 'WORKSPACE_MISMATCH', '/workspaceId'),
            (self.f.call(path='password=private-value'), 'ARGUMENT_CREDENTIAL_DENIED', ''),
        ]
        for call, reason, path in cases:
            with self.subTest(reason=reason):
                executor=FakeToolExecutor()
                with self.assertRaises(LLMRuntimeError) as raised:
                    await self.execute(FakeProvider(tool_response(call)), (self.f.tool(),), executor)
                report=raised.exception.public_validation()
                self.assertEqual(report['stage'], 'TOOL_POLICY')
                self.assertEqual(report['reasonCode'], reason)
                self.assertEqual(report['fieldPath'], path)
                self.assertEqual(raised.exception.code, LLMErrorCode.TOOL_POLICY)
                self.assertNotIn('private', json.dumps(report))
                self.assertIsNone(raised.exception.__context__)
                self.assertEqual(executor.calls, [])

    async def test_source_literal_rejection_and_runtime_fixture_are_distinguished(self):
        tool=self.f.tool('write_source_file', input_schema=object_schema({
            'workspaceId':{'type':'string'}, 'path':{'type':'string'}, 'content':{'type':'string'}}),
            source_argument_fields=('content',))
        executor=FakeToolExecutor()
        with self.assertRaises(LLMRuntimeError) as raised:
            await self.execute(FakeProvider(tool_response(self.f.call(name='write_source_file',
                content='password = "private-fixture-value"'))), (tool,), executor)
        self.assertEqual(raised.exception.public_validation()['reasonCode'], 'SOURCE_CREDENTIAL_DENIED')
        self.assertEqual(raised.exception.public_validation()['fieldPath'], '/content')
        self.assertNotIn('private-fixture', json.dumps(raised.exception.public_validation()))
        source='import secrets\npassword = secrets.token_urlsafe(16)\n'
        await self.execute(FakeProvider(tool_response(self.f.call(name='write_source_file', content=source)),
            text_response()), (tool,), executor)
        self.assertEqual(executor.calls[0][1]['content'], source)

    async def test_repeated_call_id_has_a_distinct_policy_reason(self):
        executor=FakeToolExecutor()
        with self.assertRaises(LLMRuntimeError) as raised:
            await self.execute(FakeProvider(tool_response(self.f.call('same'),self.f.call('same'))),
                               (self.f.tool(),), executor)
        self.assertEqual(raised.exception.public_validation()['reasonCode'], 'CALL_ID_REUSED')
        self.assertEqual(executor.calls, [])

    async def test_qa_rejected_test_write_gets_one_value_free_correction(self):
        self.f.role=AgentRole.QA
        self.f.prompt=self.f.prepared(AgentRole.QA)
        tool=self.f.tool('write_test_file', input_schema=object_schema({
            'workspaceId':{'type':'string'}, 'path':{'type':'string'}, 'content':{'type':'string'}}),
            source_argument_fields=('content',))
        bad=self.f.call('bad',name='write_test_file', content='password="private-test-fixture"')
        good=self.f.call('good',name='write_test_file',content='import secrets\npassword=secrets.token_urlsafe(16)\n')
        provider=FakeProvider(tool_response(bad),tool_response(good),text_response())
        executor=FakeToolExecutor()
        reports=[]
        result=await self.execute(provider,(tool,),executor,sink=reports.append)
        self.assertEqual(len(provider.requests),3)
        self.assertEqual(len(executor.calls),1)
        self.assertEqual(len(result.records),3)
        self.assertEqual(reports[0].reason_code,'SOURCE_CREDENTIAL_DENIED')
        self.assertNotIn('private-test-fixture',provider.requests[1].input_items_json)
        self.assertNotIn('private-test-fixture',provider.requests[2].input_items_json)
        self.assertIn('hostToolFeedback',provider.requests[1].input_items_json)
        provider=FakeProvider(tool_response(bad),tool_response(replace(bad,call_id='again')),text_response())
        executor=FakeToolExecutor()
        with self.assertRaises(LLMRuntimeError) as raised:
            await self.execute(provider,(tool,),executor)
        self.assertEqual(raised.exception.code,LLMErrorCode.TOOL_POLICY)
        self.assertEqual(len(provider.requests),2)
        self.assertEqual(executor.calls,[])

    async def test_rejected_test_write_batch_never_gets_partial_execution_or_correction(self):
        self.f.role=AgentRole.SECURITY
        self.f.prompt=self.f.prepared(AgentRole.SECURITY)
        tool=self.f.tool('write_test_file', input_schema=object_schema({
            'workspaceId':{'type':'string'}, 'path':{'type':'string'}, 'content':{'type':'string'}}),
            source_argument_fields=('content',))
        provider=FakeProvider(tool_response(
            self.f.call('good',name='write_test_file',content='x=1'),
            self.f.call('bad',name='write_test_file',content='password="private-test-fixture"')))
        executor=FakeToolExecutor()
        with self.assertRaises(LLMRuntimeError): await self.execute(provider,(tool,),executor)
        self.assertEqual(len(provider.requests),1)
        self.assertEqual(executor.calls,[])

    async def test_entire_batch_is_checked_before_any_side_effect(self):
        provider=FakeProvider(tool_response(self.f.call('first'),self.f.call('second',path=None)))
        executor=FakeToolExecutor()
        with self.assertRaises(LLMRuntimeError) as raised:
            await self.execute(provider,(self.f.tool(),),executor)
        self.assertEqual(executor.calls,[])
        self.assertEqual(raised.exception.validation.stage,'TOOL_ARGUMENTS')

    async def test_private_values_and_unknown_keys_never_enter_report(self):
        data={'answer':42,'private-key-do-not-log':'password=private-value'}
        with self.assertRaises(LLMRuntimeError) as raised:
            await self.execute(FakeProvider(text_response(json_text(data))))
        report=raised.exception.public_validation()
        self.assertEqual(report['stage'],'MODEL_OUTPUT')
        self.assertEqual({x['reasonCode'] for x in report['issues']},{'TYPE_INVALID','UNEXPECTED_FIELD'})
        self.assertNotIn('private',json.dumps(report))
        self.assertIsNone(raised.exception.__context__)

    async def test_raw_and_redacted_output_failures_are_distinguishable(self):
        output=replace(self.f.output,schema=object_schema({'answer':{'type':'string','maxLength':12}}))
        with self.assertRaises(LLMRuntimeError) as raised:
            await self.execute(FakeProvider(text_response('{"answer":"password=a"}')),output=output)
        self.assertEqual(raised.exception.validation.stage,'MODEL_OUTPUT_REDACTED')
        self.assertNotIn('password',json.dumps(raised.exception.public_validation()))

    async def test_tool_result_stages_and_completed_usage_are_preserved(self):
        for raw, output, stage in (({'answer':1},None,'TOOL_RESULT'),
            ({'answer':'password=a'},object_schema({'answer':{'type':'string','maxLength':12}}),'TOOL_RESULT_REDACTED')):
            with self.subTest(stage=stage):
                provider=FakeProvider(tool_response(self.f.call()))
                executor=FakeToolExecutor(raw)
                with self.assertRaises(LLMRuntimeError) as raised:
                    await self.execute(provider,(self.f.tool(output_schema=output),),executor)
                self.assertEqual(raised.exception.validation.stage,stage)
                self.assertEqual(len(executor.calls),1)
                self.assertEqual(len(raised.exception.records),1)

    async def test_diagnostic_storage_failure_preserves_original_schema_error(self):
        def fail(_): raise RuntimeError('private-store-failure')
        with self.assertRaises(LLMRuntimeError) as raised:
            await self.execute(FakeProvider(text_response('{"answer":2}')),sink=fail)
        self.assertEqual(raised.exception.code,LLMErrorCode.SCHEMA)
        self.assertFalse(raised.exception.public_validation()['traceRecorded'])
        self.assertNotIn('private',str(raised.exception))
        self.assertIsNone(raised.exception.__context__)

    def test_reports_are_bounded_and_unknown_property_names_are_not_copied(self):
        schema=object_schema({'cases':{'type':'array','items':{'type':'integer'}}})
        report=diagnose(schema,{'cases':['private']*100},stage='MODEL_OUTPUT',sequence=1)
        self.assertEqual(len(report.issues),64)
        self.assertTrue(report.issues_truncated)
        self.assertNotIn('private',json.dumps(report.public()))

    def test_error_cannot_expose_arbitrary_diagnostic_payload(self):
        error=LLMRuntimeError(LLMErrorCode.SCHEMA,validation={'raw':'private-secret'},validation_recorded=True)
        self.assertIsNone(error.validation)
        self.assertIsNone(error.public_validation())
        self.assertFalse(error.validation_recorded)
        self.assertNotIn('private',repr(error))

    async def test_mock_openai_wire_null_is_decoded_only_for_executor_and_raw_history_is_preserved(self):
        self.f.workspace_id=str(uuid4())
        self.f.model=ModelConfiguration(provider='openai',modelId='gpt-4.1',temperature=0)
        schema=JsonSchema(TOOL_CONTRACTS['write_source_file'].input_schema_json)
        tool=self.f.tool('write_source_file',input_schema=schema,source_argument_fields=('content',))
        arguments={'workspaceId':self.f.workspace_id,'path':'source/models.py',
                   'content':'password = request.password\n','expectedSha256':None}
        call={'id':'fc_test','type':'function_call','call_id':'call_test','name':'write_source_file',
              'arguments':json_text(arguments),'status':'completed'}
        sent=[]
        def respond(request):
            sent.append(json.loads(request.content))
            if len(sent)==1:return httpx.Response(200,json=response_payload(output=[call]))
            payload=response_payload()
            payload['output'][0]['content'][0]['text']='{"answer":"done"}'
            return httpx.Response(200,json=payload)
        provider=OpenAIResponsesProvider(SecretStr('OFFLINE_TEST_KEY'),transport=httpx.MockTransport(respond))
        executor=FakeToolExecutor()
        observations=[]
        try: result=await self.execute(provider,(tool,),executor,observed=observations.append)
        finally:await provider.aclose()
        self.assertEqual(result.data,{'answer':'done'})
        executed_call,actual,_=executor.calls[0]
        self.assertNotIn('expectedSha256',actual)
        self.assertEqual(json.loads(executed_call.arguments_json),actual)
        self.assertEqual(actual['content'],arguments['content'])
        self.assertIsNone(json.loads(observations[0]['toolCalls'][0]['arguments'])['expectedSha256'])
        self.assertEqual(sent[1]['input'][1],call)
        self.assertIs(sent[0]['tools'][0]['strict'],True)

    async def test_strict_wire_invalid_required_value_stops_before_executor(self):
        self.f.model=ModelConfiguration(provider='openai',modelId='fake-model',temperature=0)
        provider=FakeProvider(replace(tool_response(self.f.call(path=None)),tool_arguments_format=STRICT_TOOL_ARGUMENTS))
        provider.name='openai'
        executor=FakeToolExecutor()
        with self.assertRaises(LLMRuntimeError) as raised:
            await self.execute(provider,(self.f.tool(),),executor)
        self.assertEqual(raised.exception.validation.stage,'TOOL_ARGUMENTS_WIRE')
        self.assertEqual(executor.calls,[])
