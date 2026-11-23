import { useEffect, useMemo, useRef, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { api } from '../api';
import Icon from '../components/Icon';
import ConfirmPanel from '../components/ConfirmPanel';
import { PageHead, Chip } from '../components/ui';
import { useApp } from '../context/AppContext';
import { P } from '../permissions';
import TemplateDetailsModal from '../modals/TemplateDetailsModal';
import { TEMPLATE_CATEGORIES } from '../types';
import type { Template } from '../types';

type SourceTab = 'all' | 'release' | 'organization' | 'private';
type ViewMode = 'gallery' | 'manage';

const originOf = (t: Template) => t.origin || (t.release_managed ? 'release' : t.source === 'platform' ? 'organization' : 'private');

/** "1 knowledge base · 2 tools" — what whoever installs this has to bring. */
function needsSummary(t: Template): string {
  const r = t.requirements;
  if (!r) return '';
  const plural = (n: number, one: string, many = `${one}s`) => `${n} ${n === 1 ? one : many}`;
  const bits: string[] = [];
  if (r.knowledge_bases?.length) bits.push(plural(r.knowledge_bases.length, 'knowledge base'));
  if (r.tools?.length) bits.push(plural(r.tools.length, 'tool'));
  if (r.catalog_objects?.length) bits.push(plural(r.catalog_objects.length, 'data object'));
  if (r.env_vars?.length) bits.push(plural(r.env_vars.length, 'variable'));
  return bits.join(' · ');
}

function download(filename: string, data: unknown) {
  const blob = new Blob([JSON.stringify(data, null, 2)], { type: 'application/json' });
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url; a.download = filename;
  a.click();
  URL.revokeObjectURL(url);
}

function lifecycleLabel(t: Template): { text: string; cls: string } {
  if (t.release_managed) return { text: 'Release managed', cls: 'line' };
  if (t.status === 'draft') return { text: 'Draft', cls: 'amber' };
  if (t.has_unpublished_changes) return { text: 'Unpublished changes', cls: 'amber' };
  return { text: `Published v${t.version}`, cls: 'teal' };
}

export default function Templates() {
  const [templates, setTemplates] = useState<Template[]>([]);
  const [viewMode, setViewMode] = useState<ViewMode>('gallery');
  const [busy, setBusy] = useState<string | null>(null);
  const [sourceTab, setSourceTab] = useState<SourceTab>('all');
  const [category, setCategory] = useState<string>('all');
  const [vertical, setVertical] = useState<string>('all');
  const [editing, setEditing] = useState<Template | null>(null);
  const [archiving, setArchiving] = useState<Template | null>(null);
  const importRef = useRef<HTMLInputElement>(null);
  const { toast, endUserIdentity, can } = useApp();
  const navigate = useNavigate();

  const reload = () => {
    const call = viewMode === 'gallery' ? api.listTemplates() : api.listManagedTemplates();
    call.then(setTemplates).catch((e) => toast(e.message));
  };
  useEffect(reload, [viewMode]); // eslint-disable-line react-hooks/exhaustive-deps

  const customize = (t: Template) => {
    setBusy(t.id);
    api.instantiateTemplate(t.id, { publish: false })
      .then((r) => { toast(`${r.agent.name} created — opening the Builder`); navigate(`/builder?agent=${r.agent.id}`); })
      .catch((e) => toast(e.message))
      .finally(() => setBusy(null));
  };

  const createAndPublishAgent = (t: Template) => {
    setBusy(t.id);
    api.instantiateTemplate(t.id, { publish: true, invoking_user: endUserIdentity })
      .then((r) => {
        if (r.published) {
          toast(`${r.agent.name} published and live — ${r.agent.version}`);
          navigate('/agents');
        } else {
          toast(`Created as an Agent draft — publish was blocked: ${r.publish_error?.slice(0, 100) || 'publish validation failed'}`);
          navigate(`/builder?agent=${r.agent.id}`);
        }
      })
      .catch((e) => toast(e.message))
      .finally(() => setBusy(null));
  };

  const duplicate = (t: Template, state: 'published' | 'draft' = 'published') => {
    setBusy(t.id);
    api.duplicateTemplate(t.id, { state })
      .then((copy) => {
        toast(`Private draft created — ${copy.name}`);
        if (viewMode === 'manage') reload(); else setViewMode('manage');
        setSourceTab('private');
      })
      .catch((e) => toast(e.message))
      .finally(() => setBusy(null));
  };

  const exportOne = (t: Template, state: 'published' | 'draft' = 'published') => {
    api.exportTemplate(t.id, state)
      .then((pkg) => {
        const suffix = state === 'draft' ? '-draft' : '';
        download(`${(t.key || t.name).replace(/[^a-z0-9]+/gi, '-').toLowerCase()}${suffix}.template.json`, pkg);
        toast(`${state === 'draft' ? 'Draft' : 'Published template'} exported`);
      })
      .catch((e) => toast(e.message));
  };

  const publishTemplate = (t: Template) => {
    setBusy(t.id);
    api.publishTemplate(t.id, t.draft_revision)
      .then((r) => {
        toast(`${r.template.name} published — v${r.version.version}`);
        reload();
      })
      .catch((e) => toast(e.message))
      .finally(() => setBusy(null));
  };

  const importFile = (file: File) => {
    file.text()
      .then((text) => api.importTemplate(JSON.parse(text)))
      .then((r) => {
        toast(`Imported as a Private draft — ${r.template.name}`
          + (r.redactions.length ? ` (${r.redactions.length} credential field${r.redactions.length === 1 ? '' : 's'} blanked)` : ''));
        if (viewMode === 'manage') reload(); else setViewMode('manage');
        setSourceTab('private');
      })
      .catch((e) => toast(e instanceof SyntaxError ? 'That file is not valid JSON' : e.message));
  };

  const reallyArchive = (t: Template) => {
    api.deleteTemplate(t.id)
      .then(() => { toast(`Archived — ${t.name}`); reload(); })
      .catch((e) => toast(e.message));
  };

  const counts = useMemo(() => ({
    all: templates.length,
    release: templates.filter((t) => originOf(t) === 'release').length,
    organization: templates.filter((t) => originOf(t) === 'organization').length,
    private: templates.filter((t) => originOf(t) === 'private').length,
  }), [templates]);

  const verticals = useMemo(
    () => Array.from(new Set(templates.map((t) => t.vertical).filter((v): v is string => !!v))).sort(),
    [templates]);

  const visible = templates.filter((t) =>
    (sourceTab === 'all' || originOf(t) === sourceTab)
    && (category === 'all' || (t.category || 'General') === category)
    && (vertical === 'all' || t.vertical === vertical));

  const groups = TEMPLATE_CATEGORIES
    .map((c) => ({ category: c, items: visible.filter((t) => (t.category || 'General') === c) }))
    .filter((g) => g.items.length > 0);

  const presentCategories = TEMPLATE_CATEGORIES.filter((c) => templates.some((t) => (t.category || 'General') === c));

  return (
    <div className="wrap">
      <PageHead
        title="Templates" eyebrow="Build"
        subtitle={viewMode === 'gallery'
          ? 'Install immutable published Platform-provided, Organization-managed, and Private templates.'
          : 'Save template drafts, review changes, and publish immutable versions to the gallery.'}
        actions={<>
          <button className={`btn ${viewMode === 'gallery' ? '' : 'ghost'}`} onClick={() => setViewMode('gallery')}>Published gallery</button>
          <button className={`btn ${viewMode === 'manage' ? '' : 'ghost'}`} onClick={() => setViewMode('manage')}>Manage templates</button>
          <button className="btn ghost" onClick={() => importRef.current?.click()}><Icon name="i-plus" /> Import template</button>
          <input ref={importRef} type="file" accept="application/json" aria-label="Import template file" style={{ display: 'none' }}
            onChange={(e) => { const f = e.target.files?.[0]; if (f) importFile(f); e.target.value = ''; }} />
        </>}
      />

      {/* Source tabs on the left; category (and industry) filters on the
          same row's right end rather than as a second row underneath. */}
      <div className="tabs inline tabs-with-tools" style={{ marginBottom: 18 }}>
        <div className="tab-group">
          {([['all', 'All'], ['release', 'Platform provided'], ['organization', 'Organization-managed'], ['private', 'Private']] as [SourceTab, string][]).map(([key, label]) => (
            <div key={key} className={`tab${sourceTab === key ? ' on' : ''}`} onClick={() => setSourceTab(key)}>
              {label} <span className="muted small">{counts[key]}</span>
            </div>
          ))}
        </div>
        <div className="tab-group tab-group-end tab-group-sub" role="tablist" aria-label="Filter by category">
          <button type="button" role="tab" className={`tab${category === 'all' ? ' on' : ''}`} aria-selected={category === 'all'} onClick={() => setCategory('all')}>All categories</button>
          {presentCategories.map((c) => (
            <button key={c} type="button" role="tab" className={`tab${category === c ? ' on' : ''}`} aria-selected={category === c} onClick={() => setCategory(c)}>{c}</button>
          ))}
        </div>
        {verticals.length > 0 && (
          <select className="tabs-tool" aria-label="Filter by industry" value={vertical} onChange={(e) => setVertical(e.target.value)} style={{ width: 'auto' }}>
            <option value="all">All industries</option>
            {verticals.map((v) => <option key={v} value={v}>{v}</option>)}
          </select>
        )}
      </div>

      {visible.length === 0 && (
        <p className="muted" style={{ margin: '24px 0' }}>
          {viewMode === 'manage'
            ? 'No editable or managed templates match these filters. Save an Agent as a template or import a package to create a draft.'
            : sourceTab === 'private'
              ? 'No published Private templates yet. Save an Agent as a template, then publish it from Manage templates.'
              : 'No published templates match these filters.'}
        </p>
      )}

      {groups.map((g) => (
        <section key={g.category} style={{ marginBottom: 26 }}>
          <h2 style={{ fontFamily: 'var(--disp)', fontSize: 14, margin: '0 0 10px' }}>
            {g.category} <span className="muted small">{g.items.length}</span>
          </h2>
          <div className="grid g3">
            {g.items.map((t) => {
              const origin = originOf(t);
              const isPrivate = origin === 'private';
              const needs = needsSummary(t);
              const review = t.requirements?.review ?? [];
              const lifecycle = lifecycleLabel(t);
              // Private templates are authored by anyone who may edit in Studio;
              // organization-managed (platform) ones need the platform-template permission.
              const canAuthor = !t.release_managed
                && (t.source === 'private' ? can(P.studioEdit) : can(P.templatesManagePlatform));
              const canPublish = canAuthor && (t.status === 'draft' || t.has_unpublished_changes);
              const canUsePublished = !!t.current_version_id && t.status === 'published';
              return (
                <div className="card pad" key={t.id}>
                  <div className="row-split">
                    <span style={{ display: 'flex', gap: 6, flexWrap: 'wrap' }}>
                      <Chip cls={t.type === 'Data' ? 'blue' : 'teal'}>{t.type}</Chip>
                      {t.vertical && <Chip cls="line">{t.vertical}</Chip>}
                    </span>
                    <span style={{ display: 'flex', gap: 5, flexWrap: 'wrap', justifyContent: 'flex-end' }}>
                      {origin === 'release' ? <Chip cls="teal">Platform provided</Chip> : origin === 'organization' ? <Chip cls="line">Organization-managed</Chip> : <Chip cls="amber">Private</Chip>}
                      {viewMode === 'manage' && <Chip cls={lifecycle.cls}>{lifecycle.text}</Chip>}
                    </span>
                  </div>
                  <h3 style={{ fontFamily: 'var(--disp)', fontSize: 15, margin: '12px 0 6px' }}>{t.name}</h3>
                  <p className="small muted" style={{ margin: '0 0 8px' }}>{t.desc}</p>
                  <p className="small muted mono" style={{ margin: '0 0 8px', fontSize: 10.5 }}>
                    {t.flow?.nodes?.length ?? 0} nodes: {t.flow?.nodes?.map((n) => n.type).join(' → ')}
                  </p>
                  {(t.tags?.length ?? 0) > 0 && (
                    <p style={{ margin: '0 0 8px', display: 'flex', gap: 4, flexWrap: 'wrap' }}>
                      {t.tags.map((tag) => <Chip key={tag} cls="line">{tag}</Chip>)}
                    </p>
                  )}
                  {needs && <p className="small muted" style={{ margin: '0 0 6px' }}>Needs: {needs}</p>}
                  {review.length > 0 && (
                    <p className="small" style={{ margin: '0 0 6px', color: 'var(--amber, #b7791f)' }}>
                      Review before use: {review.map((r) => `${r.type} (${r.why})`).join(', ')}
                    </p>
                  )}
                  <p className="small muted mono" style={{ margin: '0 0 12px', fontSize: 10.5 }}>
                    {t.current_version_id ? `published v${t.version}` : 'never published'}
                    {isPrivate && t.owner ? ` · saved by ${t.owner}` : ''}
                    {viewMode === 'manage' ? ` · draft r${t.draft_revision}` : ''}
                  </p>

                  {viewMode === 'gallery' ? (
                    <>
                      <div className="row-split" style={{ gap: 8 }}>
                        <button className="btn ghost" style={{ flex: 1 }} disabled={busy === t.id} onClick={() => customize(t)}>
                          <Icon name="i-flow" /> Customize
                        </button>
                        <button className="btn" style={{ flex: 1 }} disabled={busy === t.id} onClick={() => createAndPublishAgent(t)}>
                          <Icon name="i-bolt" /> {busy === t.id ? 'Working…' : 'Create & publish agent'}
                        </button>
                      </div>
                      <div className="row-split" style={{ gap: 6, justifyContent: 'flex-start', marginTop: 8 }}>
                        <button className="btn sm ghost" disabled={busy === t.id} onClick={() => duplicate(t, 'published')}
                          title="Make an editable Private draft from this published version">
                          <Icon name="i-copy" /> Duplicate
                        </button>
                        <button className="btn sm ghost" onClick={() => exportOne(t, 'published')}><Icon name="i-scroll" /> Export</button>
                      </div>
                    </>
                  ) : (
                    <>
                      <div className="row-split" style={{ gap: 8 }}>
                        {!canAuthor ? (
                          <button className="btn ghost" style={{ flex: 1 }} disabled>{t.release_managed ? 'Release managed · read only' : 'Read only · no permission to edit'}</button>
                        ) : (
                          <button className="btn ghost" style={{ flex: 1 }} onClick={() => setEditing(t)}><Icon name="i-pencil" /> Edit draft</button>
                        )}
                        {canPublish && (
                          <button className="btn" style={{ flex: 1 }} disabled={busy === t.id} onClick={() => publishTemplate(t)}>
                            <Icon name="i-bolt" /> {busy === t.id ? 'Publishing…' : 'Publish template'}
                          </button>
                        )}
                        {!canPublish && canUsePublished && <button className="btn" style={{ flex: 1 }} disabled>Published</button>}
                      </div>
                      <div className="row-split" style={{ gap: 6, justifyContent: 'flex-start', marginTop: 8, flexWrap: 'wrap' }}>
                        <button className="btn sm ghost" disabled={busy === t.id}
                          onClick={() => duplicate(t, t.current_version_id ? 'published' : 'draft')}>
                          <Icon name="i-copy" /> Duplicate
                        </button>
                        {t.current_version_id && <button className="btn sm ghost" onClick={() => exportOne(t, 'published')}><Icon name="i-scroll" /> Export published</button>}
                        {canAuthor && <button className="btn sm ghost" onClick={() => exportOne(t, 'draft')}><Icon name="i-scroll" /> Export draft</button>}
                        {canAuthor && <button className="btn sm ghost" onClick={() => setArchiving(t)}><Icon name="i-trash" /> Archive</button>}
                      </div>
                    </>
                  )}
                </div>
              );
            })}
          </div>
        </section>
      ))}

      {editing && (
        <TemplateDetailsModal target={{ mode: 'edit', template: editing }} onClose={() => setEditing(null)}
          onDone={reload} />
      )}
      {archiving && (
        <ConfirmPanel
          title={`Archive ${archiving.name}?`} confirmLabel="Archive template"
          body={<>
            This removes the template from the active template gallery and authoring list. Existing Agents and immutable published-version provenance are not changed.
          </>}
          onConfirm={() => { reallyArchive(archiving); setArchiving(null); }}
          onClose={() => setArchiving(null)} />
      )}
    </div>
  );
}
