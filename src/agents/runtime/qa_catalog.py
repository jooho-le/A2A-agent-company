"""Host-discovered QA IDs. Never import generated tests or infer requirement bindings."""
import fnmatch
from hashlib import sha256
from pathlib import Path

from agents.llm.contracts import JsonSchema, StructuredOutput, json_text
from agents.runtime.generated_tests_precheck import MAX_FILES, test_ids
from orchestrator.core.security import redact_data

QA_DRAFT_POLICY = 'HOST_DISCOVERED_QA_IDS_AND_EXHAUSTION_V1'
# Same catalog and exhaustion rules for generated Security tests (Codex review 2026-11-18 #1·#2).
SECURITY_ID_POLICY = 'HOST_DISCOVERED_SECURITY_IDS_AND_EXHAUSTION_V1'


def refresh_qa_output(output, tests_dir, scopes, *, snapshot_sha256, unit_only, field='cases',
                      policy=QA_DRAFT_POLICY):
    """Host-discovered test IDs for ``<field>[].testId`` (QA ``cases``, Security ``testCases``)."""
    root = Path(tests_dir)
    entries = sorted(root.iterdir()) if root.is_dir() and not root.is_symlink() else []
    supported = (len(entries) <= MAX_FILES and not any(
        p.is_symlink() or p.is_dir() and p.name != '__pycache__' for p in entries))
    rows, unresolved = [], []
    hashes = {}
    for selector, pattern in scopes:
        paths = [p for p in entries if p.suffix == '.py' and fnmatch.fnmatchcase(p.name, pattern)]
        ids = set()
        complete = supported and bool(paths)
        for path in paths if supported else ():
            found = test_ids(path)
            if found is None:
                complete = False
                continue
            ids.update(found)
            hashes[path.name] = sha256(path.read_bytes()).hexdigest()
        if not complete or not ids or len(ids) > 256 or redact_data(sorted(ids)) != sorted(ids):
            unresolved.append(selector)
            continue
        rows.extend({'selector': selector, 'testId': value} for value in sorted(ids))
    if len(rows) > 256:
        raise ValueError('QA_CATALOG_LIMIT')
    catalog = {'policy': policy, 'snapshotSha256': snapshot_sha256,
        'filesSha256': sha256(json_text(hashes).encode()).hexdigest(),
        'cases': rows, 'unresolvedSelectors': unresolved,
        'instruction': 'Host-discovered unittest IDs, not outcomes or requirement bindings. '
            'Choose exact testId values for the matching selector; never add a tests. prefix or omit the class. '
            'For unresolved scopes write simple top-level test_*.py unittest.TestCase classes first. '
            'After writing files the Host refreshes this catalog. Keep all assigned requirements and tests.'}
    schema = output.schema.to_dict()
    if unit_only and rows and not unresolved:
        schema['properties'][field]['items']['properties']['testId']['enum'] = sorted({r['testId'] for r in rows})
    narrowed = JsonSchema.from_dict(schema)
    narrowed.require_openai_strict()
    return StructuredOutput(name=output.name, schema=narrowed), catalog
