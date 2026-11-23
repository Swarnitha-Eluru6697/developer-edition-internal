import { useEffect, useState } from 'react';
import Icon from './Icon';
import { api } from '../api';
import { useApp } from '../context/AppContext';
import ConversationOpenerModal from './ConversationOpenerModal';
import FollowUpSettingsModal from './FollowUpSettingsModal';
import ModerationSettingsModal from './ModerationSettingsModal';
import type { Agent, AgentFeatures } from '../types';

const DEFERRED = [
  { icon: 'i-cpu', name: 'Text to Speech', desc: 'Conversation messages can be converted to speech.' },
  { icon: 'i-cpu', name: 'Speech to Text', desc: 'Voice input can be used in chat.' },
  { icon: 'i-cpu', name: 'Annotation Reply', desc: 'Manually curate a high-quality response for prioritized matching with similar questions.' },
];

type SubModal = 'opener' | 'followup' | 'moderation' | null;

export default function FeaturesPanel({ agent: initialAgent, onClose, onSaved }: { agent: Agent; onClose: () => void; onSaved: (a: Agent) => void }) {
  const { toast, notifyDataChanged } = useApp();
  const [agent, setAgent] = useState(initialAgent);
  const [subModal, setSubModal] = useState<SubModal>(null);
  // Null until asked, so neither banner flashes before the answer.
  const [identity, setIdentity] = useState<
    { verified_identity: string | null; reason: string | null } | null>(null);
  useEffect(() => {
    let live = true;
    api.whoami()
      .then((r) => { if (live) setIdentity(r); })
      .catch(() => {});
    return () => { live = false; };
  }, []);

  // Re-read the agent when this panel opens.
  //
  // Every toggle below sends the WHOLE features object, built from this
  // agent. Try It seeds that from a snapshot taken when its page
  // loaded, so reopening the panel handed back a stale base and the
  // save undid whatever had been enabled since. The server now merges,
  // which fixes the data loss on its own — this also stops the switches
  // themselves from rendering in the wrong position, which is what made
  // it look like nothing had saved.
  useEffect(() => {
    let live = true;
    api.getAgent(initialAgent.id)
      .then((fresh) => { if (live) setAgent(fresh); })
      // The snapshot is still usable; a failed refresh should not empty
      // the panel.
      .catch(() => {});
    return () => { live = false; };
  }, [initialAgent.id]);

  const persistAgent = (a: Agent) => {
    setAgent(a);
    onSaved(a);
    // So a list or page holding its own copy of this agent refetches,
    // rather than handing a stale snapshot to the next thing that opens.
    notifyDataChanged();
  };

  const toggle = (key: keyof AgentFeatures, enabled: boolean) => {
    const next: AgentFeatures = { ...(agent.features || {}), [key]: { ...((agent.features || {})[key] as any), enabled } };
    api.setAgentFeatures(agent.id, next).then((a) => {
      persistAgent(a);
      // The Conversation Opener needs a message as well as a switch: the
      // runner seeds the greeting only when `enabled` AND `text` are both
      // set. The switch alone wrote {enabled: true} with no text, so the
      // feature was on and did nothing — and because the row's preview
      // only renders when text exists, it looked identical to a
      // configured one. The message lives in this row's sub-modal, which
      // the switch's own stopPropagation stopped you from reaching, so
      // turning it on was a dead end. Open the editor instead.
      if (key === 'conversation_opener' && enabled
          && !((a.features || {}).conversation_opener?.text || '').trim()) {
        setSubModal('opener');
      }
    }).catch((e) => toast(e.message));
  };

  // require_authenticated_user is a plain boolean, not the nested
  // {enabled: ...} shape every other feature here uses — a separate
  // setter rather than forcing it into that shape for no real reason.
  const toggleBoolean = (key: keyof AgentFeatures, value: boolean) => {
    const next: AgentFeatures = { ...(agent.features || {}), [key]: value };
    api.setAgentFeatures(agent.id, next).then((a) => persistAgent(a)).catch((e) => toast(e.message));
  };

  const features = agent.features || {};

  return (
    <div className="overlay-side" onClick={(e) => { if (e.target === e.currentTarget) onClose(); }}>
      <div className="features-panel">
        <div className="dh">
          <div><h3>Features</h3><p className="small muted" style={{ margin: '4px 0 0' }}>Enhance the end-user chat experience — every toggle here is real and affects the actual run. Click a feature to configure it.</p></div>
          <button className="x" onClick={onClose}><Icon name="i-x" /></button>
        </div>
        <div className="features-body">
          <div className="feature-row click" onClick={() => setSubModal('opener')}>
            <div className="feature-row-h">
              <span className="feature-icon" style={{ background: 'var(--teal-d)' }}><Icon name="i-comment" /></span>
              <div style={{ flex: 1 }}>
                <b>Conversation Opener</b>
                {features.conversation_opener?.enabled && features.conversation_opener.text && (
                  <div className="small muted feature-preview">{features.conversation_opener.text}</div>
                )}
                {features.conversation_opener?.enabled
                  && !(features.conversation_opener.text || '').trim() && (
                  <div className="small" style={{ color: 'var(--amber, #B5650A)' }}>
                    On, but no message is set — nothing will be shown. Click this row to write one.
                  </div>
                )}
                {!features.conversation_opener?.enabled && (
                  <div className="small muted">The first message the assistant sends, before the person types anything.</div>
                )}
              </div>
              <label className="switch" onClick={(e) => e.stopPropagation()}>
                <input type="checkbox" checked={!!features.conversation_opener?.enabled} onChange={(e) => toggle('conversation_opener', e.target.checked)} /><span />
              </label>
            </div>
          </div>

          <div className="feature-row click" onClick={() => setSubModal('followup')}>
            <div className="feature-row-h">
              <span className="feature-icon" style={{ background: 'var(--blue)' }}><Icon name="i-comment" /></span>
              <div style={{ flex: 1 }}><b>Follow-up</b><div className="small muted">Real, LLM-generated suggestions based on the actual reply — not static.</div></div>
              <label className="switch" onClick={(e) => e.stopPropagation()}>
                <input type="checkbox" checked={!!features.follow_up?.enabled} onChange={(e) => toggle('follow_up', e.target.checked)} /><span />
              </label>
            </div>
          </div>

          <div className="feature-row">
            <div className="feature-row-h">
              <span className="feature-icon" style={{ background: '#B5650A' }}><Icon name="i-scroll" /></span>
              <div style={{ flex: 1 }}><b>Citations and Attributions</b><div className="small muted">Shows the real retrieved source chunks when a flow's retrieval node uses a registered knowledge base — nothing shown if there's no real data to cite.</div></div>
              <label className="switch"><input type="checkbox" checked={!!features.citations?.enabled} onChange={(e) => toggle('citations', e.target.checked)} /><span /></label>
            </div>
          </div>

          <div className="feature-row">
            <div className="feature-row-h">
              <span className="feature-icon" style={{ background: 'var(--purple)' }}><Icon name="i-clock" /></span>
              <div style={{ flex: 1 }}>
                <b>Chat History</b>
                <div className="small muted">
                  Real, per-user conversation memory — genuinely sent as prior turns to the model, not just a summary. Any LLM node without its own explicit memory setting inherits this.
                </div>
              </div>
              <label className="switch">
                <input type="checkbox" checked={!!features.chat_history?.enabled} onChange={(e) => toggle('chat_history', e.target.checked)} /><span />
              </label>
            </div>
          </div>

          <div className="feature-row">
            <div className="feature-row-h">
              <span className="feature-icon" style={{ background: 'var(--red, #C0392B)' }}><Icon name="i-key" /></span>
              <div style={{ flex: 1 }}>
                <b>Require authenticated user</b>
                <div className="small muted">
                  {features.require_authenticated_user
                    ? 'On — every run needs a real, gateway-verified identity, on every channel: Try it, the web app, batch runs, MCP and webhooks. A self-reported invoking_user is rejected, not merely ignored.'
                    : 'This platform does not perform OIDC login itself — an upstream gateway verifies the person and forwards a trusted identity header. Off by default; UCS permissions alone don\u2019t confirm who is actually calling.'}
                </div>
                {/* Turning this on from an app served through a proxy that
                    strips identity headers makes the agent unrunnable from
                    that same app — which reads as a broken agent rather
                    than a working gate. It is the one switch here that
                    depends on infrastructure outside the product. */}
                {/* Checked, not asserted. Whether runs from HERE will work
                    depends on the path a request takes — the same backend
                    answers differently through nginx (which strips the five
                    identity headers) than through a proxy that forwards
                    them — so the panel asks rather than warning blindly. */}
                {features.require_authenticated_user && identity !== null && (
                  identity.verified_identity ? (
                    <div className="banner info" style={{ marginTop: 8 }}>
                      <div className="small">
                        <b>Runs from this browser will work.</b> Requests from here arrive
                        as <span className="mono">{identity.verified_identity}</span>,
                        gateway-verified. Other callers still need to present the same
                        headers — batch runs, MCP and webhooks included.
                      </div>
                    </div>
                  ) : (
                    <div className="banner warn" style={{ marginTop: 8 }}>
                      <div className="small">
                        <b>Runs from this browser will be refused.</b> Requests from here
                        arrive with no gateway-verified identity, so Try it and the web app
                        will fail while this is on — along with batch runs, MCP and webhooks.
                        <div style={{ marginTop: 6 }}>{identity.reason}</div>
                      </div>
                    </div>
                  )
                )}
              </div>
              <label className="switch">
                <input type="checkbox" checked={!!features.require_authenticated_user} onChange={(e) => toggleBoolean('require_authenticated_user', e.target.checked)} /><span />
              </label>
            </div>
          </div>

          <div className="feature-row click" onClick={() => setSubModal('moderation')}>
            <div className="feature-row-h">
              <span className="feature-icon" style={{ background: 'var(--green)' }}><Icon name="i-shield" /></span>
              <div style={{ flex: 1 }}>
                <b>Content moderation</b>
                <div className="small muted">
                  {features.moderation?.enabled
                    ? `Provider: ${features.moderation.provider || 'keywords'} · ${[features.moderation.input_enabled !== false && 'input', features.moderation.output_enabled !== false && 'output'].filter(Boolean).join(' + ')}`
                    : 'A genuine match stops the flow before it ever runs — real, not decorative.'}
                </div>
              </div>
              <label className="switch" onClick={(e) => e.stopPropagation()}>
                <input type="checkbox" checked={!!features.moderation?.enabled} onChange={(e) => toggle('moderation', e.target.checked)} /><span />
              </label>
            </div>
          </div>

          <div className="small muted" style={{ margin: '18px 0 8px', textTransform: 'uppercase', letterSpacing: '.03em', fontSize: 10.5 }}>Not yet available in this build</div>
          {DEFERRED.map((f) => (
            <div className="feature-row" key={f.name} style={{ opacity: 0.55 }}>
              <div className="feature-row-h">
                <span className="feature-icon" style={{ background: 'var(--slate)' }}><Icon name={f.icon} /></span>
                <div style={{ flex: 1 }}><b>{f.name}</b><div className="small muted">{f.desc}</div></div>
                <span className="chip line sm">Coming soon</span>
              </div>
            </div>
          ))}
        </div>
      </div>
      {subModal === 'opener' && <ConversationOpenerModal agent={agent} onClose={() => setSubModal(null)} onSaved={persistAgent} />}
      {subModal === 'followup' && <FollowUpSettingsModal agent={agent} onClose={() => setSubModal(null)} onSaved={persistAgent} />}
      {subModal === 'moderation' && <ModerationSettingsModal agent={agent} onClose={() => setSubModal(null)} onSaved={persistAgent} />}
    </div>
  );
}
