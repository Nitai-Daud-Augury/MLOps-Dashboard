from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Callable, Literal


BACKFILL_KEY_VAULT_URL = "https://metaflow-iac-kv.vault.azure.net"
BACKFILL_ACCOUNT_KEY_SECRET_NAME = "FST-BACKFILL-ACCOUNT-KEY"


@dataclass(frozen=True)
class ManifestStorageCredential:
    kind: Literal["account_key", "sas_token"]
    value: str
    source: str


class ManifestCredentialResolver:
    """Resolve the same dedicated credential used by the FST backfill flow."""

    def __init__(
        self,
        *,
        environment: dict[str, str] | None = None,
        key_vault_loader: Callable[[], str] | None = None,
    ) -> None:
        self.environment = environment if environment is not None else os.environ
        self.key_vault_loader = key_vault_loader or _load_backfill_account_key_from_key_vault
        self._resolved: ManifestStorageCredential | None = None

    def resolve(self) -> ManifestStorageCredential:
        if self._resolved:
            return self._resolved
        account_key = self.environment.get("FST_BACKFILL_ACCOUNT_KEY") or self.environment.get("AZURE_STORAGE_KEY")
        if account_key:
            self._resolved = ManifestStorageCredential("account_key", account_key, "environment")
            return self._resolved

        sas_token = self.environment.get("FST_BACKFILL_SAS_TOKEN") or self.environment.get("AZURE_STORAGE_SAS_TOKEN")
        if sas_token:
            self._resolved = ManifestStorageCredential("sas_token", sas_token, "environment")
            return self._resolved

        try:
            account_key = self.key_vault_loader()
        except Exception as exc:
            raise RuntimeError(
                "Manifest upload is unavailable: unable to load FST-BACKFILL-ACCOUNT-KEY from "
                "Key Vault metaflow-iac-kv. Configure FST_BACKFILL_ACCOUNT_KEY (or a SAS token) "
                "for this dashboard service, or grant its identity Key Vault Secrets User access."
            ) from exc
        if not account_key:
            raise RuntimeError("Manifest upload is unavailable: Key Vault returned an empty backfill account key.")
        self._resolved = ManifestStorageCredential("account_key", account_key, "key_vault")
        return self._resolved

    def readiness(self) -> dict[str, str | bool]:
        credential = self.resolve()
        return {"ready": True, "credential_source": credential.source, "credential_kind": credential.kind}


def _load_backfill_account_key_from_key_vault() -> str:
    try:
        from azure.identity import DefaultAzureCredential
        from azure.keyvault.secrets import SecretClient
    except ImportError as exc:
        raise RuntimeError("Azure Key Vault dependencies are missing from the dashboard environment.") from exc

    client = SecretClient(vault_url=BACKFILL_KEY_VAULT_URL, credential=DefaultAzureCredential())
    return client.get_secret(BACKFILL_ACCOUNT_KEY_SECRET_NAME).value or ""
