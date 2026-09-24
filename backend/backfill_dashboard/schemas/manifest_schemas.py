from pydantic import BaseModel, Field


class MachineManifestRequestModel(BaseModel):
    machine_id: str = ""
    since: str = ""
    until: str = ""
    manifest_path: str = ""


class MultiMachineManifestRequestModel(BaseModel):
    machine_ids: list[str] = Field(default_factory=list)
    since: str = ""
    until: str = ""
    manifest_path: str = ""
