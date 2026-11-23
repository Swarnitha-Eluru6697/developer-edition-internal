import { useEffect, useState } from 'react';
import { api } from '../api';
import { ChatRunner, FormRunner, APP_TYPE_LABEL, isChatLikeAgent } from '../components/RunnerUI';
import Icon from '../components/Icon';
import SearchSelect from '../components/SearchSelect';
import { useApp } from '../context/AppContext';
import type { Agent } from '../types';

export default function WebApps() {
  const { endUserIdentity, setEndUserIdentity } = useApp();
  // Every user runs as the chosen UCS principal. Inventing a principal
  // from anything else (an earlier version synthesized "<name>-preview")
  // asks UCS about an identity it has never heard of, so the entitlement
  // check on any governed object could only deny (see
  // ucs_sync.principal_groups). What the UI shows is decided by the
  // user's permissions; which principal a run is checked as is this.
  const identity = endUserIdentity;
  const [apps, setApps] = useState<Agent[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  // Real UCS principals for the "acting as" chooser. The three options
  // here were hardcoded (user:alice, user:bob, user:nobody) and none of
  // them belonged to a group holding a grant, so both this page's access
  // check and Publish refused every object for the invoking user — not
  // because the grant was missing, but because the identity could not
  // hold one. Loaded from UCS's own membership instead.
  const [principals, setPrincipals] = useState<
    { principal: string; groups: string[]; entitled: boolean }[] | null>(null);

  useEffect(() => {
    api.listUcsPrincipals()
      .then((r) => setPrincipals(r.principals))
      // A chooser that cannot reach UCS keeps the identity already
      // selected rather than emptying itself; the page's own error path
      // below reports anything that actually matters.
      .catch(() => setPrincipals([]));
  }, []);

  useEffect(() => {
    setApps(null);
    setError(null);
    setSelectedId(null);
    api.listPublishedForUser(identity)
      .then((a) => { setApps(a); if (a.length) setSelectedId(a[0].id); })
      .catch((e) => setError(e.message));
  }, [identity]);

  const selected = apps?.find((a) => a.id === selectedId) || null;

  return (
    <div className="webapps-shell">
      <div className="webapps-rail">
        <div className="webapps-rail-head">
          <div className="small muted" style={{ textTransform: 'uppercase', letterSpacing: '.04em', fontWeight: 700 }}>Web Apps</div>
          {/* Always shown, because every run here uses this identity —
              hiding it while still using it meant the access check was
              decided by a value the page would not show you. */}
          {(
            <div style={{ marginTop: 8 }}>
              <label id="webapps-running-as-label" className="small muted" style={{ display: 'block', marginBottom: 4 }}>Running as</label>
              {/* user:nobody is kept deliberately: an identity in no group is
                  the only way to see a real denial, which is the point of
                  this page's access check. */}
              <SearchSelect labelledBy="webapps-running-as-label" noun="identity" value={endUserIdentity}
                style={{ width: '100%' }}
                options={[
                  ...(principals !== null && endUserIdentity !== 'user:nobody'
                    && !principals.some((p) => p.principal === endUserIdentity)
                    ? [{ value: endUserIdentity, label: `${endUserIdentity} (not known to UCS)` }] : []),
                  ...(principals || []).map((p) => ({
                    value: p.principal,
                    label: `${p.principal}${p.groups.length ? ` — ${p.groups.join(', ')}` : ' (no groups)'}`,
                  })),
                  { value: 'user:nobody', label: 'user:nobody (no access)' },
                ]}
                onChange={(v) => setEndUserIdentity(v)} />
            </div>
          )}
        </div>
        {error && (
          <div className="small" style={{ color: 'var(--red)', padding: '12px 14px' }}>
            Couldn't load your apps — {error}
          </div>
        )}
        {!error && apps === null && <div className="small muted" style={{ padding: '12px 14px' }}>Loading…</div>}
        {!error && apps !== null && apps.length === 0 && (
          <div className="small muted" style={{ padding: '12px 14px' }}>
            No apps are accessible to <span className="mono">{identity}</span> right now. Access is granted in UCS, not here.
          </div>
        )}
        {apps?.map((a) => (
          <div key={a.id} className={`webapps-rail-item${a.id === selectedId ? ' active' : ''}`} onClick={() => setSelectedId(a.id)}>
            <span className="webapps-rail-icon"><Icon name="i-cube" /></span>
            <span className="webapps-rail-label">{a.name}</span>
          </div>
        ))}
      </div>
      <div className="webapps-main">
        {selected ? (
          <RunnerShell agent={selected}>
            {isChatLikeAgent(selected)
              ? <ChatRunner agent={selected} appType={selected.app_type || 'workflow'} busy={busy} setBusy={setBusy} invokingUser={identity} />
              : <FormRunner agent={selected} appType={selected.app_type || 'workflow'} busy={busy} setBusy={setBusy} invokingUser={identity} />}
          </RunnerShell>
        ) : (
          !error && apps !== null && (
            <div className="empty-cfg" style={{ padding: 60 }}>Pick an app from the left to get started.</div>
          )
        )}
      </div>
    </div>
  );
}

function RunnerShell({ agent, children }: { agent: Agent; children: React.ReactNode }) {
  return (
    <div className="runner-shell" style={{ height: '100%' }}>
      <div className="runner-topbar">
        <div className="runner-brand"><span className="logo" style={{ width: 22, height: 22, fontSize: 13 }}>◆</span> {agent.name}</div>
        {agent.app_type && <span className="chip line">{APP_TYPE_LABEL[agent.app_type] || agent.app_type}</span>}
      </div>
      <div className="runner-body">{children}</div>
    </div>
  );
}
