import { useEffect, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { api } from '../api';
import Icon from '../components/Icon';
import { PageHead, Stat, Banner, Chip, StatusChip, HealthChip, PlaneChip } from '../components/ui';
import { P } from '../permissions';
import { useApp } from '../context/AppContext';
import type { HomeStats, Alert, Agent } from '../types';

const TYPE_COLOR: Record<string, string> = {
  Analysis: 'blue', Engineer: 'amber', 'ERD Designer': 'purple', Scientist: 'green',
  Conversational: 'teal', 'Task-Automation': 'slate',
};

export default function Home() {
  const { me, can, toast, openDrawer, dataVersion } = useApp();
  const navigate = useNavigate();
  const [stats, setStats] = useState<HomeStats | null>(null);
  const [alerts, setAlerts] = useState<Alert[]>([]);
  const [agents, setAgents] = useState<Agent[]>([]);

  useEffect(() => {
    api.getHomeStats().then(setStats).catch(() => {});
    api.listAlerts().then((a) => setAlerts(a.slice(0, 5))).catch(() => {});
    api.listAgents().then((all) => setAgents(all.filter((a) => a.plane !== 'Snippet'))).catch(() => {});
    // Same trigger as the sidebar badge, so an acknowledge/resolve moves both together.
  }, [dataVersion]);

  return (
    <div className="wrap home-pinned">
      <PageHead
        title="Overview"
        eyebrow="App Engine"
        subtitle={`Welcome back${me?.user ? `, ${me.user}` : ''}. Here's what's happening across Data and App agents.`}
        actions={<>
          {can(P.templatesView) && <button className="btn ghost" onClick={() => navigate('/templates')}><Icon name="i-grid" /> Browse templates</button>}
          {can(P.studioEdit) && <button className="btn" onClick={() => navigate('/agents')}><Icon name="i-plus" /> New agent</button>}
        </>}
      />
      {/* The header and stat cards never move: the page itself does not scroll. Everything
          below them lives in .home-body, a scroll region that STARTS under the cards, so
          nothing slides behind them — the agent list's pinned column heads included. */}
      {stats && (
        <div className="home-stats">
        <div className="grid g4">
          <Stat k="Active agents" v={stats.agents} icon="i-cube" m="live count" cls="up" />
          <Stat k="Runs today" v={stats.runsToday} icon="i-play" m={stats.successRate == null ? (stats.runsToday === 0 ? 'no runs yet today' : 'success rate n/a') : `${stats.successRate}% success rate`} cls="up" />
          <Stat k="Open alerts" v={stats.openAlerts} icon="i-bell"
                m={stats.acknowledgedAlerts ? `review needed · ${stats.acknowledgedAlerts} acknowledged` : 'review needed'}
                cls={stats.openAlerts > 0 ? 'warn' : ''} />
          {/* Says what the number is averaged over, the way Monitoring's cost cards do — the
              average covers today's runs, the same window as the Runs today card. */}
          <Stat k="Avg cost / run" v={stats.avgCost ?? '—'} icon="i-bolt"
                m={stats.avgCost == null ? 'no runs yet today'
                  : `across ${stats.runsToday.toLocaleString()} run${stats.runsToday === 1 ? '' : 's'} today`} />
        </div>
        </div>
      )}
      <div className="home-body">
      <div className="grid g2">
        <div className="card">
          <div className="card-h">
            <h3><Icon name="i-bell" /> Recent activity</h3>
            <span className="link" onClick={() => toast('This lives in UCS Catalog — a separate console')}>In UCS Catalog</span>
          </div>
          <div className="card-b feed">
            {alerts.map((a) => (
              <div className="row" key={a.id}>
                <div className="ic" style={{
                  background: a.state === 'resolved' ? 'var(--green-t)' : 'var(--amber-t)',
                  color: a.state === 'resolved' ? 'var(--green)' : 'var(--amber)',
                }}>
                  <Icon name={a.state === 'resolved' ? 'i-check' : 'i-alert'} />
                </div>
                <div>
                  <div className="tx"><b>{a.rule}</b> · {a.agent_name}</div>
                  <div className="ts mono">{a.ts}</div>
                </div>
              </div>
            ))}
          </div>
        </div>
        <div className="card">
          <div className="card-h">
            <h3><Icon name="i-cube" /> Runtime planes</h3>
            <span className="link" onClick={() => navigate('/settings')}>Settings →</span>
          </div>
          <div className="card-b">
            <div className="row-split" style={{ padding: '8px 0', borderBottom: '1px solid var(--line-2)' }}>
              <Chip cls="blue">Data plane</Chip><span className="small muted">6 agents · CPU+GPU pool</span>
            </div>
            <div className="row-split" style={{ padding: '8px 0' }}>
              <Chip cls="teal">App plane</Chip><span className="small muted">8 agents · latency-optimized pool</span>
            </div>
            <hr className="sep" />
            <Banner kind="info">Every run is governed by UCS — dual entitlement check (agent + invoking user), every hop.</Banner>
          </div>
        </div>
      </div>

      <div className="card-h" style={{ marginTop: 26 }}>
        <h3><Icon name="i-cube" /> All agents</h3>
        <span className="link" onClick={() => navigate('/agents')}>Open Studio →</span>
      </div>
      {/* Its own scroll region (bar hidden app-wide), so a long agent list stays one
          bounded card instead of stretching the page; the column heads stay pinned. */}
      <div className="card home-agents">
        <div className="home-agents-scroll">
        <table>
          <thead>
            <tr><th>Agent</th><th>Type</th><th>Plane</th><th>Status</th><th>Health</th><th>Model</th><th>Runs (7d)</th><th>Cost (7d)</th></tr>
          </thead>
          <tbody>
            {agents.map((a) => (
              <tr className="click" key={a.id} onClick={() => openDrawer('agentProfile', a.id)}>
                <td>
                  <b>{a.name}</b>{a.first_party && <Chip cls="teal">first-party</Chip>}
                  <div className="small muted">{a.subtype} · {a.owner} · {a.version}</div>
                </td>
                <td><Chip cls={TYPE_COLOR[a.subtype] || 'slate'}>{a.type}</Chip></td>
                <td><PlaneChip plane={a.plane} /></td>
                <td><StatusChip status={a.status} /></td>
                <td><HealthChip score={a.health_score} status={a.health_status} /></td>
                <td className="mono small">{a.model}</td>
                <td>{a.runs_7d}</td>
                <td className="mono">{a.cost_7d}</td>
              </tr>
            ))}
          </tbody>
        </table>
        </div>
      </div>
      </div>
    </div>
  );
}
