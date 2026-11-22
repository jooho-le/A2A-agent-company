"""One bounded rewrite of held free-text claims after a semantic-review meaning hold (docs/live-api/05 14·15절).

Live Run #27 (2026-11-18) stopped because the publication semantic review could not
confirm two Security rationale texts. The review stays the only judge: the producing
role gets the held field pointers and closed reason codes once and returns its READY/PLAN
again, and the same review runs on the new candidate.

The rewrite scope is enforced by the Host, not by the instruction (Codex review 2026-11-18):

* the rewrite runs on a tool-less engine, so no file can be read or written;
* the result must report no tool call or executed tool;
* every JSON value except the held string fields must be identical to the first candidate
  (same keys, list lengths, IDs, selectors, test cases, outcomes and references);
* only a meaning hold whose issues all point at free-text fields of the candidate qualifies.

A second hold, a technical (format/evidence/binding) hold, any other change, a model
format error during the rewrite or a non-qualifying hold keeps the original hold path.
No reviewer free text is shown to the producing role.
"""
from agents.llm.contracts import (JsonSchema, LLMErrorCode, LLMResult, LLMRuntimeError, StructuredOutput,
                                  json_text, parse_json)
from agents.semantic_review import TECHNICAL_CATEGORIES, SemanticReviewRequired, draft_fields

POLICY = 'ONE_SEMANTIC_REVISION_V4'
PATCH_OUTPUT_NAME = 'semantic_revision_patch_v1'
FEEDBACK_KEY = 'semanticReviewFeedback'
INSTRUCTION = (
    'The Host semantic review could not confirm the listed free-text fields of your previous answer. The review '
    'confirms a text only from reviewBasis: the approved requirements assigned to this step and the frozen '
    'policies. It never sees source code or test output. Return only replacements: for each fieldPointer in '
    'editablePointers exactly one new text that states only what reviewBasis directly says; narrow or drop any '
    'other claim, including implementation details. The Host puts these texts into previousAnswer; every other '
    'value stays exactly as it was. No tools are available for this answer and no file can change.')
# V4 (Codex review 2026-11-18 #6): the model returns only the held texts; the Host applies them to the
# first candidate, so IDs, test bindings, outcomes and references cannot change by construction.
# V3 (Codex review 2026-11-18 #3): the rewrite also receives the review's own basis, bounded; it is
# the approved requirements/policies the reviewer judges against, never code or measured results.
_BASIS_MAX_BYTES = 24 * 1024
# A model-format failure of the rewrite itself falls back to the original hold; provider,
# budget, authentication and configuration errors keep their own meaning.
_REWRITE_FORMAT_ERRORS = frozenset({LLMErrorCode.SCHEMA, LLMErrorCode.RESPONSE, LLMErrorCode.INCOMPLETE,
                                    LLMErrorCode.SECURITY_REFERENCE, LLMErrorCode.VALIDATION_DRAFT,
                                    LLMErrorCode.TOOL_POLICY, LLMErrorCode.NO_PROGRESS})
_NOT_REVISABLE = TECHNICAL_CATEGORIES | {'NONE'}


def held_pointers(held, candidate):
    """Free-text pointers of a meaning hold, or None when the hold does not qualify for a rewrite."""
    validation = held.record.get('validation') if isinstance(held.record, dict) else None
    if type(validation) is not dict or validation.get('category') in _NOT_REVISABLE:
        return None  # legacy or technical record: keep its original hold behavior
    issues = validation.get('issues')
    if not isinstance(issues, list) or not issues or not all(isinstance(item, dict) for item in issues):
        return None
    texts = draft_fields(candidate) if isinstance(candidate, dict) else {}
    pointers = [item.get('fieldPointer') for item in issues]
    if not all(isinstance(pointer, str) and pointer in texts for pointer in pointers):
        return None
    return frozenset(pointers)


def _shape(value, pointer='', out=None):
    out = {} if out is None else out
    if type(value) is dict:
        out[pointer] = ('object', tuple(sorted(value)))
        for key, child in value.items():
            _shape(child, pointer + '/' + key.replace('~', '~0').replace('/', '~1'), out)
    elif type(value) is list:
        out[pointer] = ('array', len(value))
        for index, child in enumerate(value):
            _shape(child, f'{pointer}/{index}', out)
    else:
        out[pointer] = ('value', json_text(value))
    return out


def only_held_texts_changed(before, after, pointers) -> bool:
    """True when ``after`` equals ``before`` except for non-empty string values at ``pointers``."""
    try:
        old, new = _shape(before), _shape(after)
    except (TypeError, ValueError, LLMRuntimeError):
        return False
    if old.keys() != new.keys():
        return False
    for pointer, value in old.items():
        if pointer in pointers:
            changed = parse_json(new[pointer][1]) if new[pointer][0] == 'value' else None
            if type(changed) is not str or not changed.strip():
                return False
        elif new[pointer] != value:
            return False
    return True


def revision_basis(execution, role):
    """The semantic review's own approved basis for this step (requirements + frozen policies), or None."""
    from agents.semantic_review import approved_basis
    try:
        basis = approved_basis(execution, role)
        view = {'requirements': basis['requirements'], 'policies': basis['policies']}
        return view if len(json_text(view).encode('utf-8')) <= _BASIS_MAX_BYTES else None
    except Exception:
        return None


def revision_input(task_input, previous, held, pointers, basis=None):
    """The original task input unchanged, plus one Host feedback object under FEEDBACK_KEY."""
    record = held.record
    issues = [{'fieldPointer': item.get('fieldPointer', ''), 'reasonCode': item.get('reasonCode'),
               'category': item.get('category')}
              for item in record['validation']['issues'][:64] if isinstance(item, dict)]
    feedback = {
        'policy': POLICY, 'reviewId': record.get('reviewId'), 'reasonCode': record.get('reasonCode'),
        'issues': issues, 'editablePointers': sorted(pointers),
        'instruction': INSTRUCTION, 'previousAnswer': parse_json(json_text(previous))}
    if basis is not None:
        feedback['reviewBasis'] = parse_json(json_text(basis))
    return {**dict(task_input), FEEDBACK_KEY: feedback}


def patch_output(pointers):
    """Strict output for the rewrite: one text per held pointer (the Host checks exact coverage)."""
    schema = {'type': 'object', 'additionalProperties': False, 'required': ['replacements'], 'properties': {
        'replacements': {'type': 'array', 'items': {
            'type': 'object', 'additionalProperties': False, 'required': ['fieldPointer', 'text'],
            'properties': {'fieldPointer': {'type': 'string', 'enum': sorted(pointers)},
                           'text': {'type': 'string'}}}}}}
    output = JsonSchema.from_dict(schema)
    output.require_openai_strict()
    return StructuredOutput(name=PATCH_OUTPUT_NAME, schema=output)


def _decode(pointer):
    return [part.replace('~1', '/').replace('~0', '~') for part in pointer.split('/')[1:]]


def apply_patch(data, patch, pointers):
    """The first candidate with exactly the held texts replaced, or None if the patch is not exact."""
    rows = patch.get('replacements') if isinstance(patch, dict) else None
    if not isinstance(rows, list) or len(rows) != len(pointers):
        return None
    texts = {}
    for row in rows:
        if (not isinstance(row, dict) or set(row) != {'fieldPointer', 'text'} or row['fieldPointer'] not in pointers
                or row['fieldPointer'] in texts or type(row['text']) is not str or not row['text'].strip()):
            return None
        texts[row['fieldPointer']] = row['text']
    if set(texts) != set(pointers):
        return None
    revised = parse_json(json_text(data))
    for pointer, text in texts.items():
        node, parts = revised, _decode(pointer)
        try:
            for part in parts[:-1]:
                node = node[int(part)] if type(node) is list else node[part]
            last = int(parts[-1]) if type(node) is list else parts[-1]
            if type(node[last]) is not str:
                return None
            node[last] = text
        except (KeyError, IndexError, ValueError, TypeError):
            return None
    return revised


async def review_with_revision(check, rerun_text, valid, task_input, result, *, allowed, basis=None):
    """``check(result)`` raises SemanticReviewRequired on a hold; returns the result that passed review.

    ``rerun_text(task_input, output)`` runs the role's model on a tool-less engine with ``output``
    (the patch schema) and no role-specific checks. ``valid(result)`` is the role's own contract check
    of the patched answer. The patched result keeps the first candidate's Tool facts.
    """
    try:
        await check(result)
        return result
    except SemanticReviewRequired as held:
        pointers = held_pointers(held, result.data) if allowed else None
        if pointers is None:
            raise
        try:
            patch = await rerun_text(revision_input(task_input, result.data, held, pointers, basis),
                                     patch_output(pointers))
        except LLMRuntimeError as error:
            if error.code in _REWRITE_FORMAT_ERRORS:
                raise held from None
            raise
        if patch.tool_calls or patch.executed_tool_names:
            raise held
        patched = apply_patch(result.data, patch.data, pointers)
        if patched is None or not only_held_texts_changed(result.data, patched, pointers):
            raise held
        revised = LLMResult(data_json=json_text(patched), records=patch.records, tool_calls=result.tool_calls,
                            executed_tool_names=result.executed_tool_names)
        if not valid(revised):
            raise held
        await check(revised)  # a second hold publishes the new record
        return revised
