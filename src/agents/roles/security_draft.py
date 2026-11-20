"""Host-bound references and a typed pending state for generated Security tests.

The wire draft chooses immutable source anchors, never invents coordinates.
Decoding is a representation change, not proof of a read or a product verdict;
SecurityRuntimeServices still verifies reads, Snapshot identity and tool evidence.
The legacy independently verified proof path keeps its original contract.
"""
from hashlib import sha256

from agents.llm.contracts import JsonSchema, StructuredOutput, json_text, parse_json
from agents.roles.security_contract import MAX_SECURITY_REFERENCE_LINES, SecurityContractError

SECURITY_DRAFT_POLICY = 'HOST_ANCHORED_SECURITY_TEST_PLAN_V1'
PENDING_TESTS = 'HOST_TESTS_PENDING'
MAX_ANCHORS = 2048


class SecurityTestDraft:
    def __init__(self, source_files, snapshot_sha256):
        anchors = {}
        unanchored = {}
        if (not isinstance(snapshot_sha256, str) or len(snapshot_sha256) != 64
                or any(c not in '0123456789abcdef' for c in snapshot_sha256)):
            raise ValueError('SECURITY_DRAFT_SNAPSHOT_INVALID')
        for path, content in sorted(source_files.items()):
            try:
                text = content.decode('utf-8')
            except UnicodeDecodeError:
                # Binary assets remain in the Snapshot/scanner inventory. They
                # cannot be cited as UTF-8 lines through read_project_file.
                unanchored[path] = 'NON_UTF8'
                continue
            lines = text.replace('\r\n', '\n').replace('\r', '\n').split('\n')
            if lines[-1] == '':
                lines.pop()
            if not lines:
                unanchored[path] = 'EMPTY_FILE'
            for start in range(1, len(lines) + 1, MAX_SECURITY_REFERENCE_LINES):
                end = min(start + MAX_SECURITY_REFERENCE_LINES - 1, len(lines))
                identity = 'S' + sha256(json_text([snapshot_sha256, path, start, end]).encode()).hexdigest()[:24]
                if len(anchors) >= MAX_ANCHORS or identity in anchors:
                    raise ValueError('SECURITY_DRAFT_CATALOG_LIMIT')
                anchors[identity] = {'path': path, 'startLine': start, 'endLine': end}
        self._catalog_json = json_text({'policy': SECURITY_DRAFT_POLICY,
            'snapshotSha256': snapshot_sha256, 'anchors': anchors,
            'unanchoredPaths': unanchored}, max_bytes=512 * 1024)

    @property
    def catalog(self):
        return parse_json(self._catalog_json, max_bytes=512 * 1024)

    def output(self, output):
        schema = output.schema.to_dict()
        anchors = self.catalog['anchors']
        for field in ('requirementReviews', 'findingReviews'):
            properties = schema['properties'][field]['items']['properties']
            references = properties['references']
            references['items'] = {'type': 'object', 'additionalProperties': False,
                'required': ['anchorId'], 'properties': {
                    'anchorId': {'type': 'string', 'enum': list(anchors) or ['NO_SOURCE_ANCHOR']}}}
            if not anchors:
                references['maxItems'] = 0
        requirement = schema['properties']['requirementReviews']['items']['properties']
        requirement['proposedOutcome'] = {'type': 'string', 'enum': ['UNVERIFIED']}
        requirement['rationale'] = {'type': 'string', 'enum': [PENDING_TESTS],
            'description': 'Typed pending state, not a product claim. The Host will execute testCases.'}
        narrowed = JsonSchema.from_dict(schema)
        narrowed.require_openai_strict()
        return StructuredOutput(name=output.name, schema=narrowed)

    def restrict_to_read(self, output, read_paths):
        """Codex review 2026-11-18 #5 (#21): every anchor stays listed for exploration in the task input,
        but the answer may cite only anchors of files read in this step (same Snapshot). Nothing is
        unlocked here: decode/finalize still apply the full anchor proof."""
        schema = output.schema.to_dict()
        readable = [identity for identity, anchor in self.catalog['anchors'].items() if anchor['path'] in read_paths]
        for field in ('requirementReviews', 'findingReviews'):
            references = schema['properties'][field]['items']['properties']['references']
            references['items']['properties']['anchorId']['enum'] = readable or ['NO_SOURCE_ANCHOR']
            if readable:
                references.pop('maxItems', None)
            else:
                references['maxItems'] = 0
        narrowed = JsonSchema.from_dict(schema)
        narrowed.require_openai_strict()
        return StructuredOutput(name=output.name, schema=narrowed)

    def decode(self, wire):
        """Resolve exactly the advertised IDs, retaining the original wire in telemetry."""
        try:
            result = parse_json(json_text(wire))
            anchors = self.catalog['anchors']
            if type(result) is not dict or 'testCases' not in result:
                raise ValueError()
            for field in ('requirementReviews', 'findingReviews'):
                for row in result[field]:
                    if field == 'requirementReviews' and (
                            row['proposedOutcome'] != 'UNVERIFIED' or row['rationale'] != PENDING_TESTS):
                        raise ValueError()
                    decoded = []
                    for ref in row['references']:
                        if type(ref) is not dict or set(ref) != {'anchorId'} or ref['anchorId'] not in anchors:
                            raise ValueError()
                        decoded.append(dict(anchors[ref['anchorId']]))
                    row['references'] = decoded
            return result
        except Exception:
            raise SecurityContractError() from None

    def reference_issues(self, services, measured, wire):
        issues = services.reference_issues(measured, self.decode(wire))
        # Feedback names the model's actual wire field, not resolved Host coordinates.
        return [{**issue, 'fieldPath': issue['fieldPath'].rsplit('/', 1)[0] + '/anchorId'}
                for issue in issues]


def pending_fields(candidate):
    """Exact closed wire markers only; free text and finding reviews remain reviewed.

    Called only for Security. No substring/keyword/fuzzy matching or replacement
    of a model's meaning judgment is allowed.
    """
    if (type(candidate) is not dict or candidate.get('kind') != 'READY'
            or not isinstance(candidate.get('testCases'), list) or not candidate['testCases']):
        return set()
    result = set()
    for i, row in enumerate(candidate.get('requirementReviews', [])):
        if (type(row) is dict and set(row) == {'requirementId', 'proposedOutcome', 'rationale', 'references'}
                and row['proposedOutcome'] == 'UNVERIFIED' and row['rationale'] == PENDING_TESTS
                and type(row['references']) is list
                and all(type(ref) is dict and set(ref) == {'anchorId'} for ref in row['references'])):
            result.add(f'/requirementReviews/{i}/rationale')
    return result


TEST_PLAN_INSTRUCTIONS = (
    'sourceReferenceCatalog is Host-owned and bound to this frozen Snapshot. '
    'Read each source file you cite, then choose its anchorId from the catalog. The answer schema offers '
    'only anchorIds of files you have already read in this step, so read a file before citing it. '
    'References contain only anchorId; never calculate or type path/startLine/endLine. '
    'unanchoredPaths names files with no UTF-8 line anchor; they remain in the source inventory. '
    'Catalog ranges are source locations, not proof that a requirement passed. '
    'Every requirementReviews row is a pre-execution state: proposedOutcome must be UNVERIFIED '
    'and rationale must be HOST_TESTS_PENDING. This marker is not free prose. '
    'Express the approved test intent in testCases.title and the exact acceptance criterion in '
    'testCases.expectedResult; do not put implementation observations or claimed verification limits '
    'into requirementReviews. The Host executes generated and protected tests and records their results. '
    'Finding reviews remain evidence-bound proposals, not confirmed product outcomes.'
)
