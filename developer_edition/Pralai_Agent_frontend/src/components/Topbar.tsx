import { useEffect, useRef, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { api } from '../api';
import { SEARCH_CLEARED } from '../searchEvents';
import Icon from './Icon';
import { DeveloperUserMenu, inDeveloperShell } from './DeveloperUserMenu';
import { useApp } from '../context/AppContext';
import { hostAccountMenu, hostRoleBadges, isHosted } from '../host';
import { AGENT_READER_PERMS, KB_READER_PERMS, P } from '../permissions';

/** Up to two initials from the signed-in user's name, for the fallback avatar. */
export function initialsOf(user: string | null | undefined): string {
  const parts = (user || '').replace(/@.*$/, '').split(/[\s._-]+/).filter(Boolean);
  if (!parts.length) return '?';
  const letters = parts.length === 1 ? parts[0].slice(0, 2) : parts[0][0] + parts[parts.length - 1][0];
  return letters.toUpperCase();
}

export default function Topbar() {
  const { me, can, canAny } = useApp();
  // Only what the user may read is searched; with neither, there is no search.
  const searchAgents = canAny(AGENT_READER_PERMS);
  const searchKbs = canAny(KB_READER_PERMS);
  const navigate = useNavigate();
  const [q, setQ] = useState('');
  const inputRef = useRef<HTMLInputElement>(null);

  // ⌘K / Ctrl+K focuses the search from any page, as the hint promises.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === 'k') { e.preventDefault(); inputRef.current?.focus(); }
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, []);
  // A page's Clear empties this box too.
  useEffect(() => {
    const clear = () => setQ('');
    window.addEventListener(SEARCH_CLEARED, clear);
    return () => window.removeEventListener(SEARCH_CLEARED, clear);
  }, []);

  // An exact agent or RAG pipeline name opens it; anything else lands on the
  // list it matches, filtered to the term.
  const search = async () => {
    const term = q.trim();
    if (!term) return;
    const t = term.toLowerCase();
    const [agents, kbs] = await Promise.all([
      searchAgents ? api.listAllAgents().catch(() => []) : Promise.resolve([]),
      searchKbs ? api.listKnowledgeBases().catch(() => []) : Promise.resolve([]),
    ]);
    const exactAgents = agents.filter((a) => a.name.toLowerCase() === t || a.id.toLowerCase() === t);
    // Results land only on pages the user may open (Studio / RAG).
    const opensStudio = can(P.studioEdit);
    const opensRag = can(P.ragManage);
    if (exactAgents.length === 1 && opensStudio) return navigate(`/builder?agent=${encodeURIComponent(exactAgents[0].id)}`);
    const exactKbs = kbs.filter((k) => k.name.toLowerCase() === t || k.id.toLowerCase() === t);
    if (exactKbs.length === 1 && !exactAgents.length && opensRag) return navigate(`/rag?open=${encodeURIComponent(exactKbs[0].id)}`);
    const agentHit = agents.some((a) => a.name.toLowerCase().includes(t));
    const kbHit = kbs.some((k) => k.name.toLowerCase().includes(t));
    const toRag = opensRag && (!opensStudio || (!agentHit && kbHit));
    if (!toRag && !opensStudio) return;
    navigate(`${toRag ? '/rag' : '/agents'}?q=${encodeURIComponent(term)}`);
  };

  return (
    <div className="topbar">
      {(searchAgents || searchKbs) && <div className="cmd">
        <Icon name="i-search" />
        <input ref={inputRef} placeholder="Search agents or RAG pipelines…" aria-label="Search agents or RAG pipelines"
          value={q} onChange={(e) => setQ(e.target.value)} onKeyDown={(e) => { if (e.key === 'Enter') search(); }} />
        <kbd>⌘K</kbd>
      </div>}
      <div className="spacer" />
      {/* Inside the Control Portal: its role strip, display only. The portal's
          rail carries the bell and the account menu, so neither is drawn here
          (an account menu is still shown if a portal passes one). */}
      {isHosted() ? (
        <>
          <div className="rolebadges">{hostRoleBadges()}</div>
          {hostAccountMenu()}
        </>
      ) : inDeveloperShell() ? null : (
        /* Inside the Developer Edition dashboard its left rail carries notifications and the account menu. */
        <>
          {can(P.alertsManage) && (
            <button type="button" className="iconbtn" title="Alerts" aria-label="Alerts" onClick={() => navigate('/alerts')}>
              <Icon name="i-bell" />
            </button>
          )}
          {/* An older portal passes its account menu instead; it takes the avatar's place. */}
          {hostAccountMenu() ?? (
            <DeveloperUserMenu name={me?.user || ''} />
          )}
        </>
      )}
    </div>
  );
}
