import ConfirmPanel from '../components/ConfirmPanel';
import PromptPanel from '../components/PromptPanel';
import { useEffect, useMemo, useState } from 'react';
import { useNavigate, useSearchParams } from 'react-router-dom';
import { api } from '../api';
import Icon from '../components/Icon';
import SearchSelect from '../components/SearchSelect';
import { PageHead, SearchNote, StatusChip } from '../components/ui';
import { useApp } from '../context/AppContext';
import type { Agent } from '../types';

const TYPE_META: Record<string, { label: string; icon: string; dot: string }> = {
  workflow: { label: 'Workflow', icon: 'i-flow', dot: 'var(--teal-d)' },
  chatflow: { label: 'Chatflow', icon: 'i-comment', dot: '#E0A62B' },
  chatbot: { label: 'Chatbot', icon: 'i-comment', dot: '#E0A62B' },
  agent: { label: 'Agent', icon: 'i-agent', dot: 'var(--purple)' },
  text_generator: { label: 'Text Generator', icon: 'i-file', dot: 'var(--blue)' },
  data: { label: 'Agent', icon: 'i-agent', dot: 'var(--purple)' },
};

function typeMeta(a: Agent) {
  if (a.type === 'App' && a.app_type) return TYPE_META[a.app_type] || TYPE_META.chatbot;
  return TYPE_META.data;
}

export default function Agents() {
  const navigate = useNavigate();
  const { openModal, toast, openDrawer, dataVersion } = useApp();
  const [agents, setAgents] = useState<Agent[]>([]);
  const [searchParams, setSearchParams] = useSearchParams();
  const [search, setSearch] = useState(searchParams.get('q') ?? '');
  // The top-bar search lands here with ?q=; follow it while already on this page too.
  useEffect(() => { const q = searchParams.get('q'); if (q !== null) setSearch(q); }, [searchParams]);
  const [typeFilter, setTypeFilter] = useState('all');
  const [creatorFilter, setCreatorFilter] = useState('all');
  const [tagFilter, setTagFilter] = useState<string[]>([]);
  const [tagMenuOpen, setTagMenuOpen] = useState(false);
  const [sortBy, setSortBy] = useState<'modified' | 'name'>('modified');
  const [statusTab, setStatusTab] = useState<'all' | Agent['status']>('all');
  // Which agent is awaiting a tag, and which a delete confirmation —
  // side panels rather than browser dialogs.
  const [tagging, setTagging] = useState<Agent | null>(null);
  const [deleting, setDeleting] = useState<Agent | null>(null);
  const [deletingId, setDeletingId] = useState<string | null>(null);

  const load = () => api.listAgents().then((all) => setAgents(all.filter((a) => a.plane !== 'Snippet'))).catch(() => {});
  // dataVersion, not just mount: the card grid is what the Edit modal
  // changes, and the modal is rendered by ModalHost rather than by
  // this page, so a rename had no way to tell the card underneath.
  // eslint-disable-next-line react-hooks/exhaustive-deps
  useEffect(() => { load(); }, [dataVersion]);

  const allTypes = useMemo(() => {
    const s = new Set<string>();
    agents.forEach((a) => s.add(a.type === 'App' ? (a.app_type || 'chatbot') : 'data'));
    return Array.from(s);
  }, [agents]);
  const allCreators = useMemo(() => Array.from(new Set(agents.map((a) => a.owner))).sort(), [agents]);
  const allTags = useMemo(() => {
    const s = new Set<string>();
    agents.forEach((a) => (a.tags || []).forEach((t) => s.add(t)));
    return Array.from(s).sort();
  }, [agents]);

  // Every filter except status. The status tabs count against this, so each
  // tab's number is what clicking it would show with the other filters kept.
  const matching = agents
    .filter((a) => (typeFilter === 'all' ? true : (a.type === 'App' ? (a.app_type || 'chatbot') : 'data') === typeFilter))
    .filter((a) => (creatorFilter === 'all' ? true : a.owner === creatorFilter))
    .filter((a) => (tagFilter.length === 0 ? true : tagFilter.every((t) => (a.tags || []).includes(t))))
    .filter((a) => {
      const q = search.trim().toLowerCase();
      if (!q) return true;
      return a.name.toLowerCase().includes(q) || (a.description || '').toLowerCase().includes(q);
    })
    .sort((a, b) => (sortBy === 'name' ? a.name.localeCompare(b.name) : new Date(b.updated_at).getTime() - new Date(a.updated_at).getTime()));
  const countOf = (st: Agent['status']) => matching.filter((a) => a.status === st).length;
  // Staged is a transient state, so its tab appears only while an agent is in it.
  const statusTabs: { key: 'all' | Agent['status']; label: string; count: number }[] = [
    { key: 'all', label: 'All', count: matching.length },
    { key: 'published', label: 'Published', count: countOf('published') },
    { key: 'draft', label: 'Drafts', count: countOf('draft') },
    ...(countOf('staged') > 0 || statusTab === 'staged'
      ? [{ key: 'staged' as const, label: 'Staged', count: countOf('staged') }] : []),
  ];
  const filtered = statusTab === 'all' ? matching : matching.filter((a) => a.status === statusTab);

  const toggleTag = (tag: string) => setTagFilter((prev) => (prev.includes(tag) ? prev.filter((t) => t !== tag) : [...prev, tag]));

  const addTag = (agent: Agent) => setTagging(agent);

  const saveTag = (agent: Agent, tag: string) => {
    const next = Array.from(new Set([...(agent.tags || []), tag]));
    api.setAgentMeta(agent.id, { tags: next })
      .then(load).catch((e) => toast(e.message)).finally(() => setTagging(null));
  };

  // Draft agents only. The card's own onClick opens the builder, so every
  // handler here has to stopPropagation or deleting would also navigate.
  const deleteAgent = (e: React.MouseEvent, agent: Agent) => {
    e.stopPropagation();
    if (agent.status !== 'draft' || deletingId) return;
    setDeleting(agent);
  };

  const reallyDeleteAgent = (agent: Agent) => {
    setDeletingId(agent.id);
    api.deleteAgent(agent.id)
      .then(() => {
        // Drop the card locally rather than refetching, so it disappears
        // immediately; the count in the sidebar refreshes on next mount.
        setAgents((prev) => prev.filter((x) => x.id !== agent.id));
        toast(`Deleted ${agent.name}`);
      })
      .catch((err) => toast(err.message))
      .finally(() => setDeletingId(null));
  };

  const openAgent = (a: Agent) => navigate(`/builder?agent=${a.id}`);

  return (
    <div className="wrap">
      <PageHead
        title="Studio"
        eyebrow="Build"
        subtitle="Every agent you can build, edit, and publish — click one to open its playground."
        actions={<>
          <button className="btn ghost" onClick={() => navigate('/snippets')}><Icon name="i-braces" /> Snippets</button>
          <button className="btn ghost" onClick={() => openModal('generateAgent')}><Icon name="i-brain" /> Create with AI</button>
          <button className="btn" onClick={() => openModal('newAgent')}><Icon name="i-plus" /> New agent</button>
        </>}
      />

      {/* Tabs and filters pin as ONE block: pinned separately they share the
          same top and the filter bar slides over the tabs on scroll. */}
      <div className="sticky-head">
        <div className="tabs" style={{ marginBottom: 14 }}>
          {statusTabs.map((t) => (
            <div key={t.key} className={`tab${statusTab === t.key ? ' on' : ''}`} onClick={() => setStatusTab(t.key)}>
              {t.label} <span className="muted small">{t.count}</span>
            </div>
          ))}
        </div>

        <div className="row-split studio-filterbar">
          <div className="row-split" style={{ gap: 10, justifyContent: 'flex-start' }}>
            <SearchSelect ariaLabel="Filter by type" noun="type" value={typeFilter}
              style={{ width: 150, flex: 'none' }}
              options={[
                { value: 'all', label: 'Types' },
                ...allTypes.map((t) => ({ value: t, label: TYPE_META[t]?.label || t })),
              ]}
              onChange={(v) => setTypeFilter(v)} />
            <div style={{ position: 'relative' }}>
              <button type="button" className="studio-tagtrigger" aria-haspopup="true" aria-expanded={tagMenuOpen}
                onClick={() => setTagMenuOpen((v) => !v)}>
                Tags{tagFilter.length > 0 ? ` (${tagFilter.length})` : ''} <Icon name="i-chev-down" />
              </button>
              {tagMenuOpen && (
                <div className="studio-tagmenu">
                  {allTags.length === 0 && <div className="small muted" style={{ padding: 8 }}>No tags yet</div>}
                  {allTags.map((t) => (
                    <label key={t} className="studio-tagoption">
                      <input type="checkbox" checked={tagFilter.includes(t)} onChange={() => toggleTag(t)} /> {t}
                    </label>
                  ))}
                </div>
              )}
            </div>
            <SearchSelect ariaLabel="Filter by creator" noun="creator" value={creatorFilter}
              style={{ width: 170, flex: 'none' }}
              options={[
                { value: 'all', label: 'Creators' },
                ...allCreators.map((c) => ({ value: c, label: c })),
              ]}
              onChange={(v) => setCreatorFilter(v)} />
            <SearchSelect ariaLabel="Sort by" noun="sort order" value={sortBy}
              style={{ width: 200, flex: 'none' }}
              options={[
                { value: 'modified', label: 'Sort by Last modified' },
                { value: 'name', label: 'Sort by Name' },
              ]}
              onChange={(v) => setSortBy(v as 'modified' | 'name')} />
          </div>
          <SearchNote term={search} onClear={() => {
            setSearch('');
            setSearchParams((p) => { const n = new URLSearchParams(p); n.delete('q'); return n; }, { replace: true });
          }} />
        </div>

      </div>

      <div className="studio-grid">
        {filtered.map((a) => {
          const meta = typeMeta(a);
          return (
            <div key={a.id} className="studio-card" onClick={() => openAgent(a)}>
              <div className="row-split" style={{ alignItems: 'flex-start' }}>
                <div className="row-split" style={{ gap: 10, justifyContent: 'flex-start' }}>
                  <span className="studio-card-icon" style={{ background: 'var(--teal)' }}><Icon name={meta.icon} /></span>
                  <div>
                    <div className="studio-card-title">{a.name}</div>
                    <div className="small muted" style={{ textTransform: 'uppercase', letterSpacing: '.03em', fontSize: 10.5 }}>{meta.label}</div>
                  </div>
                </div>
                <StatusChip status={a.status} />
              </div>
              <div className="small muted studio-card-desc">{a.description || `${a.subtype} · ${a.owner}`}</div>
              <div className="row-split" style={{ marginTop: 10 }}>
                <div className="row-split" style={{ gap: 6, justifyContent: 'flex-start', flexWrap: 'wrap' }}>
                  {(a.tags || []).map((t) => <span key={t} className="chip line sm">{t}</span>)}
                  <button className="chip line sm studio-addtag" onClick={(e) => { e.stopPropagation(); addTag(a); }}>
                    <Icon name="i-plus" /> add tag
                  </button>
                </div>
              </div>
              <div className="small muted" style={{ marginTop: 10 }}>{a.owner} · Edited {new Date(a.updated_at).toLocaleString()}</div>
              <div className="studio-card-footer" style={{ display: 'flex', justifyContent: 'flex-end', gap: 8, alignItems: 'center' }}>
                {a.type === 'App' && a.status === 'published' && (
                  <a
                    className="studio-card-openapp" title="Open app in a new tab"
                    href={`#/app/${a.id}`} target="_blank" rel="noreferrer"
                    onClick={(e) => e.stopPropagation()}
                  >
                    <Icon name="i-flow" />
                  </a>
                )}
                <button
                  className="icon-action"
                  title="Agent details, entitlements, API access"
                  aria-label="Details"
                  onClick={(e) => { e.stopPropagation(); openDrawer('agentProfile', a.id); }}
                >
                  <Icon name="i-eye" />
                </button>
                <button
                  className="studio-card-edit"
                  title={`Edit "${a.name}" — name and description`}
                  aria-label={`Edit ${a.name}`}
                  onClick={(e) => { e.stopPropagation(); openModal('editAgent', a.id); }}
                >
                  <Icon name="i-pencil" />
                </button>
                {a.status === 'draft' && (
                  <button
                    className="studio-card-delete"
                    title={`Delete draft "${a.name}"`}
                    aria-label={`Delete draft ${a.name}`}
                    disabled={deletingId === a.id}
                    onClick={(e) => deleteAgent(e, a)}
                  >
                    <Icon name="i-trash" />
                  </button>
                )}
              </div>
            </div>
          );
        })}
      </div>
      {filtered.length === 0 && <div className="empty-cfg" style={{ padding: 60 }}>No agents match these filters.</div>}
    {tagging && (
      <PromptPanel
        title={`Add a tag to ${tagging.name}`}
        label="Tag" placeholder="e.g. finance"
        hint="Tags are how the Studio grid is filtered. Existing tags are reused if they match."
        confirmLabel="Add tag"
        onSubmit={(t) => saveTag(tagging, t)}
        onClose={() => setTagging(null)} />
    )}
    {deleting && (
      <ConfirmPanel
        title={`Delete ${deleting.name}?`} confirmLabel="Delete agent"
        body={<>
          This removes the agent&rsquo;s flow, runs, traces, logs and environment variables.
          <b> This cannot be undone.</b>
        </>}
        onConfirm={() => { reallyDeleteAgent(deleting); setDeleting(null); }}
        onClose={() => setDeleting(null)} />
    )}
    </div>
  );
}
