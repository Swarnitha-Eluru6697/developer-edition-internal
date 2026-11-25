import { useEffect, useState } from 'react';
import { api } from '../api';
import SearchSelect from '../components/SearchSelect';
import { PageHead, RunStatusChip, FailClassChip } from '../components/ui';
import { useApp } from '../context/AppContext';
import type { Run } from '../types';
import { useFitToViewport } from '../hooks/useFitToViewport';

export default function Runs() {
  const { openDrawer } = useApp();
  const [filter, setFilter] = useState('all');
  const [runs, setRuns] = useState<Run[]>([]);
  const [pageSize, setPageSize] = useState(25);
  const [loading, setLoading] = useState(true);
  const [hasMore, setHasMore] = useState(false);
  const [nextCursor, setNextCursor] = useState<string | null>(null);
  // Keyset paging, as on Traces: each entry is a page visited — the cursor
  // that fetched it and how many rows came before it — so Previous can go
  // back and the row numbers stay right after a short page.
  const [pages, setPages] = useState<{ cursor: string | null; offset: number }[]>(
    [{ cursor: null, offset: 0 }]);
  const pageIndex = pages.length - 1;
  const offset = pages[pageIndex].offset;
  // A cursor is a position in one ordered result set, so switching tab or
  // page size starts over from the first page.
  const resetPaging = () => setPages([{ cursor: null, offset: 0 }]);
  const scrollRef = useFitToViewport<HTMLDivElement>();

  useEffect(() => {
    setLoading(true);
    api.listRunsPage({ status: filter, cursor: pages[pageIndex].cursor, limit: pageSize })
      .then((page) => { setRuns(page.items); setHasMore(page.hasMore); setNextCursor(page.nextCursor); })
      .catch(() => { setRuns([]); setHasMore(false); })
      .finally(() => setLoading(false));
  }, [filter, pageSize, pages]);

  return (
    <div className="wrap">
      <PageHead title="Runs" eyebrow="Operate"
        subtitle="Every run resolves its full UCS entitlement plan upfront — agent principal AND invoking user, at every hop — before touching data." />
      <div className="tabs">
        {['all', 'success', 'failed', 'denied'].map((f) => (
          <div key={f} className={`tab${filter === f ? ' on' : ''}`} onClick={() => { setFilter(f); resetPaging(); }}>{f[0].toUpperCase() + f.slice(1)}</div>
        ))}
      </div>
      {/* Rows scroll inside the card so the tabs and the column headers stay
          on screen — the same .table-scroll frame Traces uses, sized to the room on screen. */}
      <div className="card">
        <div className="table-scroll" ref={scrollRef}>
          <table>
            <thead><tr><th>Run</th><th>Agent</th><th>Status</th><th>Failure class</th><th>Hops</th><th>Duration</th><th>Cost</th><th>Time</th></tr></thead>
            <tbody>
              {runs.map((r) => (
                <tr className="click" key={r.id} onClick={() => openDrawer('runDetail', r.id)}>
                  <td className="mono small">{r.id}</td>
                  <td><b>{r.agent_name}</b></td>
                  <td><RunStatusChip status={r.status} /></td>
                  <td><FailClassChip fail={r.fail_class} /></td>
                  <td>{r.hops}</td>
                  <td className="mono">{r.duration}</td>
                  <td className="mono">{r.cost}</td>
                  <td className="mono small muted">{r.ts}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        {/* No total: keyset paging can't give one without a COUNT over every
            run on each page, so the pager says which rows these are and
            whether more exist. */}
        <div className="pager">
          <div className="pager-info small muted">
            {loading ? 'Loading…'
              : runs.length === 0 ? 'No runs'
              : `Showing ${offset + 1}–${offset + runs.length}`}
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
              onClick={() => setPages((p) => [...p, { cursor: nextCursor, offset: offset + runs.length }])}>
              Next ›
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}
