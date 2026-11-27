"""Closed A2A Artifact envelopes; synthetic payloads are not execution proof."""

from copy import deepcopy
import unittest
from uuid import uuid1, uuid4

from a2a.types import Task
from google.protobuf.json_format import ParseDict

from agents.roles.contracts import get_role_contract
from orchestrator.application.output_envelope import OutputEnvelopeError, validate_output_envelope
from orchestrator.domain.role_outputs import ROLE_ARTIFACT_NAMES
from orchestrator.domain.states import AgentRole


class OutputEnvelopeTests(unittest.TestCase):
    def setUp(self):
        self.run_id, self.step_id = uuid4(), uuid4()

    def wire(self, role):
        return {"id": " task/opaque?!% ", "contextId": " context/opaque?!% ",
            "status": {"state": "TASK_STATE_COMPLETED"},
            "artifacts": [{"name": name, "artifactId": f" artifact/{index}?!% ",
                "metadata": {"runId": str(self.run_id), "workflowStepId": str(self.step_id),
                    "projectArtifactId": str(uuid4()), "artifactVersion": 1},
                "parts": [{"data": {}, "mediaType": "application/json"}]}
                for index, name in enumerate(ROLE_ARTIFACT_NAMES[role])]}

    def validate(self, wire, role):
        task = ParseDict(wire, Task())
        before = task.SerializeToString(deterministic=True)
        result = validate_output_envelope(task, role, run_id=self.run_id, workflow_step_id=self.step_id)
        self.assertEqual(task.SerializeToString(deterministic=True), before)
        return result

    def deny(self, wire, role):
        with self.assertRaises(OutputEnvelopeError) as caught:
            self.validate(wire, role)
        self.assertEqual(str(caught.exception), "ROLE_ARTIFACT_ENVELOPE_INVALID")
        self.assertIsNone(caught.exception.__cause__)
        self.assertIsNone(caught.exception.__context__)

    def test_role_declarations_and_host_sets_match(self):
        for role in AgentRole:
            self.assertEqual(tuple(output.name for output in get_role_contract(role).outputs), ROLE_ARTIFACT_NAMES[role])

    def test_exact_sets_allow_different_order_and_preserve_opaque_ids(self):
        for role in AgentRole:
            wire = self.wire(role)
            wire["artifacts"].reverse()
            self.assertIsNone(self.validate(wire, role))
        # Empty JSON is enough for the envelope only. Payload parsers and real
        # receipt verification must still reject it as a product result.

    def test_unknown_cross_role_duplicate_missing_or_extra_artifacts_are_rejected(self):
        for role in AgentRole:
            base = self.wire(role)
            for foreign_name in ("extra.txt", "requirements.json", "qa-report.json", "security-report.json"):
                wire = deepcopy(base)
                extra = deepcopy(wire["artifacts"][0])
                extra.update(name=foreign_name, artifactId="another-opaque-id")
                extra["metadata"]["projectArtifactId"] = str(uuid4())
                wire["artifacts"].append(extra)
                self.deny(wire, role)
            wire = deepcopy(base)
            wire["artifacts"].pop()
            self.deny(wire, role)
            wire = deepcopy(base)
            wire["artifacts"][0]["name"] = "unknown-role-result.json"
            self.deny(wire, role)
        wire = self.wire(AgentRole.DEVELOPER)
        wire["artifacts"][1]["name"] = wire["artifacts"][0]["name"]
        self.deny(wire, AgentRole.DEVELOPER)

    def test_duplicate_opaque_or_project_id_is_rejected(self):
        for field in ("artifactId", "projectArtifactId"):
            wire = self.wire(AgentRole.DEVELOPER)
            first, second = wire["artifacts"][:2]
            if field == "artifactId":
                second[field] = first[field]
            else:
                second["metadata"][field] = first["metadata"][field].upper()
            self.deny(wire, AgentRole.DEVELOPER)

    def test_each_artifact_requires_one_json_data_part(self):
        for parts in ([], [{"text": "PASS"}], [{"data": {}, "mediaType": "text/plain"}],
                      [{"data": {}, "mediaType": "application/json"}]*2):
            wire = self.wire(AgentRole.QA)
            wire["artifacts"][0]["parts"] = parts
            self.deny(wire, AgentRole.QA)

    def test_metadata_is_closed_bound_and_uuid_v4(self):
        base = self.wire(AgentRole.PLANNER)
        for field, value in (("runId", str(uuid4())), ("workflowStepId", str(uuid4())),
                             ("projectArtifactId", str(uuid1())), ("projectArtifactId", "not-uuid"),
                             ("projectArtifactId", True), ("approval", "token=fixture-only")):
            wire = deepcopy(base)
            wire["artifacts"][0]["metadata"][field] = value
            self.deny(wire, AgentRole.PLANNER)
        for field in base["artifacts"][0]["metadata"]:
            wire = deepcopy(base)
            del wire["artifacts"][0]["metadata"][field]
            self.deny(wire, AgentRole.PLANNER)

    def test_versions_follow_protojson_numbers_without_accepting_bool_or_nonfinite(self):
        for version in (True, False, "1", 0, -1, 1.5, float("inf"), float("nan")):
            wire = self.wire(AgentRole.PLANNER)
            wire["artifacts"][0]["metadata"]["artifactVersion"] = version
            self.deny(wire, AgentRole.PLANNER)
        wire = self.wire(AgentRole.PLANNER)
        wire["artifacts"][0]["metadata"]["artifactVersion"] = 1.0
        self.assertIsNone(self.validate(wire, AgentRole.PLANNER))

    def test_unfinished_task_blank_artifact_id_or_wrong_role_is_not_admitted(self):
        for state in ("TASK_STATE_WORKING", "TASK_STATE_FAILED", "TASK_STATE_INPUT_REQUIRED"):
            wire = self.wire(AgentRole.PLANNER)
            wire["status"]["state"] = state
            self.deny(wire, AgentRole.PLANNER)
        wire = self.wire(AgentRole.PLANNER)
        wire["artifacts"][0]["artifactId"] = " \t "
        self.deny(wire, AgentRole.PLANNER)
        self.deny(self.wire(AgentRole.PLANNER), "PLANNER")
