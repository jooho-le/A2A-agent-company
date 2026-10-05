"""검사 결과를 dev 공통 QA_REPORT / SECURITY_REPORT 형식으로 바꾸는 에이전트 호환용 보조 경로.

실제 A2A Task·Artifact ID가 필요하다. 독립 평가 보고서는 independent.py를 쓴다.
도구 실행·A2A 전송·증거 확인·비밀정보 정리(context·findings·toolEvidence)는 하지 않는다.
"""
from copy import deepcopy
from dataclasses import dataclass
from importlib.resources import files
import json
from urllib.parse import urlsplit

from jsonschema import Draft202012Validator, FormatChecker
from referencing import Registry, Resource

from .aggregation import Status, _uuid4, aggregate
from .manifest import ExecutionManifest


@dataclass(frozen=True)
class TestBinding:
    """내부 검사 UUID → 보고서의 검사 이름(예: QA-SIGNUP-001)·요구사항 UUID·제목.

    pytest가 테스트 클래스로 오해하지 않도록 테스트에서는 다른 이름으로 import한다.
    """
    external_test_id: str
    requirement_id: str
    title: str


def validate_contract(payload: dict, name: str) -> None:
    """contracts/의 스키마 사본으로 payload를 검사한다(네트워크 미사용). 실패 시 ValidationError."""
    documents = [json.loads(path.read_text()) for path in
                 files('evaluation').joinpath('contracts').iterdir()
                 if path.name.endswith('.schema.json')]
    registry = Registry().with_resources(
        (doc['$id'], Resource.from_contents(doc)) for doc in documents
    )
    schema = next(doc for doc in documents if doc['$id'].endswith('/' + name + '.schema.json'))
    Draft202012Validator(schema, registry=registry, format_checker=FormatChecker()).validate(payload)


def _artifact_uri(value):
    """공유 가능한 저장소 주소인지 확인한다. 로컬 파일·드라이브 경로·호스트 없는 주소·공백은 거부."""
    parsed = urlsplit(value)
    if (not parsed.scheme or parsed.scheme.lower() == 'file' or len(parsed.scheme) == 1
            or not (parsed.netloc or parsed.path) or '\\' in value
            or any(c.isspace() for c in value)
            or (parsed.scheme.lower() in {'http', 'https', 's3', 'gs'} and not parsed.netloc)):
        raise ValueError('Expected a non-local Artifact Registry URI')


def _tool_evidence(evidence, manifest, role, outcome):
    """도구 실행 근거를 검사하고 복사본을 돌려준다(dev tool_evidence.py와 같은 규칙).

    - PASS·FAIL에는 근거 필수, Manifest 일치, 역할에 맞는 도구, 시도 번호 0부터 연속
    - 재시도는 안전한 인프라 오류 뒤에만(RESOURCE_BUSY 등, timeout류는 retrySafe=True일 때)
    - 마지막 시도: 검사가 PASS·FAIL이면 도구 PASS, UNVERIFIED면 도구 UNVERIFIED
      (도구는 정상 종료했는데 파싱 실패로 UNVERIFIED가 된 경우는 여기서 오류가 난다. OPEN-13)
    """
    if evidence is None:
        if outcome != 'UNVERIFIED':
            raise ValueError('PASS/FAIL requires actual Tool execution evidence')
        return None
    validate_contract(evidence, 'tool_execution_evidence')
    if ExecutionManifest.from_payload(evidence['executionManifest']) != manifest:
        raise ValueError('Tool evidence belongs to another Manifest')
    allowed = {'run_unit_tests', 'run_browser_tests'} if role == 'QA' else {'run_security_scan'}
    if evidence['toolName'] not in allowed:
        raise ValueError('Tool evidence does not match the report role')
    _artifact_uri(evidence['evidenceRef'])
    attempts = evidence['attempts']
    for item in attempts:
        _artifact_uri(item['evidenceRef'])
    if [item['attempt'] for item in attempts] != list(range(len(attempts))):
        raise ValueError('Tool attempts must start at zero and be contiguous')
    for item in attempts[:-1]:
        safe = item.get('errorKind') in {'PROCESS_STARTUP_FAILURE', 'RESOURCE_BUSY'} or (
            item.get('retrySafe') is True and item.get('errorKind') in
            {'TIMEOUT', 'TOOL_TIMEOUT', 'MCP_TRANSPORT_INTERRUPTED'})
        if item['outcome'] != 'UNVERIFIED' or not safe:
            raise ValueError('Retry follows a non-retryable attempt')
    expected = 'UNVERIFIED' if outcome == 'UNVERIFIED' else 'PASS'
    if attempts[-1]['outcome'] != expected:
        raise ValueError('Tool completion and product outcome are inconsistent')
    return deepcopy(evidence)


def build_report(*, role, context, expected, observed, plan, results,
                 bindings, tool_evidence, findings=None, sensitive_values=()) -> dict:
    """QA_REPORT 또는 SECURITY_REPORT dict를 만든다.

    - context: artifactId, artifactVersion, previousArtifactId, runId, workflowStepId,
      a2aTaskId, a2aArtifactId, createdAt, requirementIds (+ 선택 artifactUri)
    - plan은 필수 검사만, bindings는 계획 전체를 덮어야 하고 요구사항 집합이 context와 같아야 한다.
    - ERROR·NOT_RUN은 UNVERIFIED로 바꾸고 원래 상태·사유·증거 ID는 details에 남긴다.
    - SECURITY는 요구사항당 결과 1개, findings는 빈 목록이라도 명시. NOT_APPLICABLE은 거부.
    - finalVerdict는 만들지 않는다.
    """
    # 1) 입력 검사
    if role not in ('QA', 'SECURITY'):
        raise ValueError('Unsupported report role')
    if not isinstance(expected, ExecutionManifest) or observed != expected:
        raise ValueError('Cannot publish results from a missing/mismatched Manifest')
    required_context = {'artifactId', 'artifactVersion', 'previousArtifactId', 'runId',
                        'workflowStepId', 'a2aTaskId', 'a2aArtifactId', 'createdAt',
                        'requirementIds'}
    if not isinstance(context, dict) or not required_context <= context.keys() or context.keys() - required_context - {'artifactUri'}:
        raise ValueError('Report context has missing or unknown fields')
    checks, results = tuple(plan), tuple(results)
    # 공통 보고서에는 필수/선택 구분이 없어 선택 검사가 섞이면 필수처럼 읽힌다.
    if any(check.required is not True for check in checks):
        raise ValueError('Only the complete required plan can be exported')
    summary = aggregate(checks, results, sensitive_values=sensitive_values)

    # 2) 대응표 검사
    ids = {check.test_id for check in checks}
    if set(bindings) != ids or set(tool_evidence) - ids:
        raise ValueError('Bindings/evidence do not match the fixed plan')
    external_ids, requirement_ids = [], []
    for binding in bindings.values():
        if not isinstance(binding, TestBinding):
            raise ValueError('Expected explicit trusted TestBinding')
        _uuid4(binding.requirement_id)
        for text in (binding.external_test_id, binding.title):
            if not isinstance(text, str) or not text.strip() or text != text.strip():
                raise ValueError('Test binding text must not be blank')
        external_ids.append(binding.external_test_id)
        requirement_ids.append(binding.requirement_id)
    if len(external_ids) != len(set(external_ids)):
        raise ValueError('Duplicate external test ID')
    # 1번은 단계의 요구사항마다 결과가 있어야 보고서를 받는다.
    if set(requirement_ids) != set(context['requirementIds']):
        raise ValueError('Bindings do not cover the exact Step requirement set')
    if role == 'SECURITY' and len(requirement_ids) != len(set(requirement_ids)):
        raise ValueError('Security needs one pre-aggregated check per requirement')
    if any(result.status == Status.NOT_APPLICABLE for result in results):
        raise ValueError('NOT_APPLICABLE requires an agreed external contract')

    # 3) 검사별 행
    rows = []
    executions = {}  # executionId별 근거. 같은 실행에 다른 근거가 붙는 모순을 찾는다.
    received_status = {result.test_id: result.status.value for result in results}
    for row in summary['results']:
        binding = bindings[row['testId']]
        outcome = row['status'] if row['status'] in ('PASS', 'FAIL') else 'UNVERIFIED'
        item = {
            'requirementId': binding.requirement_id, 'outcome': outcome,
            # 공통 규격에 전용 필드가 없는 정보는 details(JSON 문자열)에 보존한다.
            'details': json.dumps({'localTestId': row['testId'], 'localStatus': row['status'],
                                   'reportedStatus': received_status.get(row['testId']),
                                   'errorCode': row['errorCode'], 'reason': row['reason'],
                                   'evidenceIds': row['evidenceIds']},
                                  sort_keys=True),
            'toolEvidence': _tool_evidence(tool_evidence.get(row['testId']), expected, role, outcome),
        }
        for field in ('expectedResult', 'actualResult', 'normalizedLocation'):
            if row[field] is not None:
                item[field] = row[field]
        evidence = item['toolEvidence']
        if evidence is not None:
            execution_id = evidence['executionId']
            if execution_id in executions and executions[execution_id] != evidence:
                raise ValueError('Contradictory evidence for the same execution ID')
            executions[execution_id] = evidence
        if role == 'QA':
            item.update(testId=binding.external_test_id, title=binding.title)
        rows.append(item)

    # 4) 조립. 입력 context는 복사해서 쓰고 원본은 바꾸지 않는다.
    report = deepcopy(context)
    report.update(artifactType=role + '_REPORT', createdBy=role,
                  codeVersion=expected.code_version, executionManifest=expected.to_payload())
    report['tests' if role == 'QA' else 'requirementResults'] = rows
    if role == 'SECURITY':
        # "발견사항 없음([])"과 "확인 안 함(None)"을 구분한다.
        if findings is None:
            raise ValueError('Security findings must be explicitly supplied, including an empty list')
        report['findings'] = deepcopy(findings)
    elif findings is not None:
        raise ValueError('QA reports cannot carry Security findings')

    # 5) 스키마 검사 후, 스키마로 표현하기 어려운 규칙을 추가 확인한다.
    validate_contract(report, 'qa_report' if role == 'QA' else 'security_report')
    if (report['artifactVersion'] == 1) != (report['previousArtifactId'] is None):
        raise ValueError('Artifact lineage version and predecessor disagree')
    if 'artifactUri' in report:
        _artifact_uri(report['artifactUri'])
    if report['artifactId'] == report['previousArtifactId']:
        raise ValueError('Artifact cannot be its own predecessor')
    for key in ('a2aTaskId', 'a2aArtifactId'):
        if not report[key].strip():
            raise ValueError('A2A references must not be blank')
    if role == 'SECURITY':
        finding_ids = [finding['findingId'] for finding in report['findings']]
        if len(finding_ids) != len(set(finding_ids)):
            raise ValueError('Duplicate finding ID')
        for finding in report['findings']:
            for key in ('findingId', 'title', 'description'):
                if not finding[key].strip():
                    raise ValueError('Finding text must not be blank')
            if finding.get('requirementId') is not None and finding['requirementId'] not in report['requirementIds']:
                raise ValueError('Finding refers to a requirement outside the Step')
    return report
