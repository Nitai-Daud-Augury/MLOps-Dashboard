SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;
CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS inventory (
 machine_id TEXT PRIMARY KEY, display_name TEXT NOT NULL, is_test_machine INTEGER NOT NULL DEFAULT 0, site_id TEXT, site_name TEXT,
 organization_id TEXT, organization_name TEXT, status TEXT NOT NULL, archived INTEGER NOT NULL,
 tags TEXT NOT NULL, endpoint_count INTEGER NOT NULL, endpoint_hardware_types TEXT NOT NULL,
 resource_cohort TEXT NOT NULL, classification_reason TEXT NOT NULL,
 classification_source_version TEXT NOT NULL, backfill_eligible INTEGER NOT NULL,
 exclusion_reason TEXT, source_updated_at TEXT, last_recorded_at TEXT, installation_at TEXT,
 inventory_version TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS inventory_stage AS SELECT * FROM inventory WHERE 0;
CREATE TABLE IF NOT EXISTS estimates (
 id TEXT PRIMARY KEY, signature TEXT NOT NULL, created_at TEXT NOT NULL, expires_at TEXT NOT NULL,
 inventory_version TEXT NOT NULL, capacity_digest TEXT NOT NULL, request_json TEXT NOT NULL,
 result_json TEXT NOT NULL, production_ready INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS campaigns (
 id TEXT PRIMARY KEY, name TEXT NOT NULL, created_by TEXT NOT NULL, created_at TEXT NOT NULL,
 state TEXT NOT NULL, inventory_version TEXT NOT NULL, selection_snapshot TEXT NOT NULL,
 cohort_counts TEXT NOT NULL, date_start TEXT NOT NULL, date_end TEXT NOT NULL,
 feature_version TEXT NOT NULL, config_digest TEXT NOT NULL, estimate_id TEXT NOT NULL,
 estimate_signature TEXT NOT NULL, production INTEGER NOT NULL DEFAULT 0,
 submitted_at TEXT, paused_at TEXT, completed_at TEXT,
 FOREIGN KEY(estimate_id) REFERENCES estimates(id));
CREATE TABLE IF NOT EXISTS work_items (
 idempotency_key TEXT PRIMARY KEY, campaign_id TEXT NOT NULL, machine_id TEXT NOT NULL,
 cohort TEXT NOT NULL, window_start TEXT NOT NULL, window_end TEXT NOT NULL,
 sequence_number INTEGER NOT NULL, state TEXT NOT NULL, classifier_version TEXT NOT NULL,
 resource_profile_version TEXT NOT NULL, attempt_count INTEGER NOT NULL DEFAULT 0,
 next_attempt_at TEXT, lease_owner TEXT, lease_expires_at TEXT, workflow_name TEXT, run_id TEXT,
 started_at TEXT, finished_at TEXT, error_code TEXT, error_summary TEXT, runtime_metrics TEXT,
 FOREIGN KEY(campaign_id) REFERENCES campaigns(id),
 UNIQUE(campaign_id,machine_id,sequence_number));
CREATE TABLE IF NOT EXISTS attempts (
 id INTEGER PRIMARY KEY AUTOINCREMENT, work_item_id TEXT NOT NULL, attempt_number INTEGER NOT NULL,
 workflow_name TEXT, run_id TEXT, resource_request TEXT NOT NULL, price_snapshot TEXT,
 queued_at TEXT NOT NULL, started_at TEXT, finished_at TEXT, outcome TEXT, peak_memory_mib REAL,
 UNIQUE(work_item_id,attempt_number));
CREATE TABLE IF NOT EXISTS benchmarks (
 id INTEGER PRIMARY KEY AUTOINCREMENT, cohort TEXT NOT NULL, resource_profile_version TEXT NOT NULL,
 feature_version TEXT NOT NULL, window_days INTEGER NOT NULL, endpoint_bucket TEXT NOT NULL,
 duration_hours REAL NOT NULL, retry_count INTEGER NOT NULL, peak_memory_mib REAL,
 recorded_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS price_quotes (
 cache_key TEXT PRIMARY KEY, payload TEXT NOT NULL, retrieved_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS campaign_events (
 id INTEGER PRIMARY KEY AUTOINCREMENT, campaign_id TEXT NOT NULL, kind TEXT NOT NULL,
 payload TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS machine_controls (
 campaign_id TEXT NOT NULL, machine_id TEXT NOT NULL, state TEXT NOT NULL,
 reason TEXT NOT NULL, updated_by TEXT NOT NULL, updated_at TEXT NOT NULL,
 PRIMARY KEY(campaign_id,machine_id), FOREIGN KEY(campaign_id) REFERENCES campaigns(id));
CREATE INDEX IF NOT EXISTS idx_inventory_filter ON inventory(resource_cohort,status,backfill_eligible,machine_id);
CREATE INDEX IF NOT EXISTS idx_work_campaign_state ON work_items(campaign_id,state);
CREATE INDEX IF NOT EXISTS idx_work_ready ON work_items(cohort,state,next_attempt_at);
CREATE INDEX IF NOT EXISTS idx_work_machine_state ON work_items(machine_id,state);
CREATE INDEX IF NOT EXISTS idx_events_campaign ON campaign_events(campaign_id,id);
CREATE INDEX IF NOT EXISTS idx_machine_controls_state ON machine_controls(campaign_id,state);
"""
