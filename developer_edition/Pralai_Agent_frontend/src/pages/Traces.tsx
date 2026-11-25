import { useEffect, useState } from 'react';
import { api } from '../api';
import SearchSelect from '../components/SearchSelect';
import { PageHead, PlaneChip, Chip } from '../components/ui';
import Icon from '../components/Icon';
import { useApp } from '../context/AppContext';
import type { Trace } from '../types';
import { useFitToViewport } from '../hooks/useFitToViewport';

export default function Traces() {
  const { openDrawer } = useApp();
  const [filter, setFilter] = useState('all');
  const [fromDate, setFromDate] = useState('');
  const [toDate, setToDate] = useState('');
  const [pageSize, setPageSize] = useState(25);
  const [traces, setTraces] = useState<Trace[]>([]);
  const [agents, setAgents] = useState<{ id: string; name: string }[]>([]);
  const [loading, setLoading] = useState(true);
  const [hasMore, setHasMore] = useState(false);

  // Keyset pagination hands back a cursor for the NEXT page only, so
  // going back needs the cursors already visited. The stack's length is
  // the page number, and its last entry is the cursor for the page on
  // screen — index 0 is the first page, which has no cursor.
  // Each entry is a page: the cursor that fetched it, and how many rows
  // came before it. The offset is carried rather than derived from
  // pageIndex * pageSize, because that assumes every earlier page was
  // full — a short page would then make the row numbers drift and never
  // recover.
  const [pages, setPages] = useState<{ cursor: string | null; offset: number }[]>(
    [{ cursor: null, offset: 0 }]);
  const pageIndex = pages.length - 1;
  const offset = pages[pageIndex].offset;

  // Every filter goes to the server. This endpoint is paginated, so
  // filtering in the browser would narrow only the rows already fetched
  // and silently hide matches on later pages — the result would depend
  // on how far someone had scrolled.
  useEffect(() => {
    setLoading(true);
    api.listTracesPage({
      agentId: filter, fromDate: fromDate || undefined, toDate: toDate || undefined,
      cursor: pages[pageIndex].cursor, limit: pageSize,
    })
      .then((page) => { setTraces(page.items); setHasMore(page.hasMore); setNextCursor(page.nextCursor); })
      .catch(() => { setTraces([]); setHasMore(false); })
      .finally(() => setLoading(false));
  }, [filter, fromDate, toDate, pageSize, pages]);

  const [nextCursor, setNextCursor] = useState<string | null>(null);

  // Any change to what is being listed invalidates every cursor already
  // held: a cursor is a position in one specific ordered result set, so
  // reusing it across a filter change would land on an unrelated row.
  const resetPaging = () => setPages([{ cursor: null, offset: 0 }]);

  // The dropdown lists real agents rather than only those appearing in
  // the current page of traces, so an agent whose traces are all older
  // than the page is still selectable.
  useEffect(() => {
    api.listAgents()
      .then((rows) => setAgents(rows.filter((a) => a.plane !== 'Snippet').map((a) => ({ id: a.id, name: a.name }))))
      .catch(() => {});
  }, []);

  const filtered = Boolean(filter !== 'all' || fromDate || toDate);
  // Re-fit when the filter bar can change height (Clear filters appears).
  const scrollRef = useFitToViewport<HTMLDivElement>([filtered, toDate]);

  return (
    <div className="wrap">
      <PageHead title="Traces" eyebrow="Operate"
        subtitle="Root span per run, child span per node execution, with the real user input and agent reply attached — not just timing. Distinct from Runs (status/cost/duration) and Logs (a running feed): this is what actually happened in one specific execution." />
      <div className="row-split traces-filterbar" style={{ gap: 12, alignItems: 'flex-end', flexWrap: 'wrap' }}>
        <div className="field" style={{ margin: 0, minWidth: 240 }}>
          <label id="traces-agent-label" htmlFor="traces-agent">Agent</label>
          <SearchSelect id="traces-agent" labelledBy="traces-agent-label" noun="agent" value={filter}
            options={[
              { value: 'all', label: 'All agents' },
              ...agents.map((a) => ({ value: a.id, label: a.name })),
            ]}
            onChange={(v) => { setFilter(v); resetPaging(); }} />
        </div>
        <div className="field" style={{ margin: 0 }}>
          <label htmlFor="traces-from">From</label>
          {/* max={toDate} stops a range that cannot match anything being
              selectable in the first place. */}
          <input id="traces-from" type="date" value={fromDate} max={toDate || undefined}
            onChange={(e) => { setFromDate(e.target.value); resetPaging(); }} />
        </div>
        <div className="field" style={{ margin: 0 }}>
          <label htmlFor="traces-to">To</label>
          <input id="traces-to" type="date" value={toDate} min={fromDate || undefined}
            onChange={(e) => { setToDate(e.target.value); resetPaging(); }} />
        </div>
        {filtered && (
          <button className="icon-action" style={{ marginBottom: 2 }} title="Clear filters" aria-label="Clear filters"
            onClick={() => { setFilter('all'); setFromDate(''); setToDate(''); resetPaging(); }}>
            <Icon name="i-x" />
          </button>
        )}
        <span className="small muted" style={{ marginLeft: 'auto', marginBottom: 6 }}>
          {/* The To date includes its whole day, which is what picking
              one date in both boxes is meant to mean. */}
          {toDate ? 'To includes the whole day' : ''}
        </span>
      </div>
      <div className="card">
        <div className="table-scroll" ref={scrollRef}>
          <table>
          <thead><tr><th>Trace</th><th>Agent</th><th>Message</th><th>Version</th><th>Plane</th><th>Principal chain</th><th>Spans</th><th>Duration</th><th>Time</th></tr></thead>
          <tbody>
            {traces.map((t) => (
              <tr className="click" key={t.id} onClick={() => openDrawer('traceDetail', t.id)}>
                <td className="mono small">{t.id}</td>
                <td><b>{t.agent_name}</b></td>
                <td className="small" style={{ maxWidth: 240, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                  {t.user_input || <span className="muted">—</span>}
                </td>
                <td className="mono small">{t.version}</td>
                <td><PlaneChip plane={t.plane} /></td>
                <td className="small mono" style={{ maxWidth: 220, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{t.principal}</td>
                <td>{t.spans}</td>
                <td className="mono">{t.duration}</td>
                <td className="mono small muted">{t.ts}</td>
              </tr>
            ))}
          {!loading && traces.length === 0 && (
            <tr><td colSpan={9}>
              {/* An empty result under a filter is a different
                  thing from having no traces at all, and only one
                  of them is worth acting on. */}
              <div className="empty">
                {filtered
                  ? 'No traces match these filters.'
                  : 'No traces yet — they appear as soon as an agent runs.'}
              </div>
            </td></tr>
          )}
          </tbody>
        </table>
        </div>
        {/* Keyset pagination gives no total count — it cannot, without a
          second COUNT query over the whole filtered set on every page.
          So the pager states what it honestly knows: which rows these
          are, and whether more exist. A fabricated "of 685" would go
          stale the moment a run finished. */}
      <div className="pager">
        <div className="pager-info small muted">
          {loading ? 'Loading…'
            : traces.length === 0 ? 'No rows'
            : `Showing ${offset + 1}–${offset + traces.length}`}
          {!loading && hasMore ? ' of more' : ''}
        </div>
        <div className="pager-controls">
          <div className="small muted" style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
            <span>Rows</span>
            <SearchSelect size="sm" ariaLabel="Rows per page" noun="page size" menuMinWidth={140}
              value={String(pageSize)}
              options={[25, 50, 100].map((n) => ({ value: String(n), label: String(n) }))}
              onChange={(v) => { setPageSize(Number(v)); resetPaging(); }} />
          </div>
          <button className="btn sm ghost" disabled={pageIndex === 0 || loading}
            onClick={() => setPages((p) => p.slice(0, -1))}>
            ‹ Previous
          </button>
          <span className="small" style={{ minWidth: 62, textAlign: 'center' }}>
            Page {pageIndex + 1}
          </span>
          <button className="btn sm ghost" disabled={!hasMore || loading}
            onClick={() => setPages((p) => [...p, { cursor: nextCursor, offset: offset + traces.length }])}>
            Next ›
          </button>
        </div>
      </div>
      </div>
    </div>
  );
}
