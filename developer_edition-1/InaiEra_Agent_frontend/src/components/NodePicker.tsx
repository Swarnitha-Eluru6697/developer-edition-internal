import { useState } from 'react';
import Icon from './Icon';
import { NODE_TYPES, PALETTE_GROUPS } from '../constants';

export interface NodePickerTarget { sourceId: string; handle: string | null; x: number; y: number }

export default function NodePicker({
  target, onPick, onClose,
}: { target: NodePickerTarget; onPick: (type: string) => void; onClose: () => void }) {
  const [query, setQuery] = useState('');
  const [tab, setTab] = useState<'nodes' | 'tools' | 'snippets'>('nodes');
  const q = query.trim().toLowerCase();

  const groups = PALETTE_GROUPS
    .map((g) => ({ ...g, types: g.types.filter((t) => !q || NODE_TYPES[t].pal!.toLowerCase().includes(q)) }))
    .filter((g) => g.types.length > 0);

  return (
    <div className="overlay" style={{ background: 'transparent' }} onClick={onClose}>
      <div
        className="node-picker"
        style={{ position: 'absolute', left: Math.min(target.x + 10, window.innerWidth - 330), top: Math.max(10, target.y - 20) }}
        onClick={(e) => e.stopPropagation()}
      >
        <div className="tabs" style={{ margin: 0, padding: '0 12px' }}>
          <div className={`tab${tab === 'nodes' ? ' on' : ''}`} onClick={() => setTab('nodes')}>Nodes</div>
          <div className={`tab${tab === 'tools' ? ' on' : ''}`} onClick={() => setTab('tools')}>Tools</div>
          <div className={`tab${tab === 'snippets' ? ' on' : ''}`} onClick={() => setTab('snippets')}>Snippets</div>
        </div>
        <div style={{ padding: 10 }}>
          <div className="cmd" style={{ marginBottom: 4 }}>
            <Icon name="i-search" />
            <input autoFocus placeholder="Search nodes, agent, human, knowledge…" value={query} onChange={(e) => setQuery(e.target.value)} />
          </div>
        </div>
        <div className="node-picker-list">
          {tab !== 'nodes' && (
            <div className="empty-cfg" style={{ padding: '30px 16px' }}>
              {tab === 'tools' ? 'Connect a tool from Integrations first — installed tools will show up here.' : 'No snippets saved yet.'}
            </div>
          )}
          {tab === 'nodes' && groups.map((g) => (
            <div key={g.lbl}>
              <div className="lbl" style={{ padding: '8px 12px 4px' }}>{g.lbl}</div>
              {g.types.map((t) => (
                <div key={t} className="node-picker-item" onClick={() => onPick(t)}>
                  <i style={{ background: NODE_TYPES[t].dot }} />
                  <span>{NODE_TYPES[t].pal}</span>
                </div>
              ))}
            </div>
          ))}
          {tab === 'nodes' && groups.length === 0 && <div className="empty-cfg" style={{ padding: '30px 16px' }}>No nodes match "{query}"</div>}
        </div>
      </div>
    </div>
  );
}
