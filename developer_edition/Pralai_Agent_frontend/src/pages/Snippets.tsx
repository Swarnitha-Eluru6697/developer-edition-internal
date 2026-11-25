import PromptPanel from '../components/PromptPanel';
import { useEffect, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { api } from '../api';
import Icon from '../components/Icon';
import { PageHead, StatusChip } from '../components/ui';
import { useApp } from '../context/AppContext';
import type { Agent } from '../types';

export default function Snippets() {
  const navigate = useNavigate();
  const { toast } = useApp();
  const [snippets, setSnippets] = useState<Agent[]>([]);
  const [search, setSearch] = useState('');

  const load = () => api.listAgents('Snippet').then(setSnippets).catch(() => {});
  useEffect(() => { load(); }, []);

  const [naming, setNaming] = useState(false);

  const filtered = snippets
    .filter((s) => (search.trim() ? s.name.toLowerCase().includes(search.trim().toLowerCase()) : true))
    .sort((a, b) => new Date(b.updated_at).getTime() - new Date(a.updated_at).getTime());

  const createBlank = () => setNaming(true);

  const createNamed = (name: string) => {
    api.createAgent({ name, subtype: 'Custom', plane: 'Snippet' })
      .then((s) => { toast('Snippet created — opening the Builder'); navigate(`/builder?agent=${s.id}`); })
      .catch((e) => toast(e.message))
      .finally(() => setNaming(false));
  };

  return (
    <div className="wrap">
      <PageHead
        title="Snippets"
        eyebrow="Build"
        subtitle="Reusable groups of nodes — build the logic once, then insert a real copy into any workflow. No Start node, no end node; declare Input Variables instead."
        actions={<>
          <button className="btn ghost" onClick={() => navigate('/agents')}><Icon name="i-chev" /> Back to Studio</button>
          <button className="btn" onClick={createBlank}><Icon name="i-plus" /> Create Snippet</button>
        </>}
      />

      <div className="row-split studio-filterbar">
        <div className="cmd" style={{ maxWidth: 320 }}>
          <Icon name="i-search" />
          <input placeholder="Search snippets…" value={search} onChange={(e) => setSearch(e.target.value)} />
        </div>
      </div>

      {filtered.length === 0 && (
        <div className="empty" style={{ marginTop: 40 }}>
          <div className="ic"><Icon name="i-braces" /></div>
          {snippets.length === 0 ? 'No snippets yet — create one from a blank canvas, or select nodes in a workflow and choose Create Snippet.' : 'No snippets match your search.'}
        </div>
      )}

      <div className="grid g3" style={{ marginTop: 16 }}>
        {filtered.map((s) => (
          <div key={s.id} className="card pad click" onClick={() => navigate(`/builder?agent=${s.id}`)}>
            <div className="row-split" style={{ marginBottom: 8 }}>
              <span className="cnode-icon" style={{ background: 'var(--slate)' }}><Icon name="i-braces" /></span>
              <StatusChip status={s.status} />
            </div>
            <div style={{ fontWeight: 700, marginBottom: 4 }}>{s.name}</div>
            <div className="small muted">{(s.flow.nodes || []).length} node{(s.flow.nodes || []).length === 1 ? '' : 's'} · {(s.input_variables || []).length} input variable{(s.input_variables || []).length === 1 ? '' : 's'}</div>
            <div className="small muted" style={{ marginTop: 8 }}>Updated {new Date(s.updated_at).toLocaleDateString()}</div>
          </div>
        ))}
      </div>
    {naming && (
      <PromptPanel
        title="New snippet" label="Name" placeholder="e.g. Redact PII"
        hint="A snippet is a reusable flow fragment other agents can call."
        confirmLabel="Create snippet"
        onSubmit={createNamed} onClose={() => setNaming(false)} />
    )}
    </div>
  );
}
