import type { BackfillStatus } from '../types';

/** Visual status tone (spec §5): always rendered as icon + label, never color alone. */
export type Tone = 'queued' | 'dispatching' | 'running' | 'success' | 'failed' | 'warn' | 'na';

export const TONE_ICON: Record<Tone, string> = {
  queued: '◌', dispatching: '↗', running: '⧗', success: '✓', failed: '✗', warn: '⚠', na: '—',
};

export const BACKFILL_STATUS: Record<BackfillStatus, { label: string; tone: Tone }> = {
  backfilled: { label: 'Complete', tone: 'success' },
  needs_backfill: { label: 'Needs backfill', tone: 'warn' },
  no_source_data: { label: 'No source data', tone: 'na' },
  not_backfillable: { label: 'Not backfillable', tone: 'na' },
  unknown: { label: 'Unknown', tone: 'na' },
  scan_error: { label: 'Scan error', tone: 'failed' },
};

/** Workflow / admin-action phase → tone + label. */
export function workflowTone(status?: string | null): { tone: Tone; label: string } {
  const value = (status ?? '').toLowerCase();
  if (!value) return { tone: 'na', label: 'Unknown' };
  if (value === 'dispatching' || value === 'submitted' || value === 'submitting') return { tone: 'dispatching', label: value[0].toUpperCase() + value.slice(1) };
  if (/(queued|pending|waiting|cancel_window)/.test(value)) return { tone: 'queued', label: value === 'cancel_window' ? 'Queued' : value[0].toUpperCase() + value.slice(1) };
  if (/(running|stopping|cancel_requested|requeue)/.test(value)) return { tone: 'running', label: value.replaceAll('_', ' ').replace(/^./, (c) => c.toUpperCase()) };
  if (/(succeeded|completed|accepted|complete)/.test(value)) return { tone: 'success', label: value[0].toUpperCase() + value.slice(1) };
  if (/(failed|error)/.test(value)) return { tone: 'failed', label: value[0].toUpperCase() + value.slice(1) };
  if (/(cancelled|canceled|terminated|stopped)/.test(value)) return { tone: 'na', label: value[0].toUpperCase() + value.slice(1) };
  return { tone: 'na', label: status ?? 'Unknown' };
}

export const ACTIVE_MONTH_STATES = ['queued', 'running', 'cancel_requested', 'stopping', 'requeue_pending'];
export const isFinishedWorkflow = (status: string) => ['succeeded', 'completed', 'failed', 'error'].includes(status.toLowerCase());
