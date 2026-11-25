import { announceSearchCleared } from '../searchEvents';
import Icon from './Icon';

/** An icon-only button whose full operation shows as a hover/focus tooltip
 *  (the [data-tip] rule in styles.css) and is its accessible name. */
export function IconAction({
  icon, tip, danger, sm, tipAlign, className, type = 'button', children, ...rest
}: {
  icon: string; tip: string; danger?: boolean; sm?: boolean; tipAlign?: 'start' | 'end' | 'left' | 'up';
} & React.ButtonHTMLAttributes<HTMLButtonElement>) {
  const base = className ?? `icon-action${danger ? ' danger' : ''}${sm ? ' sm' : ''}`;
  return (
    <button {...rest} type={type} className={tipAlign ? `${base} tip-${tipAlign}` : base} data-tip={tip} aria-label={tip}>
      <Icon name={icon} />{children}
    </button>
  );
}

export function Chip({ children, cls = 'line' }: { children: React.ReactNode; cls?: string }) {
  return <span className={`chip ${cls}`}>{children}</span>;
}

export function HealthChip({ score, status }: { score: number | null; status: string }) {
  if (status === '—') return <Chip cls="line">not run</Chip>;
  const cls = status === 'pass' ? 'green' : status === 'warn' ? 'amber' : 'red';
  return <Chip cls={cls}>{score} · {status}</Chip>;
}

export function StatusChip({ status }: { status: string }) {
  const map: Record<string, string> = { published: 'green', staged: 'amber', draft: 'slate' };
  return <Chip cls={map[status] || 'slate'}>{status}</Chip>;
}

export function PlaneChip({ plane }: { plane: string }) {
  return <Chip cls={plane === 'Data' ? 'blue' : 'teal'}>{plane} plane</Chip>;
}

export function SevChip({ sev }: { sev: string }) {
  return <Chip cls={sev === 'high' ? 'red' : sev === 'medium' ? 'amber' : 'slate'}>{sev}</Chip>;
}

// What UCS shows for each access level when the object is an agent.
export const AGENT_PRIVILEGE_LABEL: Record<string, string> = {
  select: 'USE_AGENT', writer: 'CREATE_AGENT_VERSION', owner: 'CREATE_AGENT',
};

export function TierChip({ tier, label }: { tier: string; label?: string }) {
  return <Chip cls={tier === 'owner' ? 'purple' : tier === 'writer' ? 'amber' : 'teal'}>{label ?? tier}</Chip>;
}

export function RunStatusChip({ status }: { status: string }) {
  // "paused" (§0.74, GAP_REPORT) — a real, honest, distinct status a
  // batch row's own Run/Trace can now carry when it paused for human
  // input, rather than the prior bug's silent "success" mislabeling.
  // Amber, matching "running"'s own color — both mean "not yet a
  // terminal success/failure", which is exactly what a real paused
  // row is.
  //
  // "blocked" (§0.107, GAP_REPORT) — a real, honest, distinct
  // terminal status a Run can now carry when its own output was
  // withheld by content moderation — the actual fix for a real,
  // reported audit-consistency gap: this used to always persist
  // "success" for a moderation-blocked run, silently misrepresenting
  // it in every real monitoring/audit view. Purple, matching
  // FailClassChip's own "harness" treatment just below — a real,
  // deliberate, policy-driven outcome, not a genuine orchestration
  // failure (red) and not merely "still in progress" (amber).
  const map: Record<string, string> = { success: 'green', failed: 'red', denied: 'red', running: 'amber', paused: 'amber', blocked: 'purple' };
  return <Chip cls={map[status]}>{status}</Chip>;
}

export function FailClassChip({ fail }: { fail: string | null }) {
  if (!fail) return <Chip cls="line">—</Chip>;
  const map: Record<string, [string, string]> = {
    orchestration: ['slate', 'orchestration'],
    harness: ['purple', 'harness'],
    ucs: ['red', 'UCS denial'],
    // §0.107, GAP_REPORT — the real, distinct fail_class a moderation-
    // blocked run now carries, matching RunStatusChip's own new
    // "blocked" status above.
    moderation: ['purple', 'moderation blocked'],
  };
  const [c, l] = map[fail] || ['slate', fail];
  return <Chip cls={c}>{l}</Chip>;
}

export function Stat({ k, v, icon, m, cls = '' }: { k: string; v: React.ReactNode; icon: string; m: string; cls?: string }) {
  return (
    <div className="card stat">
      <div className="k"><Icon name={icon} /> {k}</div>
      <div className="v">{v}</div>
      <div className={`m ${cls}`}>{m}</div>
    </div>
  );
}

export function Banner({ kind, children }: { kind: 'good' | 'warn' | 'info'; children: React.ReactNode }) {
  const icon = kind === 'good' ? 'i-check' : kind === 'warn' ? 'i-alert' : 'i-shield';
  return (
    <div className={`banner ${kind}`}>
      <Icon name={icon} />
      <div>{children}</div>
    </div>
  );
}

export function PageHead({
  title, eyebrow, crumb, subtitle, actions,
}: { title: string; eyebrow?: string; crumb?: React.ReactNode; subtitle?: string; actions?: React.ReactNode }) {
  return (
    <div className="pagehead">
      <div>
        {eyebrow && <div className="eyebrow">{crumb || eyebrow}</div>}
        <h1 className="title">{title}</h1>
        {subtitle && <div className="subtitle">{subtitle}</div>}
      </div>
      {actions && <div className="headactions">{actions}</div>}
    </div>
  );
}

export function SecHead({ children, style }: { children: React.ReactNode; style?: React.CSSProperties }) {
  return <div className="sechead" style={style}>{children}</div>;
}

export function KV({ k, v }: { k: string; v: React.ReactNode }) {
  return (
    <div className="kv">
      <span className="k">{k}</span>
      <span className="v">{v}</span>
    </div>
  );
}

export function Empty({ icon, children }: { icon: string; children: React.ReactNode }) {
  return (
    <div className="empty">
      <div className="ic"><Icon name={icon} /></div>
      {children}
    </div>
  );
}

// A metric this platform may have no data for (§0.77, GAP_REPORT) —
// either because it is genuinely untracked, or because the scope has no
// runs in the window. Either way a null renders as a visible "—" rather
// than a blank gap or a literal "null". Shared by the Monitoring page
// and the agent profile drawer so the two cannot format it differently.
export function fmtMetric(v: unknown, suffix = ''): string {
  return v === null || v === undefined ? '\u2014' : `${v}${suffix}`;
}

/** What the top-bar search is filtering this page to, with a way out. Pages carry no search
 *  box of their own — the one in the top bar is the only one — so this is where a person
 *  sees the filter is on and clears it. */
export function SearchNote({ term, onClear }: { term: string; onClear: () => void }) {
  if (!term.trim()) return null;
  return (
    <span className="search-note" role="status">
      Results for “<b>{term.trim()}</b>”
      <button type="button" className="icon-action sm" title="Clear search" aria-label="Clear search"
        onClick={() => { onClear(); announceSearchCleared(); }}><Icon name="i-x" /></button>
    </span>
  );
}
