export function Progress({ value, max }: { value: number; max: number }) {
  const pct = max === 0 ? 0 : Math.round((value / max) * 100);
  return <div className="progress-cell"><div className="progress-track"><div className="progress-fill" style={{ width: `${pct}%` }} /></div><span>{value}/{max}</span></div>;
}
