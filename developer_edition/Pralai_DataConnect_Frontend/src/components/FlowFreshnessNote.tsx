import { relativeTime } from "../runtimeState";
import type { FlowFreshness } from "../flowCache";

/**
 * FE-02: says so when the runtime numbers on the page are not a live reading — a snapshot restored from a previous visit, the last values seen
 * before a failed refresh, or a reply saying Kafka Connect is unreachable. Nothing when the numbers are live.
 */
export default function FlowFreshnessNote({ freshness }: { freshness: FlowFreshness }) {
  // "restored" (a saved snapshot shown for a moment after a page refresh, while the live status loads) is deliberately not announced: on a normal refresh it
  // lasted a few hundred milliseconds and read as a glitch. The failure cases below are still shown at once.
  if (freshness.kind === "live" || freshness.kind === "restored") return null;
  if (freshness.kind === "unreachable") {
    return (
      <div className="alert alert-w" role="status" data-flow-freshness="unreachable">
        <div className="at">Live runtime status is unavailable</div>
        <div className="ab">{freshness.message} Connector counts below are not current.</div>
      </div>
    );
  }
  if (freshness.kind === "unavailable") {
    return (
      <div className="alert alert-w" role="status" data-flow-freshness="unavailable">
        <div className="at">Runtime status could not be loaded</div>
        <div className="ab">DataConnect couldn't read runtime status, and no earlier reading is available. Connector counts below are not current.</div>
      </div>
    );
  }
  const age = relativeTime(freshness.observedAt);
  const title = `Observed ${new Date(freshness.observedAt).toLocaleString()}`;
  return (
    <div className="alert alert-w" role="status" data-flow-freshness={freshness.kind} title={title}>
      <div className="at">Runtime status is not live</div>
      <div className="ab">
        DataConnect couldn't refresh runtime status just now. These values were observed {age}. Connector counts below may be out of date.
      </div>
    </div>
  );
}
