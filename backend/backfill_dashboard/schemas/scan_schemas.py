from pydantic import BaseModel


class ScanRequest(BaseModel):
    source_account: str | None = None
    source_container: str | None = None
