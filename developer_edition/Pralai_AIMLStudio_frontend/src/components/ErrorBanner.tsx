/** An error a person can read. Server errors sometimes carry a whole Python traceback;
 *  shown raw it ran off the side of the drawer. The headline is what failed plus the
 *  exception it ended in; the full text stays available, wrapped and scrollable, under
 *  "Technical details". */
export function ErrorBanner({ message, style }: { message: string; style?: React.CSSProperties }) {
  const at = message.indexOf("Traceback (most recent call last)");
  if (at < 0 && message.length <= 280) {
    return <div className="error-banner" role="alert" style={style}>{message}</div>;
  }
  const lead = (at >= 0 ? message.slice(0, at) : message.slice(0, 200)).replace(/[\s:—-]+$/, "");
  const lines = message.trim().split(/\r?\n|(?<=\)) {2,}/).map((l) => l.trim()).filter(Boolean);
  const last = at >= 0 ? lines[lines.length - 1] : "";
  return (
    <div className="error-banner" role="alert" style={style}>
      <div>{lead || "Something went wrong"}{last && last !== lead ? <> — <b>{last}</b></> : at < 0 ? "…" : null}</div>
      <details className="error-details">
        <summary>Technical details</summary>
        <pre>{message}</pre>
      </details>
    </div>
  );
}
