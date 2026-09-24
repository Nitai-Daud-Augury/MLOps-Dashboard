from backfill_dashboard.inventory_models import MachineSearchQuery
from backfill_dashboard.mongo_inventory import MongoMachineInventoryProvider
from backfill_dashboard.inventory_provider import _record
from sibling_repos import requires_canonical_classifier


class Cursor(list):
    def sort(self, *_): return self
    def limit(self, value): return Cursor(self[:value])
    def max_time_ms(self, *_): return self


class Collection:
    def __init__(self, rows): self.rows = rows
    def find(self, *_args, **_kwargs): return Cursor(self.rows)


class Database(dict):
    def __getitem__(self, name): return dict.__getitem__(self, name)


class Client(dict):
    def __getitem__(self, name): return dict.__getitem__(self, name)


@requires_canonical_classifier
def test_provider_joins_endpoint_on_nested_machine_id_and_classifies_ulrpm():
    config = {"_id": "m1", "name": "Pump", "status": "active", "components": [],
              "containedIn": {"_id": "site", "name": "Plant", "type": "facility",
                              "company": {"_id": "org", "name": "Organization"}}}
    endpoint = {"machine": {"id": "m1"}, "info": {"type": "low_rpm_us"}}
    db = Database(machine_configurations=Collection([config]), machines=Collection([]), endpoints=Collection([endpoint]))
    provider = MongoMachineInventoryProvider(Client(production=db), "production")
    record = provider.search(MachineSearchQuery(limit=10)).machines[0]
    assert record.resource_cohort == "ulrpm"
    assert record.site_id == "site" and record.organization_id == "org"


@requires_canonical_classifier
def test_missing_tags_do_not_hide_recognized_standard_hardware():
    config = {"_id": "m1", "status": "active", "endpoints": [{"type": "apus_alpha"}]}
    db = Database(machine_configurations=Collection([config]), machines=Collection([]), endpoints=Collection([]))
    provider = MongoMachineInventoryProvider(Client(production=db), "production")
    assert provider.search(MachineSearchQuery(limit=10)).machines[0].resource_cohort == "standard"


def test_record_extracts_nested_last_recorded_timestamp():
    record = _record({"_id": "m1", "lastRecorded": {"timestamp": "2026-06-15T00:00:00Z"}}, "test")
    assert record.last_recorded_at == "2026-06-15T00:00:00Z"


def test_test_machine_is_derived_from_display_name_or_tags():
    named = _record({"_id": "6a747b591f957aebfe6c59cf", "name": "ULRPM E2E test #5"}, "test")
    tagged = _record({"_id": "machine-2", "name": "Pump", "tags": ["test-machine"]}, "test")
    ordinary = _record({"_id": "machine-3", "name": "Pump 3", "tags": ["ulrpm"]}, "test")

    assert named.is_test_machine
    assert tagged.is_test_machine
    assert not ordinary.is_test_machine


def test_provider_preserves_machine_name_and_tags_for_test_classification():
    config = {"_id": "m1", "status": "active", "endpoints": [{"type": "low_rpm_us"}]}
    machine = {"_id": "m1", "name": "ULRPM E2E test #5", "tags": ["ulrpm"]}
    db = Database(machine_configurations=Collection([config]), machines=Collection([machine]), endpoints=Collection([]))

    record = MongoMachineInventoryProvider(Client(production=db), "production").get_many(["m1"])[0]

    assert record.display_name == "ULRPM E2E test #5"
    assert record.is_test_machine


@requires_canonical_classifier
def test_provider_uses_ulrpm_endpoint_installation_date():
    config = {
        "_id": "m1",
        "name": "Pump",
        "status": "active",
        "tags": ["ulrpm"],
        # Configuration-embedded endpoints do not carry the authoritative
        # installation date and must not shadow the joined endpoint documents.
        "endpoints": [{"type": "low_rpm_us"}],
    }
    endpoints = [
        {
            "machine": {"id": "m1"},
            "info": {"type": "apus_alpha"},
            "installation_date": "2025-01-01T00:00:00Z",
        },
        {
            "machine": {"id": "m1"},
            "info": {"type": "low_rpm_us"},
            "installation_date": "2026-08-14T12:00:00Z",
        },
    ]
    db = Database(
        machine_configurations=Collection([config]),
        machines=Collection([]),
        endpoints=Collection(endpoints),
    )

    record = MongoMachineInventoryProvider(
        Client(production=db), "production"
    ).get_many(["m1"])[0]

    assert record.installation_at == "2026-08-14T12:00:00Z"
