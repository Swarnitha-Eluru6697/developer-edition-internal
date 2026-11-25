import Icon from './Icon';

/** From/to date inputs, shared by the Alerts tabs that list events.
 *
 * Not shown on Channels: a channel is configuration, not an event, so
 * "channels created last Tuesday" is not a question anyone asks of it.
 */
export default function DateRangeFilter({
  from, to, onFrom, onTo, disabled,
}: {
  from: string; to: string;
  onFrom: (v: string) => void; onTo: (v: string) => void;
  disabled?: boolean;
}) {
  const active = Boolean(from || to);
  return (
    <div className="row-split" style={{ gap: 8, justifyContent: 'flex-start' }}>
      <label className="small muted" style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
        From
        <input type="date" value={from} disabled={disabled}
          onChange={(e) => onFrom(e.target.value)} />
      </label>
      <label className="small muted" style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
        To
        {/* Inclusive: picking the same date in both boxes means that
            day, which the server handles by treating a bare end date as
            the whole day. */}
        <input type="date" value={to} disabled={disabled}
          onChange={(e) => onTo(e.target.value)} />
      </label>
      {active && (
        <button className="icon-action" title="Clear dates" aria-label="Clear dates" onClick={() => { onFrom(''); onTo(''); }}>
          <Icon name="i-x" />
        </button>
      )}
    </div>
  );
}
