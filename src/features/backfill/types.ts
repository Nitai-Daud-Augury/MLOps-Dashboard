export type SplitMode = 'month' | 'day' | 'week' | 'days';

export interface SplitSpec {
  mode: SplitMode;
  days?: number;
}

export interface RangeSelection {
  startDay: string;
  endDay: string;
}
