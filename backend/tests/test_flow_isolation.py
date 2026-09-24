from pathlib import Path

from sibling_repos import requires_flow_sources

pytestmark = requires_flow_sources

ROOT = Path(__file__).resolve().parents[3]
FLOW = ROOT / "Augury repos" / "metaflow-bx" / "FSTBackfill_prod_flow.py"
ORCHESTRATOR = (
    ROOT
    / "Augury repos"
    / "MLOps research"
    / "ulrpm_dev_backfill"
    / "UlrpmDevBackfillOrchestratorFlow.py"
)
BUNDLED_FLOW = ORCHESTRATOR.with_name("FSTBackfill_prod_flow.py")


def test_flow_has_no_hardcoded_750_or_parallelism_alias():
    source = FLOW.read_text()
    assert "total_steps=750" not in source
    assert 'Parameter("max_parallel_steps"' not in source
    assert "manifest_bucket_count=self.manifest_bucket_count" in source


def test_production_namespace_skips_dev_feature_seeding():
    source = FLOW.read_text()
    assert 'self.is_prod = self.ns == "feature-store-container"' in source
    assert "if not self.is_prod:" in source
    assert "seed_dev_features" not in source


def test_orchestrator_accepts_and_validates_selected_namespace():
    source = ORCHESTRATOR.read_text()
    assert 'PROD_NAMESPACE = "feature-store-container"' in source
    assert "Refusing to run against production FST namespace" not in source
    assert 'f"Is prod: {name_space == PROD_NAMESPACE}"' in source
    assert '"--name-space",\n        name_space,' in source
    assert 'CHILD_BRANCH = "dev_fst_month_isolation"' in source
    assert "seed_dev_features" not in source


def test_orchestrator_packages_current_child_launcher_and_has_no_azure_control_io():
    source = ORCHESTRATOR.read_text()
    assert BUNDLED_FLOW.read_text() == FLOW.read_text()
    assert "validate_child_launcher_contract()" in source
    assert 'Path(__file__).resolve().with_name(FLOW_SCRIPT)' in source
    assert "--manifest_bucket_count" in source
    assert "DefaultAzureCredential" not in source
    assert "update_control_state" not in source
    assert "cancellation_requested" not in source
    assert "upload_batch_summary" not in source


def test_resource_wrappers_are_explicit_and_standard_has_no_pool_selector():
    source = FLOW.read_text()
    assert 'RESOURCE_MEMORY = "8192" if RESOURCE_PROFILE == "standard" else os.getenv("FST_BACKFILL_ULRPM_MEMORY", "300000")' in source
    assert 'FST_BACKFILL_ULRPM_MEMORY", "300000"' in source
    assert 'if RESOURCE_PROFILE == "ulrpm" else {}' in source
    assert '"outerbounds.co/compute-pool": "obp-main-big3"' in source
    assert "three 64G-request heavy pods" in source
    assert ">half-node request enforces one heavy pod per node" in source
    for name, profile in (("FSTBackfill_standard_flow.py", "standard"), ("FSTBackfill_ulrpm_flow.py", "ulrpm")):
        assert f'FST_BACKFILL_RESOURCE_PROFILE"] = "{profile}"' in (FLOW.parent / name).read_text()


def test_caught_step_failures_are_reported_and_fail_the_child_run():
    source = FLOW.read_text()
    assert "Skipping feature extraction because fetch_machine_samples failed" in source
    assert "Skipping anomaly features because an upstream step failed" in source
    assert '"failure_details": failure_details' in source
    assert "FST backfill failed in one or more caught steps" in source
    assert 'unsuccessful_machines.append("unknown")' not in source
