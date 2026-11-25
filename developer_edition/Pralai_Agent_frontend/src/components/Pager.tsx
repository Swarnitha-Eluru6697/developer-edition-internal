import type { KeysetPage } from '../hooks/useKeysetPage';
import SearchSelect from './SearchSelect';

/** The pager shown under every paginated list on the Alerts page.
 *
 * The endpoint reports whether more rows exist, not how many — counting
 * them would be a second full query on every page load, and the total
 * would go stale as soon as a rule fired. So this states what it
 * honestly knows rather than inventing "of 685".
 */
export default function Pager<T>({ page, noun }: { page: KeysetPage<T>; noun: string }) {
  return (
    <div className="pager">
      <div className="pager-info small muted">
        {page.loading ? 'Loading…'
          : page.items.length === 0 ? `No ${noun}`
          : `Showing ${page.offset + 1}–${page.offset + page.items.length}`}
        {!page.loading && page.hasMore ? ' of more' : ''}
      </div>
      <div className="pager-controls">
        <div className="small muted" style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
          <span>Rows</span>
          <SearchSelect size="sm" ariaLabel="Rows per page" noun="page size" menuMinWidth={140}
            value={String(page.pageSize)}
            options={[25, 50, 100].map((n) => ({ value: String(n), label: String(n) }))}
            onChange={(v) => page.setPageSize(Number(v))} />
        </div>
        <button className="btn sm ghost" disabled={page.pageIndex === 0 || page.loading}
          onClick={page.previous}>‹ Previous</button>
        <span className="small" style={{ minWidth: 62, textAlign: 'center' }}>
          Page {page.pageIndex + 1}
        </span>
        <button className="btn sm ghost" disabled={!page.hasMore || page.loading}
          onClick={page.next}>Next ›</button>
      </div>
    </div>
  );
}
