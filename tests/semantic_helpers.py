"""Explicit synthetic review responses for product composition tests; no API."""

import inspect
from dataclasses import replace

from agents.llm.contracts import LLMRuntimeError, json_text, parse_json
from test_llm_runtime import FakeProvider, text_response


def review_input(request):
    data = parse_json(parse_json(request.input_items_json)[0]['content'])
    return data['taskInput'] if data.get('trusted', {}).get('purpose') == 'PUBLICATION_SEMANTIC_REVIEW' else data


def supported_review(request):
    """Synthetic transport fixture, NOT a semantic-accuracy oracle.

    Explicit policy tests must supply field-specific citations. The default is
    retained only for workflow plumbing tests and must not certify live readiness.
    """
    data = review_input(request)
    source = data['basis']['requirements'][0]
    return {'candidateSha256': data['candidateSha256'], 'basisSha256': data['basisSha256'],
        'fieldReviews': {pointer: {'status': 'SUPPORTED', 'evidence': [{'sourceRef': source['requirementId'],
            'quote': source['description']}], 'reasonCode': 'DIRECT_SUPPORT'} for pointer in data['fields']}}


def policy_catalog_review(request, citations_by_field):
    """Use explicit frozen policy paths, with no first-requirement fallback."""
    data = review_input(request)
    if set(citations_by_field) != set(data['fields']):
        raise AssertionError('Every field needs explicit policy evidence in this fixture')
    catalog = data['basis']['policyEvidence']['quotes']
    rows = {}
    for pointer, paths in citations_by_field.items():
        evidence = []
        for path in paths:
            matches = [quote for quote in catalog if quote.startswith(path + ' = ')]
            if len(matches) != 1:
                raise AssertionError('Policy path absent or ambiguous')
            evidence.append({'sourceRef': '@policy', 'quote': matches[0]})
        rows[pointer] = {'status': 'SUPPORTED', 'reasonCode': 'DIRECT_SUPPORT', 'evidence': evidence}
    return {'candidateSha256': data['candidateSha256'], 'basisSha256': data['basisSha256'], 'fieldReviews': rows}


class SemanticFakeProvider(FakeProvider):
    def __init__(self, *script, review_decider=None):
        super().__init__(*script)
        self.review_decider = review_decider

    async def complete(self, request):
        if request.output.name != 'product_semantic_review_v5':
            return await super().complete(request)
        self.requests.append(request)
        if self.review_decider is None:
            action = text_response(json_text(supported_review(request)))
        else:
            action = self.review_decider(request)
            if inspect.isawaitable(action):
                action = await action
        if isinstance(action, BaseException):
            raise action
        # Version the transport fixture, preserving invalid citations as invalid
        # IDs. Never choose another source when the explicit pair is wrong.
        try:
            data = parse_json(action.output_text)
            catalog = review_input(request)['evidenceCatalog']
            pairs = {(item['sourceRef'], item['quote']): identity for identity, item in catalog.items()}
            rows = data.get('fieldReviews') if type(data) is dict else None
            for row in rows.values() if type(rows) is dict else ():
                if type(row) is not dict or type(row.get('evidence')) is not list:
                    continue
                for item in row['evidence']:
                    if type(item) is dict and 'quote' in item:
                        quote = item.pop('quote')
                        item['quoteId'] = pairs.get((item.get('sourceRef'), quote), 'INVALID_ID')
            encoded = json_text(data)
            items = parse_json(action.output_items_json)
            for item in items:
                for part in item.get('content', []):
                    if part.get('type') == 'output_text': part['text'] = encoded
            action = replace(action, output_text=encoded, output_items_json=json_text(items))
        except LLMRuntimeError:
            pass  # Deliberately malformed JSON fixtures remain malformed.
        return action


def patch_reply(text='A narrower claim stated only from the approved requirement.'):
    """Fake model reply to the semantic rewrite (docs/live-api/05 25절 V4): one new text per held
    pointer, read from the patch schema the Host offered in this request."""
    import json as _json
    from test_llm_runtime import text_response

    def reply(request):
        schema = request.output.schema.to_dict()
        pointers = schema['properties']['replacements']['items']['properties']['fieldPointer']['enum']
        return text_response(_json.dumps({'replacements': [{'fieldPointer': p, 'text': text} for p in pointers]},
                                         ensure_ascii=False))
    return reply
