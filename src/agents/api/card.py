"""Honest A2A 1.0 Cards: bootstrap by default, opt-in implemented roles."""

from a2a.types import (
    AgentCapabilities,
    AgentCard,
    AgentInterface,
    AgentSkill,
    HTTPAuthSecurityScheme,
    SecurityRequirement,
    SecurityScheme,
    StringList,
)

from agents.core.config import AgentSettings
from agents.core.contracts import A2A_PROTOCOL_BINDING, A2A_PROTOCOL_VERSION
from orchestrator.domain.states import AgentRole


_ROLE_PURPOSES = {
    AgentRole.PLANNER: "requirement analysis and task planning",
    AgentRole.DEVELOPER: "product implementation, fixes, and build requests",
    AgentRole.QA: "independent functional testing and QA reporting",
    AgentRole.SECURITY: "independent security inspection and reporting",
}


def build_agent_card(settings: AgentSettings, *, execution_ready: bool = False) -> AgentCard:
    """Use official SDK models without advertising unimplemented skills."""
    if type(execution_ready) is not bool or execution_ready and settings.role not in tuple(AgentRole):
        raise ValueError("AGENT_EXECUTOR_ROLE_MISMATCH")
    description = (
        "Host-configured Planner: produces a validated task plan while preserving "
        "frozen requirements. Per-Run configuration and budget must be admitted; "
        "no Source modification, MCP execution or product verdict is performed."
        if execution_ready and settings.role is AgentRole.PLANNER else
        "Host-configured Developer: implements the protected plan or saved fix Issues via local MCP, "
        "freezes a measured Git candidate and reports its real Build receipt. "
        "Approved Host configuration and shared budget are required; "
        "no independent QA/Security or product verdict is performed."
        if execution_ready and settings.role is AgentRole.DEVELOPER else
        "Host-configured QA: validates initial and revised candidates, generates isolated tests and binds cases "
        "to frozen requirements, then reports measured Unit/Browser receipts "
        "for the same read-only Source. Approved Host policies and shared "
        "budget are required; no Source edits or product verdict are performed."
        if execution_ready and settings.role is AgentRole.QA else
        "Host-configured Security: validates initial and revised candidates, scans approved profiles on the same "
        "read-only Source and reviews measured findings with bounded code references. "
        "Trusted Host semantic proof is required for verified outcomes; missing "
        "proof stays unverified. No Source edits or product verdict are performed."
        if execution_ready and settings.role is AgentRole.SECURITY else
        f"Intended role: {_ROLE_PURPOSES[settings.role]}. "
        "Transport-only bootstrap: the Agent runtime is not configured. "
        "Tasks are explicitly rejected; no LLM or MCP execution occurs."
    )
    card = AgentCard(
        name=f"{settings.role.value} Agent",
        description=description,
        version="0.1.0",
        supported_interfaces=[
            AgentInterface(
                url=settings.agent_base_url,
                protocol_binding=A2A_PROTOCOL_BINDING,
                protocol_version=A2A_PROTOCOL_VERSION,
            )
        ],
        capabilities=AgentCapabilities(streaming=False, push_notifications=False),
        default_input_modes=["application/json"],
        default_output_modes=["application/json"],
        skills=[AgentSkill(
            id="protected-task-planning", name="Protected requirement task planning",
            description="Plans tasks from Host-frozen requirements; clarification or rejection does not fabricate a plan.",
            tags=["planning", "requirements"],
        )] if execution_ready and settings.role is AgentRole.PLANNER else [AgentSkill(
            id="measured-initial-implementation", name="Measured Source implementation and Issue fixes",
            description="Implements the protected plan or saved Issues through MCP and reports a new immutable candidate with measured Build evidence and predecessor lineage, not project success.",
            tags=["implementation", "snapshot", "build"],
        )] if execution_ready and settings.role is AgentRole.DEVELOPER else [AgentSkill(
            id="measured-initial-qa", name="Measured functional QA and revalidation",
            description="Generates isolated tests and binds protected requirements to Host-measured Unit/Browser cases on the current immutable candidate, preserving report lineage without inventing results or project success.",
            tags=["qa", "unit-tests", "browser-tests", "snapshot"],
        )] if execution_ready and settings.role is AgentRole.QA else [AgentSkill(
            id="measured-initial-security", name="Measured security review and revalidation",
            description="Reviews all approved frozen scans with actual Source references; only Host-verified proof can establish confirmed findings or requirement outcomes, not project success.",
            tags=["security", "scan", "code-review", "snapshot"],
        )] if execution_ready and settings.role is AgentRole.SECURITY else [],
    )
    if settings.bearer_token is not None:
        card.security_schemes["bearerAuth"].CopyFrom(
            SecurityScheme(
                http_auth_security_scheme=HTTPAuthSecurityScheme(scheme="bearer")
            )
        )
        card.security_requirements.append(
            SecurityRequirement(schemes={"bearerAuth": StringList(list=[])})
        )
    return card
