import { useId, useState } from 'react';
import Modal from './Modal';

/** Add one eval case, in a side panel.
 *
 * This was an inline form rendered inside every expanded set, which made
 * each set several hundred pixels tall and the whole section unreadable.
 */
export default function EvalCaseModal({
  setName, onSubmit, onClose,
}: {
  setName: string;
  onSubmit: (c: { input: string; expected_output: string; criteria?: string }) => void;
  onClose: () => void;
}) {
  const [input, setInput] = useState('');
  const [expected, setExpected] = useState('');
  const [criteria, setCriteria] = useState('');
  const ids = { input: useId(), expected: useId(), criteria: useId() };
  const ready = input.trim().length > 0 && expected.trim().length > 0;

  return (
    <Modal title="Add eval case" sub={setName} onClose={onClose} footer={<>
      <button className="btn ghost" onClick={onClose}>Cancel</button>
      <button className="btn" disabled={!ready}
        onClick={() => onSubmit({
          input: input.trim(), expected_output: expected.trim(),
          criteria: criteria.trim() || undefined,
        })}>Add case</button>
    </>}>
      <div className="field">
        <label htmlFor={ids.input}>Input</label>
        <input id={ids.input} autoFocus value={input}
          placeholder="e.g. What were Q3 revenue trends?"
          onChange={(e) => setInput(e.target.value)} />
        <p className="small muted" style={{ margin: '6px 0 0' }}>
          Sent to the live agent flow, exactly as a user would.
        </p>
      </div>
      <div className="field">
        <label htmlFor={ids.expected}>Expected output</label>
        <input id={ids.expected} value={expected} placeholder="e.g. revenue increased"
          onChange={(e) => setExpected(e.target.value)} />
        <p className="small muted" style={{ margin: '6px 0 0' }}>
          With no criteria below, this is matched as a substring — so a short,
          distinctive phrase works better than a whole sentence.
        </p>
      </div>
      <div className="field">
        <label htmlFor={ids.criteria}>Criteria <span className="muted">(optional)</span></label>
        <input id={ids.criteria} value={criteria}
          placeholder="e.g. Does the output correctly summarize the key numbers?"
          onChange={(e) => setCriteria(e.target.value)} />
        <p className="small muted" style={{ margin: '6px 0 0' }}>
          Fill this in and the case is judged by DeepEval&rsquo;s GEval via a real Claude
          call instead of a substring match. Without an <span className="mono">
          ANTHROPIC_API_KEY</span> those cases report &ldquo;could not judge&rdquo; rather
          than faking a result.
        </p>
      </div>
    </Modal>
  );
}
