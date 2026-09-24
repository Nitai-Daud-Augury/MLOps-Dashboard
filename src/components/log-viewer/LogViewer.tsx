import { useEffect, useRef, useState } from 'react';
import { ChevronDown, ChevronUp, Search } from 'lucide-react';

import { formatNumber } from '../../utils/format';

function logLineClass(line: string) {
  if (/\b(error|exception|traceback|fatal|failed)\b/i.test(line)) return 'log-line error';
  if (/\b(warn|retry|warning)\b/i.test(line)) return 'log-line warning';
  return 'log-line';
}

export function LogViewer({ stdout, stderr }: { stdout: string; stderr: string }) {
  const [query, setQuery] = useState('');
  const [stream, setStream] = useState<'combined' | 'stdout' | 'stderr'>('combined');
  const [levelFilter, setLevelFilter] = useState<'all' | 'error' | 'warning' | 'info'>('all');
  const [expanded, setExpanded] = useState(false);
  const preRef = useRef<HTMLPreElement>(null);
  const source = stream === 'stdout' ? stdout : stream === 'stderr' ? stderr : [stdout, stderr].filter(Boolean).join('\n');
  const needle = query.trim().toLowerCase();
  const lines = source.split('\n').map((line, index) => ({ line, index })).filter(({ line }) => {
    const matchesSearch = !needle || line.toLowerCase().includes(needle);
    const matchesLevel = levelFilter === 'all'
      || (levelFilter === 'error' && /\b(error|exception|traceback|fatal|failed)\b/i.test(line))
      || (levelFilter === 'warning' && /\b(warn|retry|warning)\b/i.test(line))
      || (levelFilter === 'info' && !/\b(error|exception|traceback|fatal|failed|warn|retry|warning)\b/i.test(line));
    return matchesSearch && matchesLevel;
  });
  const visibleLines = lines.slice(-2000);
  useEffect(() => { if (preRef.current) preRef.current.scrollTop = preRef.current.scrollHeight; }, [visibleLines.length]);
  return <section className="log-viewer"><div className="log-toolbar"><strong>Command output</strong>
    <label className="search-field log-search"><Search size={14} /><input value={query} onChange={(event) => setQuery(event.target.value)} placeholder="Find in output" /></label>
    <select value={stream} onChange={(event) => setStream(event.target.value as typeof stream)} aria-label="Log stream"><option value="combined">Combined</option><option value="stdout">Stdout</option><option value="stderr">Stderr</option></select>
    <select value={levelFilter} onChange={(event) => setLevelFilter(event.target.value as typeof levelFilter)} aria-label="Log level"><option value="all">All Levels</option><option value="error">Errors Only</option><option value="warning">Warnings Only</option><option value="info">Info Only</option></select>
    <button className="icon-button log-expand" type="button" onClick={() => setExpanded((value) => !value)} title={expanded ? 'Collapse logs' : 'Expand logs'}>{expanded ? <ChevronDown size={14} /> : <ChevronUp size={14} />}</button>
  </div><p className="log-summary">{formatNumber(lines.length)} matching lines{lines.length > visibleLines.length ? `; showing the latest ${formatNumber(visibleLines.length)}` : ''}</p>
  <pre ref={preRef} className={`log-output${expanded ? ' expanded' : ''}`}>{visibleLines.map(({ line, index }) => <span className={logLineClass(line)} key={`${index}-${line}`}><b>{index + 1}</b>{line || ' '}</span>)}</pre></section>;
}
