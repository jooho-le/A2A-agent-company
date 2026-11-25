"""Bounded implementation choices shared by service agents, never user authority.

SCN-001 does not use this contract. The original request, permissions, budget
and acceptance criteria stay frozen; the Planner selects interfaces within them.
"""
import re
import json
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

SERVICE_POLICY = "SERVICE_AUTONOMY_V2"


class DesignModel(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class ServiceEndpoint(DesignModel):
    method: Literal["GET", "POST", "PUT", "PATCH", "DELETE"]
    path: str = Field(pattern=r"^/[A-Za-z0-9_/{.}-]{0,199}$")
    body_format: Literal["NONE", "JSON", "FORM"] = Field(alias="bodyFormat")
    request_fields: list[str] = Field(alias="requestFields", max_length=32)
    response_format: Literal["JSON", "HTML", "EMPTY"] = Field(alias="responseFormat")
    success_statuses: list[int] = Field(alias="successStatuses", min_length=1, max_length=8)
    rejection_statuses: list[int] = Field(alias="rejectionStatuses", max_length=8)

    @model_validator(mode="after")
    def valid_interface(self):
        if (any(not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,63}", name) for name in self.request_fields)
                or len(set(self.request_fields)) != len(self.request_fields)
                or self.body_format == "NONE" and self.request_fields
                or self.method in ("GET", "DELETE") and self.body_format != "NONE"
                or any(type(n) is not int or not 200 <= n < 300 for n in self.success_statuses)
                or any(type(n) is not int or not 400 <= n < 500 for n in self.rejection_statuses)):
            raise ValueError("SERVICE_INTERFACE_INVALID")
        return self


class ServiceDesign(DesignModel):
    delivery: Literal["API", "WEB"]
    endpoints: list[ServiceEndpoint] = Field(min_length=1, max_length=32)
    storage: Literal["NONE", "SQLITE"]
    data_model: str = Field(alias="dataModel", min_length=1, max_length=2000)
    implementation_decisions: list[str] = Field(alias="implementationDecisions", max_length=8)

    @model_validator(mode="after")
    def bounded_design(self):
        identities = [(e.method, e.path) for e in self.endpoints]
        if (len(identities) != len(set(identities))
                or len(json.dumps(self.model_dump(mode='json', by_alias=True), ensure_ascii=False).encode()) > 7000
                or any(not value.strip() or len(value) > 600 for value in self.implementation_decisions)
                or self.delivery == "WEB" and not any(
                    e.method == "GET" and e.path == "/" and e.response_format == "HTML" for e in self.endpoints)):
            raise ValueError("SERVICE_DESIGN_INVALID")
        return self


def service_policy(contract):
    return (isinstance(contract, dict) and contract.get("scenarioKey") == "SERVICE-WEB"
            and contract.get("externalContract", {}).get("servicePolicy") == SERVICE_POLICY)


def explicit_api_request(contract):
    """Narrow guard for an explicit API request without a requested UI.

    Ambiguous requests remain the Planner's decision; this never infers the
    product's functionality or silently changes the user's request.
    """
    if not service_policy(contract):
        return False
    request = contract.get('externalContract', {}).get('userRequest', '')
    if not isinstance(request, str) or not re.search(
            r'(?<![a-z0-9])api(?![a-z0-9])\s*(?:(?:를|을|만|부터)\s*)?(?:만들|구현|개발|작성)', request, re.I):
        return False
    if re.search(r'api.{0,40}(?:필요\s*없|원하지|원치|하지\s*마|말고|없이|만들지\s*마)', request, re.I):
        return False
    ui = r'화면|페이지|사이트|프론트|브라우저|모바일|앱|(?<![a-z0-9])(?:html|ui|frontend|front-end|website|webpage|browser|screen)(?![a-z0-9])'
    return re.search(ui, request, re.I) is None


def service_design_schema(contract=None):
    """Inline, all-required schema for provider strict output; no remote refs."""
    text_list = {"type": "array", "maxItems": 32, "items": {"type": "string", "pattern": r"^[A-Za-z_][A-Za-z0-9_]{0,63}$"}}
    properties = {
        "method": {"type": "string", "enum": ["GET", "POST", "PUT", "PATCH", "DELETE"]},
        "path": {"type": "string", "pattern": r"^/[A-Za-z0-9_/{.}-]{0,199}$"},
        "bodyFormat": {"type": "string", "enum": ["NONE", "JSON", "FORM"]},
        "requestFields": text_list,
        "responseFormat": {"type": "string", "enum": ["JSON", "HTML", "EMPTY"]},
        "successStatuses": {"type": "array", "minItems": 1, "maxItems": 8, "items": {"type": "integer", "minimum": 200, "maximum": 299}},
        "rejectionStatuses": {"type": "array", "maxItems": 8, "items": {"type": "integer", "minimum": 400, "maximum": 499}},
    }
    design = {
        "delivery": {"type": "string", "enum": ["API", "WEB"]},
        "endpoints": {"type": "array", "minItems": 1, "maxItems": 32,
                      "items": {"type": "object", "additionalProperties": False, "required": list(properties), "properties": properties}},
        "storage": {"type": "string", "enum": ["NONE", "SQLITE"]},
        "dataModel": {"type": "string", "minLength": 1, "maxLength": 2000},
        "implementationDecisions": {"type": "array", "maxItems": 8, "items": {"type": "string", "minLength": 1, "maxLength": 600}},
    }
    if explicit_api_request(contract):
        design['delivery']['enum'] = ['API']
        properties['responseFormat']['enum'] = ['JSON', 'EMPTY']
    return {"type": "object", "additionalProperties": False, "required": list(design), "properties": design}


def validate_service_plan(contract, plan):
    if service_policy(contract) and plan.service_design is None:
        raise ValueError("SERVICE_DESIGN_REQUIRED")
    if explicit_api_request(contract) and plan.service_design is not None and (
            plan.service_design.delivery != 'API'
            or any(endpoint.response_format == 'HTML' for endpoint in plan.service_design.endpoints)):
        raise ValueError('SERVICE_API_REQUEST_REQUIRES_API_DELIVERY')
    if contract.get("scenarioKey") != "SERVICE-WEB" and plan.service_design is not None:
        raise ValueError("EXPERIMENT_DESIGN_OVERRIDE_DENIED")


def design_for_execution(execution):
    """All Source roles authenticate the same immutable Requirement Artifact."""
    from orchestrator.application.planner_output import PlannerPlan
    plan = getattr(execution, "plan", None)
    if plan is None:
        artifact = getattr(execution, "requirement_artifact", None)
        plan = None if artifact is None else PlannerPlan.model_validate(artifact.payload)
    if plan is None:
        return None
    validate_service_plan(execution.configuration.scenario_contract, plan)
    return plan.service_design
