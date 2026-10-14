/**
 * Plain-language version of snapshot.warnings (free text from backend scanner.py).
 * Most are informational notes about how the scan ran; `action` marks the ones that need a step.
 * Unknown text is shown as-is.
 */
export interface ScanNote { text: string; action?: string; raw: string }

const RULES: [RegExp, (m: RegExpMatchArray) => Omit<ScanNote, 'raw'>][] = [
  [/^Lifecycle filtering applied \((\d+) installation/, (m) => ({ text: `Install dates found for ${m[1]} machines; months before install are not counted.` })],
  [/^Lifecycle (lookup failed|filtering unavailable)/, () => ({ text: 'Install dates were unavailable, so months before install may count as gaps.', action: 'Rescan later.' })],
  [/^Coverage starts at the first observed Feature Store partition: (\d{4}-\d{2})/, (m) => ({ text: `Coverage is counted from ${m[1]}, the first month in the Feature Store.` })],
  [/^Full-schema completeness is checked/, () => ({ text: 'Each month is checked for the full set of feature columns.' })],
  [/silver consistency is disabled/, () => ({ text: 'The silver-table consistency check is off.' })],
  [/^Feature Store cross-check vs .*?: (\d+) real mismatched month\(s\) across (\d+) machine/, (m) => {
    const stale = m.input?.match(/(\d+) month\(s\) skipped as feature_store_stale/)?.[1];
    return {
      text: `Databricks copy: ${m[1]} month(s) on ${m[2]} machine(s) differ from blob${stale ? `; ${stale} month(s) are outdated there` : ''}. Status is not affected.`,
      action: Number(m[1]) > 0 ? 'See the Databricks copy column.' : undefined,
    };
  }],
  [/^Feature Store cross-check failed/, () => ({ text: 'The Databricks comparison failed and was skipped. Status is not affected.' })],
  [/^Feature coverage read from Databricks/, () => ({ text: 'Coverage was read from Databricks, not blob.' })],
  [/query failed .*scan_error/, () => ({ text: 'The Databricks query failed, so every month is marked Scan error.', action: 'Rescan, or switch the source to Blob.' })],
  [/^Target feature\(s\) not in/, () => ({ text: 'Some required features are missing in Databricks; those months are marked Scan error.', action: 'Switch the source to Blob.' })],
  [/^Schema check unavailable in Databricks mode/, () => ({ text: 'Missing feature columns are not checked when reading from Databricks.' })],
  [/^The Databricks copy may be stale/, () => ({ text: 'Databricks may hold an older copy of recently backfilled months.' })],
  [/^FEATURES_SOURCE=databricks: the blob-vs-feature_store cross-check is disabled/, () => ({ text: 'The Databricks comparison is off while reading from Databricks.' })],
];

export function toScanNote(raw: string): ScanNote {
  for (const [re, make] of RULES) { const m = raw.match(re); if (m) return { ...make(m), raw }; }
  return { text: raw, raw };
}
