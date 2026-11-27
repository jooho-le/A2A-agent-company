"""Real Host routes and durable receipts; remote agents are isolated test doubles."""
import asyncio
import tempfile
import unittest
from unittest import mock
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import httpx
from a2a.types import Task
from google.protobuf.json_format import ParseDict
from pydantic import SecretStr

from orchestrator.api.dependencies import get_workflow_controls
from orchestrator.application.run_interactions import interaction_revision
from orchestrator.core.config import Settings
from orchestrator.domain import A2ATaskState, AgentRole, FinalVerdict, SCN_001_ID, WorkflowRun, WorkflowStep, WorkflowStatus, WorkflowStepStatus
from orchestrator.infrastructure import SQLiteWorkflowRepository
from orchestrator.infrastructure.run_commands import CommandConflict, RunCommandStore
from orchestrator.main import create_app


class RemoteQuestions:
    context = "current-context"
    data = None

    async def __aenter__(self): return self
    async def __aexit__(self, *args): pass
    async def get_task(self, task_id):
        return ParseDict({"id": task_id, "contextId": self.context, "status": {
            "state": "TASK_STATE_INPUT_REQUIRED", "message": {"parts": [{"data": self.data or {
                "code": "PLANNER_INPUT_REQUIRED", "questions": ["어떤 가입 화면이 필요한가요?"],
                "privateHistory": "never-return-this"}}]}}}, Task())


class InteractionTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(); self.addCleanup(self.directory.cleanup)
        self.repository = SQLiteWorkflowRepository(Path(self.directory.name) / "host.db")
        self.run = WorkflowRun(scenario_id=SCN_001_ID, request_text="회원가입 화면",
                               status=WorkflowStatus.WAITING_INPUT, resume_state=WorkflowStatus.PLANNING)
        self.step = WorkflowStep(run_id=self.run.run_id, agent_role=AgentRole.PLANNER,
            status=WorkflowStepStatus.WAITING_INPUT, a2a_task_id="current-task",
            agent_context_id="current-context", a2a_task_state=A2ATaskState.INPUT_REQUIRED)
        self.repository.create_run(self.run, (self.step,), ())
        self.remote = RemoteQuestions(); self.calls = []
        async def resume(run_id, **kwargs):
            self.calls.append(("RESUME", run_id, kwargs)); return self.run
        async def cancel(run_id, reason):
            self.calls.append(("CANCEL", run_id, reason))
            return self.run.model_copy(update={"status": WorkflowStatus.ABORTED,
                "resume_state": None, "termination_reason": reason})
        self.controls = SimpleNamespace(registry=SimpleNamespace(require_base_url=lambda role: "http://test-agent"),
            client_factory=lambda url: self.remote, resume=resume, cancel=cancel)
        self.app = create_app(self.repository, settings=Settings(_env_file=None, database_path=str(self.repository.database_path)))
        self.token = "test-host-service-token-" + "x" * 32
        self.app.state.host_api_auth_required = True
        self.app.state.host_api_token = SecretStr(self.token)
        self.app.dependency_overrides[get_workflow_controls] = lambda: self.controls
        self.path = f"/api/v1/runs/{self.run.run_id}"

    def request(self, method, suffix, *, body=None, key=None, auth=True):
        async def invoke():
            headers = {"Authorization": "Bearer " + self.token} if auth else {}
            if key: headers["Idempotency-Key"] = str(key)
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url="http://test") as client:
                return await client.request(method, self.path + suffix, json=body, headers=headers)
        return asyncio.run(invoke())

    def body(self):
        return {"action": "RESUME", "revision": interaction_revision(self.run, [self.step]),
                "workflowStepId": str(self.step.workflow_step_id), "clarification": "정해진 회원가입 범위에서 진행해주세요."}

    def test_questions_are_bound_and_allowlisted_without_starting_work(self):
        self.assertEqual(self.request("GET", "/interaction", auth=False).status_code, 401)
        response = self.request("GET", "/interaction")
        self.assertEqual(response.status_code, 200)
        data = response.json(); self.assertEqual(data["requests"][0]["replyMode"], "ANSWER")
        self.assertNotIn("never-return-this", response.text)
        self.assertEqual(data["requests"][0]["workflowStepId"], str(self.step.workflow_step_id))
        self.assertEqual(self.calls, [])
        self.remote.context = "another-context"
        self.assertEqual(self.request("GET", "/interaction").json()["requests"][0]["replyMode"], "UNAVAILABLE")

    def test_old_host_policy_never_advertises_or_admits_a_resumable_question(self):
        from agents.platform.host_policy_store import HostPolicyStore
        with self.repository._transaction() as db:
            HostPolicyStore._schema(db)
            db.execute('INSERT INTO run_host_policies VALUES(?,?)',(str(self.run.run_id),'a'*64))
        self.app.state.host_policy_sha256 = 'b'*64
        data = self.request('GET','/interaction').json()
        self.assertEqual(data['requests'][0]['replyMode'],'NEW_RUN')
        self.assertEqual(data['requests'][0]['questions'],[])
        response = self.request('POST','/commands',body=self.body(),key=uuid4())
        self.assertEqual((response.status_code,response.json()['detail']),(409,'QUESTION_NOT_ACTIONABLE'))
        self.assertEqual(self.calls,[])
        self.assertEqual(self.repository.get_run(self.run.run_id),self.run)

    def test_missing_policy_binding_does_not_advertise_a_legacy_question(self):
        self.app.state.host_policy_sha256 = 'b'*64
        data = self.request('GET','/interaction').json()
        self.assertEqual(data['requests'][0]['replyMode'],'NEW_RUN')
        self.assertIn('실행 규칙 기록',data['continuationUnavailable'])
        self.assertEqual(self.request('POST','/commands',body=self.body(),key=uuid4()).status_code,409)
        self.assertEqual(self.calls,[])

    def test_resume_receipt_replays_once_and_conflicts_with_changed_input(self):
        key = uuid4(); body = self.body()
        self.assertEqual(self.request("POST", "/commands", body=body, key=key).status_code, 202)
        self.assertEqual(self.request("POST", "/commands", body=body, key=key).status_code, 200)
        receipt = self.request("GET", f"/commands/{key}").json()
        self.assertEqual(receipt["status"], "SUCCEEDED")
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.calls[0][2], {"step_id": self.step.workflow_step_id, "input_data": {"clarification": body["clarification"]}})
        self.assertEqual(self.request("POST", "/commands", body={**body, "clarification": "다른 답변"}, key=key).status_code, 409)

    def test_terminal_or_active_run_never_advertises_a_historical_question(self):
        for status in (WorkflowStatus.FINISHED, WorkflowStatus.ABORTED, WorkflowStatus.VALIDATING):
            with self.subTest(status=status):
                run = self.run.model_copy(update={"status": status, "resume_state": None,
                    "verdict": FinalVerdict.UNVERIFIED if status is WorkflowStatus.FINISHED else None,
                    "termination_reason": "사용자 중단" if status is WorkflowStatus.ABORTED else None})
                self.repository.save_run_update(run, ())
                with mock.patch.object(self.remote, "get_task", side_effect=AssertionError("stale Task read")):
                    data = self.request("GET", "/interaction").json()
                self.assertEqual(data["status"], status.value)
                self.assertEqual(data["requests"], [])
                self.assertEqual(self.request("POST", "/commands", body=self.body(), key=uuid4()).status_code, 409)
        self.assertEqual(self.calls, [])

    def test_stale_final_wrong_step_and_new_run_policy_do_not_resume(self):
        for patch in ({"revision": "0" * 64}, {"workflowStepId": str(uuid4())}):
            self.assertEqual(self.request("POST", "/commands", body={**self.body(), **patch}, key=uuid4()).status_code, 409)
        self.remote.data = {"code": "SEMANTIC_REVIEW_INPUT_REQUIRED", "questions": ["질문"],
                            "semanticReview": {"reasonCode": "SEMANTIC_EVIDENCE_UNVERIFIED", "resumePolicy": "NEW_RUN_REQUIRED"}}
        data = self.request("GET", "/interaction").json()["requests"][0]
        self.assertEqual(data["replyMode"], "NEW_RUN")
        self.assertIn("근거", data["reason"])
        self.assertEqual(self.request("POST", "/commands", body=self.body(), key=uuid4()).status_code, 409)
        self.assertEqual(self.calls, [])
        self.repository.save_run_update(self.run.model_copy(update={"status": WorkflowStatus.ABORTED,
            "resume_state": None, "termination_reason": "사용자 중단"}), ())
        self.assertEqual(self.request("POST", "/commands", body=self.body(), key=uuid4()).status_code, 409)

    def test_stop_targets_whole_run_even_if_employee_progress_has_changed(self):
        key = uuid4()
        response = self.request("POST", "/commands", body={"action": "CANCEL", "revision": "0" * 64}, key=key)
        self.assertEqual(response.status_code, 202)
        self.assertEqual(self.request("GET", f"/commands/{key}").json()["resultStatus"], "ABORTED")
        self.assertEqual(len(self.calls), 1)

    def test_cancel_and_unknown_effects_are_reported_without_false_success(self):
        body = {"action": "CANCEL", "revision": self.body()["revision"]}
        key = uuid4(); self.request("POST", "/commands", body=body, key=key)
        receipt = self.request("GET", f"/commands/{key}").json()
        self.assertEqual((receipt["status"], receipt["resultStatus"]), ("SUCCEEDED", "ABORTED"))
        async def fails(*args, **kwargs): raise RuntimeError("private-token-must-not-escape")
        self.controls.cancel = fails; key = uuid4()
        self.request("POST", "/commands", body=body, key=key)
        response = self.request("GET", f"/commands/{key}")
        self.assertEqual(response.json()["status"], "UNKNOWN")
        self.assertNotIn("private-token", response.text)
        self.assertEqual(self.request("POST", "/commands", body=body, key=uuid4()).status_code, 409)

    def test_restart_preserves_receipt_and_opposite_action_can_still_be_admitted(self):
        a = RunCommandStore(self.repository.database_path); key = uuid4(); body = self.body()
        a.admit(self.run.run_id, key, body); self.assertTrue(a.claim(key))
        b = RunCommandStore(self.repository.database_path)
        self.assertEqual(b.get(self.run.run_id, key)["state"], "UNKNOWN")
        with self.assertRaises(CommandConflict): b.admit(self.run.run_id, uuid4(), body)
        b.admit(self.run.run_id, uuid4(), {"action": "CANCEL", "revision": body["revision"]})

    def test_service_mode_invalid_input_and_missing_identity_block_control(self):
        self.assertEqual(self.request("POST", "/commands", body=self.body(), auth=False, key=uuid4()).status_code, 401)
        self.assertEqual(self.request("POST", "/commands", body=self.body()).status_code, 422)
        self.assertEqual(self.request("POST", "/commands", body={**self.body(), "configuration": {}}, key=uuid4()).status_code, 422)
        self.app.state.host_api_auth_required = False
        self.assertEqual(self.request("GET", "/interaction").status_code, 503)
        self.assertEqual(self.calls, [])
