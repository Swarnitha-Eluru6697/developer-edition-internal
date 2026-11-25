import { useEffect, useState } from 'react';
import { Link } from 'react-router-dom';
import { api } from '../api';
import SearchSelect from './SearchSelect';
import type { ChunkStructure, IndexMethod, KBSettings, RetrievalMethod } from '../types';

export type RerankModelOption = {
  name: string; provider: string | null; builtin: boolean;
  available: boolean; note: string | null;
};

export type RerankAvailability = {
  available: boolean; source: string | null; kind: 'llm' | null; reason: string | null;
  catalogued_rerank_models: { name: string; provider: string; api_model_id: string }[];
  models: RerankModelOption[];
};

function Toggle({ on, onChange, disabled }: { on: boolean; onChange: (v: boolean) => void; disabled?: boolean }) {
  return <div className={`tog${on ? ' on' : ''}`} style={disabled ? { opacity: 0.5, cursor: 'default' } : undefined} onClick={() => !disabled && onChange(!on)} />;
}

function SelectCard({
  selected, icon, title, badge, description, onClick, disabled, children,
}: {
  selected: boolean; icon: string; title: string; badge?: string; description: string;
  onClick: () => void; disabled?: boolean; children?: React.ReactNode;
}) {
  return (
    <div className={`pipeline-card${selected ? ' selected' : ''}${disabled ? ' disabled' : ''}`} onClick={disabled ? undefined : onClick}>
      <div className="row-split" style={{ justifyContent: 'flex-start', gap: 12, alignItems: 'flex-start' }}>
        <span className="pipeline-card-icon">{icon}</span>
        <div style={{ flex: 1 }}>
          <div className="row-split" style={{ justifyContent: 'flex-start', gap: 8 }}>
            <b style={{ fontSize: 13.5 }}>{title}</b>
            {badge && <span className="pipeline-card-badge">{badge}</span>}
          </div>
          <div className="small muted" style={{ marginTop: 2 }}>{description}</div>
        </div>
      </div>
      {selected && children && <div style={{ marginTop: 12 }}>{children}</div>}
    </div>
  );
}

const CHUNK_STRUCTURES: { value: ChunkStructure; icon: string; title: string; description: string }[] = [
  { value: 'general', icon: '☰', title: 'General', description: 'General text chunking mode — the chunks retrieved and recalled are the same.' },
  { value: 'parent_child', icon: '👪', title: 'Parent-Child', description: 'The smaller child chunk is matched at query time; its larger parent chunk is returned as context — real, distinct chunking, not the same chunk reused twice.' },
  { value: 'qa', icon: '💬', title: 'Q&A', description: "Real parsing of Q:/A: pairs (or a question-mark heuristic if unmarked) — indexes on the question, recalls the full pair." },
];

export function ChunkStructurePicker({
  value, onChange, locked,
}: { value: ChunkStructure; onChange: (v: ChunkStructure) => void; locked?: boolean }) {
  return (
    <div>
      {locked && (
        <p className="small muted" style={{ marginTop: 0 }}>
          This knowledge base already has documents — changing Chunk Structure only affects files ingested from now on, existing chunks aren't reprocessed.
        </p>
      )}
      {CHUNK_STRUCTURES.map((cs) => (
        <SelectCard key={cs.value} selected={value === cs.value} icon={cs.icon} title={cs.title} description={cs.description} onClick={() => onChange(cs.value)} />
      ))}
    </div>
  );
}

export type EmbeddingModelOption = {
  name: string; provider: string | null; embedding_dimension: number | null;
  builtin: boolean; available: boolean; note?: string | null;
  // True when the model earned its place here from its model id rather
  // than from a declared "Text Embedding" type — worth showing, because
  // it also means nobody has confirmed the provider serves it.
  inferred?: boolean;
  types?: string[];
  api_model_id?: string | null;
};

export function IndexMethodPicker({
  value, keywordCount, onChange, onKeywordCountChange, embedModel,
  embedModels = [], onEmbedModelChange, embedModelLocked = false,
}: {
  value: IndexMethod; keywordCount: number; onChange: (v: IndexMethod) => void;
  onKeywordCountChange: (n: number) => void; embedModel: string;
  // The models selectable here come from every configured provider, so
  // this control renders whatever the backend reports rather than a
  // hardcoded list. Locked once the pipeline holds vectors: two models'
  // vectors cannot share one column, and its width is fixed at creation.
  embedModels?: EmbeddingModelOption[];
  onEmbedModelChange?: (name: string) => void;
  embedModelLocked?: boolean;
}) {
  const selected = embedModels.find((m) => m.name === embedModel);
  return (
    <div>
      <SelectCard
        selected={value === 'high_quality'} icon="✨" title="High Quality" badge="RECOMMEND"
        description="Calling the embedding model to process documents for more precise retrieval helps the LLM generate high-quality answers."
        onClick={() => onChange('high_quality')}
      >
        <div className="field" style={{ margin: 0 }}>
          <label id="pipeline-embed-model-label">Embedding Model</label>
          {onEmbedModelChange && embedModels.length > 0 ? (
            <>
              {/* Clicks on the trigger and in the (portalled) panel bubble
                  through React to the card, which would re-select it. */}
              <div onClick={(e) => e.stopPropagation()}>
                <SearchSelect
                  labelledBy="pipeline-embed-model-label" noun="embedding model"
                  value={embedModel}
                  disabled={embedModelLocked}
                  options={embedModels.map((m) => ({
                    value: m.name,
                    disabled: !m.available,
                    label: m.name
                      + (m.provider ? ` — ${m.provider}` : ' — built in, no key needed')
                      + (m.embedding_dimension ? ` (${m.embedding_dimension} dims)` : '')
                      + (m.available ? '' : ' — needs an API key')
                      + (m.inferred ? ' — type not set to Text Embedding' : ''),
                  }))}
                  onChange={(v) => onEmbedModelChange(v)}
                />
              </div>
              <div className="small muted" style={{ marginTop: 4 }}>
                {embedModelLocked
                  ? 'Locked — this pipeline already holds vectors. Changing the model means re-ingesting with force reindex, which deletes them first.'
                  : selected?.note
                    || (selected?.provider
                        ? `Called through ${selected.provider} at query and ingest time.`
                        : 'Runs locally with no API key.')}
              </div>
            </>
          ) : (
            // No dropdown supplied (or nothing to choose): show what is
            // actually configured rather than asserting what it is.
            <div className="row-split" style={{ padding: '9px 12px', background: 'var(--card, #fff)', border: '1px solid var(--line)', borderRadius: 8 }}>
              <span className="mono small">{embedModel}</span>
            </div>
          )}
        </div>
      </SelectCard>
      <SelectCard
        selected={value === 'economical'} icon="🐷" title="Economical"
        description={`Real frequency-based keyword extraction — no embedding tokens spent, at the cost of retrieval precision.`}
        onClick={() => onChange('economical')}
      >
        <div className="field" style={{ margin: 0 }}>
          <label>Number of Keywords</label>
          <div className="row-split" style={{ gap: 10 }}>
            <input type="range" min={1} max={20} value={keywordCount} style={{ flex: 1 }} onChange={(e) => onKeywordCountChange(Number(e.target.value))} />
            <input type="number" min={1} max={20} value={keywordCount} style={{ width: 60 }} onChange={(e) => onKeywordCountChange(Number(e.target.value) || 10)} />
          </div>
        </div>
      </SelectCard>
    </div>
  );
}

export function RetrievalSettingPicker({
  settings, onChange,
}: { settings: KBSettings; onChange: (patch: Partial<KBSettings>) => void }) {
  // Fetched here rather than passed in, so no caller can forget to
  // supply it and land back on a hardcoded claim. null = not yet known.
  const [rerank, setRerank] = useState<RerankAvailability | null>(null);
  useEffect(() => {
    let cancelled = false;
    api.getRerankAvailability()
      .then((r) => { if (!cancelled) setRerank(r); })
      // A failed check must not become a claim in either direction, so
      // the box reports that it could not be determined.
      .catch(() => {
        if (!cancelled) {
          setRerank({
            available: false, source: null, kind: null,
            reason: 'Could not check what is configured — the reranker may or may not run.',
            catalogued_rerank_models: [], models: [],
          });
        }
      });
    return () => { cancelled = true; };
  }, []);

  const method: RetrievalMethod = settings.retrieval_method || 'vector';
  const rerankOn = !!settings.reranking_enabled;
  const semanticWeight = settings.semantic_weight ?? 0.7;
  const topK = settings.top_k ?? 3;
  const thresholdOn = !!settings.score_threshold_enabled;
  const threshold = settings.score_threshold ?? 0;

  const topKAndThreshold = (
    <div className="row-split" style={{ gap: 20, marginTop: 12, alignItems: 'flex-start' }}>
      <div style={{ flex: 1 }}>
        <label className="small" style={{ fontWeight: 700 }}>Top K</label>
        <div className="row-split" style={{ gap: 8, marginTop: 6 }}>
          <input type="number" min={1} max={20} value={topK} style={{ width: 60 }} onChange={(e) => onChange({ top_k: Number(e.target.value) || 3 })} />
          <input type="range" min={1} max={20} value={topK} style={{ flex: 1 }} onChange={(e) => onChange({ top_k: Number(e.target.value) })} />
        </div>
      </div>
      <div style={{ flex: 1 }}>
        <div className="row-split" style={{ justifyContent: 'flex-start', gap: 8 }}>
          <label className="small" style={{ fontWeight: 700 }}>Score Threshold</label>
          <Toggle on={thresholdOn} onChange={(v) => onChange({ score_threshold_enabled: v })} />
        </div>
        {thresholdOn && (
          <div className="row-split" style={{ gap: 8, marginTop: 6 }}>
            <input type="number" min={0} max={1} step={0.05} value={threshold} style={{ width: 60 }} onChange={(e) => onChange({ score_threshold: Number(e.target.value) })} />
            <input type="range" min={0} max={1} step={0.05} value={threshold} style={{ flex: 1 }} onChange={(e) => onChange({ score_threshold: Number(e.target.value) })} />
          </div>
        )}
      </div>
    </div>
  );

  // The state of this box is read from the server, never asserted.
  // It used to be a hardcoded amber warning saying reranking would fall
  // back to Weighted Score "if a real Anthropic key is configured",
  // rendered identically whether one was or not — so a workspace with a
  // working key was still warned. It also pointed at "Settings → Model
  // Providers", which is not where that page lives, and its "Check
  // config" chip was a <span> with no handler.
  const rerankRow = (
    <div className="field" style={{ margin: 0, marginBottom: 10 }}>
      <div className="row-split" style={{ justifyContent: 'flex-start', gap: 8 }}>
        <label className="small" style={{ fontWeight: 700, marginBottom: 0 }}>Rerank Model</label>
        <Toggle on={rerankOn} onChange={(v) => onChange({ reranking_enabled: v })} />
      </div>
      {/* Just the dropdown. Options come from every provider configured
          on the Model Providers page; reranking works by asking a chat
          model to score each candidate, so any model this platform can
          execute is a usable reranker — which is why these are LLMs
          rather than dedicated rerank models.

          Rendered whatever the availability check reports, because an
          option that cannot run is already disabled and labelled "needs
          an API key" — that state belongs in the list itself rather than
          in a banner beside it. */}
      {rerankOn && rerank !== null && (
        <SearchSelect
          ariaLabel="Rerank model" noun="rerank model"
          value={settings.rerank_model || ''}
          style={{ marginTop: 8 }}
          options={rerank.models.map((m) => ({
            value: m.name,
            disabled: !m.available,
            label: (m.name || 'Platform default')
              + (m.provider ? ` — ${m.provider}` : '')
              + (m.builtin && m.name ? ' (built in)' : '')
              + (m.available ? '' : ' — needs an API key'),
          }))}
          onChange={(v) => onChange({ rerank_model: v })}
        />
      )}
      {/* The one thing a dropdown cannot express on its own: nothing in
          it can run, so enabling this would change nothing at all. One
          line, with a link that actually goes somewhere. */}
      {rerankOn && rerank !== null && !rerank.models.some((m) => m.available) && (
        <div className="small muted" style={{ marginTop: 6 }}>
          No configured provider can rerank yet.{' '}
          <Link to="/integrations" className="link">Add a model provider key</Link>.
        </div>
      )}
    </div>
  );


  return (
    <div>
      <SelectCard
        selected={method === 'vector'} icon="⚏" title="Vector Search"
        description="Generate query embeddings and search for the text chunk most similar to its vector representation."
        onClick={() => onChange({ retrieval_method: 'vector', reranking_enabled: false })}
      >
        {topKAndThreshold}
      </SelectCard>
      <SelectCard
        selected={method === 'fulltext'} icon="📄" title="Full-Text Search"
        description="Index all terms in the document, allowing users to search any term and retrieve relevant text chunks containing those terms."
        onClick={() => onChange({ retrieval_method: 'fulltext', reranking_enabled: false })}
      >
        {topKAndThreshold}
      </SelectCard>
      <SelectCard
        selected={method === 'hybrid'} icon="⚏" title="Hybrid Search" badge="RECOMMEND"
        description="Execute full-text search and vector search simultaneously, re-rank to select the best match for the user's query."
        onClick={() => onChange({ retrieval_method: 'hybrid' })}
      >
        {rerankRow}
        {!rerankOn && (
          <div className="field" style={{ margin: 0 }}>
            <label className="small" style={{ fontWeight: 700 }}>Weighted Score</label>
            <div className="row-split" style={{ marginTop: 6 }}>
              <span className="small" style={{ color: 'var(--blue)', fontWeight: 700 }}>SEMANTIC {semanticWeight.toFixed(1)}</span>
              <span className="small" style={{ color: 'var(--teal-d)', fontWeight: 700 }}>{(1 - semanticWeight).toFixed(1)} KEYWORD</span>
            </div>
            <input
              type="range" min={0} max={1} step={0.1} value={semanticWeight} style={{ width: '100%' }}
              onChange={(e) => {
                const sw = Number(e.target.value);
                onChange({ semantic_weight: sw, keyword_weight: Math.round((1 - sw) * 10) / 10 });
              }}
            />
          </div>
        )}
        {topKAndThreshold}
      </SelectCard>
    </div>
  );
}
