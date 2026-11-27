"""Frozen real-policy provenance regressions; no API or model-accuracy claims."""
import copy
import json
from pathlib import Path
import unittest
import tempfile
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

from agents.semantic_review import (inspect_result, policy_evidence, ProductSemanticReviewer,
                                   SemanticReviewRequired)
from agents.semantic_store import SemanticReviewStore
from orchestrator.domain.json_fingerprint import json_fingerprint


class SemanticPolicyCatalogTests(unittest.TestCase):
    def setUp(self):
        self.fixture = json.loads((Path(__file__).parent / 'fixtures/semantic_policy_live_failure.json').read_text())
        self.fields = self.fixture['fields']
        self.basis = {k: self.fixture[k] for k in ('requirements', 'policies')}
        self.basis['policyEvidence'] = policy_evidence(self.basis['policies'])

    def inspect(self, rows=None, basis=None):
        basis = self.basis if basis is None else basis
        hashes = {'candidate_sha': json_fingerprint(self.fields), 'basis_sha': json_fingerprint(basis)}
        data = {'candidateSha256': hashes['candidate_sha'], 'basisSha256': hashes['basis_sha'],
                'fieldReviews': self.fixture['fieldReviews'] if rows is None else rows}
        return inspect_result(data, self.fields, basis, **hashes)

    def quote(self, path):
        return next(q for q in self.basis['policyEvidence']['quotes'] if q.startswith(path + ' = '))

    def repaired_rows(self):
        rows = copy.deepcopy(self.fixture['fieldReviews'])
        paths = {
            '/implementationPlan/0/description': ['/emailPolicy/' + k for k in
                ('canonicalEmailUniqueConstraint', 'domainCase', 'localPartCase', 'trimWhitespace')],
            '/implementationPlan/1/title': ['/emailPolicy/canonicalEmailUniqueConstraint'],
            '/implementationPlan/1/description': ['/emailPolicy/canonicalEmailUniqueConstraint'],
            '/implementationPlan/2/title': ['/securityPolicy/passwordHashPolicy/' + k for k in
                ('algorithm', 'iterations', 'memoryKiB')],
            '/implementationPlan/2/description': ['/securityPolicy/passwordHashPolicy/' + k for k in
                ('algorithm', 'iterations', 'memoryKiB')],
        }
        for field, selected in paths.items():
            rows[field]['evidence'] = [x for x in rows[field]['evidence'] if x['sourceRef'] != '@policy']
            rows[field]['evidence'] += [{'sourceRef': '@policy', 'quote': self.quote(path)} for path in selected]
        return rows

    def test_real_failure_reproduces_all_five_before_and_after_catalog(self):
        legacy = {k:v for k,v in self.basis.items() if k != 'policyEvidence'}
        for basis in (legacy, self.basis):
            with self.subTest(catalog='policyEvidence' in basis):
                result = self.inspect(basis=basis)
                self.assertEqual(result.status, 'HELD')
                self.assertEqual(result.reason_code, 'SEMANTIC_EVIDENCE_INVALID')
                self.assertEqual(list(result.issues), self.fixture['expectedIssues'])

    def test_same_real_fields_accept_explicit_catalog_citations(self):
        rows = self.repaired_rows()
        self.assertEqual(self.inspect(rows).status, 'SUPPORTED')
        self.assertEqual(self.inspect(rows).issues, ())

    def test_types_paths_lists_and_empty_collections_remain_distinct(self):
        result = policy_evidence({'p': {'b':True, 'f':False, 'n':2, 'float':2.5, 's':'2', 'z':None,
                                       'a/b~c': [1, 'a'], 'emptyList':[], 'emptyObject':{}}})
        self.assertEqual(set(result['quotes']), {
            '/p/b = true', '/p/f = false', '/p/n = 2', '/p/float = 2.5', '/p/s = "2"', '/p/z = null',
            '/p/a~1b~0c/0 = 1', '/p/a~1b~0c/1 = "a"', '/p/emptyList = []', '/p/emptyObject = {}'})

    def test_catalog_order_deterministic_and_input_unchanged(self):
        policies = copy.deepcopy(self.basis['policies'])
        before = copy.deepcopy(policies)
        reverse = dict(reversed(list(policies.items())))
        self.assertEqual(policy_evidence(policies), policy_evidence(reverse))
        self.assertEqual(policies, before)

    def test_only_full_path_and_exact_typed_value_are_accepted(self):
        valid = self.quote('/emailPolicy/canonicalEmailUniqueConstraint')
        for quote in ('true', 'canonicalEmailUniqueConstraint', valid.replace('true', 'false'),
                      valid.replace('true', '"true"'), valid + '; ' + self.quote('/emailPolicy/trimWhitespace'),
                      valid + ' ', valid.replace('canonicalEmailUniqueConstraint', 'unknownPolicy')):
            with self.subTest(quote=quote):
                rows = self.repaired_rows()
                rows['/implementationPlan/1/title']['evidence'][-1]['quote'] = quote
                result = self.inspect(rows)
                self.assertEqual(result.status, 'HELD')
                self.assertEqual(len(result.issues), 1)
                self.assertEqual(result.issues[0]['reasonCode'], 'QUOTE_NOT_IN_SOURCE')

    def test_catalog_tampering_never_falls_back_to_old_string_rules(self):
        for mutate in ('quote', 'version', 'missingQuotes', 'extra', 'stringVersion', 'null'):
            with self.subTest(mutate=mutate):
                basis = copy.deepcopy(self.basis)
                if mutate == 'quote': basis['policyEvidence']['quotes'].append('forged')
                elif mutate == 'version': basis['policyEvidence']['schemaVersion'] = 2
                elif mutate == 'missingQuotes': del basis['policyEvidence']['quotes']
                elif mutate == 'extra': basis['policyEvidence']['approved'] = True
                elif mutate == 'stringVersion': basis['policyEvidence']['schemaVersion'] = '1'
                else: basis['policyEvidence'] = None
                result = self.inspect(self.repaired_rows(), basis)
                self.assertEqual((result.status, result.category), ('HELD', 'BINDING'))
                self.assertEqual(result.issues[0]['reasonCode'], 'POLICY_CATALOG_INVALID')

    def test_requirement_and_policy_sources_cannot_be_swapped(self):
        rows = self.repaired_rows()
        evidence = rows['/implementationPlan/1/title']['evidence']
        evidence[-1]['sourceRef'] = evidence[0]['sourceRef']
        self.assertEqual(self.inspect(rows).status, 'HELD')

    def test_valid_catalog_citation_does_not_override_meaning_rejection(self):
        for status in ('UNSUPPORTED', 'UNVERIFIED'):
            rows = self.repaired_rows()
            rows['/implementationPlan/1/title']['status'] = status
            self.assertEqual((self.inspect(rows).status, self.inspect(rows).category), ('HELD', 'MEANING'))

    def test_policy_key_and_false_value_do_not_approve_broad_data_exclusion(self):
        self.basis['policies']['do not collect personal data'] = False
        self.basis['policyEvidence'] = policy_evidence(self.basis['policies'])
        self.fields['/implementationPlan/1/title'] = '개인정보를 수집하지 않는 서비스'
        result = self.inspect(self.repaired_rows())
        self.assertEqual((result.status, result.category, result.reason_code),
                         ('HELD', 'MEANING', 'UNAPPROVED_DATA_SCOPE'))

    def test_bounds_and_secret_rules_do_not_leak_values_in_errors(self):
        for policies in ({'p': float('nan')}, {'p': ['x'] * 1025}, {'p': 'x' * 4097},
                         {'p': 'password=do-not-copy-this'}):
            with self.subTest(kind=type(policies['p'])):
                with self.assertRaises(ValueError) as error:
                    policy_evidence(policies)
                self.assertEqual(str(error.exception), 'SEMANTIC_POLICY_CATALOG_INVALID')
                self.assertIsNone(error.exception.__context__)


class SemanticPolicyResumeTests(unittest.IsolatedAsyncioTestCase):
    async def test_old_and_new_holds_use_their_recorded_basis_version_without_rejudging(self):
        with tempfile.TemporaryDirectory() as directory:
            store = SemanticReviewStore(Path(directory) / 'reviews.sqlite3')
            reviewer = ProductSemanticReviewer(object(), store)
            for version in (2, 3):
                with self.subTest(version=version):
                    run, step, task, context = (str(uuid4()) for _ in range(4))
                    basis = {'requirements': [], 'policies': {'emailPolicy': {'trimWhitespace': True}}}
                    if version == 3: basis['policyEvidence'] = policy_evidence(basis['policies'])
                    binding = {'runId': run, 'workflowStepId': step, 'a2aTaskId': task,
                               'agentContextId': context, 'stepRole': 'DEVELOPER',
                               'basisSha256': json_fingerprint(basis)}
                    if version == 3: binding['semanticReviewVersion'] = 3
                    original = store.append(binding=binding, candidate={}, result={}, status='HELD',
                                            reason='SEMANTIC_EVIDENCE_INVALID', resume_policy='NEW_RUN_REQUIRED')
                    execution = SimpleNamespace(metadata=SimpleNamespace(run_id=run, workflow_step_id=step))
                    with patch('agents.semantic_review.approved_basis', return_value=basis) as approved:
                        with self.assertRaises(SemanticReviewRequired) as held:
                            await reviewer.check_resume(SimpleNamespace(task_id=task, context_id=context), execution)
                    self.assertEqual(approved.call_args.kwargs['review_version'], version)
                    self.assertEqual(held.exception.record, original)
                    self.assertEqual(store.latest(run, step, task), original)
