/**
 * Minimal SVG line/area chart for real, irregular series.
 *
 * Deliberately plots points by their actual position in the series rather than
 * padding to a fixed window: these series come from real events, and a day with
 * no traffic has no point at all. Interpolating one would invent traffic that
 * never happened, which is exactly the failure this whole page avoids
 * elsewhere.
 *
 * A single point renders as a dot, not a line — one measurement is not a trend.
 */
export function TrendChart({
  points, height = 64, suffix = "", decimals = 1, color = "var(--brand-strong)",
}: {
  points: Array<{ t: string; value: number }>;
  height?: number;
  suffix?: string;
  decimals?: number;
  color?: string;
}) {
  if (!points.length) {
    return <div className="empty-hint" style={{ padding: "18px 0" }}>No measurements in this window yet.</div>;
  }

  const W = 200;
  const H = 34;
  const values = points.map((p) => p.value);
  const min = Math.min(...values);
  const max = Math.max(...values);
  // A flat series would divide by zero and collapse to the top edge; give it a
  // band so it draws as a flat line through the middle.
  const span = max - min || Math.abs(max) || 1;
  const lo = max === min ? min - span / 2 : min;
  const hi = max === min ? max + span / 2 : max;
  const x = (i: number) => (points.length === 1 ? W / 2 : (i / (points.length - 1)) * W);
  const y = (v: number) => H - ((v - lo) / (hi - lo)) * H;

  const line = points.map((p, i) => `${i === 0 ? "M" : "L"}${x(i).toFixed(2)},${y(p.value).toFixed(2)}`).join(" ");
  const area = `${line} L${x(points.length - 1).toFixed(2)},${H} L${x(0).toFixed(2)},${H} Z`;
  const fmt = (v: number) => `${v.toFixed(decimals)}${suffix}`;

  return (
    <div>
      <div style={{ display: "flex", justifyContent: "space-between", fontSize: 10, color: "var(--text-faint)" }}>
        <span>{fmt(max)}</span>
      </div>
      <svg viewBox={`0 0 ${W} ${H}`} style={{ width: "100%", height, display: "block" }} preserveAspectRatio="none">
        {points.length > 1 && <path d={area} fill={color} opacity={0.12} />}
        {points.length > 1
          ? <path d={line} fill="none" stroke={color} strokeWidth={1.2} vectorEffect="non-scaling-stroke" />
          : <circle cx={x(0)} cy={y(points[0].value)} r={2.5} fill={color} />}
      </svg>
      <div style={{ display: "flex", justifyContent: "space-between", fontSize: 10, color: "var(--text-faint)" }}>
        <span>{points[0].t.slice(0, 10)}</span>
        <span>{fmt(min)}</span>
        <span>{points[points.length - 1].t.slice(0, 10)}</span>
      </div>
      {points.length === 1 && (
        <div className="hint" style={{ marginTop: 4, marginBottom: 0, fontSize: 10 }}>
          One measurement so far — a trend needs more than one point.
        </div>
      )}
    </div>
  );
}
