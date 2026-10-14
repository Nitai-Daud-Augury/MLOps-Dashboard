import { ChevronDown, Info, TriangleAlert } from 'lucide-react';
import { toScanNote } from './scanNoteText';

/** Compact, collapsible scan notes. Opens by itself only when a note needs action. */
export function ScanNotes({ warnings }: { warnings?: string[] | null }) {
  if (!warnings?.length) return null;
  const notes = warnings.map(toScanNote);
  const actions = notes.filter((n) => n.action).length;
  return <details className={`notes${actions ? ' notes-warn' : ''}`} open={actions > 0}>
    <summary>
      {actions ? <TriangleAlert size={15} aria-hidden="true" /> : <Info size={15} aria-hidden="true" />}
      <strong>Scan notes</strong>
      <span className="muted">{notes.length} · {actions ? `${actions} need${actions === 1 ? 's' : ''} action` : 'no action needed'}</span>
      <ChevronDown size={15} className="notes-chevron" aria-hidden="true" />
    </summary>
    <ul>{notes.map((n) => <li key={n.raw} title={n.raw}>
      {n.action ? <TriangleAlert size={13} className="notes-icon warn" aria-hidden="true" /> : <span className="notes-icon dot" aria-hidden="true" />}
      <span>{n.text}{n.action ? <strong> {n.action}</strong> : null}</span></li>)}</ul>
  </details>;
}
