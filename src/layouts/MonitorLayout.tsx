import { FeaturesSourceBanner } from '../components/features-source/FeaturesSourceBanner';
import { FeaturesSourceControl } from '../components/features-source/FeaturesSourceControl';
import { Banner } from '../components/ui';
import { useStatus } from '../state/statusStore';
import { AppShell } from './AppShell';

/** Monitoring: read-only views. The only state-changing control is the features-source switch (Nitai's decision). */
export function MonitorLayout() {
  const { error, payload, reload, scanError, clearScanError, startScan } = useStatus();
  return <AppShell mode="monitor"
    headerExtra={<span className="only-desktop"><FeaturesSourceControl placement="header" canSwitch /></span>}
    banners={<>
      {error && payload ? <Banner tone="error" title="Status refresh failed; showing the last loaded data." actions={<button type="button" className="btn" onClick={() => void reload()}>Retry</button>}>{error}</Banner> : null}
      {scanError ? <Banner tone="error" title="Scan failed." onDismiss={clearScanError} actions={<button type="button" className="btn" onClick={() => void startScan()}>Retry scan</button>}>{scanError}</Banner> : null}
      <FeaturesSourceBanner />
    </>} />;
}
