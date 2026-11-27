"""Constrain generated cases' ``expectedResult`` to the assigned acceptance criteria.

A generated QA/Security case must quote one acceptance criterion of its
requirement verbatim (``validate_generated_case_expectations``). gpt-4.1-mini
repeatedly failed to write the middle dot of "승인한 알고리즘·파라미터 충족, …":
one U+0000 in live Run 3754980f, and 2,469 of them until the 8,192-token cap in
live Run 42f131fe (docs/live-api problem P3). Listing the criteria as an
``enum`` in the strict output schema lets the provider's constrained decoding
produce only those exact strings, so the character never has to be typed.

Only the wire schema is narrowed. The role validators still rebuild and check
their own schema, and the expectation check still binds each criterion to its
own requirement. The criteria contain no quotes (batch4's API 400 was quotes in
an enum); this module refuses to build an enum that would contain them.

Callers apply it only when ``llmLimits.max_output_corrections`` is on (measurement
settings), like the other recovery measures: with it, an invented expectation is
rejected by the schema (``LLM_SCHEMA_INVALID``) instead of the product's
``REQUIREMENT_SCOPE_INVALID`` path.
"""
from agents.llm.contracts import JsonSchema, StructuredOutput

FORBIDDEN = ('"', '\\')


def with_expected_results(output: StructuredOutput, field: str, criteria) -> StructuredOutput:
    """A copy of ``output`` whose ``<field>[].expectedResult`` must be one of ``criteria``."""
    values = list(dict.fromkeys(value for value in criteria if isinstance(value, str) and value))
    if not values or any(mark in value for value in values for mark in FORBIDDEN):
        return output
    schema = output.schema.to_dict()
    try:
        item = schema['properties'][field]['items']
        if 'expectedResult' not in item['properties']:
            return output
    except (KeyError, TypeError):
        return output
    item['properties']['expectedResult'] = {'type': 'string', 'enum': values}
    narrowed = JsonSchema.from_dict(schema)
    narrowed.require_openai_strict()
    return StructuredOutput(name=output.name, schema=narrowed)
