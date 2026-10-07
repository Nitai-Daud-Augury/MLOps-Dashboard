import { CalendarDays } from 'lucide-react';
import { utcToday } from '../../utils';

export function BackfillRangeFields({
  startDay,
  endDay,
  onStartDayChange,
  onEndDayChange,
  onUntilNow,
  onClear,
}: {
  startDay: string;
  endDay: string;
  onStartDayChange: (day: string) => void;
  onEndDayChange: (day: string) => void;
  onUntilNow?: () => void;
  onClear?: () => void;
}) {
  return (
    <div className="runner-date-fields">
      <label onClick={(event) => event.currentTarget.querySelector('input')?.showPicker()}>
        <span>Start date <CalendarDays size={15} /></span>
        <input
          type="date"
          max={utcToday()}
          value={startDay}
          onFocus={(event) => event.currentTarget.showPicker()}
          onChange={(event) => onStartDayChange(event.target.value)}
          aria-label="Start date calendar"
        />
      </label>
      <label onClick={(event) => event.currentTarget.querySelector('input')?.showPicker()}>
        <span>End date (inclusive) <CalendarDays size={15} /></span>
        <input
          type="date"
          max={utcToday()}
          value={endDay}
          onFocus={(event) => event.currentTarget.showPicker()}
          onChange={(event) => onEndDayChange(event.target.value)}
          aria-label="End date calendar"
        />
      </label>
      {(onUntilNow || onClear) ? (
        <div className="actions" style={{ gridColumn: '1 / -1' }}>
          {onUntilNow ? <button className="secondary-button" type="button" onClick={onUntilNow}>Until now</button> : null}
          {onClear ? <button className="secondary-button" type="button" onClick={onClear}>Clear</button> : null}
        </div>
      ) : null}
    </div>
  );
}
