const TZ = 'Asia/Jerusalem';

export function exactTime(value?: string | number | null): string {
  if (value === null || value === undefined || value === '') return 'unknown';
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return String(value);
  return new Intl.DateTimeFormat('en-GB', { dateStyle: 'medium', timeStyle: 'medium', timeZone: TZ }).format(date);
}

export function relativeTime(value?: string | number | null, now = Date.now()): string {
  if (value === null || value === undefined || value === '') return 'never';
  const time = new Date(value).getTime();
  if (Number.isNaN(time)) return String(value);
  const seconds = Math.round((now - time) / 1000);
  if (Math.abs(seconds) < 5) return 'just now';
  const abs = Math.abs(seconds);
  const [amount, unit] = abs < 60 ? [abs, 's'] : abs < 3600 ? [Math.floor(abs / 60), 'm'] : abs < 86_400 ? [Math.floor(abs / 3600), 'h'] : [Math.floor(abs / 86_400), 'd'];
  return seconds >= 0 ? `${amount}${unit} ago` : `in ${amount}${unit}`;
}

export function duration(from?: string | null, to?: string | null, now = Date.now()): string {
  if (!from) return '—';
  const start = new Date(from).getTime();
  const end = to ? new Date(to).getTime() : now;
  if (Number.isNaN(start) || Number.isNaN(end)) return '—';
  const total = Math.max(0, Math.floor((end - start) / 1000));
  const h = Math.floor(total / 3600);
  const m = Math.floor((total % 3600) / 60);
  const s = total % 60;
  return h ? `${h}:${String(m).padStart(2, '0')}:${String(s).padStart(2, '0')}` : `${m}:${String(s).padStart(2, '0')}`;
}

export function monthLabel(year: number, month: number): string {
  return `${year}-${String(month).padStart(2, '0')}`;
}

export const plural = (count: number, word: string) => `${count} ${word}${count === 1 ? '' : 's'}`;
