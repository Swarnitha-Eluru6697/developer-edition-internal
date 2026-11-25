import { useId, useState } from 'react';
import Modal from './Modal';

/** Ask for one value, in a side panel.
 *
 * Replaces `window.prompt`, which offers no label, no validation and no
 * hint about what the value is for.
 */
export default function PromptPanel({
  title, label, hint, placeholder, confirmLabel = 'Save', initial = '',
  onSubmit, onClose,
}: {
  title: string;
  label: string;
  hint?: string;
  placeholder?: string;
  confirmLabel?: string;
  initial?: string;
  onSubmit: (value: string) => void;
  onClose: () => void;
}) {
  const [value, setValue] = useState(initial);
  // htmlFor/id, not just adjacency: without it the label is decoration
  // and a screen reader announces the box as unlabelled.
  const id = useId();
  const ready = value.trim().length > 0;
  const submit = () => { if (ready) onSubmit(value.trim()); };
  return (
    <Modal title={title} onClose={onClose} footer={<>
      <button className="btn ghost" onClick={onClose}>Cancel</button>
      <button className="btn" onClick={submit} disabled={!ready}>{confirmLabel}</button>
    </>}>
      <div className="field">
        <label htmlFor={id}>{label}</label>
        <input id={id} autoFocus value={value} placeholder={placeholder}
          onChange={(e) => setValue(e.target.value)}
          // Enter is what someone types after filling one box; making
          // them reach for the button instead is friction for nothing.
          onKeyDown={(e) => { if (e.key === 'Enter') submit(); }} />
        {hint && <p className="small muted" style={{ margin: '6px 0 0' }}>{hint}</p>}
      </div>
    </Modal>
  );
}
