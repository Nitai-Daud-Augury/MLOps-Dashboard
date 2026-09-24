from __future__ import annotations

from pydantic import BaseModel, Field, model_validator


class SelectionFilter(BaseModel):
    search: str = Field(default="", max_length=200)
    cohort: str | None = None
    status: str | None = None
    eligible: bool | None = True
    site_id: str | None = None
    organization_id: str | None = None
    classification_issue: str | None = None
    sort_by: str = "machine_id"
    sort_dir: str = "asc"


class MachineSelection(BaseModel):
    inventory_version: str = ""
    filter: SelectionFilter = Field(default_factory=SelectionFilter)
    excluded_machine_ids: list[str] = Field(default_factory=list, max_length=5000)
    explicit_machine_ids: list[str] = Field(default_factory=list, max_length=500)


class EstimateRequest(BaseModel):
    selection: MachineSelection
    start_at: str
    end_at: str
    feature_set_version: str = Field(min_length=1, max_length=100)
    standard_window_days: int = Field(default=30, ge=1, le=31)
    ulrpm_window_days: int = Field(default=5, ge=1, le=7)


class CampaignSubmitRequest(BaseModel):
    estimate_id: str = Field(min_length=8, max_length=64)
    estimate_signature: str = Field(min_length=32, max_length=128)
    name: str = Field(default="", max_length=120)
    created_by: str = Field(default="dashboard-user", max_length=120)
    production: bool = False
    confirmation_text: str = ""
    ulrpm_confirmation_text: str = ""


class CampaignActionRequest(BaseModel):
    action: str
    reason: str = Field(default="", max_length=500)

    @model_validator(mode="after")
    def valid_action(self):
        if self.action not in {"pause", "resume", "cancel"}:
            raise ValueError("action must be pause, resume, or cancel")
        return self


class WorkItemActionRequest(BaseModel):
    action: str
    reason: str = Field(min_length=3, max_length=500)

    @model_validator(mode="after")
    def valid_action(self):
        if self.action not in {"retry", "skip"}:
            raise ValueError("action must be retry or skip")
        return self


class MachineActionRequest(BaseModel):
    action: str
    reason: str = Field(min_length=3, max_length=500)

    @model_validator(mode="after")
    def valid_action(self):
        if self.action not in {"pause", "resume", "cancel"}:
            raise ValueError("action must be pause, resume, or cancel")
        return self


class BenchmarkRecordRequest(BaseModel):
    cohort: str
    resource_profile_version: str = Field(min_length=1, max_length=100)
    feature_set_version: str = Field(min_length=1, max_length=100)
    window_days: int = Field(ge=1, le=31)
    endpoint_count: int = Field(default=1, ge=0, le=1000)
    duration_hours: float = Field(gt=0, le=720)
    retry_count: int = Field(default=0, ge=0, le=100)
    peak_memory_mib: float | None = Field(default=None, gt=0)

    @model_validator(mode="after")
    def valid_cohort(self):
        if self.cohort not in {"standard", "ulrpm"}:
            raise ValueError("cohort must be standard or ulrpm")
        return self
