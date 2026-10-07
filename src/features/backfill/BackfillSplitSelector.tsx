import type { SplitSpec, SplitMode } from './types';
import { previewSummary } from './windows';

const MODES: Array<{ mode: SplitMode; label: string }> = [
  { mode: 'month', label: 'Full month' },
  { mode: 'day', label: 'Per day' },
  { mode: 'week', label: 'Per week' },
  { mode: 'days', label: 'Every N days' },
];

export function BackfillSplitSelector({
  value,
  onChange,
  sinceIso = '',
  untilIso = '',
}: {
  value: SplitSpec;
  onChange: (next: SplitSpec) => void;
  sinceIso?: string;
  untilIso?: string;
}) {
  const summary = sinceIso && untilIso ? previewSummary(sinceIso, untilIso, value) : previewSummary('', '', value);
  return (
    <section className="wide backfill-split-selector" aria-label="Manifest split">
      <div className="backfill-split-modes" role="group" aria-label="Split mode">
        {MODES.map(({ mode, label }) => (
          <button
            key={mode}
            type="button"
            className={value.mode === mode ? 'active' : ''}
            onClick={() => onChange({ mode, days: mode === 'days' ? (value.days ?? 3) : undefined })}
          >
            {label}
          </button>
        ))}
        {value.mode === 'days' ? (
          <label className="backfill-split-days">
            N
            <input
              type="number"
              min={1}
              max={31}
              value={value.days ?? 3}
              onChange={(event) => onChange({ mode: 'days', days: Math.max(1, Math.min(31, Number(event.target.value) || 1)) })}
            />
          </label>
        ) : null}
      </div>
      <p className="text-muted">{summary}</p>
      {value.mode !== 'month' ? (
        <p className="text-muted" role="note">
          Live triggers with per-day/week/N-day split need the orchestrator contract v5 update. Manifest dry-runs still work locally.
        </p>
      ) : null}
    </section>
  );
}
