from pydantic import BaseModel, Field


class FullRlblTestRequestModel(BaseModel):
    machine_ids: list[str] = Field(default_factory=list)
    fst_namespace: str = "ulrpm-fst-dev-20260830"
    lst_namespace: str = "severity-relabel-dev-ulrpm"
    pipeline_name: str = ""
    manifest_path: str = ""
    runtime_patch: bool = False
    persist_dev_lst: bool = False
    seed_dev_lst: bool = False
    feature_fetch_mode: str = "legacy"
    test_mode: str = "fetch_only"
    wide_range_since: str = "2024/08/30/00"
    wide_range_until: str = "2026/08/30/00"
    memory_mb: int = 8192
