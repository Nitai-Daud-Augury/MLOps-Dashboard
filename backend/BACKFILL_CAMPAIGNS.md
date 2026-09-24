# Backfill campaign operations

## Production prerequisites

1. Verify Mongo collection shapes, lifecycle counts, query plans, and indexes.
2. Configure a persistent control-plane database path, stable estimate signing
   key, and token or trusted-proxy operator authorization.
3. Record the standard pool region/SKU/node capacity and the ULRPM pool facts;
   set the matching `*_CAPACITY_VERIFIED=1` flags only after review.
4. Configure both explicit workflow templates created from
   `FSTBackfill_standard_flow.py` and `FSTBackfill_ulrpm_flow.py`.
5. Configure or refresh Azure price quotes and record at least five standard
   and three ULRPM benchmark windows. Seeded priors remain low confidence.
6. Keep dispatch disabled while validating API/UI behavior, then enable it for
   the approved canary only.

## Pause, cancel, and recovery

- Pause stops new leases and lets submitted/running windows finish.
- Resume returns a paused campaign to the queue.
- Cancel stops pending work first, then the dispatcher terminates active Argo
  workflows and records each result.
- A process restart reopens the WAL database. Expired leases without a workflow
  return to `ready`; ambiguous submits use the deterministic workflow name to
  recover before retrying.
- A failed window keeps later windows for that machine blocked. Investigate the
  dependency/resource error before retrying or authorizing a skip.
- Campaign and machine pause/resume/cancel actions require an audited reason;
  a machine pause blocks only its future leases. Repeated failures automatically
  pause the campaign according to `BACKFILL_AUTO_PAUSE_FAILURES`.
- Record successful canary observations through authenticated
  `POST /api/v1/runtime-benchmarks`; estimates remain low-confidence until the
  minimum fresh sample count is present.

## Canary gates

Standard machines: 1, 10, 100, 1,000, 5,000, 30,000. Evaluate concurrency at
5, 10, 20, and 40, never above the live recommendation. ULRPM is approved
separately at 1, 3, then 5 concurrent machines. Pause on repeated OOM, FST
lease/write failure, throttling, unschedulable pods, excessive eviction, or
material estimate error. Never infer ULRPM approval from a standard gate.
