"""Databricks SQL auth selection: Databricks Apps SP vs explicit local profile.

Inside Databricks Apps the app service principal is injected as
DATABRICKS_HOST / DATABRICKS_CLIENT_ID / DATABRICKS_CLIENT_SECRET; the code must
use the SDK default chain there and never a CLI profile. Locally a profile is
used only when DATABRICKS_PROFILE is set. Token acquisition is serialized and
shares one Config so parallel connections cannot race a token refresh.
"""
from __future__ import annotations

import sys
import threading
import time
import types

import pytest

import backfill_dashboard.lifecycle_databricks as lifecycle_mod
from backfill_dashboard import silver
from backfill_dashboard.lifecycle_databricks import DatabricksLifecycleProvider, build_databricks_lifecycle_provider
from backfill_dashboard.silver import _create_config, auth_description, serialized_credentials_provider, shared_config


class ConfigStub:
    constructed: list[dict] = []

    def __init__(self, **kwargs):
        ConfigStub.constructed.append(kwargs)
        self.host = "https://adb-123.azuredatabricks.net"
        self.kwargs = kwargs

    def authenticate(self):
        return {"Authorization": "Bearer test"}


@pytest.fixture(autouse=True)
def _reset(monkeypatch):
    ConfigStub.constructed = []
    silver.clear_config_cache()
    for key in ("DATABRICKS_PROFILE", "MLOPS_DASHBOARD_RUNTIME", "DATABRICKS_APP_PORT"):
        monkeypatch.delenv(key, raising=False)
    yield
    silver.clear_config_cache()


def _apps_env(monkeypatch):
    monkeypatch.setenv("MLOPS_DASHBOARD_RUNTIME", "databricks")
    monkeypatch.setenv("DATABRICKS_HOST", "adb-123.azuredatabricks.net")
    monkeypatch.setenv("DATABRICKS_CLIENT_ID", "00000000-0000-0000-0000-000000000000")
    monkeypatch.setenv("DATABRICKS_CLIENT_SECRET", "not-a-real-secret")


def test_apps_runtime_never_uses_a_profile():
    _create_config(ConfigStub, "mlops-dev", mode="databricks")
    _create_config(ConfigStub, None, mode="databricks")
    assert ConfigStub.constructed == [{}, {}]
    assert auth_description("mlops-dev", mode="databricks").startswith("auth=databricks-apps service principal")


def test_local_uses_profile_only_when_set():
    _create_config(ConfigStub, "mlops-dev", mode="local")
    _create_config(ConfigStub, None, mode="local")
    _create_config(ConfigStub, "   ", mode="local")
    _create_config(ConfigStub, None, mode="docker")
    assert ConfigStub.constructed == [{"profile": "mlops-dev"}, {}, {}, {}]
    assert auth_description("mlops-dev", mode="local") == "profile=mlops-dev"
    assert auth_description(None, mode="local") == "auth=sdk default chain"


def test_build_without_profile_env_uses_default_chain(monkeypatch):
    monkeypatch.setenv("DATABRICKS_WAREHOUSE_ID", "6ed9ddd0b2661edc")
    seen = []

    def fake_create(config_class, profile):
        seen.append(profile)
        return ConfigStub()

    monkeypatch.setattr(lifecycle_mod, "_create_config", fake_create)
    provider, status = build_databricks_lifecycle_provider()
    assert status == "healthy"
    assert seen == [None]
    assert provider.profile is None
    assert provider.auth_description == "auth=sdk default chain"


def test_build_in_apps_runtime_uses_app_service_principal_and_no_cli(monkeypatch):
    """Real _create_config path in Apps mode: Config() with no profile, no CLI subprocess."""
    _apps_env(monkeypatch)
    monkeypatch.setenv("DATABRICKS_WAREHOUSE_ID", "6ed9ddd0b2661edc")
    monkeypatch.setenv("DATABRICKS_PROFILE", "mlops-dev")  # must be ignored in Apps
    import subprocess

    def no_cli(*args, **kwargs):
        raise AssertionError(f"subprocess/CLI must not be used for auth in Databricks Apps: {args!r}")

    monkeypatch.setattr(subprocess, "run", no_cli)
    monkeypatch.setattr(subprocess, "Popen", no_cli)
    sdk_core = types.ModuleType("databricks.sdk.core")
    sdk_core.Config = ConfigStub
    monkeypatch.setitem(sys.modules, "databricks.sdk.core", sdk_core)

    provider, status = build_databricks_lifecycle_provider()
    assert status == "healthy"
    assert ConfigStub.constructed == [{}]
    assert provider.auth_description.startswith("auth=databricks-apps service principal")


def test_data_sources_reports_auth_mode(monkeypatch):
    from backfill_dashboard.config import Settings
    from backfill_dashboard.data_sources import describe_data_sources

    monkeypatch.setenv("LIFECYCLE_SOURCE", "databricks")
    _apps_env(monkeypatch)
    monkeypatch.setenv("DATABRICKS_PROFILE", "mlops-dev")
    info = describe_data_sources(Settings(), None, "healthy", None)
    assert info["databricks_auth"].startswith("auth=databricks-apps service principal")
    assert info["databricks_profile"] is None
    assert "not-a-real-secret" not in repr(info)


def test_shared_config_is_built_once_per_profile():
    a = shared_config(ConfigStub, "mlops-dev", mode="local")
    b = shared_config(ConfigStub, "mlops-dev", mode="local")
    c = shared_config(ConfigStub, None, mode="local")
    assert a is b and a is not c
    assert ConfigStub.constructed == [{"profile": "mlops-dev"}, {}]


def test_token_acquisition_is_serialized_across_threads():
    active = {"now": 0, "max": 0}
    guard = threading.Lock()

    class SlowAuth:
        def authenticate(self):
            with guard:
                active["now"] += 1
                active["max"] = max(active["max"], active["now"])
            time.sleep(0.05)
            with guard:
                active["now"] -= 1
            return {"Authorization": "Bearer x"}

    header_factory = serialized_credentials_provider(SlowAuth())()
    threads = [threading.Thread(target=header_factory) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert active["max"] == 1


def test_provider_reuses_one_config_for_concurrent_statements(monkeypatch):
    """Lifecycle + both cross-check queries share the provider's Config."""
    built = []

    def fake_create(config_class, profile):
        built.append(profile)
        time.sleep(0.02)
        return ConfigStub()

    calls = []

    class Cursor:
        description = [("x",)]

        def execute(self, query):
            pass

        def fetchall(self):
            return [(1,)]

        def close(self):
            pass

    class Conn:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def cursor(self):
            return Cursor()

    def fake_connect(**kwargs):
        headers = kwargs["credentials_provider"]()()
        calls.append((kwargs["server_hostname"], kwargs["http_path"], headers))
        return Conn()

    monkeypatch.setattr(lifecycle_mod, "_create_config", fake_create)
    sql_mod = types.ModuleType("databricks.sql")
    sql_mod.connect = fake_connect
    import databricks

    monkeypatch.setattr(databricks, "sql", sql_mod, raising=False)
    monkeypatch.setitem(sys.modules, "databricks.sql", sql_mod)

    provider = DatabricksLifecycleProvider(table="a.b.c", profile=None, warehouse_id="wh")
    threads = [threading.Thread(target=provider._execute_databricks, args=("SELECT 1",)) for _ in range(3)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert built == [None]
    assert len(calls) == 3
    assert all(call[0] == "adb-123.azuredatabricks.net" and call[1] == "/sql/1.0/warehouses/wh" for call in calls)
