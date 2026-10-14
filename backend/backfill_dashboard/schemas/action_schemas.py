from pydantic import BaseModel, Field

from .workflow_schemas import WorkflowSourceModel


class CancelMonthRequest(BaseModel):
    parent_workflow_id: str = ""
    child_workflow_id: str = ""
    requeue_month_indices: list[int] = Field(default_factory=list)
    requested_by: str = "dashboard"
    environment: str = "dev"


class ReleaseMonthLockRequest(BaseModel):
    """Admin release of one stale month lock. ``confirm`` must equal ``<machine_id>:<YYYY-MM>``."""
    confirm: str = ""
    reason: str = Field(default="", max_length=500)
    dry_run: bool = True


class LogsRequest(BaseModel):
    source: WorkflowSourceModel = Field(default_factory=WorkflowSourceModel)
    run_task_path: str = ""
    stream: str = "stdout"
