"""Owned, role-isolated A2A clients for one local diagnostic server session."""

from contextlib import asynccontextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from uuid import uuid4

import httpx

from orchestrator.a2a import A2AAgentClient


@dataclass(repr=False)
class _Binding:
    role: str
    request_hook: object
    response_hook: object
    active: bool = True


@dataclass(repr=False)
class _Peer:
    endpoint: str
    token: str
    transport: httpx.AsyncClient
    client: A2AAgentClient
    active: bool = False


class DiagnosticPeerSessions:
    """No global cache and no I/O until an explicitly borrowed peer is used."""

    def __init__(self, *, transport_factory=None):
        self.session_id = str(uuid4())
        self._peers = {}
        self._binding = ContextVar("diagnostic_peer_binding", default=None)
        self._closed = False
        self._transport_factory = transport_factory

    def _current(self, role):
        binding = self._binding.get()
        if binding is None or not binding.active or binding.role != role:
            raise RuntimeError("DIAGNOSTIC_PEER_UNBOUND")
        return binding

    async def _create(self, role, endpoint, token):
        async def on_request(request):
            await self._current(role).request_hook(request)

        async def on_response(response):
            await self._current(role).response_hook(response)

        transport = httpx.AsyncClient(timeout=8, trust_env=False,
            transport=self._transport_factory(role) if self._transport_factory is not None else None,
            headers={"Authorization": "Bearer " + token},
            event_hooks={"request": [on_request], "response": [on_response]})
        try:
            client = A2AAgentClient(endpoint, httpx_client=transport, trust_env=False)
        except Exception:
            await transport.aclose()
            raise RuntimeError("DIAGNOSTIC_PEER_CONFIGURATION_INVALID") from None
        return _Peer(endpoint, token, transport, client)

    @asynccontextmanager
    async def borrow(self, role, endpoint, token, request_hook, response_hook):
        if self._closed:
            raise RuntimeError("DIAGNOSTIC_PEER_SESSION_CLOSED")
        peer = self._peers.get(role)
        reused = peer is not None
        if peer is None:
            peer = await self._create(role, endpoint, token)
            self._peers[role] = peer
        elif peer.endpoint != endpoint or peer.token != token:
            raise RuntimeError("DIAGNOSTIC_PEER_BINDING_CHANGED")
        if peer.active:
            raise RuntimeError("DIAGNOSTIC_PEER_BUSY")
        try:
            peer.client.agent_card
            card_cached = True
        except RuntimeError:
            card_cached = False
        binding = _Binding(role, request_hook, response_hook)
        context_token = self._binding.set(binding)
        peer.active = True
        try:
            yield peer.client, {"scope": "DIAGNOSTIC_SESSION", "sessionId": self.session_id,
                                "clientReused": reused, "agentCardCached": card_cached}
        finally:
            binding.active = False
            peer.active = False
            self._binding.reset(context_token)

    async def aclose(self):
        if self._closed:
            return
        self._closed = True
        failed = False
        for peer in self._peers.values():
            try:
                await peer.transport.aclose()
            except Exception:
                failed = True
        if failed:
            raise RuntimeError("DIAGNOSTIC_PEER_CLOSE_UNVERIFIED") from None
