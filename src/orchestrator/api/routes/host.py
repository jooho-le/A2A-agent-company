"""Public approved configuration; never expose the operator's input files."""

from hashlib import sha256
import json
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, Request, Response
from pydantic import UUID4

from orchestrator.api.schemas.host import ApprovedConfigurationResponse
from orchestrator.core.security import redact_data
from orchestrator.domain import get_scenario
from orchestrator.domain.run_configuration import RunConfiguration


router = APIRouter(prefix="/host", tags=["host"])


@router.get("/approved-configuration", response_model=ApprovedConfigurationResponse)
def get_approved_configuration(request: Request, response: Response,
                               scenario_id: Annotated[UUID4, Query(alias="scenarioId")]):
    response.headers["Cache-Control"] = "no-store"
    if get_scenario(scenario_id) is None:
        raise HTTPException(status_code=422, detail="SCENARIO_NOT_REGISTERED",
                            headers={"Cache-Control": "no-store"})
    provider = request.app.state.approved_configuration_provider
    try:
        if not callable(provider):
            raise ValueError
        configuration = provider()
        if type(configuration) is not RunConfiguration:
            raise ValueError
        # Revalidate even a mutated model_construct/model_copy result.
        configuration = RunConfiguration.model_validate_json(configuration.model_dump_json())
        public = configuration.model_dump(mode="json", by_alias=True)
        if redact_data(public) != public:
            raise ValueError
        request.app.state.submission_validator(configuration)
        canonical = json.dumps(public, ensure_ascii=False, sort_keys=True,
                               separators=(",", ":"), allow_nan=False).encode("utf-8")
    except Exception:
        raise HTTPException(status_code=503, detail="HOST_CONFIGURATION_UNAVAILABLE",
                            headers={"Cache-Control": "no-store"}) from None
    return ApprovedConfigurationResponse(scenarioId=scenario_id,
        configurationId=sha256(canonical).hexdigest(), configuration=configuration)
