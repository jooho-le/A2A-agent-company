"""Real SQLite, confined files and ASGI; no servers, provider or generated execution."""
import asyncio
from dataclasses import replace
import json
from pathlib import Path
import sqlite3
from types import SimpleNamespace as N
import unittest
from unittest.mock import patch
from uuid import UUID, uuid4

import httpx
from pydantic import SecretStr

import test_starting_source as fixtures
from agents.platform.starting_source import SourcePreparationError
from orchestrator.core.config import Settings
from orchestrator.artifacts.git_snapshot import GitSnapshotLimits
from orchestrator.domain import AgentRole, WorkflowRun, WorkflowStatus, WorkflowStep, WorkflowStepStatus
from orchestrator.domain.run_configuration import RunConfiguration, RunConfigurationArtifact
from orchestrator.domain.scenario_registry import SERVICE_WEB_ID, service_request_contract
from orchestrator.domain.workspaces import WorkspaceRecord
from orchestrator.infrastructure.service_continuations import (
    ContinuationConflict, capture_continuation, public_continuation, read_continuation,
)
from orchestrator.main import create_app


class ServiceContinuationTests(fixtures._SourceFixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.verified = self.verify()
        self.approved = RunConfiguration(starting_commit_hash=self.commit,
            starting_snapshot_sha256=self.snapshot.snapshot_sha256, environment=self.environment)
        self.parent, _, self.parent_root = self.record('이메일과 비밀번호로 회원가입 API 만들어줘.', paused=True)
        self.registry.provision(self.parent.workspace_id, run_id=self.parent.run_id)
        source = self.parent_root / 'source'
        (source / 'requirements.lock').write_bytes(fixtures.LOCK)
        (source / 'main.py').write_text('# existing partially written service\n')
        self.artifacts = N(_git_limits=GitSnapshotLimits())

    def record(self, text, *, paused=False, bundle=None, run=None):
        run = run or WorkflowRun(scenario_id=SERVICE_WEB_ID, request_text=text,
            status=WorkflowStatus.HUMAN_REVIEW if paused else WorkflowStatus.PLANNING,
            **({'resume_state': WorkflowStatus.IMPLEMENTING} if paused else {}))
        step = WorkflowStep(run_id=run.run_id, agent_role=AgentRole.PLANNER,
            status=WorkflowStepStatus.FAILED if paused else WorkflowStepStatus.RUNNING,
            failure_code='LLM_PROVIDER_ERROR' if paused else None)
        saved = RunConfigurationArtifact(run_id=run.run_id, workspace_id=run.workspace_id,
            scenario_id=run.scenario_id, configuration=self.approved,
            frozen_scenario_contract_json=bundle.contract_json if bundle else json.dumps(service_request_contract(text)))
        workspace = WorkspaceRecord(run_id=run.run_id, workspace_id=run.workspace_id,
            root_path=str(self.registry.base_path / str(run.workspace_id)))
        self.repository.create_run(run, (step,), (), workspace=workspace, run_configuration=saved, continuation=bundle)
        return run, step, Path(workspace.root_path)

    def capture(self, child=None, **kwargs):
        child = child or WorkflowRun(scenario_id=SERVICE_WEB_ID, request_text='기존 실패를 수정하고 재검증해줘.')
        return child, capture_continuation(self.repository, self.registry, self.artifacts, self.verified.configuration,
            parent_id=self.parent.run_id, run_id=child.run_id, instruction=child.request_text,
            configuration=kwargs.get('configuration', self.approved))

    def test_draft_inheritance_is_immutable_and_keeps_original_requirements_and_goal(self):
        child, bundle = self.capture()
        before = self.repository.get_run_configuration(self.parent.run_id)
        run, _, root = self.record(child.request_text, bundle=bundle,
            run=child.model_copy(update={'status': WorkflowStatus.PLANNING}))
        frozen = self.repository.get_run_configuration(run.run_id)
        context = frozen.scenario_contract['externalContract']['continuation']
        assert context['originalRequest'] == self.parent.request_text
        assert context['sourceKind'] == 'DRAFT_CHECKPOINT' and context['sourceArtifactId'] is None
        assert context['stepFailures'][0]['code'] == 'LLM_PROVIDER_ERROR'
        assert frozen.scenario_contract['requirements'] == before.scenario_contract['requirements']
        assert frozen.configuration == before.configuration
        self.registry.provision(run.workspace_id, run_id=run.run_id)
        prepare = self.preparer()
        prepare(run.run_id)
        assert (root / 'source/main.py').read_text() == '# existing partially written service\n'
        assert not (root / 'source/signup.py').exists()  # old deleted paths are not resurrected
        assert self.git('rev-parse', 'HEAD', cwd=root / 'source').strip() == self.commit
        prepare(run.run_id)  # verifies, does not overwrite
        (root / 'source/main.py').write_text('# user edited after preparation\n')
        with self.assertRaises(SourcePreparationError): prepare(run.run_id)
        assert (root / 'source/main.py').read_text() == '# user edited after preparation\n'
        assert self.repository.get_run_configuration(self.parent.run_id) == before
        assert self.repository.get_run(self.parent.run_id) == self.parent
        with self.repository._transaction() as db, self.assertRaises(sqlite3.IntegrityError):
            db.execute('UPDATE service_continuation_files SET content=?', (b'changed',))
        assert read_continuation(self.repository, run.run_id, files=True)[1]['main.py'][0].startswith(b'# existing')
        assert not public_continuation(self.repository, self.parent)['available']

    def test_capture_is_not_a_live_reference_and_registered_source_wins_over_workspace(self):
        from orchestrator.sandbox.materialization import _canonical_archive
        selected = N(artifact_type='SOURCE', code_version=1, artifact_id=uuid4())
        inventory = {'requirements.lock': (fixtures.LOCK, 0o644), 'main.py': (b'# registered candidate\n', 0o644)}
        self.artifacts.read_registered_source = lambda *_args: N(content=_canonical_archive(
            [(p, b, m) for p, (b, m) in sorted(inventory.items())]))
        with patch.object(self.repository, 'list_project_artifacts', return_value=[selected]):
            # No validation reports: no generated receipt or protected tests are exposed.
            child, bundle = self.capture()
        assert json.loads(bundle.payload_json)['sourceKind'] == 'REGISTERED_SOURCE'
        (self.parent_root / 'source/main.py').write_text('# later parent edit\n')
        assert dict((p, b) for p, b, _ in bundle.files)['main.py'] == b'# registered candidate\n'

    def test_developer_accepts_only_the_pinned_seed_and_inheritance_alone_is_not_progress(self):
        from agents.runtime.developer_workspace import GitDeveloperCheckpoint, DeveloperCheckpointError
        from agents.llm.contracts import SourceEditStatus
        child, bundle = self.capture()
        child = child.model_copy(update={'status': WorkflowStatus.PLANNING})
        run, _, root = self.record(child.request_text, bundle=bundle, run=child)
        self.registry.provision(run.workspace_id, run_id=run.run_id)
        self.preparer()(run.run_id)
        bound = self.registry.bind(run.workspace_id, run_id=run.run_id, role=AgentRole.DEVELOPER)
        kwargs = dict(baseline_commit_hash=self.commit, repository_id=self.verified.configuration.repository_id,
                      lock_path='requirements.lock')
        with self.assertRaises(DeveloperCheckpointError):
            GitDeveloperCheckpoint(bound, **kwargs).prepare()
        pinned = read_continuation(self.repository, run.run_id, files=True)[1]
        checkpoint = GitDeveloperCheckpoint(bound, initial_source_files=pinned, **kwargs)
        checkpoint.prepare()
        assert checkpoint.source_edit_status() is SourceEditStatus.UNCHANGED
        (root / 'source/main.py').write_text('# a different comment is not a product fix\n')
        assert checkpoint.source_edit_status() is SourceEditStatus.UNCHANGED
        assert checkpoint._working_base_commit != self.commit
        assert self.git('rev-parse', 'HEAD', cwd=root / 'source').strip() == self.commit
        (root / 'source/main.py').write_text('FIXED = True\n')
        assert checkpoint.source_edit_status() is SourceEditStatus.CHANGED
        result = checkpoint.checkpoint()
        assert result.baseline_commit_hash == self.commit and result.parent_commit_hash == self.commit
        assert [e.path for e in result.edits] == ['source/main.py']
        assert result.working_base_commit == checkpoint._working_base_commit
        assert self.git('rev-parse', 'HEAD', cwd=root / 'source').strip() == self.commit

    def test_changed_parent_or_active_sibling_rolls_back_entire_child(self):
        child, bundle = self.capture()
        self.repository.save_run_update(self.parent.model_copy(update={'updated_at': self.parent.updated_at.replace(year=2027)}), ())
        with self.assertRaisesRegex(ContinuationConflict, 'CONTINUATION_PARENT_CHANGED'):
            self.record(child.request_text, bundle=bundle, run=child)
        assert self.repository.get_run(child.run_id) is None
        self.repository.save_run_update(self.parent, ())
        first, first_bundle = self.capture()
        second, second_bundle = self.capture()
        self.record(first.request_text, bundle=first_bundle, run=first)
        with self.assertRaisesRegex(ContinuationConflict, 'CONTINUATION_ALREADY_ACTIVE'):
            self.record(second.request_text, bundle=second_bundle, run=second)
        assert self.repository.get_run(second.run_id) is None

    def test_unchanged_service_inheritance_reuses_its_authenticated_seed(self):
        from agents.runtime.developer_workspace import GitDeveloperCheckpoint
        from agents.llm.contracts import SourceEditStatus
        child, bundle = self.capture()
        run, _, root = self.record(child.request_text, bundle=bundle,
            run=child.model_copy(update={'status': WorkflowStatus.PLANNING}))
        self.registry.provision(run.workspace_id, run_id=run.run_id)
        self.preparer()(run.run_id)
        pinned = read_continuation(self.repository, run.run_id, files=True)[1]
        bound = self.registry.bind(run.workspace_id, run_id=run.run_id, role=AgentRole.DEVELOPER)
        checkpoint = GitDeveloperCheckpoint(bound, baseline_commit_hash=self.commit,
            repository_id=self.verified.configuration.repository_id, lock_path='requirements.lock',
            initial_source_files=pinned, allow_unchanged_source=True)
        checkpoint.prepare()
        assert checkpoint.source_edit_status() is SourceEditStatus.UNCHANGED
        with patch.object(checkpoint, '_commit', side_effect=AssertionError('do not create a fake change')):
            result = checkpoint.checkpoint()
        assert result.edits == () and result.commit_hash == result.working_base_commit
        assert result.commit_hash != self.commit  # pinned inherited bytes differ from approved starter
        assert (root / 'source/main.py').read_bytes() == pinned['main.py'][0]
        assert self.git('rev-parse', 'HEAD', cwd=root / 'source').strip() == self.commit

    def test_foreign_environment_modified_inventory_and_unsafe_paths_fail_closed(self):
        with self.assertRaisesRegex(ContinuationConflict, 'CONTINUATION_BASELINE_CHANGED'):
            self.capture(configuration=self.approved.model_copy(update={'starting_commit_hash': 'f'*40}))
        child, bundle = self.capture()
        damaged = replace(bundle, files=bundle.files + (('../outside.py', b'# denied', 0o644),))
        with self.assertRaises(Exception): self.record(child.request_text, bundle=damaged, run=child)
        assert self.repository.get_run(child.run_id) is None

    def test_authenticated_api_receipts_replay_exactly_and_plain_reference_is_rejected(self):
        app = create_app(self.repository, settings=Settings(_env_file=None,
            database_path=str(self.repository.database_path), workspace_root=str(self.registry.base_path)))
        app.state.workspace_registry = self.registry
        app.state.service_source_configuration = self.verified.configuration
        app.state.host_api_auth_required = True
        app.state.host_api_token = SecretStr('offline-continuation-token-'+'a'*40)
        def request(body, key=None, authenticated=True):
            async def send():
                headers = {'Authorization':'Bearer '+app.state.host_api_token.get_secret_value()} if authenticated else {}
                if key: headers['Idempotency-Key'] = str(key)
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
                    return await client.post('/api/v1/runs', json=body, headers=headers)
            return asyncio.run(send())
        body = {'scenarioId': str(SERVICE_WEB_ID), 'requestText': '이전 작업 오류 수정해줘',
                'configuration': self.approved.model_dump(mode='json', by_alias=True)}
        assert request(body, authenticated=False).status_code == 401
        assert request(body).json()['detail'] == 'CONTINUATION_TARGET_REQUIRED'
        body['continuationOf'] = str(self.parent.run_id)
        key = uuid4()
        response = request(body, key)
        assert response.status_code == 201, response.text
        assert response.json()['continuationOf'] == str(self.parent.run_id)
        replay = request(body, key)
        assert replay.status_code == 200 and replay.json() == response.json()
        assert request({**body, 'continuationOf': str(uuid4())}, key).status_code == 409
        child_id = UUID(response.json()['run']['runId'])
        assert read_continuation(self.repository, child_id)['parentRunId'] == str(self.parent.run_id)


def test_repair_plan_cannot_turn_a_previous_signup_failure_into_completion_management():
    from orchestrator.domain.service_design import validate_service_plan, ServiceDesign
    from service_test_helpers import service_design
    import pytest
    original = service_design('API', '/signup', ['email', 'password'])
    contract = service_request_contract('이전 오류를 고쳐줘.')
    contract['externalContract']['continuation'] = {'previousServiceDesign': original}
    plan = N(service_design=ServiceDesign.model_validate(service_design('WEB', '/complete')))
    with pytest.raises(ValueError, match='CONTINUATION_INTERFACE_CHANGED'): validate_service_plan(contract, plan)
    plan.service_design = ServiceDesign.model_validate(original)
    validate_service_plan(contract, plan)
