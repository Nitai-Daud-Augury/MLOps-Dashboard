export function formatNumber(value: number) {
  return new Intl.NumberFormat('en-IL').format(value);
}

export function formatElapsedTime(seconds: number) {
  const totalSeconds = Math.max(0, Math.floor(seconds));
  const minutes = Math.floor(totalSeconds / 60);
  const remainingSeconds = totalSeconds % 60;
  return minutes === 0
    ? `${remainingSeconds}s`
    : `${minutes}m ${String(remainingSeconds).padStart(2, '0')}s`;
}
