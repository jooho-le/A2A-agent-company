"""Run-local Developer conversation state. MCP receipts and the budget ledger stay authoritative."""

from dataclasses import dataclass
import re

from agents.llm.contracts import OPENAI_RESPONSES_GLOBAL_TEXT, json_text


@dataclass(frozen=True, repr=False)
class MeasuredInput:
    """A measured text-only prefix for one pinned model in one engine invocation."""

    history: str
    item_count: int
    wire_contract: tuple
    input_tokens: int

    @classmethod
    def from_receipt(cls, request, response, item_count):
        model_id = request.model.model_revision or request.model.model_id
        if (request.model.provider != 'openai' or response.status != 'completed'
                or response.billing_context != OPENAI_RESPONSES_GLOBAL_TEXT
                or response.model_id != model_id
                or not re.fullmatch(r'gpt-4\.1(?:-mini|-nano)?-\d{4}-\d{2}-\d{2}', model_id)
                or response.usage is None or response.usage.input_tokens <= 0):
            return None
        contract = (request.model, request.system_prompt, request.tools, request.output,
                    request.required_tool, request.required_tool_alternatives)
        return cls(request.input_items_json, item_count, contract, response.usage.input_tokens)

    def reserve(self, full_bound, history, items, wire_contract, *, repair_schema_changed=False):
        # JSON is an array; its closing bracket moves as complete, separate input items are appended.
        replacement_bytes = 0
        if self.wire_contract != wire_contract:
            if (not repair_schema_changed or self.wire_contract[:3] != wire_contract[:3]
                    or self.wire_contract[4:] != wire_contract[4:] or self.wire_contract[3].name != wire_contract[3].name):
                return full_bound
            # A Host-narrowed READY schema can change after each source edit. Count the entire new
            # schema; do not subtract the old schema's tokens or discount the measured input.
            replacement_bytes = len(wire_contract[3].schema.schema_json.encode('utf-8'))
        if (len(items) < self.item_count
                or not (history == self.history or history.startswith(self.history[:-1] + ','))):
            return full_bound
        added = items[self.item_count:]
        if any(item.get('type') not in (None, 'message', 'function_call', 'function_call_output') for item in added):
            return full_bound
        suffix_bytes = len(history[len(self.history) - 1:].encode('utf-8'))
        # Use the entire measured input, including cached tokens. Charge each new byte as up to
        # one token, with per-item framing and an additional allowance for request boundaries.
        measured_bound = self.input_tokens + replacement_bytes + suffix_bytes + 64 * len(added) + 4096
        return measured_bound if full_bound < self.input_tokens else min(full_bound, measured_bound)


class RepairReadHistory:
    """Reference identical, already delivered source bytes after each real, authorized MCP read."""

    def __init__(self, task):
        self.sent = {}
        inspection = task
        for key in ('activeRepairFocus', 'brief', 'sourceInspection', 'firstReadBatch'):
            inspection = inspection.get(key) if isinstance(inspection, dict) else None
        self.waiting = (frozenset(inspection) if isinstance(inspection, list) and
                        all(isinstance(path, str) and path.startswith('source/') for path in inspection)
                        else frozenset())

    def require_fresh_content(self, path):
        """A rejected fragment edit needs the bytes again, not a cached-read pointer.

        This only changes the next model transcript; the MCP read and its
        original evidence remain authoritative and are never skipped.
        """
        self.sent.pop(path, None)

    def completed_inspection(self):
        if self.waiting and self.waiting.issubset(self.sent):
            self.waiting = frozenset()
            return {'hostRepairInspection': 'All firstReadBatch files have been read. Use the file contents and '
                    'latest sha256 already supplied to edit the affected paths. Read more files only if a '
                    'specific unresolved dependency requires them. Submit READY after completing the edits.'}
        return None

    def for_model(self, name, arguments, result, serialized, call_id):
        path = arguments.get('path')
        if name == 'apply_patch':
            self.sent.clear()
        elif name in ('write_source_file', 'edit_source_file'):
            self.sent.pop(path, None)
        elif (name == 'read_project_file' and isinstance(path, str) and path.startswith('source/')
              and result.get('path') == path and set(result) == {'path', 'sha256', 'sizeBytes', 'content'}):
            first = self.sent.get(path)
            if first is not None and serialized == first[1]:
                pointer = json_text({'path': path, 'sha256': result['sha256'], 'sizeBytes': result['sizeBytes'],
                                     'unchangedSinceReadCallId': first[0]})
                return pointer if len(pointer.encode()) < len(serialized.encode()) else serialized
            self.sent[path] = (call_id, serialized)
        return serialized
