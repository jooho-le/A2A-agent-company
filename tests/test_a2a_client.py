import hashlib
import json
import unittest
from pathlib import Path
from uuid import uuid4

import httpx
from a2a.types import AgentCard, TaskState
from a2a.utils.constants import A2A_JSON_MEDIA_TYPE, VERSION_HEADER
from a2a.utils.errors import VersionNotSupportedError
from google.protobuf.json_format import MessageToDict, ParseDict

from orchestrator.a2a import (
    A2AAgentClient,
    A2AProjectContractError,
    A2AWorkflowMetadata,
    AgentCardContractError,
    build_send_message_request,
    build_snapshot_handoff_data,
    build_snapshot_handoff_request,
    validate_agent_card,
)
from orchestrator.domain import (
    AgentRole,
    CodeSnapshotArtifact,
    GitObjectFormat,
    SnapshotHandoff,
)


def agent_card(
    *,
    protocol_version: str = "1.0",
    interface_url: str = "https://qa.example.test/a2a",
) -> AgentCard:
    return ParseDict(
        {
            "name": "QA Agent",
            "description": "Project QA worker",
            "version": "0.1.0",
            "supportedInterfaces": [
                {
                    "url": interface_url,
                    "protocolBinding": "HTTP+JSON",
                    "protocolVersion": protocol_version,
                }
            ],
            "capabilities": {"streaming": False, "pushNotifications": False},
            "defaultInputModes": ["application/json"],
            "defaultOutputModes": ["application/json"],
            "skills": [],
        },
        AgentCard(),
    )


class A2ARequestContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.metadata = A2AWorkflowMetadata(
            runId=uuid4(),
            workflowStepId=uuid4(),
            scenarioId=uuid4(),
            attempt=0,
            requirementIds=[uuid4()],
        )

    def test_agent_card_requires_project_http_json_protocol_and_json_modes(self) -> None:
        card = agent_card()
        self.assertEqual(validate_agent_card(card), "https://qa.example.test/a2a")
        self.assertEqual(card.version, "0.1.0")  # app version is not protocol version

        with self.assertRaises(VersionNotSupportedError):
            validate_agent_card(agent_card(protocol_version="1.1"))

        with self.assertRaises(AgentCardContractError):
            validate_agent_card(
                agent_card(interface_url="https://qa.example.test:invalid/a2a")
            )

        no_json_input = agent_card()
        no_json_input.default_input_modes[:] = ["text/plain"]
        with self.assertRaises(AgentCardContractError):
            validate_agent_card(no_json_input)

    def test_send_message_request_uses_project_metadata_at_top_level(self) -> None:
        request = build_send_message_request(
            {"request": "Run QA", "labels": ["signup"]}, self.metadata
        )
        wire = MessageToDict(request)

        self.assertEqual(wire["metadata"], self.metadata.to_a2a_json())
        self.assertNotIn("metadata", wire["message"])
        self.assertNotIn("taskId", wire["message"])
        self.assertNotIn("contextId", wire["message"])
        self.assertEqual(wire["message"]["role"], "ROLE_USER")
        self.assertEqual(wire["message"]["parts"][0]["mediaType"], "application/json")
        self.assertEqual(wire["message"]["parts"][0]["data"]["request"], "Run QA")
        self.assertTrue(wire["configuration"]["returnImmediately"])
        self.assertEqual(wire["configuration"]["acceptedOutputModes"], ["application/json"])

        continuation = MessageToDict(
            build_send_message_request(
                {"request": "Continue QA"},
                self.metadata,
                context_id="qa-context::opaque",
            )
        )
        self.assertEqual(continuation["message"]["contextId"], "qa-context::opaque")
        self.assertNotIn("taskId", continuation["message"])

        same_task = MessageToDict(
            build_send_message_request(
                {"userInput": "Add clarification"},
                self.metadata,
                context_id="qa-context::opaque",
                task_id="agent-task-opaque",
            )
        )
        self.assertEqual(same_task["message"]["taskId"], "agent-task-opaque")

        root = Path(__file__).resolve().parents[1]
        schema = json.loads(
            (root / "schemas/project/workflow_metadata.schema.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertFalse(schema["additionalProperties"])
        self.assertEqual(set(wire["metadata"]), set(schema["required"]) | {"requirementIds"})

    def test_metadata_and_payload_reject_invalid_values(self) -> None:
        with self.assertRaises(ValueError):
            A2AWorkflowMetadata(
                runId=uuid4(),
                workflowStepId=uuid4(),
                scenarioId=uuid4(),
                attempt=-1,
            )
        with self.assertRaises(ValueError):
            A2AWorkflowMetadata(
                runId=uuid4(),
                workflowStepId=uuid4(),
                scenarioId=uuid4(),
                attempt=True,
            )
        with self.assertRaises(A2AProjectContractError):
            build_send_message_request({"not_json": object()}, self.metadata)


class A2AAgentClientTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.requests: list[httpx.Request] = []
        card = {
            "name": "QA Agent",
            "description": "Project QA worker",
            "version": "0.1.0",
            # Deliberately place 1.1 first: project client must select only 1.0.
            "supportedInterfaces": [
                {
                    "url": "https://qa.example.test/a2a-v1.1",
                    "protocolBinding": "HTTP+JSON",
                    "protocolVersion": "1.1",
                },
                {
                    "url": "https://qa.example.test/a2a",
                    "protocolBinding": "HTTP+JSON",
                    "protocolVersion": "1.0",
                },
            ],
            "capabilities": {"streaming": False, "pushNotifications": False},
            "defaultInputModes": ["application/json"],
            "defaultOutputModes": ["application/json"],
            "skills": [],
        }

        async def handler(request: httpx.Request) -> httpx.Response:
            self.requests.append(request)
            if request.url.path == "/.well-known/agent-card.json":
                return httpx.Response(200, json=card)
            if request.method == "POST" and request.url.path == "/a2a/message:send":
                return httpx.Response(
                    200,
                    json={
                        "task": {
                            "id": "agent/task?opaque#7",
                            "contextId": "agent-context-opaque",
                            "status": {"state": "TASK_STATE_SUBMITTED"},
                        }
                    },
                )
            if (
                request.method == "GET"
                and request.url.raw_path
                == b"/a2a/tasks/agent%2Ftask%3Fopaque%237"
            ):
                return httpx.Response(
                    200,
                    json={
                        "id": "agent/task?opaque#7",
                        "contextId": "agent-context-opaque",
                        "status": {"state": "TASK_STATE_WORKING"},
                    },
                )
            return httpx.Response(404)

        self.httpx_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        self.client = A2AAgentClient(
            "https://qa.example.test", httpx_client=self.httpx_client
        )

    async def asyncTearDown(self) -> None:
        await self.httpx_client.aclose()

    async def test_card_resolution_send_and_poll_use_project_a2a_10_contract(self) -> None:
        metadata = A2AWorkflowMetadata(
            runId=uuid4(), workflowStepId=uuid4(), scenarioId=uuid4(), attempt=0
        )
        task = await self.client.send_task({"request": "Run tests"}, metadata)
        polled = await self.client.get_task(task.id)

        self.assertEqual(task.id, "agent/task?opaque#7")
        self.assertEqual(polled.id, task.id)
        self.assertEqual(polled.status.state, TaskState.TASK_STATE_WORKING)
        self.assertEqual(self.client.interface_url, "https://qa.example.test/a2a")
        self.assertEqual(
            [request.url.path for request in self.requests],
            [
                "/.well-known/agent-card.json",
                "/a2a/message:send",
                "/a2a/tasks/agent/task?opaque#7",
            ],
        )

        send_request = self.requests[1]
        self.assertEqual(send_request.headers[VERSION_HEADER], "1.0")
        self.assertEqual(send_request.headers["content-type"], A2A_JSON_MEDIA_TYPE)
        self.assertEqual(self.requests[2].headers[VERSION_HEADER], "1.0")
        self.assertEqual(
            self.requests[2].url.raw_path,
            b"/a2a/tasks/agent%2Ftask%3Fopaque%237",
        )
        self.assertEqual(self.requests[2].url.query, b"")
        body = json.loads(send_request.content)
        self.assertTrue(body["configuration"]["returnImmediately"])
        self.assertEqual(body["message"]["parts"][0]["mediaType"], "application/json")

    async def test_card_interface_cannot_redirect_agent_credentials_to_other_origin(self) -> None:
        async def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "name": "QA Agent",
                    "version": "0.1.0",
                    "supportedInterfaces": [
                        {
                            "url": "https://other.example.test/a2a",
                            "protocolBinding": "HTTP+JSON",
                            "protocolVersion": "1.0",
                        }
                    ],
                    "capabilities": {},
                    "defaultInputModes": ["application/json"],
                    "defaultOutputModes": ["application/json"],
                    "skills": [],
                },
            )

        await self.httpx_client.aclose()
        self.httpx_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        self.client = A2AAgentClient(
            "https://qa.example.test", httpx_client=self.httpx_client
        )
        with self.assertRaises(AgentCardContractError):
            await self.client.resolve_agent_card()

    async def test_project_task_call_rejects_immediate_message_response(self) -> None:
        async def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/.well-known/agent-card.json":
                return httpx.Response(
                    200,
                    json=MessageToDict(agent_card()),
                )
            return httpx.Response(
                200,
                json={
                    "message": {
                        "messageId": str(uuid4()),
                        "role": "ROLE_AGENT",
                        "parts": [{"text": "done"}],
                    }
                },
            )

        await self.httpx_client.aclose()
        self.httpx_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        self.client = A2AAgentClient(
            "https://qa.example.test", httpx_client=self.httpx_client
        )
        metadata = A2AWorkflowMetadata(
            runId=uuid4(), workflowStepId=uuid4(), scenarioId=uuid4(), attempt=0
        )
        with self.assertRaises(A2AProjectContractError):
            await self.client.send_task({"request": "Run tests"}, metadata)

    async def test_continue_task_sends_server_ids_without_replacing_them(self) -> None:
        sent: list[httpx.Request] = []

        async def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/.well-known/agent-card.json":
                return httpx.Response(200, json=MessageToDict(agent_card()))
            sent.append(request)
            return httpx.Response(
                200,
                json={
                    "task": {
                        "id": "planner-task-opaque",
                        "contextId": "planner-context-opaque",
                        "status": {"state": "TASK_STATE_WORKING"},
                    }
                },
            )

        await self.httpx_client.aclose()
        self.httpx_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        self.client = A2AAgentClient(
            "https://qa.example.test", httpx_client=self.httpx_client
        )
        metadata = A2AWorkflowMetadata(
            runId=uuid4(), workflowStepId=uuid4(), scenarioId=uuid4(), attempt=1
        )
        task = await self.client.continue_task(
            "planner-task-opaque",
            {"userInput": "Clarification"},
            metadata,
            context_id="planner-context-opaque",
        )

        self.assertEqual(task.id, "planner-task-opaque")
        body = json.loads(sent[0].content)
        self.assertEqual(body["message"]["taskId"], "planner-task-opaque")
        self.assertEqual(body["message"]["contextId"], "planner-context-opaque")
        self.assertEqual(body["metadata"]["attempt"], 1)


class SnapshotA2AHandoffTests(unittest.TestCase):
    def test_snapshot_handoff_payload_keeps_manifest_and_project_ids_distinct(self) -> None:
        archive = b"frozen-source"
        snapshot = CodeSnapshotArtifact(
            artifact_version=1,
            run_id=uuid4(),
            workflow_step_id=uuid4(),
            a2a_task_id="developer-task-opaque",
            a2a_artifact_id="developer-artifact-opaque",
            requirement_ids=(uuid4(),),
            code_version=1,
            repository_id="a2a-agent-company",
            commit_hash="a" * 40,
            git_object_format=GitObjectFormat.SHA1,
            tree_hash="b" * 40,
            snapshot_sha256=hashlib.sha256(archive).hexdigest(),
            artifact_uri="registry://source/artifact/versions/1",
            container_image_digest="sha256:" + "c" * 64,
            dependency_lock_hash="sha256:" + "d" * 64,
        )
        handoff = SnapshotHandoff.from_snapshot(snapshot)
        payload = build_snapshot_handoff_data(handoff, AgentRole.QA, "Check tests")

        self.assertEqual(payload["snapshot"]["projectArtifactId"], str(snapshot.artifact_id))
        self.assertEqual(payload["snapshot"]["sourceAccess"], "READ_ONLY")
        self.assertEqual(payload["snapshot"]["executionManifest"]["projectArtifactId"], str(snapshot.artifact_id))
        self.assertNotEqual(payload["snapshot"]["projectArtifactId"], snapshot.a2a_artifact_id)

        request = build_snapshot_handoff_request(
            handoff,
            AgentRole.QA,
            "Check tests",
            workflow_step_id=uuid4(),
            scenario_id=uuid4(),
            requirement_ids=snapshot.requirement_ids,
        )
        wire = MessageToDict(request)
        self.assertEqual(wire["metadata"]["runId"], str(snapshot.run_id))
        self.assertEqual(wire["metadata"]["codeVersion"], snapshot.code_version)
        self.assertEqual(
            wire["metadata"]["projectArtifactIds"], [str(snapshot.artifact_id)]
        )
        self.assertEqual(
            wire["message"]["parts"][0]["data"]["snapshot"]["executionManifest"],
            payload["snapshot"]["executionManifest"],
        )
        with self.assertRaises(A2AProjectContractError):
            build_snapshot_handoff_data(
                handoff, AgentRole.DEVELOPER, "Check tests"
            )


if __name__ == "__main__":
    unittest.main()
