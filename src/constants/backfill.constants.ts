import type { BackfillStatus, MachineStatus } from '../types';

export const ORCHESTRATOR_FIRST_MONTH = { year: 2024, month: 8 };
export const STATUS_OPTIONS: Array<'all' | BackfillStatus> = ['all', 'needs_backfill', 'backfilled', 'scan_error', 'unknown', 'no_source_data'];
export const EMPTY_MACHINES: MachineStatus[] = [];
export const RECOMMENDED_MACHINE_CONCURRENCY = 5;
