import { useState } from 'react';
import Icon from './Icon';
import { useApp } from '../context/AppContext';
import { ChatRunner, FormRunner, isChatLikeAgent } from './RunnerUI';
import FeaturesPanel from './FeaturesPanel';
import type { Agent } from '../types';

export default function TryItModal({ agent: initialAgent, onClose }: { agent: Agent; onClose: () => void }) {
  const [agent, setAgent] = useState(initialAgent);
  // The identity a preview run is attributed to. This was the literal
  // "user:builder-preview", which reads like a label but is sent to UCS
  // as a principal — and UCS decides against the groups that principal
  // belongs to. A name no IdP has ever heard of belongs to none, so
  // every preview of an agent that touches a governed object died with
  // "denied: ucs", which the runner then reported as "real failure, not
  // a placeholder". It was real, but it was this string, not the grant.
  //
  // `preview` below is what actually marks a run as builder traffic;
  // the identity does not have to be fictional to do that.
  const { endUserIdentity } = useApp();
  const [busy, setBusy] = useState(false);
  const [featuresOpen, setFeaturesOpen] = useState(false);
  const appType = agent.app_type || 'workflow';
  const isApp = agent.type === 'App';

  return (
    <div className="overlay" onClick={(e) => { if (e.target === e.currentTarget) onClose(); }}>
      <div className="tryit-modal">
        <div className="dh">
          <h3><Icon name="i-play" /> Try it — {agent.name}</h3>
          <div className="row-split" style={{ gap: 10 }}>
            {isApp && <button className="btn sm ghost" onClick={() => setFeaturesOpen(true)}><Icon name="i-cog" /> Features</button>}
            <button className="x" onClick={onClose}><Icon name="i-x" /></button>
          </div>
        </div>
        <div className="tryit-body">
          {agent.status !== 'published' && (
            <div className="banner info" style={{ margin: '0 0 12px' }}>
              {/* Wrapped in a div, like the Banner component does. .banner is
                  display:flex, so bare text and <b> placed directly inside it
                  each became a separate flex ITEM — the sentence rendered as
                  a row of side-by-side boxes with the bold values stacked
                  between them. */}
              <div>
                This agent is still <b>{agent.status}</b> — you're testing the same real flow an end user will get
                once it's published, tagged as a builder preview run, not real end-user traffic.
              </div>
            </div>
          )}
          {agent.status === 'published' && (
            <div className="banner info" style={{ margin: '0 0 12px' }}>
              <div>
                You're testing this agent's real, live draft — which may now differ from what's currently published
                (<b>{agent.version}</b>). Production traffic keeps running <b>{agent.version}</b>'s own frozen flow
                until you publish again.
              </div>
            </div>
          )}
          {isChatLikeAgent(agent)
            ? <ChatRunner agent={agent} appType={appType} busy={busy} setBusy={setBusy} invokingUser={endUserIdentity} preview />
            : <FormRunner agent={agent} appType={appType} busy={busy} setBusy={setBusy} invokingUser={endUserIdentity} preview />}
        </div>
      </div>
      {featuresOpen && <FeaturesPanel agent={agent} onClose={() => setFeaturesOpen(false)} onSaved={setAgent} />}
    </div>
  );
}
