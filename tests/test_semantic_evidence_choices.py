"""Reproduce wrong citation pairs; constrain generation without repairing them."""
import copy
import json
from pathlib import Path

import pytest

from agents.llm.contracts import LLMRuntimeError
from agents.semantic_review import (evidence_choices, evidence_catalog, inspect_result,
                                    resolve_evidence_ids, review_output)
from orchestrator.domain.json_fingerprint import json_fingerprint


@pytest.fixture
def case():
    return json.loads((Path(__file__).parent / 'fixtures/semantic_source_pair_live_failure.json').read_text())


def prepare(case):
    fields, basis = case['fields'], case['basis']
    hashes = {'candidate_sha': json_fingerprint(fields), 'basis_sha': json_fingerprint(basis)}
    response = {'candidateSha256': hashes['candidate_sha'], 'basisSha256': hashes['basis_sha'],
                'fieldReviews': copy.deepcopy(case['fieldReviews'])}
    choices = evidence_choices(basis)
    output = review_output(fields, list(choices), catalog=evidence_catalog(basis), **hashes)
    return response, choices, output, hashes


def wire_response(response, case):
    result = copy.deepcopy(response)
    catalog = evidence_catalog(case['basis'])
    pairs = {(item['sourceRef'], item['quote']): key for key, item in catalog.items()}
    for row in result['fieldReviews'].values():
        for item in row['evidence']:
            if 'quote' in item:
                quote = item.pop('quote')
                item['quoteId'] = pairs.get((item.get('sourceRef'), quote), 'INVALID_ID')
    return result


def valid_response(case):
    # Synthetic positive fixture only. Production never reattributes evidence.
    response, choices, output, hashes = prepare(case)
    for row in response['fieldReviews'].values():
        for item in row['evidence']:
            matches = [ref for ref, quotes in choices.items() if item['quote'] in quotes]
            assert len(matches) == 1
            item['sourceRef'] = matches[0]
    return response, choices, output, hashes


def test_actual_run_wrong_pairs_are_rejected_without_mutation(case):
    before = copy.deepcopy(case)
    response, choices, output, hashes = prepare(case)
    result = inspect_result(response, case['fields'], case['basis'], **hashes)
    assert result.status == 'HELD'
    assert sum(x['reasonCode'] == 'QUOTE_NOT_IN_SOURCE' for x in result.issues) == 17
    with pytest.raises(LLMRuntimeError):
        output.schema.validate(wire_response(response, case))
    assert case == before


def test_exact_pairs_are_strict_and_do_not_skip_meaning_gate(case):
    response, choices, output, hashes = valid_response(case)
    output.schema.require_openai_strict()
    wire = wire_response(response, case)
    output.schema.validate(wire)
    assert resolve_evidence_ids(wire, evidence_catalog(case['basis'])) == response
    assert inspect_result(response, case['fields'], case['basis'], **hashes).status == 'SUPPORTED'
    row = next(iter(response['fieldReviews'].values()))
    row.update(status='UNSUPPORTED', reasonCode='UNAPPROVED_SCOPE')
    output.schema.validate(wire_response(response, case))
    result = inspect_result(response, case['fields'], case['basis'], **hashes)
    assert (result.status, result.category) == ('HELD', 'MEANING')


@pytest.mark.parametrize('mutation', ['foreign_source', 'policy_as_requirement', 'joined', 'paraphrased', 'missing', 'extra'])
def test_impossible_evidence_combinations_fail_the_generation_schema(case, mutation):
    response, choices, output, hashes = valid_response(case)
    evidence = next(iter(response['fieldReviews'].values()))['evidence'][0]
    if mutation == 'foreign_source': evidence['sourceRef'] = 'foreign'
    elif mutation == 'policy_as_requirement': evidence['quote'] = choices['@policy'][0]
    elif mutation == 'joined': evidence['quote'] += '; ' + choices['@policy'][0]
    elif mutation == 'paraphrased': evidence['quote'] = 'summarized text'
    elif mutation == 'missing': del evidence['sourceRef']
    else: evidence['approved'] = True
    with pytest.raises(LLMRuntimeError): output.schema.validate(wire_response(response, case))


def test_selection_catalog_is_frozen_and_shared_across_many_fields(case):
    before = copy.deepcopy(case['basis'])
    choices = evidence_choices(case['basis'])
    fields = {f'/tasks/{i}/title': 'task' for i in range(256)}
    output = review_output(fields, list(choices), catalog=evidence_catalog(case['basis']), candidate_sha='a'*64, basis_sha='b'*64)
    assert len(json.dumps(output.schema.to_dict())) < 60000
    assert case['basis'] == before
    case['basis']['policyEvidence']['quotes'].append('forged choice')
    with pytest.raises(ValueError, match='SEMANTIC_EVIDENCE_CHOICES_INVALID'):
        evidence_choices(case['basis'])


def test_wire_enum_contains_ids_instead_of_policy_text_and_unknown_ids_fail_closed(case):
    response, choices, output, hashes = valid_response(case)
    catalog = evidence_catalog(case['basis'])
    assert any('"' in item['quote'] for item in catalog.values())
    def inspect_enums(node):
        if type(node) is dict:
            for value in node.get('enum', []):
                assert '"' not in value and '\n' not in value
            for child in node.values(): inspect_enums(child)
        elif type(node) is list:
            for child in node: inspect_enums(child)
    inspect_enums(output.schema.to_dict())
    wire = wire_response(response, case)
    before = copy.deepcopy(wire)
    item = next(iter(wire['fieldReviews'].values()))['evidence'][0]
    item['quoteId'] = 'Q9999'
    resolved = resolve_evidence_ids(wire, catalog)
    assert inspect_result(resolved, case['fields'], case['basis'], **hashes).status == 'HELD'
    assert item['quoteId'] == 'Q9999'
    assert resolve_evidence_ids(before, catalog) == response


def test_wrong_source_id_pair_is_never_reattributed(case):
    response, choices, output, hashes = valid_response(case)
    catalog = evidence_catalog(case['basis'])
    wire = wire_response(response, case)
    item = next(iter(wire['fieldReviews'].values()))['evidence'][0]
    original_ref = item['sourceRef']
    item['quoteId'] = next(key for key, value in catalog.items() if value['sourceRef'] != original_ref)
    with pytest.raises(LLMRuntimeError): output.schema.validate(wire)
    decoded = resolve_evidence_ids(wire, catalog)
    observed = next(iter(decoded['fieldReviews'].values()))['evidence'][0]
    assert observed['sourceRef'] == original_ref and observed['quote'] == ''
    assert inspect_result(decoded, case['fields'], case['basis'], **hashes).status == 'HELD'
