"""지금 dev의 시험 하네스(가짜 LLM·가짜 docker, 생성 코드 실행 없음)로 Run을 만들어 dev evaluation_export로 꺼낸다.

test_dev_compatibility.py가 고정 기록 대신 "지금 dev가 실제로 남기는 모양"으로 평가 코드를 확인할 때 쓴다. 이 파일은
시험 모음이 아니라 도우미라서 이름이 test_로 시작하지 않는다(pytest가 아래 unittest 클래스를 따로 모으지 않게).
dev 시험 도구는 tests/ 폴더의 최상위 모듈로 서로를 불러오므로 그 폴더를 경로에 넣는다.
"""
import asyncio
from contextlib import contextmanager
import io
import json
from pathlib import Path
import sys
import unittest
from uuid import UUID

TESTS = str(Path(__file__).resolve().parents[1])


@contextmanager
def dev_test_imports():
    added = TESTS not in sys.path
    if added:
        sys.path.insert(0, TESTS)
    try:
        yield
    finally:
        if added:
            sys.path.remove(TESTS)


with dev_test_imports():
    from agents.llm.contracts import LLMErrorCode, LLMRuntimeError
    from agents.platform.evaluation_export import write_evaluation_export
    from orchestrator.domain import AgentRole
    import test_owned_agent_fix_pipeline as fix_fixture
    import test_owned_agent_pipeline as multi_fixture
    from test_product_semantic_review import held_review

ROLES = ('PLANNER', 'DEVELOPER', 'QA', 'SECURITY')


class _Exports(unittest.IsolatedAsyncioTestCase):
    out = None  # make_exports가 정한다

    def stores(self, directory):
        return {role: directory / (role.lower() + '-tasks.sqlite3') for role in ROLES}


class FixSuccess(_Exports, fix_fixture.OwnedAgentFixPipelineTests):
    async def runTest(self):
        harness = fix_fixture._FixHarness(self)
        run, _artifacts, _steps, _calls = await self.submit(harness)
        self.assertEqual((run.status.value, run.verdict.value if run.verdict else None, run.fix_attempt),
                         ('FINISHED', 'SUCCESS', 1), 'The exported fixture must include an actual successful repair.')
        write_evaluation_export(harness.base.repository, run.run_id, self.out / 'fix-success',
                                stores=self.stores(harness.base.directory))


class Holds(_Exports):
    async def run_case(self, name, configure):
        fixture = multi_fixture.OwnedAgentPipelineTests('runTest')
        fixture.setUp()
        # The borrowed IsolatedAsyncioTestCase has no runner of its own.
        # Its doCleanups() would swallow the runner assertion without restoring env.
        for function, arguments, keywords in fixture._cleanups:
            self.addCleanup(function, *arguments, **keywords)
        fixture._cleanups.clear()
        providers = fixture.providers()
        configure(providers)
        async with fixture.running_platform(providers=providers) as (client, _):
            response = await asyncio.wait_for(client.post('/api/v1/runs', json=fixture.submission()), 30)
            self.assertEqual(response.status_code, 201, response.text)
            run_id = UUID(response.json()['run']['runId'])
        write_evaluation_export(fixture.repository, run_id, self.out / name, stores=self.stores(fixture.directory))


class PlannerSemanticHold(Holds):
    async def runTest(self):
        def configure(providers):
            providers[AgentRole.PLANNER].review_decider = held_review
        await self.run_case('planner-semantic-hold', configure)


class DeveloperProviderError(Holds):
    async def runTest(self):
        def configure(providers):
            providers[AgentRole.DEVELOPER].script.insert(0, LLMRuntimeError(LLMErrorCode.PROVIDER))
        await self.run_case('developer-provider-error', configure)


CASES = (FixSuccess, PlannerSemanticHold, DeveloperProviderError)


def make_exports(out: Path) -> dict:
    """세 Run을 만들어 out/<이름>/에 내보낸다. 돌려주는 값: {'ok': bool, 'log': 실패 내용}."""
    out.mkdir(parents=True, exist_ok=True)
    suite = unittest.TestSuite()
    for case in CASES:
        case.out = out
        suite.addTest(case())
    stream = io.StringIO()
    with dev_test_imports():
        result = unittest.TextTestRunner(stream=stream, verbosity=0).run(suite)
    problems = [f'{test}: {text[-800:]}' for test, text in result.errors + result.failures]
    return {'ok': result.wasSuccessful(), 'log': json.dumps(problems, ensure_ascii=False)}
