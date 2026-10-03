"""A2A 1.0 HTTP+JSON client wrapper for project Agent calls."""

from collections.abc import AsyncIterator, Mapping
from urllib.parse import quote, urlsplit
from uuid import UUID

import httpx
from a2a.client import A2ACardResolver, Client, ClientConfig, ClientFactory
from a2a.client.client import ClientCallContext
from a2a.client.transports.rest import RestTransport
from a2a.types import (
    AgentCard, CancelTaskRequest, GetTaskRequest, SendMessageRequest, StreamResponse, Task,
)
from google.protobuf.json_format import MessageToDict, ParseDict
from a2a.utils.constants import (
    A2A_JSON_MEDIA_TYPE,
    PROTOCOL_VERSION_1_0,
    TransportProtocol,
    VERSION_HEADER,
)
from a2a.utils.errors import VersionNotSupportedError

from orchestrator.a2a.requests import (
    A2AProjectContractError,
    A2AWorkflowMetadata,
    build_send_message_request,
    build_snapshot_handoff_request,
)
from orchestrator.domain.snapshot_handoff import SnapshotHandoff
from orchestrator.domain.states import AgentRole


class AgentCardContractError(ValueError):
    """Raised when an Agent Card does not satisfy the project interface contract."""


def _task_id_path_segment(task_id: str) -> str:
    encoded = quote(task_id, safe="")
    # quote never escapes dots, but httpx normalizes a literal '.'/'..' segment.
    return encoded.replace(".", "%2E") if task_id in {".", ".."} else encoded


class _OpaqueTaskRestTransport(RestTransport):
    """Extend the SDK HTTP+JSON binding only where it omits ID path escaping."""

    async def get_task(
        self, request: GetTaskRequest, *, context: ClientCallContext | None = None
    ) -> Task:
        params = MessageToDict(request)
        params.pop("id", None)
        params.pop("tenant", None)
        response = await self._execute_request(
            "GET", f"/tasks/{_task_id_path_segment(request.id)}", request.tenant,
            context=context, params=params,
        )
        return ParseDict(response, Task())

    async def cancel_task(
        self, request: CancelTaskRequest, *, context: ClientCallContext | None = None
    ) -> Task:
        response = await self._execute_request(
            "POST", f"/tasks/{_task_id_path_segment(request.id)}:cancel", request.tenant,
            context=context, json=MessageToDict(request),
        )
        return ParseDict(response, Task())


def validate_agent_card(card: AgentCard) -> str:
    """Validate and return the protocol-1.0 HTTP+JSON interface URL."""
    if not card.name.strip():
        raise AgentCardContractError("Agent Card name must not be blank")
    if not card.version.strip():
        raise AgentCardContractError("Agent Card application version must not be blank")

    http_json_interfaces = [
        interface
        for interface in card.supported_interfaces
        if interface.protocol_binding == TransportProtocol.HTTP_JSON.value
    ]
    if not http_json_interfaces:
        raise AgentCardContractError("Agent Card must declare an HTTP+JSON interface")

    protocol_1_0 = [
        interface
        for interface in http_json_interfaces
        if interface.protocol_version == PROTOCOL_VERSION_1_0
    ]
    if not protocol_1_0:
        advertised = sorted(
            {interface.protocol_version or "<missing>" for interface in http_json_interfaces}
        )
        raise VersionNotSupportedError(
            "Project requires A2A Protocol 1.0; Agent Card advertises "
            + ", ".join(advertised)
        )

    if "application/json" not in card.default_input_modes:
        raise AgentCardContractError(
            "Agent Card must accept application/json input for this project"
        )
    if "application/json" not in card.default_output_modes:
        raise AgentCardContractError(
            "Agent Card must support application/json output for this project"
        )

    interface_url = protocol_1_0[0].url.strip()
    parsed = urlsplit(interface_url)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.netloc
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise AgentCardContractError("Agent interface URL must be an absolute HTTP(S) URL")
    try:
        parsed.port
    except ValueError as exc:
        raise AgentCardContractError("Agent interface URL has an invalid port") from exc
    return interface_url


class A2AAgentClient:
    """Resolve an Agent Card, send new Task requests, and retrieve their Tasks."""

    def __init__(
        self,
        agent_base_url: str,
        *,
        httpx_client: httpx.AsyncClient | None = None,
        timeout_seconds: float = 10.0,
        headers: Mapping[str, str] | None = None,
    ) -> None:
        parsed = urlsplit(agent_base_url)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.netloc
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("agent_base_url must be an absolute HTTP(S) URL")
        try:
            parsed.port
        except ValueError as exc:
            raise ValueError("agent_base_url contains an invalid port") from exc
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")

        self._agent_base_url = agent_base_url.rstrip("/")
        self._owns_httpx_client = httpx_client is None
        self._httpx_client = httpx_client or httpx.AsyncClient(timeout=timeout_seconds, headers=headers)
        if httpx_client is not None and headers:
            self._httpx_client.headers.update(headers)
        self._httpx_client.headers[VERSION_HEADER] = PROTOCOL_VERSION_1_0
        self._httpx_client.headers["Content-Type"] = A2A_JSON_MEDIA_TYPE
        self._resolver = A2ACardResolver(self._httpx_client, self._agent_base_url)
        self._factory = ClientFactory(
            ClientConfig(
                streaming=False,
                polling=True,
                httpx_client=self._httpx_client,
                supported_protocol_bindings=[TransportProtocol.HTTP_JSON.value],
                accepted_output_modes=["application/json"],
            )
        )
        self._factory.register(
            TransportProtocol.HTTP_JSON.value,
            lambda card, url, config: _OpaqueTaskRestTransport(self._httpx_client, card, url),
        )
        self._card: AgentCard | None = None
        self._client: Client | None = None
        self._interface_url: str | None = None

    @property
    def agent_card(self) -> AgentCard:
        if self._card is None:
            raise RuntimeError("Agent Card has not been resolved yet")
        return self._card

    @property
    def interface_url(self) -> str:
        if self._interface_url is None:
            raise RuntimeError("Agent Card has not been resolved yet")
        return self._interface_url

    async def resolve_agent_card(self, *, refresh: bool = False) -> AgentCard:
        if self._card is not None and not refresh:
            return self._card
        if refresh:
            self._card = None
            self._client = None
            self._interface_url = None

        card = await self._resolver.get_agent_card()
        interface_url = validate_agent_card(card)
        if not _same_origin(self._agent_base_url, interface_url):
            raise AgentCardContractError(
                "Agent Card interface must use the configured Agent origin"
            )
        try:
            client_card = _card_with_selected_interface(card, interface_url)
            client = self._factory.create(client_card)
        except ValueError as exc:
            raise AgentCardContractError("No compatible A2A 1.0 HTTP+JSON interface") from exc

        self._card = card
        self._client = client
        self._interface_url = interface_url
        return card

    async def send_task(
        self,
        payload: Mapping[str, object],
        metadata: A2AWorkflowMetadata,
        *,
        context_id: str | None = None,
        message_id: UUID | None = None,
    ) -> Task:
        """Send one new, non-streaming Task and return the server-generated Task."""
        client = await self._require_client()
        request = build_send_message_request(
            payload, metadata, context_id=context_id, message_id=message_id
        )
        return await self._send_request(client, request)

    async def continue_task(
        self,
        task_id: str,
        payload: Mapping[str, object],
        metadata: A2AWorkflowMetadata,
        *,
        context_id: str | None = None,
        message_id: UUID | None = None,
    ) -> Task:
        """Continue an interrupted Agent-issued Task without changing its ID."""
        if not task_id.strip():
            raise A2AProjectContractError("task_id must not be blank")
        client = await self._require_client()
        request = build_send_message_request(
            payload,
            metadata,
            context_id=context_id,
            task_id=task_id,
            message_id=message_id,
        )
        task = await self._send_request(client, request)
        if task.id != task_id:
            raise A2AProjectContractError(
                "continued SendMessage response changed the existing Task ID"
            )
        return task

    async def send_snapshot_handoff(
        self,
        handoff: SnapshotHandoff,
        recipient: AgentRole,
        request_text: str,
        *,
        workflow_step_id: UUID,
        scenario_id: UUID,
        context_id: str | None = None,
        attempt: int = 0,
        requirement_ids: tuple[UUID, ...] | None = None,
        message_id: UUID | None = None,
    ) -> Task:
        """Send a prepared Snapshot handoff payload as a new A2A Task."""
        client = await self._require_client()
        request = build_snapshot_handoff_request(
            handoff,
            recipient,
            request_text,
            workflow_step_id=workflow_step_id,
            scenario_id=scenario_id,
            context_id=context_id,
            attempt=attempt,
            requirement_ids=requirement_ids,
            message_id=message_id,
        )
        return await self._send_request(client, request)

    async def get_task(self, task_id: str) -> Task:
        """Fetch the current Task state; Task IDs are opaque Agent-issued strings."""
        if not task_id.strip():
            raise A2AProjectContractError("task_id must not be blank")
        client = await self._require_client()
        return await client.get_task(GetTaskRequest(id=task_id))

    async def cancel_task(self, task_id: str) -> Task:
        """Request cancellation remotely; return the Agent's actual Task state."""
        if not task_id.strip():
            raise A2AProjectContractError("task_id must not be blank")
        client = await self._require_client()
        task = await client.cancel_task(CancelTaskRequest(id=task_id))
        if task.id != task_id:
            raise A2AProjectContractError("CancelTask response changed the Agent Task ID")
        return task

    async def aclose(self) -> None:
        if self._owns_httpx_client:
            await self._httpx_client.aclose()

    async def __aenter__(self) -> "A2AAgentClient":
        return self

    async def __aexit__(self, exc_type: object, exc: object, traceback: object) -> None:
        await self.aclose()

    async def _require_client(self) -> Client:
        if self._client is None:
            await self.resolve_agent_card()
        assert self._client is not None
        return self._client

    async def _send_request(
        self, client: Client, request: SendMessageRequest
    ) -> Task:
        response = await _first_response(client.send_message(request))
        if not response.HasField("task"):
            raise A2AProjectContractError(
                "this project operation requires a Task response, not an immediate Message"
            )
        task = response.task
        if not task.id.strip():
            raise A2AProjectContractError("Agent returned a Task without a server Task ID")
        return task


async def _first_response(
    responses: AsyncIterator[StreamResponse],
) -> StreamResponse:
    async for response in responses:
        return response
    raise A2AProjectContractError("A2A server returned no SendMessage response")


def _card_with_selected_interface(card: AgentCard, interface_url: str) -> AgentCard:
    """Prevent the SDK selector from silently choosing another protocol version."""
    selected = next(
        interface
        for interface in card.supported_interfaces
        if interface.url == interface_url
        and interface.protocol_binding == TransportProtocol.HTTP_JSON.value
        and interface.protocol_version == PROTOCOL_VERSION_1_0
    )
    client_card = AgentCard()
    client_card.CopyFrom(card)
    del client_card.supported_interfaces[:]
    client_card.supported_interfaces.add().CopyFrom(selected)
    return client_card


def _same_origin(first_url: str, second_url: str) -> bool:
    first = urlsplit(first_url)
    second = urlsplit(second_url)
    first_port = first.port or (443 if first.scheme.lower() == "https" else 80)
    second_port = second.port or (443 if second.scheme.lower() == "https" else 80)
    return (
        first.scheme.lower(),
        first.hostname,
        first_port,
    ) == (
        second.scheme.lower(),
        second.hostname,
        second_port,
    )
