import { useEffect, useState } from 'react';
import { api } from '../api';
import Icon from './Icon';
import EvalCaseModal from './EvalCaseModal';
import PromptPanel from './PromptPanel';
import { useApp } from '../context/AppContext';
import type { EvalSet, EvalCase } from '../types';

export default function EvalManager({ agentId, agentName, onRunComplete }: { agentId: string; agentName: string; onRunComplete?: () => void }) {
  const { toast } = useApp();
  const [sets, setSets] = useState<EvalSet[]>([]);
  const [openSetId, setOpenSetId] = useState<string | null>(null);
  const [cases, setCases] = useState<Record<string, EvalCase[]>>({});
  const [running, setRunning] = useState<string | null>(null);
  const [naming, setNaming] = useState(false);
  const [addingTo, setAddingTo] = useState<EvalSet | null>(null);

  const loadSets = () => api.listEvalSets(agentId).then(setSets).catch(() => {});
  useEffect(() => { loadSets(); setOpenSetId(null); /* eslint-disable-next-line */ }, [agentId]);

  const createSet = (name: string) => {
    api.createEvalSet(name, agentId)
      .then(() => { toast(`${name} created`); loadSets(); })
      .catch((e) => toast(e.message))
      .finally(() => setNaming(false));
  };

  const toggleSet = (setId: string) => {
    if (openSetId === setId) { setOpenSetId(null); return; }
    setOpenSetId(setId);
    if (!cases[setId]) api.listEvalCases(setId).then((c) => setCases((prev) => ({ ...prev, [setId]: c }))).catch(() => {});
  };

  const addCase = (set: EvalSet, body: { input: string; expected_output: string; criteria?: string }) => {
    api.addEvalCase(set.id, body).then(() => {
      toast('Case added');
      api.listEvalCases(set.id).then((c) => setCases((prev) => ({ ...prev, [set.id]: c })));
      loadSets();
    }).catch((e) => toast(e.message)).finally(() => setAddingTo(null));
  };

  const runSet = (setId: string, setName: string) => {
    setRunning(setId);
    api.runEvalSet(setId)
      .then((r) => {
        toast(`${setName}: ${r.run.pass_count}/${r.run.total_count} passed (${r.rate})${r.run.drift !== 'none' ? ` — drift: ${r.run.drift}` : ''}`);
        onRunComplete?.();
      })
      .catch((e) => toast(e.message))
      .finally(() => setRunning(null));
  };

  return (
    <>
      <div className="row-split" style={{ marginBottom: 10 }}>
        <p className="small muted" style={{ margin: 0, maxWidth: 620 }}>
          Every case runs the live agent flow. Cases with no criteria are scored by a
          substring match; cases with criteria are judged by DeepEval&rsquo;s GEval.
        </p>
        <button className="btn sm" onClick={() => setNaming(true)}>
          <Icon name="i-plus" /> New eval set
        </button>
      </div>

      {/* One row per set rather than a padded card each — with three or
          four sets the card layout ran past the fold before the results
          table even started. */}
      <div className="card" style={{ overflow: 'hidden' }}>
        <table>
          <thead><tr><th>Eval set</th><th>Cases</th><th></th></tr></thead>
          <tbody>
            {sets.map((s) => (
              <>
                <tr key={s.id} className="click" style={{ cursor: 'pointer' }}
                    onClick={() => toggleSet(s.id)}
                    title="Show the cases in this set">
                  <td>
                    <span className={`eval-chev${openSetId === s.id ? ' open' : ''}`}>
                      <Icon name="i-chev" />
                    </span>
                    <b>{s.name}</b>
                  </td>
                  <td className="mono small muted">{s.case_count ?? 0}</td>
                  <td style={{ textAlign: 'right', whiteSpace: 'nowrap' }}>
                    <button className="btn sm ghost" onClick={(e) => { e.stopPropagation(); setAddingTo(s); }}>
                      <Icon name="i-plus" /> Case
                    </button>
                    <button className="btn sm" style={{ marginLeft: 8 }}
                      disabled={running === s.id || !s.case_count}
                      title={!s.case_count ? 'Add a case first' : 'Run this set against the live agent'}
                      onClick={(e) => { e.stopPropagation(); runSet(s.id, s.name); }}>
                      <Icon name="i-play" /> {running === s.id ? 'Running…' : 'Run'}
                    </button>
                  </td>
                </tr>
                {openSetId === s.id && (
                  <tr key={`${s.id}-cases`}>
                    <td colSpan={3} style={{ background: 'var(--paper)' }}>
                      {(cases[s.id] || []).map((c) => (
                        <div key={c.id} className="small eval-case">
                          <div><b>Input</b> <span className="mono">{c.input}</span></div>
                          <div><b>Expected</b> <span className="mono">{c.expected_output}</span></div>
                          {c.criteria && <div className="muted"><b>Criteria</b> {c.criteria} <span className="chip line">LLM-judged</span></div>}
                        </div>
                      ))}
                      {(cases[s.id] || []).length === 0 && (
                        <div className="small muted">No cases yet — a set with no cases cannot run.</div>
                      )}
                    </td>
                  </tr>
                )}
              </>
            ))}
            {sets.length === 0 && (
              <tr><td colSpan={3}>
                <div className="empty-cfg" style={{ padding: '18px 10px' }}>
                  No eval sets for {agentName} yet.
                </div>
              </td></tr>
            )}
          </tbody>
        </table>
      </div>

      {naming && (
        <PromptPanel title="New eval set" label="Name" placeholder="e.g. Grounded recall"
          hint={`Belongs to ${agentName}. Add cases to it, then run them against the live flow.`}
          confirmLabel="Create set" onSubmit={createSet} onClose={() => setNaming(false)} />
      )}
      {addingTo && (
        <EvalCaseModal setName={addingTo.name}
          onSubmit={(body) => addCase(addingTo, body)}
          onClose={() => setAddingTo(null)} />
      )}
    </>
  );
}
