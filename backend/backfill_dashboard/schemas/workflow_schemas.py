from pydantic import BaseModel, Field

from ..admin import DEFAULT_FLOW_PATH
from ..deps import settings


class WorkflowSourceModel(BaseModel):
    source_type: str = "local"
    local_flow_path: str = str(DEFAULT_FLOW_PATH)
    github_repo_url: str = ""
    github_ref: str = "master"
    github_flow_path: str = "FSTBackfill_prod_flow.py"


class TriggerParamsModel(BaseModel):
    environment: str = "dev"
    namespace: str = "ulrpm-fst-dev-20260830"
    storage_account_manifest_path: str = ""
    include_features_to_backfill: bool = False
    features_to_backfill: list[str] = Field(default_factory=lambda: settings.target_features.copy())
    max_parallel_steps: int = 1
    force_sessions_from_bucket: bool = False
    confirm_production: bool = False
    confirmation_text: str = ""


class TerminateParamsModel(BaseModel):
    environment: str = "dev"
    namespace: str = "ulrpm-fst-dev-20260830"
    workflow_id: str = ""
    confirm_production: bool = False
    confirmation_text: str = ""


class CreateWorkflowRequest(BaseModel):
    source: WorkflowSourceModel = Field(default_factory=WorkflowSourceModel)


class TriggerWorkflowRequest(BaseModel):
    source: WorkflowSourceModel = Field(default_factory=WorkflowSourceModel)
    params: TriggerParamsModel = Field(default_factory=TriggerParamsModel)


class TerminateWorkflowRequest(BaseModel):
    params: TerminateParamsModel = Field(default_factory=TerminateParamsModel)
