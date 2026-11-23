import { useEffect, useState } from 'react';
import Modal from '../components/Modal';
import Icon from '../components/Icon';
import { Chip, KV } from '../components/ui';
import { api } from '../api';
import type { Eval, EvalCase, EvalResultRow, EvalRunRow } from '../types';

/**
 * What a row in Monitoring's "Evaluations — golden test sets" table
 * actually contains.
 *
 * The table showed a set's name and its latest pass rate and stopped
 * there: the rows were not clickable, so there was no way to see WHICH
 * cases a set applies, what each expects, or which one failed. A "7/8"
 * told you something was wrong without saying what. This joins the
 * set's cases to the latest run's per-case results.
 */
export default function EvalSetModal({ row, onClose }: { row: Eval; onClose: () => void }) {
  const [cases, setCases] = useState<EvalCase[] | null>(null);
  const [results, setResults] = useState<EvalResultRow[] | null>(null);
  const [run, setRun] = useState<EvalRunRow | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!row.set_id) { setError('This row carries no set id.'); return; }
    api.listEvalCases(row.set_id).then(setCases).catch((e) => setError(e.message));
    if (row.run_id) {
      api.getEvalRun(row.run_id)
        .then((r) => { setRun(r.run); setResults(r.results); })
        // Cases are still worth showing without the run's scores.
        .catch(() => setResults([]));
    } else {
      setResults([]);
    }
  }, [row.set_id, row.run_id]);

  const resultFor = (caseId: string) => (results || []).find((r) => r.eval_case_id === caseId);

  return (
    <Modal
      title={<><Icon name="i-check" /> {row.set}</>}
      sub={`${row.agent} · latest run ${row.passed} (${row.rate})${row.ts ? ` · ${row.ts}` : ''}`}
      onClose={onClose}
      footer={<button className="btn ghost" onClick={onClose}>Close</button>}
    >
      {error && <div className="banner warn">{error}</div>}

      {run && (
        <div className="row-split" style={{ marginBottom: 14, gap: 16, flexWrap: 'wrap' }}>
          <KV k="Drift" v={<Chip cls={run.drift === 'none' ? 'line' : run.drift === 'watch' ? 'amber' : 'red'}>{run.drift}</Chip>} />
          {/* Which published version was scored — two runs of the same
              set are only comparable if this matches. */}
          <KV k="Version scored" v={<span className="mono small">{run.agent_version || '—'}</span>} />
        </div>
      )}

      {cases === null && !error && <div className="empty">Loading cases…</div>}
      {cases && cases.length === 0 && (
        <div className="empty">
          This set has no cases yet, so a run has nothing to score. Select this agent in
          the filter above to add some.
        </div>
      )}

      {cases && cases.map((c, i) => {
        const r = resultFor(c.id);
        return (
          <div className="card pad" key={c.id} style={{ marginBottom: 10 }}>
            <div className="row-split" style={{ marginBottom: 8 }}>
              <b className="small">Case {i + 1}</b>
              {r
                ? <Chip cls={r.passed ? 'green' : 'red'}>{r.passed ? 'pass' : 'fail'}</Chip>
                : <Chip cls="line">not yet run</Chip>}
            </div>
            <KV k="Input" v={<span className="small">{c.input}</span>} />
            <KV k="Expects" v={<span className="mono small">{c.expected_output}</span>} />
            {/* Which grader ran matters: a criteria case needs a judge
                model, a substring case does not. */}
            <KV
              k="Graded by"
              v={<span className="small">{c.criteria
                ? <>LLM judge — <span className="muted">{c.criteria}</span></>
                : 'substring match (exact, so hyphens and case count)'}</span>}
            />
            {r && (
              <>
                <KV k="Actual" v={<div className="so-value small">{r.actual_output || '—'}</div>} />
                {!r.passed && r.reason && (
                  <KV k="Why it failed" v={<span className="small">{r.reason}</span>} />
                )}
              </>
            )}
          </div>
        );
      })}
    </Modal>
  );
}
