import { useEffect, useState } from 'react';
import { api } from '../api';
import Drawer from '../components/Drawer';
import Icon from '../components/Icon';
import { Chip } from '../components/ui';
import { useApp } from '../context/AppContext';
import type { EnvVar } from '../types';

const TYPE_LABEL: Record<string, string> = { string: 'String', number: 'Number', secret: 'Secret' };
const TYPE_CHIP: Record<string, string> = { string: 'blue', number: 'purple', secret: 'amber' };

export default function EnvVarsDrawer({ agentId }: { agentId: string }) {
  const { closeDrawer, openModal, toast } = useApp();
  const [vars, setVars] = useState<EnvVar[]>([]);
  const [loading, setLoading] = useState(true);

  const load = () => api.listEnvVars(agentId).then(setVars).catch((e) => toast(e.message)).finally(() => setLoading(false));
  useEffect(() => { load(); /* eslint-disable-next-line */ }, [agentId]);

  const onDelete = (v: EnvVar) => {
    api.deleteEnvVar(agentId, v.id).then(() => { toast(`${v.key} removed`); load(); }).catch((e) => toast(e.message));
  };

  return (
    <Drawer title="Environment Variables" onClose={closeDrawer} footer={<button className="btn ghost" onClick={closeDrawer}>Close</button>}>
      <p className="small muted" style={{ marginTop: 0 }}>
        Environment variables can be used to store private information and credentials, and referenced from any
        node's config as <span className="mono">{'{{env.KEY_NAME}}'}</span> — the real stored value is substituted
        in at run time. Secret-type values are write-only: shown once when saved, masked everywhere after.
      </p>
      <button className="btn block" style={{ marginBottom: 16 }} onClick={() => openModal('addEnvVar', agentId)}>
        <Icon name="i-plus" /> Add Variable
      </button>

      {loading && <p className="small muted">Loading…</p>}
      {!loading && vars.length === 0 && (
        <div className="empty-cfg" style={{ padding: '30px 10px' }}>No environment variables yet for this agent.</div>
      )}
      {vars.map((v) => (
        <div className="card pad" key={v.id} style={{ marginBottom: 10 }}>
          <div className="row-split">
            <div className="row-split" style={{ gap: 8, justifyContent: 'flex-start' }}>
              <span className="mono" style={{ fontWeight: 700 }}>{v.key}</span>
              <Chip cls={TYPE_CHIP[v.type]}>{TYPE_LABEL[v.type]}</Chip>
            </div>
            <button className="icon-action danger" title="Delete" aria-label={`Delete ${v.key}`} onClick={() => onDelete(v)}><Icon name="i-trash" /></button>
          </div>
          <div className="mono small muted" style={{ marginTop: 6 }}>
            {v.type === 'secret' ? v.value : v.value || <span style={{ fontStyle: 'italic' }}>(empty)</span>}
          </div>
          {v.description && <div className="small muted" style={{ marginTop: 4 }}>{v.description}</div>}
        </div>
      ))}
    </Drawer>
  );
}
