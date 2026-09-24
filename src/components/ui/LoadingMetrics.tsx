import { LoadingWidget } from './LoadingWidget';

export function LoadingMetrics() {
  return <>{Array.from({ length: 4 }, (_, index) => <div className="metric-card loading-card" key={index}><LoadingWidget label="Loading metric" compact /></div>)}</>;
}
