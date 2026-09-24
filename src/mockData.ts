export interface DriftMetric {
  timestamp: string;
  num_predictions: number;
  mean_prediction: number;
  stddev_prediction: number;
  min_prediction: number;
  max_prediction: number;
}

export const mockDriftData: DriftMetric[] = [
  { timestamp: "2026-07-28T10:00:00Z", num_predictions: 1000, mean_prediction: 120.5, stddev_prediction: 45.2, min_prediction: 45, max_prediction: 300 },
  { timestamp: "2026-07-29T10:00:00Z", num_predictions: 1050, mean_prediction: 122.1, stddev_prediction: 46.0, min_prediction: 46, max_prediction: 310 },
  { timestamp: "2026-07-30T10:00:00Z", num_predictions: 980, mean_prediction: 121.8, stddev_prediction: 45.5, min_prediction: 45, max_prediction: 305 },
  { timestamp: "2026-07-31T10:00:00Z", num_predictions: 1100, mean_prediction: 125.4, stddev_prediction: 48.1, min_prediction: 44, max_prediction: 315 },
  { timestamp: "2026-08-01T10:00:00Z", num_predictions: 1020, mean_prediction: 128.9, stddev_prediction: 50.2, min_prediction: 48, max_prediction: 325 },
  { timestamp: "2026-08-02T10:00:00Z", num_predictions: 1015, mean_prediction: 135.2, stddev_prediction: 55.4, min_prediction: 50, max_prediction: 340 },
  { timestamp: "2026-08-03T10:00:00Z", num_predictions: 1005, mean_prediction: 142.7, stddev_prediction: 60.1, min_prediction: 55, max_prediction: 360 },
];
