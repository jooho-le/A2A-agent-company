"""프로젝트 API의 입력과 공개 응답입니다. 기존 인증·문서 스키마는 변경하지 않습니다."""

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, UUID4


class ProjectCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    name: str = Field(min_length=1, max_length=60)
    scenario_id: UUID4 = Field(alias="scenarioId")


class ProjectUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    name: str = Field(min_length=1, max_length=60)


class ProjectResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True, populate_by_name=True)

    project_id: UUID4 = Field(alias="projectId")
    name: str
    scenario_id: UUID4 = Field(alias="scenarioId")
    created_at: datetime = Field(alias="createdAt")
    updated_at: datetime = Field(alias="updatedAt")


class ProjectRunResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True, populate_by_name=True)

    host_key: str = Field(alias="hostKey")
    run_id: UUID4 = Field(alias="runId")
    configuration_id: str = Field(alias="configurationId")
    linked_at: datetime = Field(alias="linkedAt")


class ProjectListResponse(BaseModel):
    projects: list[ProjectResponse]


class ProjectDetailResponse(ProjectResponse):
    runs: list[ProjectRunResponse]
