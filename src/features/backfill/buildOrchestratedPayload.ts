import type { SplitSpec } from './types';

export function buildOrchestratedPayload({
  machineIds,
  range,
  monthIndicesByMachine,
  split,
  manifestPrefix,
  params,
}: {
  machineIds: string[];
  range?: { since: string; until: string };
  monthIndicesByMachine?: Record<string, number[]>;
  split: SplitSpec;
  manifestPrefix?: string;
  params?: Record<string, unknown>;
}): Record<string, unknown> {
  return {
    machine_ids: machineIds,
    since: range?.since ?? '',
    until: range?.until ?? '',
    manifest_prefix: manifestPrefix ?? '',
    month_indices_by_machine: monthIndicesByMachine ?? {},
    split: { mode: split.mode, days: split.mode === 'days' ? split.days : null },
    ...(params ? { params } : {}),
  };
}
