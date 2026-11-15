"""Read-only API progress works without configuring or contacting Docker."""
import asyncio
from uuid import UUID, uuid4

import httpx

from agents.platform.package_service import HostPackageService
from orchestrator.core.config import Settings
from orchestrator.domain import SCN_001_ID
from orchestrator.infrastructure import SQLiteWorkflowRepository
from orchestrator.main import create_app


def test_package_query_reports_absent_pending_and_missing_run_without_side_effects(tmp_path):
    repository = SQLiteWorkflowRepository(tmp_path / 'runs.sqlite3')
    app = create_app(repository, settings=Settings(_env_file=None, database_path=str(repository.database_path)))

    async def exercise():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://fixture') as client:
            submitted = await client.post('/api/v1/runs', json={
                'scenarioId': str(SCN_001_ID), 'requestText': '회원가입 기능 구현'})
            assert submitted.status_code == 201
            run_id = submitted.json()['run']['runId']
            path = f'/api/v1/runs/{run_id}/package-production'
            before = (await client.get(f'/api/v1/runs/{run_id}/events')).json()
            assert (await client.get(path)).json()['status'] == 'NOT_CONFIGURED'
            service = HostPackageService(repository)
            assert (await client.get(path)).json()['status'] == 'NOT_CONFIGURED'
            service.enroll(UUID(run_id))
            assert (await client.get(path)).json()['status'] == 'PENDING'
            assert (await client.get(f'/api/v1/runs/{run_id}/events')).json() == before
            assert (await client.get(f'/api/v1/runs/{uuid4()}/package-production')).status_code == 404

    asyncio.run(exercise())
