import { useEffect, useState } from 'react';
import { api } from '../api';
import Modal from '../components/Modal';
import { useApp } from '../context/AppContext';
import { P } from '../permissions';
import { TEMPLATE_CATEGORIES } from '../types';
import type { Agent, SaveTemplateResult, Template, TemplateSource } from '../types';

/**
 * Template authoring is deliberately two-phase:
 *   1. save/update the mutable workspace draft
 *   2. publish an immutable version when it is valid
 *
 * Platform and Private templates use the same lifecycle. Release-managed
 * Platform templates are never sent here because they remain read-only.
 */
export type TemplateDetailsTarget =
  | { mode: 'save'; agent: Agent }
  | { mode: 'edit'; template: Template };

const splitTags = (raw: string) => raw.split(',').map((t) => t.trim()).filter(Boolean);

export default function TemplateDetailsModal({
  target, onClose, onDone,
}: { target: TemplateDetailsTarget; onClose: () => void; onDone?: (r: SaveTemplateResult) => void }) {
  const { toast, can } = useApp();
  const existing = target.mode === 'edit' ? target.template : null;
  const [name, setName] = useState(existing ? existing.name : target.mode === 'save' ? target.agent.name : '');
  const [desc, setDesc] = useState(existing ? existing.desc : target.mode === 'save' ? (target.agent.description || '') : '');
  const [category, setCategory] = useState<string>(existing?.category || 'General');
  const [vertical, setVertical] = useState(existing?.vertical || '');
  const [tags, setTags] = useState((existing?.tags || []).join(', '));
  const [source, setSource] = useState<TemplateSource>(existing?.source || 'private');
  const [saving, setSaving] = useState(false);
  const [result, setResult] = useState<SaveTemplateResult | null>(null);
  const [agents, setAgents] = useState<Agent[]>([]);
  const [refreshFromAgentId, setRefreshFromAgentId] = useState('');

  // Editing metadata must not silently replace the flow. When an author wants
  // a new flow revision they choose the Agent to recapture explicitly.
  useEffect(() => {
    if (target.mode === 'edit' && !target.template.release_managed) {
      api.listAgents().then(setAgents).catch(() => {});
    }
  }, [target.mode, existing?.id]);

  const saveDraft = async (andPublish: boolean) => {
    if (!name.trim()) { toast('A template needs a name'); return; }
    setSaving(true);
    setResult(null);
    try {
      const details = {
        name: name.trim(), desc: desc.trim(), category,
        vertical: vertical.trim(), tags: splitTags(tags),
      };
      let saved: SaveTemplateResult;
      if (target.mode === 'save') {
        saved = await api.saveAsTemplate({
          agent_id: target.agent.id,
          source,
          ...details,
        });
      } else {
        saved = await api.updateTemplate(target.template.id, target.template.draft_revision, {
          ...details,
          ...(refreshFromAgentId ? { refresh_from_agent_id: refreshFromAgentId } : {}),
        });
      }

      onDone?.(saved);

      if (andPublish) {
        try {
          const published = await api.publishTemplate(saved.template.id, saved.template.draft_revision);
          toast(`${published.template.name} published — v${published.version.version}`);
          // Credential-redaction information still matters even after a
          // successful publish, so show it rather than silently closing.
          if (saved.redactions.length) {
            setResult({ ...saved, template: published.template });
          } else {
            onClose();
          }
          return;
        } catch (e) {
          // Draft save is durable even when publish validation fails. Keep the
          // modal open and say exactly that so the author does not resubmit and
          // accidentally create another template.
          setResult(saved);
          toast(`Draft saved, but publish was blocked: ${e instanceof Error ? e.message : 'validation failed'}`);
          return;
        }
      }

      const errors = saved.issues.filter((i) => i.severity === 'error');
      if (saved.redactions.length || errors.length) {
        setResult(saved);
        toast(`Draft saved — ${saved.template.name}`);
      } else {
        toast(`Draft saved — ${saved.template.name}`);
        onClose();
      }
    } catch (e) {
      toast(e instanceof Error ? e.message : 'Template save failed');
    } finally {
      setSaving(false);
    }
  };

  if (result) {
    const errors = result.issues.filter((i) => i.severity === 'error');
    return (
      <Modal title="Template draft saved" onClose={onClose} footer={<button className="btn" onClick={onClose}>Done</button>}>
        <p style={{ marginTop: 0 }}>
          <b>{result.template.name}</b> is saved as a {result.template.source === 'platform' ? 'Organization-managed' : 'Private'} template draft.
          {errors.length ? ' Fix the validation items below before publishing.' : ''}
        </p>
        {result.redactions.length > 0 && (
          <div className="field">
            <label>Credentials removed</label>
            <p className="small muted" style={{ margin: '0 0 6px' }}>
              Typed-in secrets were blanked in the template. Agents created from it must supply their own credentials; the source Agent is unchanged.
            </p>
            <ul className="small mono" style={{ margin: 0, paddingLeft: 18 }}>
              {result.redactions.map((r) => <li key={r}>{r}</li>)}
            </ul>
          </div>
        )}
        {errors.length > 0 && (
          <div className="field">
            <label>Publish blockers</label>
            <ul className="small" style={{ margin: 0, paddingLeft: 18 }}>
              {errors.map((i, n) => <li key={n}>{i.message}</li>)}
            </ul>
          </div>
        )}
      </Modal>
    );
  }

  const canChoosePlatform = can(P.templatesManagePlatform);
  const isEdit = target.mode === 'edit';

  return (
    <Modal
      title={isEdit ? 'Edit template draft' : 'Save as template'}
      sub={isEdit
        ? 'Changes are saved to a draft. The published gallery keeps using the last immutable version until you publish again.'
        : 'Capture this Agent as a reusable draft, then publish it when it is ready for the template gallery.'}
      onClose={onClose}
      footer={<>
        <button className="btn ghost" onClick={onClose} disabled={saving}>Cancel</button>
        <button className="btn ghost" onClick={() => saveDraft(false)} disabled={saving || !name.trim()}>
          {saving ? 'Saving…' : 'Save draft'}
        </button>
        <button className="btn" onClick={() => saveDraft(true)} disabled={saving || !name.trim()}>
          {saving ? 'Working…' : 'Save & publish'}
        </button>
      </>}
    >
      {!isEdit && (
        <div className="field">
          <label htmlFor="tpl-source">Visibility</label>
          <select id="tpl-source" value={source} onChange={(e) => setSource(e.target.value as TemplateSource)}>
            <option value="private">Private — available only in this organization</option>
            {canChoosePlatform && <option value="platform">Organization-managed — published only in this data plane</option>}
          </select>
          {!canChoosePlatform && <p className="small muted" style={{ margin: '5px 0 0' }}>Organization-managed templates need the permission to manage platform templates. Release-managed Platform-provided templates are promoted through the repository.</p>}
        </div>
      )}
      {isEdit && (
        <div className="field">
          <label>Visibility</label>
          <input value={existing?.release_managed ? 'Platform provided' : existing?.source === 'platform' ? 'Organization-managed' : 'Private'} disabled />
        </div>
      )}
      <div className="field">
        <label htmlFor="tpl-name">Name</label>
        <input id="tpl-name" value={name} maxLength={120} onChange={(e) => setName(e.target.value)} autoFocus />
      </div>
      <div className="field">
        <label htmlFor="tpl-desc">Description</label>
        <textarea id="tpl-desc" rows={3} value={desc} maxLength={1000} onChange={(e) => setDesc(e.target.value)}
          placeholder="What it does and who it is for" />
      </div>
      <div className="row-split" style={{ gap: 12, alignItems: 'flex-start' }}>
        <div className="field" style={{ flex: 1 }}>
          <label htmlFor="tpl-category">Category</label>
          <select id="tpl-category" value={category} onChange={(e) => setCategory(e.target.value)}>
            {TEMPLATE_CATEGORIES.map((c) => <option key={c} value={c}>{c}</option>)}
          </select>
        </div>
        <div className="field" style={{ flex: 1 }}>
          <label htmlFor="tpl-vertical">Industry <span className="muted small">(optional)</span></label>
          <input id="tpl-vertical" value={vertical} maxLength={60} onChange={(e) => setVertical(e.target.value)}
            placeholder="e.g. Banking, Retail" />
        </div>
      </div>
      <div className="field">
        <label htmlFor="tpl-tags">Tags <span className="muted small">(comma separated)</span></label>
        <input id="tpl-tags" value={tags} onChange={(e) => setTags(e.target.value)} placeholder="e.g. onboarding, email" />
      </div>
      {isEdit && (
        <div className="field">
          <label htmlFor="tpl-refresh-agent">Replace draft flow from Agent <span className="muted small">(optional)</span></label>
          <select id="tpl-refresh-agent" value={refreshFromAgentId} onChange={(e) => setRefreshFromAgentId(e.target.value)}>
            <option value="">Keep current template flow</option>
            {agents.map((a) => <option key={a.id} value={a.id}>{a.name} · {a.status} · {a.version}</option>)}
          </select>
          <p className="small muted" style={{ margin: '5px 0 0' }}>
            Re-capturing updates only the draft. The current published version remains unchanged until Save &amp; publish.
          </p>
        </div>
      )}
    </Modal>
  );
}
