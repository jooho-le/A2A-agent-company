"""Frozen execution inputs; incomplete local configuration is explicit."""

from datetime import datetime
import hashlib
import json
from typing import Literal
from uuid import uuid4

from pydantic import Field, UUID4, field_validator, model_validator

from orchestrator.domain.models import utc_now
from orchestrator.domain.snapshot_handoff import ImmutableDomainModel


class ModelConfiguration(ImmutableDomainModel):
    provider: str = Field(min_length=1)
    model_id: str = Field(alias="modelId", min_length=1)
    model_revision: str | None = Field(default=None, alias="modelRevision")
    temperature: float = Field(ge=0, allow_inf_nan=False)
    seed: int | None = None

    @field_validator("provider", "model_id")
    @classmethod
    def nonblank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("model identity must not be blank")
        return value.strip()


class ExecutionBaseline(ImmutableDomainModel):
    container_image_digest: str = Field(alias="containerImageDigest", pattern=r"^sha256:[0-9a-f]{64}$")
    dependency_lock_hash: str = Field(alias="dependencyLockHash", pattern=r"^sha256:[0-9a-f]{64}$")
    hardware_profile: str = Field(alias="hardwareProfile", min_length=1)
    network_policy: Literal["DENY", "ALLOWLIST"] = Field(default="DENY", alias="networkPolicy")
    allowed_hosts: tuple[str, ...] = Field(default=(), alias="allowedHosts")

    @model_validator(mode="after")
    def validate_network_policy(self) -> "ExecutionBaseline":
        if not self.hardware_profile.strip():
            raise ValueError("hardwareProfile must not be blank")
        if self.network_policy == "DENY" and self.allowed_hosts:
            raise ValueError("DENY network policy cannot have allowed hosts")
        if self.network_policy == "ALLOWLIST" and not self.allowed_hosts:
            raise ValueError("ALLOWLIST requires explicitly approved hosts")
        return self


class ExecutionLimits(ImmutableDomainModel):
    max_fix_attempts: Literal[3] = Field(default=3, alias="maxFixAttempts")
    max_tool_retries: Literal[2] = Field(default=2, alias="maxToolRetries")
    runtime_budget_ms: int | None = Field(default=None, gt=0, alias="runtimeBudgetMs", strict=True)


class RunConfiguration(ImmutableDomainModel):
    """Experiment metadata must be complete before labeling a Run an experiment."""

    experiment_id: UUID4 | None = Field(default=None, alias="experimentId")
    architecture: Literal["MULTI_AGENT"] = "MULTI_AGENT"
    starting_commit_hash: str | None = Field(default=None, alias="startingCommitHash", pattern=r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
    starting_snapshot_sha256: str | None = Field(default=None, alias="startingSnapshotSha256", pattern=r"^[0-9a-f]{64}$")
    model: ModelConfiguration | None = None
    limits: ExecutionLimits = Field(default_factory=ExecutionLimits)
    environment: ExecutionBaseline | None = None
    protected_test_suite_ref: str | None = Field(default=None, min_length=1, alias="protectedTestSuiteRef")
    scanner_profile_ref: str | None = Field(default=None, min_length=1, alias="scannerProfileRef")
    warmup_state: str | None = Field(default=None, alias="warmupState")
    execution_order: int | None = Field(default=None, ge=0, alias="executionOrder", strict=True)

    @field_validator("protected_test_suite_ref", "scanner_profile_ref", "warmup_state")
    @classmethod
    def optional_baseline_text_is_not_blank(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("declared comparison references and warmup state must not be blank")
        return value.strip() if value is not None else None

    @model_validator(mode="after")
    def validate_experiment_inputs(self) -> "RunConfiguration":
        if self.experiment_id is not None and any(value is None for value in (
            self.starting_commit_hash, self.starting_snapshot_sha256, self.model,
            self.environment, self.limits.runtime_budget_ms,
            self.protected_test_suite_ref, self.scanner_profile_ref,
            self.warmup_state, self.execution_order,
        )):
            raise ValueError("experiment runs require the complete immutable comparison baseline")
        return self


class RunConfigurationArtifact(ImmutableDomainModel):
    artifact_id: UUID4 = Field(default_factory=uuid4, alias="artifactId")
    artifact_type: Literal["RUN_CONFIGURATION"] = Field(default="RUN_CONFIGURATION", alias="artifactType")
    artifact_version: Literal[1] = Field(default=1, alias="artifactVersion")
    created_by: Literal["Orchestrator"] = Field(default="Orchestrator", alias="createdBy")
    run_id: UUID4 = Field(alias="runId")
    scenario_id: UUID4 = Field(alias="scenarioId")
    workspace_id: UUID4 = Field(alias="workspaceId")
    configuration: RunConfiguration = Field(default_factory=RunConfiguration)
    frozen_scenario_contract_json: str = Field(default="", alias="frozenScenarioContractJson")
    created_at: datetime = Field(default_factory=utc_now, alias="createdAt")

    @model_validator(mode="after")
    def freeze_scenario_contract(self) -> "RunConfigurationArtifact":
        if not self.frozen_scenario_contract_json:
            from orchestrator.domain.scenario_registry import get_scenario
            scenario = get_scenario(self.scenario_id)
            contract = scenario.planner_contract() if scenario else {"scenarioId": str(self.scenario_id)}
            object.__setattr__(self, "frozen_scenario_contract_json", json.dumps(contract, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
        contract = json.loads(self.frozen_scenario_contract_json)
        if not isinstance(contract, dict) or contract.get("scenarioId") != str(self.scenario_id):
            raise ValueError("frozen Scenario contract must belong to this configuration's scenarioId")
        if self.created_at.tzinfo is None or self.created_at.utcoffset() is None:
            raise ValueError("configuration timestamp must include a timezone")
        return self

    @property
    def scenario_contract(self) -> dict[str, object]:
        return json.loads(self.frozen_scenario_contract_json)

    @property
    def scenario_contract_sha256(self) -> str:
        return hashlib.sha256(self.frozen_scenario_contract_json.encode("utf-8")).hexdigest()

    @property
    def artifact_uri(self) -> str:
        return f"artifact://{self.artifact_id}/run-configuration.json"

    def to_artifact_json(self) -> dict[str, object]:
        return {
            **self.model_dump(mode="json", by_alias=True), "artifactUri": self.artifact_uri,
            "scenarioContract": self.scenario_contract,
            "scenarioContractSha256": self.scenario_contract_sha256,
        }
