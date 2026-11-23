import { useState } from 'react';
import Icon from './Icon';
import Modal from './Modal';
import { api } from '../api';
import { useApp } from '../context/AppContext';
import type { Agent, AgentFeatures } from '../types';

export default function ConversationOpenerModal({ agent, onClose, onSaved }: { agent: Agent; onClose: () => void; onSaved: (a: Agent) => void }) {
  const { toast } = useApp();
  const existing = agent.features?.conversation_opener;
  const [text, setText] = useState(existing?.text || '');
  const [questions, setQuestions] = useState<string[]>(existing?.opening_questions || []);
  const [newQ, setNewQ] = useState('');

  const addQuestion = () => {
    const q = newQ.trim();
    if (!q || questions.length >= 10) return;
    setQuestions([...questions, q]);
    setNewQ('');
  };
  const removeQuestion = (i: number) => setQuestions(questions.filter((_, qi) => qi !== i));

  const save = () => {
    const next: AgentFeatures = {
      ...(agent.features || {}),
      conversation_opener: { enabled: true, text, opening_questions: questions },
    };
    api.setAgentFeatures(agent.id, next).then((a) => { onSaved(a); toast('Conversation opener saved'); onClose(); }).catch((e) => toast(e.message));
  };

  return (
    <Modal title="Conversation Opener" onClose={onClose} footer={
      <><button className="btn ghost" onClick={onClose}>Cancel</button><button className="btn" onClick={save}>Save</button></>
    }>
      <div className="field">
        <label>Opener Message</label>
        <textarea rows={5} placeholder="e.g. Hi! I can help analyze your data. What would you like to know?" value={text} onChange={(e) => setText(e.target.value)} />
      </div>
      <div className="field">
        <div className="row-split" style={{ marginBottom: 6 }}>
          <label style={{ margin: 0 }}>Opening Questions</label>
          <span className="small muted">{questions.length}/10</span>
        </div>
        {questions.map((q, i) => (
          <div key={i} className="opener-question-row">
            <span className="small">{q}</span>
            <button className="icon-action danger" onClick={() => removeQuestion(i)} title="Remove" aria-label="Remove"><Icon name="i-trash" /></button>
          </div>
        ))}
        {questions.length < 10 && (
          <div className="row-split" style={{ gap: 8 }}>
            <input
              placeholder="Add a starter question…" value={newQ}
              onChange={(e) => setNewQ(e.target.value)}
              onKeyDown={(e) => { if (e.key === 'Enter') { e.preventDefault(); addQuestion(); } }}
              style={{ flex: 1 }}
            />
            <button className="btn sm ghost" onClick={addQuestion}><Icon name="i-plus" /> Add</button>
          </div>
        )}
      </div>
    </Modal>
  );
}
