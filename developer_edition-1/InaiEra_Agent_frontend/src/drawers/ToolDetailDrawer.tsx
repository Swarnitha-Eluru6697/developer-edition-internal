import { useEffect, useState } from 'react';
import { api } from '../api';
import Drawer from '../components/Drawer';
import Icon from '../components/Icon';
import { useApp } from '../context/AppContext';
import type { Tool, ToolAction } from '../types';

export default function ToolDetailDrawer({ toolName }: { toolName: string }) {
  const { closeDrawer, toolAction, setToolAction } = useApp();
  const [tool, setTool] = useState<Tool | null>(null);
  const [actions, setActions] = useState<ToolAction[]>([]);

  useEffect(() => {
    api.listTools().then((tools) => setTool(tools.find((t) => t.name === toolName) || null)).catch(() => {});
    api.getToolActions(toolName).then(setActions).catch(() => {});
  }, [toolName]);

  if (!tool) return null;

  if (toolAction) {
    const act = actions.find((a) => a.name === toolAction);
    if (!act) return null;
    return (
      <Drawer title={
        <button onClick={() => setToolAction(null)} style={{ color: 'var(--teal-d)', fontWeight: 600, display: 'flex', alignItems: 'center', gap: 5 }}>
          <Icon name="i-chev" style={{ transform: 'rotate(180deg)' }} /> BACK
        </button>
      } onClose={closeDrawer}>
        <div className="mono small muted">{tool.org}</div>
        <h3 style={{ margin: '8px 0 4px', fontFamily: 'var(--disp)' }}>{act.name}</h3>
        <p className="small muted" style={{ margin: '0 0 18px' }}>{act.desc}</p>
        <div className="tabs"><div className="tab on">Parameters</div><div className="tab">Setting</div></div>
        {act.params.map((p, i) => (
          <div key={i} style={{ marginBottom: 16 }}>
            <div><span className="mono" style={{ fontWeight: 700 }}>{p.name}</span> <span className="small muted">{p.type}</span> {p.required && <span className="chip amber" style={{ marginLeft: 4 }}>Required</span>}</div>
            <div className="small muted" style={{ marginTop: 3 }}>{p.desc}</div>
          </div>
        ))}
      </Drawer>
    );
  }

  return (
    <Drawer title={
      <div style={{ display: 'flex', gap: 12, alignItems: 'center' }}>
        <div style={{ width: 44, height: 44, borderRadius: 10, background: 'var(--paper)', border: '1px solid var(--line)', display: 'grid', placeItems: 'center', flex: 'none' }}>
          <Icon name="i-plug" />
        </div>
        <div><div style={{ fontFamily: 'var(--disp)', fontWeight: 600, fontSize: 16 }}>{tool.name}</div>
          <div className="small muted mono">{tool.org}{tool.version ? ` · v${tool.version}` : ''}</div></div>
      </div>
    } onClose={closeDrawer}>
      <p className="small muted" style={{ marginTop: 0 }}>{tool.desc}</p>
      <div className="sechead" style={{ marginTop: 0 }}>{actions.length} action{actions.length === 1 ? '' : 's'} included</div>
      {actions.length ? actions.map((a) => (
        <div className="card pad click" key={a.id} style={{ marginBottom: 8, cursor: 'pointer' }} onClick={() => setToolAction(a.name)}>
          <b className="small">{a.name}</b><div className="small muted" style={{ marginTop: 2 }}>{a.desc}</div>
        </div>
      )) : <div className="small muted">No actions listed for this tool</div>}
    </Drawer>
  );
}
