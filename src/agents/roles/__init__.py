"""Role Prompt/output contracts; actual executors arrive in steps 30–33."""

from agents.roles.contracts import (
    AgentRoleContract, ArtifactOutputContract, ROLE_CONTRACTS,
    ROLE_CONTRACT_VERSION, RoleContractError, get_role_contract,
)
from agents.roles.outputs import RoleOutputContractError, validate_completed_role_output
from agents.roles.prompts import (
    PreparedRolePrompt, RolePromptInputError, build_system_prompt, prepare_role_prompt,
)

__all__ = [
    "AgentRoleContract", "ArtifactOutputContract", "ROLE_CONTRACTS",
    "ROLE_CONTRACT_VERSION", "RoleContractError", "get_role_contract",
    "RoleOutputContractError", "validate_completed_role_output",
    "PreparedRolePrompt", "RolePromptInputError", "build_system_prompt", "prepare_role_prompt",
]
