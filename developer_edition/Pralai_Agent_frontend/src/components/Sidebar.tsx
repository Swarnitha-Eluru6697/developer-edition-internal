import { NavLink, useLocation } from 'react-router-dom';
import Icon from './Icon';
import { NAV, navPath } from '../constants';
import { useApp } from '../context/AppContext';

// Pages with no sidebar entry of their own, and the entry they belong to —
// so the Builder and Snippets pages still show Studio as the selected page.
const PARENT_OF: Record<string, string> = { '/builder': 'agents', '/snippets': 'agents' };

export default function Sidebar({ agentCount, openAlertCount }: { agentCount: number; openAlertCount: number }) {
  const { can } = useApp();
  const { pathname } = useLocation();
  const parentId = Object.entries(PARENT_OF).find(([p]) => pathname.startsWith(p))?.[1];
  return (
    <div className="sidebar">
      {NAV.map((g, gi) => {
        const items = g.items.filter((it) => can(it.permission));
        if (!items.length) return null;
        return (
          <div className="navgroup" key={gi}>
            {g.group && <div className="lbl">{g.group}</div>}
            {items.map((it) => (
              <NavLink
                key={it.id}
                to={navPath(it.id)}
                className={({ isActive }) => `navitem${isActive || parentId === it.id ? ' active' : ''}`}
              >
                <Icon name={it.icon} />
                <span>{it.label}</span>
                {it.badge === 'agents' && <span className="count">{agentCount}</span>}
                {/* A count, not a dot: how many alerts are waiting is the
                    thing you decide on, and a dot answers only whether
                    any exist. Hidden at zero — a "0" badge is noise. */}
                {it.badge === 'openAlerts' && openAlertCount > 0 && (
                  <span className="count alert">{openAlertCount}</span>
                )}
              </NavLink>
            ))}
          </div>
        );
      })}
    </div>
  );
}
