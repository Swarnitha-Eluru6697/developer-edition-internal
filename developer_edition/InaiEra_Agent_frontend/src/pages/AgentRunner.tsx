import { useEffect, useState } from 'react';
import { useParams } from 'react-router-dom';
import { api } from '../api';
import { ChatRunner, FormRunner, APP_TYPE_LABEL, isChatLikeAgent } from '../components/RunnerUI';
import type { Agent } from '../types';

export default function AgentRunner() {
  const { agentId } = useParams<{ agentId: string }>();
  const [agent, setAgent] = useState<Agent | null>(null);
  const [notFound, setNotFound] = useState(false);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    if (!agentId) return;
    api.getAgent(agentId).then(setAgent).catch(() => setNotFound(true));
  }, [agentId]);

  if (notFound) {
    return <RunnerShell><div className="empty-cfg" style={{ padding: 60 }}>This app doesn't exist.</div></RunnerShell>;
  }
  if (!agent) {
    return <RunnerShell><div className="empty-cfg" style={{ padding: 60 }}>Loading…</div></RunnerShell>;
  }
  if (agent.status !== 'published') {
    return (
      <RunnerShell agent={agent}>
        <div className="empty-cfg" style={{ padding: 60 }}>
          This app isn't published yet — there's nothing real for an end user to use here until it is.
          {' '}Builders can preview the real end-user experience anytime from the Builder's "Try it" button.
        </div>
      </RunnerShell>
    );
  }

  const appType = agent.app_type || 'workflow';

  return (
    <RunnerShell agent={agent}>
      {/* No Features control here on purpose — a real anonymous end user
          on this public URL shouldn't be able to reconfigure moderation
          word lists or the conversation opener. That's a developer/tester
          capability, scoped to the Builder's Try It panel only. The
          features THEMSELVES (opener, follow-up, citations, moderation)
          still apply here — they're just not editable from this page. */}
      {isChatLikeAgent(agent)
        ? <ChatRunner agent={agent} appType={appType} busy={busy} setBusy={setBusy} />
        : <FormRunner agent={agent} appType={appType} busy={busy} setBusy={setBusy} />}
    </RunnerShell>
  );
}

function RunnerShell({ agent, children }: { agent?: Agent | null; children: React.ReactNode }) {
  // Everything the Web App Settings dialog collects is applied here.
  //
  // It was collected and then read by nothing: features.webapp_settings
  // was written by PublishModal and had no consumer anywhere in the
  // frontend or the backend, so a saved name, icon, theme, accent
  // colour, placeholder or legal footer changed nothing about the
  // published app — while the dialog said "live immediately, no publish
  // needed".
  const ws = agent?.features?.webapp_settings || {};
  const dark = ws.theme === 'dark';
  const accent = /^#[0-9a-fA-F]{6}$/.test(ws.color || '') ? ws.color : undefined;
  const title = (ws.display_name || '').trim() || agent?.name || 'InAiEra App';
  const icon = (ws.icon || '').trim();
  const isImageIcon = /^https?:\/\//i.test(icon);
  const legal = [
    ws.privacy_policy_url && { href: ws.privacy_policy_url, label: 'Privacy' },
    ws.terms_of_service_url && { href: ws.terms_of_service_url, label: 'Terms' },
  ].filter(Boolean) as { href: string; label: string }[];

  return (
    <div
      className={`runner-shell${dark ? ' runner-shell-dark' : ''}`}
      /* The accent is applied by redefining the tokens the runner's own
         controls already use, rather than by adding a parallel set of
         colours only this page understands. */
      style={accent ? ({ ['--teal' as string]: accent, ['--teal-d' as string]: accent } as React.CSSProperties) : undefined}
    >
      <div className="runner-topbar">
        <div className="runner-brand">
          {isImageIcon
            ? <img className="runner-app-icon" src={icon} alt="" />
            : <span className="logo" style={{ width: 22, height: 22, fontSize: 13 }}>{icon || '◆'}</span>}
          <div>
            <div>{title}</div>
            {ws.description && <div className="runner-app-desc">{ws.description}</div>}
          </div>
        </div>
        {agent?.app_type && <span className="chip line">{APP_TYPE_LABEL[agent.app_type] || agent.app_type}</span>}
      </div>
      <div className="runner-body">{children}</div>
      {(ws.copyright || legal.length > 0) && (
        <div className="runner-footer">
          {ws.copyright && <span>{ws.copyright}</span>}
          {legal.map((l) => (
            <a key={l.label} href={l.href} target="_blank" rel="noopener noreferrer">{l.label}</a>
          ))}
        </div>
      )}
    </div>
  );
}
