"""Offline regression for Run #27: coordinates and pre-test prose are different evidence."""
import copy
import json
from types import SimpleNamespace
from uuid import uuid4
import unittest

from agents.llm.contracts import LLMErrorCode, LLMRuntimeError
from agents.llm.engine import LLMEngine
from agents.roles.security_contract import build_security_output_contract, validate_security_decision, SecurityContractError
from agents.roles.security_draft import SecurityTestDraft, PENDING_TESTS, pending_fields
from agents.runtime.security_services import SecurityRuntimeServices
from agents.semantic_review import draft_fields
import test_llm_runtime as runtime_fixture


class SecurityDraftTests(unittest.TestCase):
    def setUp(self):
        self.req = uuid4()
        self.files = {'models.py': b'line\n' * 10, 'main.py': b'line\r\n' * 205, 'empty.py': b''}
        self.wire = SecurityTestDraft(self.files, 'a' * 64)
        self.output = self.wire.output(build_security_output_contract(
            (self.req,), ('finding-1',), tuple(sorted(self.files)), ('security-unit',)))
        self.anchor = next(k for k,v in self.wire.catalog['anchors'].items() if v['path'] == 'models.py')

    def candidate(self):
        return {'kind': 'READY', 'questions': [], 'requirementReviews': [{
            'requirementId': str(self.req), 'proposedOutcome': 'UNVERIFIED', 'rationale': PENDING_TESTS,
            'references': [{'anchorId': self.anchor}]}], 'findingReviews': [{
                'findingId': 'finding-1', 'proposedDisposition': 'SUSPECTED',
                'rationale': 'Scanner warning needs independent evidence.', 'references': []}],
            'testCases': [{'selector': 'security-unit', 'testId': 'test_signup.Security.test_req',
                'requirementId': str(self.req), 'title': 'Approved password storage policy',
                'expectedResult': 'Approved password hashing parameters'}]}

    def test_host_ranges_cover_real_lines_and_are_bound_to_snapshot(self):
        anchors = list(self.wire.catalog['anchors'].values())
        self.assertEqual([a for a in anchors if a['path'] == 'models.py'],
                         [{'path': 'models.py', 'startLine': 1, 'endLine': 10}])
        self.assertEqual([(a['startLine'],a['endLine']) for a in anchors if a['path']=='main.py'], [(1,200),(201,205)])
        self.assertFalse([a for a in anchors if a['path']=='empty.py'])
        self.assertTrue(set(self.wire.catalog['anchors']).isdisjoint(
            SecurityTestDraft(self.files, 'b'*64).catalog['anchors']))
        catalog = self.wire.catalog
        catalog['anchors'].clear()
        self.assertTrue(self.wire.catalog['anchors'])  # the prompt cannot mutate Host resolution

    def test_wire_schema_and_canonical_validator_both_accept_bound_plan(self):
        draft = self.candidate()
        self.output.schema.validate(draft)
        decoded = self.wire.decode(draft)
        decision = validate_security_decision(decoded, (self.req,), ('finding-1',), tuple(sorted(self.files)), ('security-unit',))
        self.assertEqual(decision.requirement_reviews[0].references[0].end_line,10)
        self.assertEqual(draft['requirementReviews'][0]['references'],[{'anchorId': self.anchor}])
        self.assertEqual(decision.requirement_reviews[0].proposed_outcome,'UNVERIFIED')

    def test_run27_handwritten_out_of_file_reference_is_rejected_not_clipped(self):
        draft=self.candidate()
        draft['requirementReviews'][0]['references']=[{'path':'models.py','startLine':6,'endLine':11}]
        with self.assertRaises(LLMRuntimeError): self.output.schema.validate(draft)
        with self.assertRaises(SecurityContractError): self.wire.decode(draft)

    def test_unknown_foreign_and_forged_reference_coordinates_fail_closed(self):
        foreign = next(iter(SecurityTestDraft(self.files,'b'*64).catalog['anchors']))
        for ref in ({'anchorId':'invented'}, {'anchorId':foreign}, {'anchorId':self.anchor,'endLine':11}):
            draft=self.candidate();draft['requirementReviews'][0]['references']=[ref]
            with self.subTest(ref=ref),self.assertRaises(SecurityContractError):self.wire.decode(draft)

    def test_pretest_product_verdict_and_mixed_observation_prose_are_not_wire_choices(self):
        for field,value in (('proposedOutcome','PASS'),('proposedOutcome','FAIL'),
                            ('rationale','main.py meets the hash policy but external verification is impossible.')):
            draft=self.candidate();draft['requirementReviews'][0][field]=value
            with self.subTest(field=field,value=value):
                with self.assertRaises(LLMRuntimeError):self.output.schema.validate(draft)
                with self.assertRaises(SecurityContractError):self.wire.decode(draft)

    def test_only_exact_pending_marker_is_deterministic_not_titles_findings_or_claims(self):
        draft=self.candidate();fixed=pending_fields(draft)
        self.assertEqual(fixed,{'/requirementReviews/0/rationale'})
        reviewed=set(draft_fields(draft))-fixed
        self.assertIn('/testCases/0/title',reviewed)
        self.assertIn('/testCases/0/expectedResult',reviewed)
        self.assertIn('/findingReviews/0/rationale',reviewed)
        for value in ('HOST_TESTS_PENDING but already PASS','Observed compliant code',''):
            draft['requirementReviews'][0]['rationale']=value
            self.assertFalse(pending_fields(draft))
        draft=self.candidate();draft['requirementReviews'][0]['proposedOutcome']='PASS'
        self.assertFalse(pending_fields(draft))
        draft=self.candidate();del draft['testCases']
        self.assertFalse(pending_fields(draft))

    def test_catalog_does_not_prove_a_source_read(self):
        services=object.__new__(SecurityRuntimeServices)
        token=uuid4();services._reads={token:{}}
        measured=SimpleNamespace(_token=token,source_files=self.files)
        decoded=self.wire.decode(self.candidate())
        self.assertEqual(services.reference_issues(measured,decoded)[0]['reasonCode'],'REFERENCE_FILE_NOT_READ')
        self.assertEqual(self.wire.reference_issues(services,measured,self.candidate())[0]['fieldPath'],
                         '/requirementReviews/0/references/0/anchorId')
        services._reads[token]={'models.py':object()}
        self.assertFalse(services.reference_issues(measured,decoded))

    def test_empty_source_cannot_create_a_fake_anchor(self):
        wire=SecurityTestDraft({'empty.py':b''},'a'*64)
        self.assertEqual(wire.catalog['anchors'],{})
        draft=self.candidate()
        with self.assertRaises(SecurityContractError):wire.decode(draft)

    def test_non_utf8_assets_stay_in_inventory_without_fake_line_anchors(self):
        files={**self.files,'logo.png':b'\x89PNG\r\n\xff'}
        wire=SecurityTestDraft(files,'a'*64)
        self.assertEqual(wire.catalog['anchors'],self.wire.catalog['anchors'])
        self.assertEqual(wire.catalog['unanchoredPaths']['logo.png'],'NON_UTF8')
        self.assertEqual(wire.catalog['unanchoredPaths']['empty.py'],'EMPTY_FILE')
        self.assertEqual(files['logo.png'],b'\x89PNG\r\n\xff')

    def test_single_and_multi_system_instructions_allow_host_anchor_wire(self):
        from agents.roles.prompts import build_system_prompt
        from agents.single.prompts import build_single_system_prompt
        from orchestrator.domain import AgentRole
        for prompt in (build_system_prompt(AgentRole.SECURITY),build_single_system_prompt()):
            self.assertIn('sourceReferenceCatalog',prompt)
            self.assertIn('anchorId',prompt)
            self.assertNotIn('references에는 sourcePaths의 저장소 기준 경로를 그대로 사용한다',prompt)


class ReferenceCorrectionExitTests(unittest.IsolatedAsyncioTestCase):
    async def test_repeated_invalid_references_stop_before_returning_candidate(self):
        f=runtime_fixture.LLMRuntimeTests('runTest');f.setUp()
        response=runtime_fixture.text_response(json.dumps({'answer':'ready'}))
        for limit,expected_calls in ((0,1),(2,2)):
            provider=runtime_fixture.FakeProvider(response,response,response)
            routes=[]
            with self.subTest(limit=limit),self.assertRaises(LLMRuntimeError) as caught:
                await LLMEngine(role=f.role,provider=provider).run(prompt=f.prompt,model=f.model,output=f.output,
                    budget=f.budget(max_output_corrections=limit),recovery_sink=routes.append,
                    output_check=lambda *_:{'code':'CODE_REFERENCE_INVALID','issues':[
                        {'fieldPath':'/requirementReviews/0/references/0/path','reasonCode':'REFERENCE_FILE_NOT_READ'}]})
            self.assertEqual(caught.exception.code,LLMErrorCode.SECURITY_REFERENCE)
            self.assertEqual(len(provider.requests),expected_calls)
            self.assertEqual(routes[-1].action,'STOP')
