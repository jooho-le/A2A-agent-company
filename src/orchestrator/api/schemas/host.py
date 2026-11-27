"""Public Host approval response, separate from runtime file/credential DTOs."""

from pydantic import Field, UUID4

from orchestrator.api.schemas.runs import APIModel
from orchestrator.domain.run_configuration import RunConfiguration


class ApprovedConfigurationResponse(APIModel):
    scenario_id: UUID4 = Field(alias="scenarioId")
    configuration_id: str = Field(alias="configurationId", pattern=r"^[0-9a-f]{64}$")
    configuration: RunConfiguration
