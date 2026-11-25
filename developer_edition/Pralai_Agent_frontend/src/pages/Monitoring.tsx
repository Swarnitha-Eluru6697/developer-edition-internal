import { useEffect, useMemo, useState } from 'react';
import EvalManager from '../components/EvalManager';
import SearchSelect from '../components/SearchSelect';
import EvalSetModal from '../modals/EvalSetModal';
import { api } from '../api';
import { PageHead, Stat, Chip, fmtMetric } from '../components/ui';
import { useApp } from '../context/AppContext';
import type { Agent, Eval, EvalHealth } from '../types';

function MonSection({ title, cards }: { title: string; cards: React.ReactNode[] }) {
  return (
    <>
      <div className="sechead" style={{ marginTop: 26 }}>{title}</div>
      <div className="grid g3">{cards}</div>
    </>
  );
}

/** Average tokens per run, short: 640, 1.2k, 15k. */
function fmtTokens(v?: number | string | null): string {
  const n = typeof v === 'string' ? Number(v) : v;
  if (n == null || Number.isNaN(n)) return '—';
  if (n < 1000) return String(Math.round(n));
  return `${(n / 1000).toFixed(n < 10000 ? 1 : 0).replace(/\.0$/, '')}k`;
}

export default function Monitoring() {
  const { toast } = useApp();
  const [agents, setAgents] = useState<Agent[]>([]);
  const [filter, setFilter] = useState('all');
  const [metrics, setMetrics] = useState<Record<string, string | number | null>>({});
  const [evals, setEvals] = useState<Eval[]>([]);
  const [evalHealth, setEvalHealth] = useState<EvalHealth | null>(null);
  // The eval row whose cases are open. Local rather than a global
  // ModalType: nothing else opens it.
  const [evalTab, setEvalTab] = useState<'sets' | 'results'>('sets');
  // /monitoring/evals computes one row per eval set in Python rather
  // than selecting a table, so there is no cursor to page with — and the
  // whole list is already in hand. Paging it here is therefore honest
  // AND can show an exact total, which keyset pagination cannot.
  const [evalPage, setEvalPage] = useState(0);
  const [evalPageSize, setEvalPageSize] = useState(10);
  const [openEval, setOpenEval] = useState<Eval | null>(null);

  useEffect(() => { api.listAgents().then((a) => setAgents(a.filter((x) => x.plane !== 'Snippet'))).catch(() => {}); }, []);

  const loadMonitoring = () => {
    api.getMetrics(filter === 'all' ? undefined : filter).then((r) => setMetrics(r.metrics)).catch((e) => toast(e.message));
    const agentName = filter === 'all' ? undefined : agents.find((a) => a.id === filter)?.name;
    api.getEvals(agentName).then(setEvals).catch(() => {});
    api.getEvalHealth(filter).then(setEvalHealth).catch(() => setEvalHealth(null));
  };

  useEffect(() => {
    loadMonitoring();
    // eslint-disable-next-line
  }, [filter, agents.length]);

  // Built here rather than inside the dropdown so the aggregate row is
  // an option like any other: it is the default, and treating it as a
  // separate control would mean two places to clear a filter from.
  const agentOptions = useMemo(() => [
    { value: 'all', label: 'All agents', hint: 'platform aggregate' },
    ...agents.map((a) => ({ value: a.id, label: a.name, hint: a.status })),
  ], [agents]);

  const m = metrics;

  const evalPageCount = Math.max(1, Math.ceil(evals.length / evalPageSize));
  // Clamped as well as reset below. The effect covers a deliberate
  // change — agent or page size — but the list can also shrink under
  // you: running a set reloads it, and a set whose run failed drops
  // out. Without the clamp that leaves an empty table and a Previous
  // button that walks back from a page number that no longer exists.
  const evalPageSafe = Math.min(evalPage, evalPageCount - 1);
  const evalStart = evalPageSafe * evalPageSize;
  const evalRows = evals.slice(evalStart, evalStart + evalPageSize);

  // Switching agent or page size changes what is listed, so the page
  // number no longer refers to anything.
  useEffect(() => { setEvalPage(0); }, [filter, evalPageSize]);

  return (
    <div className="wrap">
      <PageHead title="Monitoring" eyebrow="Operate"
        subtitle="The full metrics catalog — run, node, harness, and UCS/governance layers, plus cost, agent health, and evaluation." />
      {/* Pinned under the page header, so the scope can be changed from
          anywhere down the metrics catalog. */}
      <div className="row-split sticky-head" style={{ marginBottom: 16, paddingBottom: 4 }}>
        <div className="field" style={{ margin: 0, minWidth: 320 }}>
          <label id="mon-agent-filter-label" htmlFor="mon-agent-filter">Filter by agent</label>
          <SearchSelect id="mon-agent-filter" labelledBy="mon-agent-filter-label"
            noun="agent" searchPlaceholder="Search agents…" emptyText="No agent matches"
            value={filter} onChange={setFilter} options={agentOptions} />
        </div>
        {filter !== 'all' && <span className="chip line" style={{ marginTop: 20 }}>Scoped to {agents.find((a) => a.id === filter)?.name}</span>}
      </div>

      {/* Every card below is computed over a rolling window, so a dash
          can mean two completely different things: "this metric has no
          instrumentation" or "this scope has no runs to compute from".
          Without saying which, a freshly-picked agent looks broken. */}
      {typeof m.sample_size === 'number' && (
        m.sample_size === 0 ? (
          <div className="empty" style={{ marginBottom: 16 }}>
            No runs {filter === 'all' ? 'on the platform' : `for ${agents.find((a) => a.id === filter)?.name || 'this agent'}`}
            {' '}in the last {m.window_days ?? 7} days, so every run-derived metric below is blank.
            {filter !== 'all' && ' Open the agent and use Try it, or invoke its published app, then reload this page.'}
          </div>
        ) : (
          <div className="small muted" style={{ marginBottom: 16 }}>
            Computed from {m.sample_size} run{m.sample_size === 1 ? '' : 's'} in the last {m.window_days ?? 7} days.
          </div>
        )
      )}

      <MonSection title="Run-level" cards={[
        <Stat key="s" k="Success rate" v={fmtMetric(m.success, '%')} icon="i-check" m={`p50 ${fmtMetric(m.p50)} · p95 ${fmtMetric(m.p95)}`} cls="up" />,
        <Stat key="c" k="Cost per run (avg)" v={fmtMetric(m.costAvg)} icon="i-bolt" m={m.costAvg == null ? 'no runs in window yet' : `tokens: ${fmtTokens(m.avgInputTokens)} in / ${fmtTokens(m.avgOutputTokens)} out`} />,
        <Stat key="r" k="Retries per run" v={fmtMetric(m.retries)} icon="i-clock" m={m.retries == null ? 'no per-run attempt count is persisted — Temporal tracks it internally' : 'avg per run'} />,
      ]} />
      <MonSection title="Harness-level (§3.5)" cards={[
        <Stat key="l" k="Loop iterations / run" v={fmtMetric(m.loopIter)} icon="i-cpu" m={m.loopIter == null ? 'no run in this window used a loop or iteration node' : 'avg passes, over runs with a loop or iteration container'} />,
        <Stat key="sl" k="Step-limit hits" v={fmtMetric(m.stepLimit, '%')} icon="i-alert" m="of agentic-node executions" cls="warn" />,
        <Stat key="g" k="Guardrail triggers" v={fmtMetric(m.guardrail, '%')} icon="i-shield" m="input + output blocks combined" cls="warn" />,
      ]} />
      <MonSection title="UCS / governance-level (§7.6)" cards={[
        <Stat key="e" k="Entitlement pass rate" v={fmtMetric(m.entPass, '%')} icon="i-shield" m="per-hop, all chains" cls="up" />,
        <Stat key="cd" k="Avg chain depth" v={fmtMetric(m.chainDepth)} icon="i-branch" m={m.chainDepth == null ? 'no runs in this window' : 'avg hops per run, against the 5-hop cap'} />,
        <Stat key="ch" k="Entitlement-plan cache hit" v={fmtMetric(m.cacheHit)} icon="i-check" m={m.cacheHit == null ? 'this service has no entitlement-plan cache — UCS caches internally, where this cannot see it' : '60–120s TTL'} />,
      ]} />
      <MonSection title="Cost & business (platform-wide)" cards={[
        /* Was a hardcoded dash citing §0.77's "every Run.cost is $0.00".
           §0.78 made per-run cost real — the average two rows up proves
           it — so the total was computable and this card just never
           asked. Renamed from "per workspace" because this deployment
           has no workspace model to divide by; it is the whole
           deployment's spend. */
        <Stat key="w" k={filter === 'all' ? 'Spend (7d, all agents)' : 'Spend (7d, this agent)'}
          v={fmtMetric(m.costWindow)} icon="i-bolt"
          m={m.costWindow == null ? 'no runs in this window' : `summed over ${m.sample_size} run${m.sample_size === 1 ? '' : 's'}`} />,
        <Stat key="a" k="Active agents" v={agents.length} icon="i-cube" m="live count" cls="up" />,
        /* A count, not a percentage: agents created before provenance
           was recorded carry no template name, and dividing by a total
           that includes them would report "hardly anything is reused"
           when that is really an artifact of when tracking started. */
        <Stat key="mr" k="Agents from a template" v={fmtMetric(m.templateAgents)} icon="i-grid"
          m={m.templateAgents ? 'created from a marketplace template'
            : 'none yet — agents created before provenance was recorded are not counted'} />,
      ]} />
      <MonSection title="Agent health" cards={[
        <Stat key="ep" k="Eval pass rate (avg)" v={evalHealth?.avg_pass_rate != null ? `${evalHealth.avg_pass_rate}%` : '—'} icon="i-check"
          m={evalHealth?.avg_pass_rate != null ? 'across golden test sets, latest run each' : 'no completed eval runs yet'} cls="up" />,
        <Stat key="tr" k="7-day success trend" v={evalHealth?.trend_delta_pts != null ? `${evalHealth.trend_delta_pts >= 0 ? '▲' : '▼'} ${Math.abs(evalHealth.trend_delta_pts)}pt` : '—'} icon="i-chart"
          m={evalHealth?.trend_note || 'vs. the prior 7 days'} cls={evalHealth?.trend_delta_pts != null && evalHealth.trend_delta_pts < 0 ? 'warn' : 'up'} />,
        <Stat key="df" k="Behavioral drift flags" v={evalHealth ? evalHealth.drift_flags.length : '—'} icon="i-alert"
          m={evalHealth && evalHealth.drift_flags.length > 0 ? evalHealth.drift_flags.map((f) => f.agent_name).join(', ') : 'none flagged'}
          cls={evalHealth && evalHealth.drift_flags.length > 0 ? 'warn' : 'up'} />,
      ]} />

      {/* Clamped, not just sliced: filtering to an agent with fewer
          sets while on page 3 would otherwise show an empty table with
          no way back. */}
      {/* One section, two tabs. Managing sets and reading results were
          two stacked blocks, each with its own heading and prose, so on
          a single agent the page ran well past the fold before showing a
          single result. */}
      <div className="sechead" style={{ marginTop: 26 }}>Evaluations</div>
      <div className="tabs inline">
        {(['sets', 'results'] as const).map((t) => (
          <div key={t} className={`tab${evalTab === t ? ' on' : ''}`}
            onClick={() => setEvalTab(t)}>
            {t === 'sets' ? 'Eval sets' : 'Results'}
          </div>
        ))}
      </div>

      {/* Any change to what is listed invalidates the page number. */}
      {evalTab === 'sets' ? (
        filter === 'all' ? (
          // A set belongs to one agent, so there is nothing coherent to
          // create while scoped to every agent. Previously nothing said
          // so and the manager simply vanished.
          <div className="empty-cfg" style={{ padding: '22px 14px' }}>
            Pick a single agent in the filter above to create eval sets, add cases and run them.
          </div>
        ) : (
          <EvalManager agentId={filter}
            agentName={agents.find((a) => a.id === filter)?.name || filter}
            onRunComplete={loadMonitoring} />
        )
      ) : (
        <>
          <p className="small muted" style={{ margin: '0 0 8px' }}>
            The latest run of each golden test set. Click a row to see the cases it applies.
          </p>
          <div className="card">
            <table>
              <thead><tr><th>Agent</th><th>Eval set</th><th>Pass rate</th><th>Result</th><th>Drift</th><th>Last run</th></tr></thead>
              <tbody>
                {evalRows.map((e, i) => {
                  const rate = parseFloat(e.rate);
                  return (
                    <tr key={i} className="click" style={{ cursor: 'pointer' }}
                        title="Show the cases this set applies"
                        onClick={() => setOpenEval(e)}>
                      <td><b>{e.agent}</b></td>
                      <td className="mono small">{e.set}</td>
                      <td className="mono">{e.passed} <span className="muted small">({e.rate})</span></td>
                      <td>{rate >= 90 ? <Chip cls="green">pass</Chip> : rate >= 70 ? <Chip cls="amber">warn</Chip> : <Chip cls="red">fail</Chip>}</td>
                      <td>{e.drift === 'none' ? <Chip cls="line">none</Chip> : e.drift === 'watch' ? <Chip cls="amber">watch</Chip> : <Chip cls="red">flagged</Chip>}</td>
                      <td className="mono small muted">{e.ts}</td>
                    </tr>
                  );
                })}
                {evals.length === 0 && <tr><td colSpan={6}><div className="empty">No eval runs for this agent yet</div></td></tr>}
              </tbody>
            </table>
          </div>
          {evals.length > 0 && (
            <div className="pager">
              <div className="pager-info small muted">
                {`Showing ${evalStart + 1}–${evalStart + evalRows.length} of ${evals.length}`}
              </div>
              <div className="pager-controls">
                <div className="small muted"
                  style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
                  <span>Rows</span>
                  <SearchSelect size="sm" ariaLabel="Rows per page" noun="page size" menuMinWidth={140}
                    value={String(evalPageSize)}
                    options={[10, 25, 50].map((n) => ({ value: String(n), label: String(n) }))}
                    onChange={(v) => setEvalPageSize(Number(v))} />
                </div>
                <button className="btn sm ghost" disabled={evalPageSafe === 0}
                  onClick={() => setEvalPage(evalPageSafe - 1)}>‹ Previous</button>
                <span className="small" style={{ minWidth: 76, textAlign: 'center' }}>
                  Page {evalPageSafe + 1} of {evalPageCount}
                </span>
                <button className="btn sm ghost" disabled={evalPageSafe >= evalPageCount - 1}
                  onClick={() => setEvalPage(evalPageSafe + 1)}>Next ›</button>
              </div>
            </div>
          )}
        </>
      )}

      {openEval && <EvalSetModal row={openEval} onClose={() => setOpenEval(null)} />}
    </div>
  );
}
