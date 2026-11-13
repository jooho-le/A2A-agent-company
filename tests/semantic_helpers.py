"""Explicit synthetic review responses for product composition tests; no API."""

import inspect

from agents.llm.contracts import json_text, parse_json
from test_llm_runtime import FakeProvider, text_response


def review_input(request):
    data = parse_json(parse_json(request.input_items_json)[0]['content'])
    return data['taskInput'] if data.get('trusted', {}).get('purpose') == 'PUBLICATION_SEMANTIC_REVIEW' else data


def supported_review(request):
    data = review_input(request)
    source = data['basis']['requirements'][0]
    return {'candidateSha256': data['candidateSha256'], 'basisSha256': data['basisSha256'],
        'fieldReviews': {pointer: {'status': 'SUPPORTED', 'sourceRefs': [source['requirementId']],
            'sourceQuote': source['description'], 'reasonCode': 'DIRECT_SUPPORT'} for pointer in data['fields']}}


class SemanticFakeProvider(FakeProvider):
    def __init__(self, *script, review_decider=None):
        super().__init__(*script)
        self.review_decider = review_decider

    async def complete(self, request):
        if request.output.name != 'product_semantic_review_v1':
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
        return action
