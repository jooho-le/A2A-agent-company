"""Host-discovered QA IDs. Never import generated tests or infer requirement bindings."""
from hashlib import sha256
from pathlib import Path

from agents.llm.contracts import JsonSchema, StructuredOutput, json_text
from agents.runtime.generated_tests_precheck import test_ids
from agents.runtime.generated_test_inventory import MAX_FILE_BYTES, unit_inventory, scope_root
from orchestrator.core.security import redact_data

QA_DRAFT_POLICY = 'HOST_DISCOVERED_QA_IDS_AND_EXHAUSTION_V2'
# Same catalog and exhaustion rules for generated Security tests (Codex review 2026-11-18 #1·#2).
SECURITY_ID_POLICY = 'HOST_DISCOVERED_SECURITY_IDS_AND_EXHAUSTION_V2'


def case_repair_targets(catalog, output_text):
    """Exact missing bindings from known generated IDs, never requirement guesses."""
    from agents.llm.contracts import parse_json
    if not isinstance(output_text, str):
        return {}
    try:
        data = parse_json(output_text, max_bytes=1_048_576)
    except Exception:
        return {}
    if not isinstance(data, dict) or data.get('kind') != 'READY':
        return {}
    field = 'cases' if 'cases' in data else 'testCases'
    rows = data.get(field)
    if not isinstance(rows, list) or len(rows) > 256:
        return {}
    expected = set()
    for key, tool in (('cases', 'run_unit_tests'), ('browserCases', 'run_browser_tests')):
        for row in catalog.get(key, [])[:256]:
            if (isinstance(row, dict) and all(isinstance(row.get(k), str) for k in ('selector', 'testId'))
                    and len(row['selector']) <= 64 and len(row['testId']) <= 512 and redact_data(row) == row):
                expected.add((tool, row['selector'], row['testId']))
    declared = set()
    for row in rows:
        if not isinstance(row, dict):
            continue
        # Security's existing testCases schema is unit-only and has no toolName.
        tool = 'run_unit_tests' if field == 'testCases' else row.get('toolName')
        binding = {key: row.get(key) for key in ('selector', 'testId')}
        binding['toolName'] = tool
        if (all(isinstance(value, str) for value in binding.values())
                and tool in ('run_unit_tests', 'run_browser_tests')
                and len(binding['selector']) <= 64 and len(binding['testId']) <= 512
                and redact_data(binding) == binding):
            declared.add((tool, binding['selector'], binding['testId']))
    resolved = {(tool, selector) for tool, selector, _ in expected
                if not (tool == 'run_unit_tests' and selector in catalog.get('unresolvedSelectors', []))}
    missing = sorted(expected - declared)
    unexpected = sorted(row for row in declared - expected if row[:2] in resolved)
    def project(values):
        return [dict(zip(('toolName', 'selector', 'testId'), row)) for row in values[:32]]
    instruction = ('Add every missing binding with its exact toolName, selector and testId. '
                   if field == 'cases' else
                   'Add every missing binding to testCases with its exact selector and testId. '
                   'toolName in these hints is routing metadata; do not add it to Security testCases. ')
    return {'missingCaseBindings': project(missing), 'missingCaseCount': len(missing),
            'unexpectedCaseBindings': project(unexpected), 'unexpectedCaseCount': len(unexpected),
            'requiredGeneratedCaseCount': len(expected),
            'bindingInstruction': instruction +
                'Choose requirementId and expectedResult only from assigned requirementScope. Do not remove '
                'required test methods to shorten READY. These IDs are not outcomes or permission to change tests.'}


def refresh_qa_output(output, tests_dir, scopes, *, snapshot_sha256, unit_only, field='cases',
                      policy=QA_DRAFT_POLICY, browser_suites=()):
    """Host-discovered test IDs for ``<field>[].testId`` (QA ``cases``, Security ``testCases``)."""
    root = Path(tests_dir)
    rows, unresolved = [], []
    hashes = {}
    browser_rows, missing_writes = [], []
    for scope in scopes:
        selector, pattern = scope[:2]
        selected_root = scope_root(root, scope[2] if len(scope) == 3 else 'tests')
        inventory = unit_inventory(selected_root, pattern)
        ids = set()
        complete = inventory.complete and bool(inventory.files)
        for path, module in inventory.files if inventory.complete else ():
            with path.open('rb') as stream:
                raw = stream.read(MAX_FILE_BYTES + 1)
            if len(raw) > MAX_FILE_BYTES:
                complete = False
                continue
            hashes[selector + ':' + path.relative_to(selected_root).as_posix()] = sha256(raw).hexdigest()
            found = test_ids(path, module)
            if found is None:
                complete = False
                continue
            ids.update(found)
        if not complete or not ids or len(ids) > 256 or redact_data(sorted(ids)) != sorted(ids):
            unresolved.append(selector)
            continue
        rows.extend({'selector': selector, 'testId': value} for value in sorted(ids))
    if len(rows) > 256:
        raise ValueError('QA_CATALOG_LIMIT')
    # Browser suites have independent discovery. Their directory must neither
    # disable Python IDs nor disappear from the draft's repair instructions.
    from pathlib import PurePosixPath
    from mcp_tools.tools.browser_contract import parse_browser_suite
    for selector, relative in browser_suites:
        parts = PurePosixPath(relative).parts
        if (not parts or parts[0] != 'tests' or PurePosixPath(relative).is_absolute()
                or any(p in ('.', '..') for p in parts) or '\\' in relative):
            raise ValueError('QA_CATALOG_PATH_INVALID')
        path = root.parent
        for part in parts:
            path /= part
            if path.is_symlink():
                raise ValueError('QA_CATALOG_PATH_INVALID')
        tool_path = 'outputs/qa/' + relative
        if not path.is_file():
            missing_writes.append(tool_path)
            continue
        try:
            with path.open('rb') as stream:
                raw = stream.read(MAX_FILE_BYTES + 1)
            if len(raw) > MAX_FILE_BYTES:
                continue
            hashes['browser:' + relative] = sha256(raw).hexdigest()
            suite = parse_browser_suite(raw.decode('utf-8'), selector)
        except (OSError, ValueError, UnicodeError):
            continue  # the precheck supplies BROWSER_SUITE_INVALID, not fabricated IDs
        browser_rows.extend({'selector': selector, 'testId': case['testId'], 'path': tool_path}
                            for case in suite['tests'])
    if len(browser_rows) > 256 or redact_data(browser_rows) != browser_rows:
        raise ValueError('QA_CATALOG_LIMIT')
    catalog = {'policy': policy, 'snapshotSha256': snapshot_sha256,
        'filesSha256': sha256(json_text(hashes).encode()).hexdigest(),
        'cases': rows, 'unresolvedSelectors': unresolved,
        'browserCases': browser_rows, 'missingRequiredWritePaths': missing_writes,
        'instruction': 'Host-discovered unittest IDs, not outcomes or requirement bindings. '
            'Choose exact testId values for the matching selector; never add a tests. prefix or omit the class. '
            'For unresolved scopes write simple top-level test_*.py unittest.TestCase classes first. '
            'After writing files the Host refreshes this catalog. Keep all assigned requirements and tests. '
            'Browser IDs are in browserCases; they do not change Python module.Class.method IDs. '
            'Write every missingRequiredWritePaths entry before READY; rewriting another file does not satisfy it.'}
    schema = output.schema.to_dict()
    if unit_only and rows and not unresolved:
        schema['properties'][field]['items']['properties']['testId']['enum'] = sorted({r['testId'] for r in rows})
    narrowed = JsonSchema.from_dict(schema)
    narrowed.require_openai_strict()
    return StructuredOutput(name=output.name, schema=narrowed), catalog
