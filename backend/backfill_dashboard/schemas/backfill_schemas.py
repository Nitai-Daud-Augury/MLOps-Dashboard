from pydantic import BaseModel, Field

from .workflow_schemas import TriggerParamsModel, WorkflowSourceModel


class AllMachinesBackfillRequest(BaseModel):
    source: WorkflowSourceModel = Field(default_factory=WorkflowSourceModel)
    params: TriggerParamsModel = Field(default_factory=TriggerParamsModel)
    since: str = ""
    until: str = ""
    manifest_path: str = ""
    month_indices_by_machine: dict[str, list[int]] = Field(default_factory=dict)


class MonthPlanRequest(BaseModel):
    machine_ids: list[str] = Field(default_factory=list)
    mode: str = "gaps"


class OrchestratedBackfillRequest(BaseModel):
    machine_ids: list[str] = Field(default_factory=list)
    since: str = ""
    until: str = ""
    manifest_prefix: str = ""
    month_indices_by_machine: dict[str, list[int]] = Field(default_factory=dict)
    params: TriggerParamsModel = Field(default_factory=TriggerParamsModel)


class OrchestratedManifestRequest(BaseModel):
    machine_ids: list[str] = Field(default_factory=list)
    since: str = ""
    until: str = ""
    manifest_prefix: str = ""
    month_indices_by_machine: dict[str, list[int]] = Field(default_factory=dict)
