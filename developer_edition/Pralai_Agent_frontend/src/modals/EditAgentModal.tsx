import { useEffect, useState } from 'react';
import { api } from '../api';
import Modal from '../components/Modal';
import { useApp } from '../context/AppContext';
import type { Agent } from '../types';

/**
 * Edit what the Studio card itself shows: the agent's name and
 * description.
 *
 * Neither was editable anywhere in the product. The name was whatever
 * was typed into the New Agent dialog and stayed that way — a typo in it
 * was permanent short of deleting the agent and rebuilding it — and the
 * description could only be set by the AI generator, so a hand-built
 * agent's card showed "{subtype} · {owner}" forever.
 *
 * Opening the card already opens the Builder, which edits the flow. This
 * edits the card, which is the other thing "edit" can reasonably mean
 * here, so the two do not compete.
 */
export default function EditAgentModal({ agentId }: { agentId: string }) {
  const { closeModal, toast, notifyDataChanged } = useApp();
  const [agent, setAgent] = useState<Agent | null>(null);
  const [name, setName] = useState('');
  const [description, setDescription] = useState('');
  const [saving, setSaving] = useState(false);

  useEffect(() => {
    api.getAgent(agentId)
      .then((a) => { setAgent(a); setName(a.name); setDescription(a.description || ''); })
      .catch((e) => toast(e.message));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [agentId]);

  const dirty = !!agent && (name.trim() !== agent.name || description !== (agent.description || ''));

  const save = () => {
    if (!name.trim()) { toast('An agent needs a name'); return; }
    setSaving(true);
    api.setAgentMeta(agentId, { name: name.trim(), description })
      .then((updated) => {
        toast(`Saved — this agent is now "${updated.name}"`);
        // The card grid loads on mount, so without this the card keeps
        // showing the old name until the page is navigated away from.
        notifyDataChanged();
        closeModal();
      })
      .catch((e) => toast(e.message))
      .finally(() => setSaving(false));
  };

  return (
    <Modal
      title="Edit agent"
      sub="Changes the name and description shown on the card. Use the Builder to change the flow."
      onClose={closeModal}
      footer={
        <><button className="btn ghost" onClick={closeModal} disabled={saving}>Cancel</button>
          <button className="btn" onClick={save} disabled={saving || !dirty || !agent}>
            {saving ? 'Saving…' : 'Save'}
          </button></>
      }
    >
      {!agent ? (
        <p className="muted">Loading…</p>
      ) : (
        <>
          <div className="field">
            <label>Name</label>
            <input
              autoFocus
              placeholder="e.g. Renewal Risk Analysis Agent"
              value={name}
              maxLength={120}
              onChange={(e) => setName(e.target.value)}
              onKeyDown={(e) => { if (e.key === 'Enter' && dirty) save(); }}
            />
          </div>
          <div className="field">
            <label>Description</label>
            <textarea
              placeholder="What this agent does — shown on its card"
              rows={3}
              value={description}
              onChange={(e) => setDescription(e.target.value)}
            />
          </div>
          {/* Said plainly because renaming a published agent is the case
              people worry about: the version series, the published URL
              and every run already recorded are keyed on the id, not the
              name, so none of them move. */}
          <div className="small muted" style={{ lineHeight: 1.45 }}>
            Renaming is safe for a published agent — its app URL, version history and
            recorded runs are all keyed on the agent's id. Past runs keep the name they
            ran under.
          </div>
        </>
      )}
    </Modal>
  );
}
