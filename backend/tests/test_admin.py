from datetime import datetime
import json
from types import SimpleNamespace

import pytest

from backfill_dashboard.admin import (
    AdminAction,
    AdminActionRepository,
    DEFAULT_FLOW_PATH,
    OUTERBOUNDS_RUNS_URL,
    PROD_CONFIRMATION,
    PROD_NAMESPACE,
    STOP_PROD_CONFIRMATION,
    TerminateParams,
    TriggerParams,
    WorkflowCommandBuilder,
    WorkflowReviewRepository,
    WorkflowSourceRequest,
    ULRPM_ORCHESTRATOR_CONTRACT_VERSION,
    admin_spec,
    parse_running_workflows,
)
from backfill_dashboard.control_store import JsonControlStore
from backfill_dashboard.manifests import OrchestratedManifestWriteResult
from backfill_dashboard.schemas import OrchestratedBackfillRequest, OrchestratedManifestRequest
from sibling_repos import requires_metaflow_flow


def test_workflow_review_repository_persists_acknowledgements(tmp_path):
    state_path = tmp_path / "workflow_reviews.json"
    reviews = WorkflowReviewRepository(state_path)

    assert reviews.mark_reviewed("fstbackfill-abc123") == ["fstbackfill-abc123"]
    assert WorkflowReviewRepository(state_path).reviewed_ids() == ["fstbackfill-abc123"]
    assert reviews.clear_reviewed("fstbackfill-abc123") == []


def test_stale_queued_trigger_is_failed_but_running_trigger_is_not(tmp_path):
    repository = AdminActionRepository(tmp_path / "actions.json")
    stale = repository.create(
        action="trigger",
        command=["argo", "submit"],
        cwd=tmp_path,
        machine_ids=["683ec59079fecb5a5a240478"],
    )
    running = repository.create(
        action="trigger",
        command=["argo", "submit"],
        cwd=tmp_path,
        machine_ids=["6817571193e37ef05fffcd1c"],
    )
    repository._actions[stale.id].created_at = "2026-09-16T05:00:00+00:00"
    repository._actions[running.id].created_at = "2026-09-16T05:00:00+00:00"
    repository._actions[running.id].status = "running"

    expired = repository.fail_stale_queued_triggers(
        stale_after_seconds=1800,
        now=datetime.fromisoformat("2026-09-16T06:00:00+00:00"),
    )

    assert [action["id"] for action in expired] == [stale.id]
    assert repository.get(stale.id)["status"] == "failed"
    assert "runner started" in repository.get(stale.id)["error"]
    assert repository.get(stale.id)["reservation_cleanup_pending"] is True
    assert repository.get(running.id)["status"] == "running"

    assert [action["id"] for action in repository.pending_reservation_cleanups()] == [stale.id]
    repository.mark_reservation_cleanup_complete(stale.id)
    assert repository.pending_reservation_cleanups() == []


@pytest.mark.parametrize("phase", ["validating", "preparing_manifests"])
def test_stale_queued_preparation_phases_are_failed(tmp_path, phase):
    repository = AdminActionRepository(tmp_path / "actions.json")
    action = repository.create(
        action="trigger",
        command=[],
        cwd=tmp_path,
        phase=phase,
        machine_ids=["683ec59079fecb5a5a240478"],
    )
    repository._actions[action.id].created_at = "2026-09-16T05:00:00+00:00"

    expired = repository.fail_stale_queued_triggers(
        stale_after_seconds=1800,
        now=datetime.fromisoformat("2026-09-16T06:00:00+00:00"),
    )

    assert [item["id"] for item in expired] == [action.id]
    current = repository.get(action.id)
    assert current["status"] == "failed"
    assert current["phase"] == "failed"
    assert current["reservation_cleanup_pending"] is True


def test_stale_submitting_action_releases_only_its_owned_reservations(tmp_path, monkeypatch):
    import backfill_dashboard.app as app_module

    actions = AdminActionRepository(tmp_path / "actions.json")
    control = JsonControlStore(tmp_path / "control.json")
    stale = actions.create(
        action="trigger",
        command=[],
        cwd=tmp_path,
        phase="submitting",
        machine_ids=["683ec59079fecb5a5a240478"],
    )
    running = actions.create(
        action="trigger",
        command=[],
        cwd=tmp_path,
        phase="submitting",
        machine_ids=["6817571193e37ef05fffcd1c"],
    )
    actions._actions[stale.id].created_at = "2026-09-16T05:00:00+00:00"
    actions._actions[running.id].created_at = "2026-09-16T05:00:00+00:00"
    actions._actions[running.id].status = "running"
    stale_machine = stale.machine_ids[0]
    running_machine = running.machine_ids[0]
    control.reserve_month(stale_machine, 17, f"action:{stale.id}", "dashboard")
    control.reserve_month(running_machine, 17, f"action:{running.id}", "dashboard")
    monkeypatch.setattr(app_module, "admin_repository", actions)
    monkeypatch.setattr(app_module, "control_store", control)

    expired = app_module._release_stale_submission_reservations()

    assert [item["id"] for item in expired] == [stale.id]
    stale_state = control.get_month_state(stale_machine, 17)
    assert stale_state["state"] == "failed"
    assert stale_state["parent_workflow_id"] == f"action:{stale.id}"
    assert control.get_month_state(running_machine, 17)["state"] == "queued"
    assert actions.get(stale.id)["reservation_cleanup_pending"] is False
    assert actions.get(running.id)["status"] == "running"


def test_admin_action_repository_allows_only_one_preparing_trigger(tmp_path):
    repository = AdminActionRepository(tmp_path / "actions.json")
    pending = repository.create(
        action="trigger", command=[], cwd=tmp_path, phase="queued",
        exclusive_trigger_submission=True,
    )
    with pytest.raises(ValueError, match="already queued"):
        repository.create(
            action="trigger", command=[], cwd=tmp_path, phase="queued",
            exclusive_trigger_submission=True,
        )

    repository.set_trigger_phase(pending.id, "accepted")
    next_action = repository.create(
        action="trigger", command=[], cwd=tmp_path, phase="queued",
        exclusive_trigger_submission=True,
    )
    assert next_action.id != pending.id


@requires_metaflow_flow
def test_create_command_uses_metaflow_argo_create():
    builder = WorkflowCommandBuilder()

    cwd, command = builder.build_create(
        WorkflowSourceRequest(source_type="local", local_flow_path=str(DEFAULT_FLOW_PATH))
    )

    assert cwd == DEFAULT_FLOW_PATH.parent
    assert command[-3:] == ["--no-pylint", "argo-workflows", "create"]


def test_prod_trigger_requires_confirmation():
    builder = WorkflowCommandBuilder()

    with pytest.raises(ValueError, match="Production trigger requires confirmation"):
        builder.build_trigger(
            WorkflowSourceRequest(source_type="local", local_flow_path=str(DEFAULT_FLOW_PATH)),
            TriggerParams(
                environment="prod",
                namespace=PROD_NAMESPACE,
                storage_account_manifest_path="manifests/m.parquet",
            ),
        )


def test_prod_trigger_command_contains_backfill_params():
    builder = WorkflowCommandBuilder()

    _, command = builder.build_trigger(
        WorkflowSourceRequest(source_type="local", local_flow_path=str(DEFAULT_FLOW_PATH)),
        TriggerParams(
            environment="prod",
            namespace=PROD_NAMESPACE,
            storage_account_manifest_path="manifests/m.parquet",
            include_features_to_backfill=True,
            features_to_backfill=["f1", "f2"],
            max_parallel_steps=1,
            confirm_production=True,
            confirmation_text=PROD_CONFIRMATION,
        ),
    )

    assert command[:6] == [
        "argo",
        "submit",
        "-n",
        "jobs-default",
        "--from",
            "workflowtemplate/fstbackfill.test.devcoreme.fstbackfill-zvsui",
    ]
    assert f"name-space=\"{PROD_NAMESPACE}\"" in command
    assert 'storage_account_manifest_path="manifests/m.parquet"' in command
    assert 'features_to_backfill="f1,f2"' in command
    assert "manifest_bucket_count=1" in command
    assert not any(value.startswith("max_parallel_steps=") for value in command)


def test_feature_param_is_omitted_until_enabled():
    builder = WorkflowCommandBuilder()

    _, command = builder.build_trigger(
        WorkflowSourceRequest(source_type="local", local_flow_path=str(DEFAULT_FLOW_PATH)),
        TriggerParams(
            environment="dev",
            namespace="ulrpm-fst-dev-20260830",
            storage_account_manifest_path="manifests/m.parquet",
            include_features_to_backfill=False,
            features_to_backfill=["f1"],
        ),
    )

    assert 'features_to_backfill=""' in command


def test_ulrpm_orchestrator_trigger_uses_monthly_parent_template():
    builder = WorkflowCommandBuilder()

    _, command = builder.build_ulrpm_orchestrator_trigger(
        machine_ids=["6964a85076c68cf3b5eada68"],
        manifest_template="2026/09/02/dashboard/orchestrator/monthly_{machine_short}_{machine_id}/month_{month_index:02d}_{year}_{month:02d}.parquet",
        start_index=5,
        end_index=7,
        params=TriggerParams(environment="dev", namespace="ulrpm-fst-dev-20260830", max_parallel_steps=1),
    )

    assert command[:6] == [
        "argo",
        "submit",
        "-n",
        "jobs-default",
        "--from",
        "workflowtemplate/fstbkfill.test.devfstm.ulrpmdeatorflow-hwqbr",
    ]
    assert 'machine_ids="6964a85076c68cf3b5eada68"' in command
    assert "start_index=5" in command
    assert "end_index=7" in command
    assert "child_max_parallel_steps=1" in command
    assert not any("control_store=" in value for value in command)
    assert not any("control_prefix=" in value for value in command)
    assert not any("cancellation_poll_seconds=" in value for value in command)
    assert f'orchestrator_contract_version="{ULRPM_ORCHESTRATOR_CONTRACT_VERSION}"' in command


def test_ulrpm_orchestrator_rejects_concurrency_above_selected_machine_count():
    with pytest.raises(ValueError, match=r"cannot exceed the selected machine count \(1\)"):
        WorkflowCommandBuilder().build_ulrpm_orchestrator_trigger(
            machine_ids=["6964a85076c68cf3b5eada68"],
            manifest_template="m.parquet",
            start_index=1,
            end_index=2,
            params=TriggerParams(
                environment="dev", namespace="ulrpm-fst-dev-20260830", max_parallel_steps=2
            ),
        )


def test_ulrpm_orchestrator_accepts_concurrency_equal_to_selected_machine_count():
    _, command = WorkflowCommandBuilder().build_ulrpm_orchestrator_trigger(
        machine_ids=["machine-a", "machine-b"],
        manifest_template="m.parquet",
        start_index=1,
        end_index=2,
        params=TriggerParams(
            environment="dev", namespace="ulrpm-fst-dev-20260830", max_parallel_steps=2
        ),
    )
    assert "global_machine_concurrency=2" in command


def test_ulrpm_orchestrator_readiness_accepts_current_contract(monkeypatch):
    builder = WorkflowCommandBuilder()
    monkeypatch.setattr(
        builder,
        "platform_readiness",
        lambda: {"ready": True},
    )
    template = {
        "spec": {
            "arguments": {
                "parameters": [
                    {
                        "name": "orchestrator_contract_version",
                        "value": ULRPM_ORCHESTRATOR_CONTRACT_VERSION,
                    }
                ]
            }
        }
    }
    monkeypatch.setattr(
        "backfill_dashboard.admin.subprocess.run",
        lambda *args, **kwargs: SimpleNamespace(stdout=json.dumps(template)),
    )

    readiness = builder.ulrpm_orchestrator_readiness()

    assert readiness["ready"] is True
    assert readiness["contract_version"] == ULRPM_ORCHESTRATOR_CONTRACT_VERSION


def test_ulrpm_orchestrator_readiness_rejects_stale_template(monkeypatch):
    builder = WorkflowCommandBuilder()
    monkeypatch.setattr(
        builder,
        "platform_readiness",
        lambda: {"ready": True},
    )
    template = {"spec": {"arguments": {"parameters": []}}}
    monkeypatch.setattr(
        "backfill_dashboard.admin.subprocess.run",
        lambda *args, **kwargs: SimpleNamespace(stdout=json.dumps(template)),
    )

    readiness = builder.ulrpm_orchestrator_readiness()

    assert readiness["ready"] is False
    assert readiness["expected_contract"] == ULRPM_ORCHESTRATOR_CONTRACT_VERSION
    assert readiness["deployed_contract"] is None
    assert "stale" in readiness["detail"]


def test_ulrpm_orchestrator_production_uses_same_template_and_prod_namespace():
    _, command = WorkflowCommandBuilder().build_ulrpm_orchestrator_trigger(
        machine_ids=["6964a85076c68cf3b5eada68"],
        manifest_template="m.parquet",
        start_index=5,
        end_index=6,
        params=TriggerParams(
            environment="prod",
            namespace="ignored-dev-namespace",
            confirm_production=True,
            confirmation_text="RUN_PROD_BACKFILL",
        ),
    )

    assert "workflowtemplate/fstbkfill.test.devfstm.ulrpmdeatorflow-hwqbr" in command
    assert f'name_space="{PROD_NAMESPACE}"' in command
    assert not any("seed" in value.lower() for value in command)
    assert not any("features_to_backfill" in value for value in command)


def test_ulrpm_orchestrator_production_requires_confirmation():
    with pytest.raises(ValueError, match="Production trigger requires confirmation"):
        WorkflowCommandBuilder().build_ulrpm_orchestrator_trigger(
            machine_ids=["6964a85076c68cf3b5eada68"],
            manifest_template="m.parquet",
            start_index=5,
            end_index=6,
            params=TriggerParams(environment="prod", namespace="ignored-dev-namespace"),
        )


def test_prod_terminate_requires_confirmation():
    builder = WorkflowCommandBuilder()

    with pytest.raises(ValueError, match="Production stop requires confirmation"):
        builder.build_terminate(
            TerminateParams(environment="prod", namespace=PROD_NAMESPACE)
        )


def test_terminate_command_uses_existing_argo_helper():
    builder = WorkflowCommandBuilder()

    cwd, command = builder.build_terminate(
        TerminateParams(environment="dev", namespace="ulrpm-fst-dev-20260830")
    )

    assert cwd == DEFAULT_FLOW_PATH.parent
    assert command[:2] == [command[0], "-c"]
    assert "terminate_workflow('ulrpm-fst-dev-20260830')" in command[-1]


def test_terminate_command_can_target_specific_workflow():
    builder = WorkflowCommandBuilder()

    _, command = builder.build_terminate(
        TerminateParams(
            environment="dev",
            namespace="ulrpm-fst-dev-20260830",
            workflow_id="fstbackfill-abc123",
        )
    )

    assert command == [
        "argo",
        "terminate",
        "-n",
        "jobs-default",
        "--field-selector",
        "metadata.name=fstbackfill-abc123",
    ]


def test_prod_terminate_command_after_confirmation():
    builder = WorkflowCommandBuilder()

    _, command = builder.build_terminate(
        TerminateParams(
            environment="prod",
            namespace=PROD_NAMESPACE,
            confirm_production=True,
            confirmation_text=STOP_PROD_CONFIRMATION,
        )
    )

    assert "terminate_workflow('feature-store-container')" in command[-1]


def test_admin_spec_uses_fstbackfill_flow_id():
    assert "flow_id=FSTBackfill" in OUTERBOUNDS_RUNS_URL
    assert admin_spec()["outerbounds_running_url"].endswith("flow_id=FSTBackfill&status=running")


def test_orchestrated_manifest_endpoint_uploads_without_triggering(monkeypatch):
    import backfill_dashboard.app as app_module

    class Writer:
        def write_orchestrated_monthly_manifests(self, **kwargs):
            assert kwargs["machine_ids"] == ["683ec59079fecb5a5a240478"]
            assert kwargs["month_indices_by_machine"] == {"683ec59079fecb5a5a240478": [17, 18]}
            return OrchestratedManifestWriteResult(
                account_name="account",
                container_name="container",
                manifest_prefix="manifests/run-1",
                manifest_template="manifests/run-1/month_{month_index:02d}.parquet",
                machine_ids=kwargs["machine_ids"],
                start_index=17,
                end_index=19,
                month_count=2,
                manifest_count=2,
                blob_url="https://example.invalid/manifests/run-1",
                month_indices_by_machine=kwargs["month_indices_by_machine"],
            )

    class Repository:
        def latest(self):
            return {"snapshot": {"machines": [{
                "machine_id": "683ec59079fecb5a5a240478",
                "months": [
                    {"status": "backfilled", "activity_status": "online", "row_count": 50,
                     "partition": {"year": 2026, "month": 1}},
                    {"status": "needs_backfill", "activity_status": "online", "row_count": 25,
                     "partition": {"year": 2026, "month": 2}},
                ],
            }]}}

        def record_completed(self, **kwargs):
            assert kwargs["action"] == "manifest"
            return AdminAction(
                id="manifest-action",
                action="manifest",
                status="succeeded",
                command=kwargs["command"],
                cwd=str(kwargs["cwd"]),
                created_at="2026-09-14T00:00:00Z",
                machine_ids=kwargs["machine_ids"],
            )

    monkeypatch.setattr(app_module, "manifest_writer", Writer())
    monkeypatch.setattr(app_module, "admin_repository", Repository())
    monkeypatch.setattr(app_module, "repository", Repository())

    result = app_module.create_orchestrated_manifests(OrchestratedManifestRequest(
        machine_ids=["683ec59079fecb5a5a240478"],
        month_indices_by_machine={"683ec59079fecb5a5a240478": [17, 18]},
    ))

    assert result["manifest"]["manifest_prefix"] == "manifests/run-1"
    assert result["manifest"]["rows"] == 2
    assert result["machine_count"] == 1
    assert result["action"]["status"] == "succeeded"


def _prepare_queued_orchestrated_test(monkeypatch, tmp_path, *, writer_error=None, submit_code=0):
    import backfill_dashboard.admin as admin_module
    import backfill_dashboard.app as app_module
    from fastapi import BackgroundTasks

    machine_id = "683ec59079fecb5a5a240478"
    month_index = 17  # January 2026, relative to the August 2024 origin.

    class SnapshotRepository:
        def latest(self):
            return {"snapshot": {"machines": [{
                "machine_id": machine_id,
                "months": [{
                    "status": "needs_backfill", "activity_status": "online", "row_count": 12,
                    "partition": {"year": 2026, "month": 1},
                }],
            }]}}

    class Writer:
        def write_orchestrated_monthly_manifests(self, **kwargs):
            if writer_error:
                raise RuntimeError(writer_error)
            return OrchestratedManifestWriteResult(
                account_name="test", container_name="test", manifest_prefix="test/run",
                manifest_template="test/run/month_{month_index}.parquet",
                machine_ids=kwargs["machine_ids"], start_index=month_index,
                end_index=month_index + 1, month_count=1, manifest_count=1,
                blob_url="https://example.invalid/test/run",
                month_indices_by_machine=kwargs["month_indices_by_machine"],
            )

    class Builder:
        def build_ulrpm_orchestrator_trigger(self, **kwargs):
            return tmp_path, ["argo", "submit", "-p", "test=true"]

    actions = AdminActionRepository(tmp_path / "actions.json")
    control = JsonControlStore(tmp_path / "control.json")
    monkeypatch.setattr(app_module, "admin_repository", actions)
    monkeypatch.setattr(app_module, "repository", SnapshotRepository())
    monkeypatch.setattr(app_module, "manifest_writer", Writer())
    monkeypatch.setattr(app_module, "command_builder", Builder())
    monkeypatch.setattr(app_module, "control_store", control)
    monkeypatch.setattr(app_module, "_require_ulrpm_orchestrator_access", lambda: None)
    monkeypatch.setattr(
        admin_module.subprocess, "run",
        lambda *args, **kwargs: SimpleNamespace(
            returncode=submit_code,
            stdout="Name: test-backfill-123\n" if submit_code == 0 else "",
            stderr="submit failed" if submit_code else "",
        ),
    )
    background = BackgroundTasks()
    response = app_module.trigger_orchestrated_machine_backfill(
        OrchestratedBackfillRequest(
            machine_ids=[machine_id],
            month_indices_by_machine={machine_id: [month_index]},
        ),
        background,
    )
    return app_module, response, background, actions, control, machine_id, month_index


def test_orchestrated_backfill_returns_queued_action_before_slow_preparation(monkeypatch, tmp_path):
    import backfill_dashboard.app as app_module
    from fastapi import BackgroundTasks

    machine_id = "683ec59079fecb5a5a240478"
    actions = AdminActionRepository(tmp_path / "actions.json")
    monkeypatch.setattr(app_module, "admin_repository", actions)
    monkeypatch.setattr(app_module, "_require_ulrpm_orchestrator_access", lambda: pytest.fail("readiness must run in background"))
    monkeypatch.setattr(app_module, "manifest_writer", SimpleNamespace(write_orchestrated_monthly_manifests=lambda **_: pytest.fail("manifest upload must run in background")))
    background = BackgroundTasks()

    result = app_module.trigger_orchestrated_machine_backfill(
        OrchestratedBackfillRequest(
            machine_ids=[machine_id],
            month_indices_by_machine={machine_id: [17]},
        ),
        background,
    )

    assert result["accepted"] is True
    assert result["action"]["phase"] == "queued"
    assert result["action"]["command"] == []
    assert len(background.tasks) == 1


def test_orchestrated_backfill_background_submission_succeeds_and_links_reservation(monkeypatch, tmp_path):
    app_module, result, background, actions, control, machine_id, month_index = _prepare_queued_orchestrated_test(monkeypatch, tmp_path)
    assert result["action"]["phase"] == "queued"

    task = background.tasks[0]
    task.func(*task.args, **task.kwargs)

    action = actions.get(result["action"]["id"])
    assert action["phase"] == "accepted", action.get("error")
    assert action["status"] == "running"
    assert action["workflow_id"] == "test-backfill-123"
    assert control.get_month_state(machine_id, month_index)["parent_workflow_id"] == "test-backfill-123"


def test_orchestrated_backfill_background_failure_cleans_reserved_month(monkeypatch, tmp_path):
    _, result, background, actions, control, machine_id, month_index = _prepare_queued_orchestrated_test(
        monkeypatch, tmp_path, submit_code=1,
    )

    task = background.tasks[0]
    task.func(*task.args, **task.kwargs)

    action = actions.get(result["action"]["id"])
    state = control.get_month_state(machine_id, month_index)
    assert action["phase"] == "failed"
    assert action["status"] == "failed"
    assert action["reservation_cleanup_pending"] is False
    assert state and state["state"] == "failed", action


def test_orchestrated_manifest_rejects_offline_month_before_write(monkeypatch):
    import backfill_dashboard.app as app_module
    from fastapi import HTTPException

    machine_id = "683ec59079fecb5a5a240478"
    writes = []

    class SnapshotRepository:
        def latest(self):
            return {"snapshot": {"machines": [{
                "machine_id": machine_id,
                "months": [{"status": "needs_backfill", "activity_status": "offline", "row_count": 0,
                            "partition": {"year": 2026, "month": 1}}],
            }]}}

    class Writer:
        def write_orchestrated_monthly_manifests(self, **kwargs):
            writes.append(kwargs)
            raise AssertionError("offline selection must be rejected before writing")

    monkeypatch.setattr(app_module, "repository", SnapshotRepository())
    monkeypatch.setattr(app_module, "manifest_writer", Writer())

    with pytest.raises(HTTPException) as error:
        app_module.create_orchestrated_manifests(OrchestratedManifestRequest(
            machine_ids=[machine_id], month_indices_by_machine={machine_id: [17]},
        ))

    assert error.value.status_code == 400
    assert machine_id in error.value.detail
    assert "2026-01" in error.value.detail
    assert "offline" in error.value.detail
    assert writes == []


def test_month_plan_excludes_offline_and_unknown_even_with_legacy_gap_status(monkeypatch):
    import backfill_dashboard.app as app_module

    machine_id = "683ec59079fecb5a5a240478"

    class SnapshotRepository:
        def latest(self):
            return {"snapshot": {"machines": [{
                "machine_id": machine_id,
                "months": [
                    {"status": "needs_backfill", "activity_status": "online", "row_count": 3,
                     "partition": {"year": 2026, "month": 1}},
                    {"status": "needs_backfill", "activity_status": "offline", "row_count": 0,
                     "partition": {"year": 2026, "month": 2}},
                    {"status": "needs_backfill", "activity_status": "unknown", "row_count": 7,
                     "partition": {"year": 2026, "month": 3}},
                    {"status": "needs_backfill", "row_count": 2,
                     "partition": {"year": 2026, "month": 4}},
                ],
            }]}}

    monkeypatch.setattr(app_module, "repository", SnapshotRepository())
    result = app_module.backfill_month_plan(app_module.MonthPlanRequest(machine_ids=[machine_id], mode="gaps"))

    assert result["month_indices_by_machine"] == {machine_id: [17, 20]}


def test_parse_running_workflows_includes_all_workflows():
    workflows = parse_running_workflows(
        {
            "items": [
                {
                    "metadata": {
                        "name": "fstbackfill-prod-123",
                        "namespace": "jobs-default",
                        "creationTimestamp": "2026-08-31T10:00:00Z",
                        "annotations": {"metaflow/flow_name": "FSTBackfill"},
                    },
                    "status": {"phase": "Running", "startedAt": "2026-08-31T10:01:00Z"},
                },
                {
                    "metadata": {"name": "unrelated-flow"},
                    "status": {"phase": "Running"},
                },
            ]
        }
    )

    assert [workflow.workflow_id for workflow in workflows] == ["fstbackfill-prod-123", "unrelated-flow"]
    assert workflows[0].flow_name == "FSTBackfill"
    assert workflows[1].flow_name == "unrelated-flow"


def test_parse_running_workflows_accepts_single_argo_get_object():
    workflows = parse_running_workflows(
        {
            "metadata": {
                "name": "fstbackfill-parent-dcqmk",
                "namespace": "jobs-default",
                "annotations": {
                    "metaflow/flow_name": "UlrpmDevBackfillOrchestratorFlow",
                    "metaflow/run_id": "argo-fstbackfill-parent-dcqmk",
                },
            },
            "status": {
                "phase": "Succeeded",
                "startedAt": "2026-09-09T15:37:24Z",
                "finishedAt": "2026-09-10T01:19:34Z",
            },
        }
    )

    assert len(workflows) == 1
    assert workflows[0].workflow_id == "fstbackfill-parent-dcqmk"
    assert workflows[0].flow_name == "UlrpmDevBackfillOrchestratorFlow"
    assert workflows[0].status == "Succeeded"
    assert workflows[0].finished_at == "2026-09-10T01:19:34Z"
