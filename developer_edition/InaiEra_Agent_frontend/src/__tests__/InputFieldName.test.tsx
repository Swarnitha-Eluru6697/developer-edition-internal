import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, fireEvent, waitFor, cleanup } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { AppProvider } from '../context/AppContext';
import AddInputFieldModal from '../components/AddInputFieldModal';
import ToastHost from '../components/ToastHost';
import { api } from '../api';
import type { Agent, FlowNode } from '../types';

vi.mock('../api');
afterEach(() => cleanup());

function makeAgent(): Agent {
  return {
    id: 'a1', name: 'WF', type: 'App', subtype: 'Custom', plane: 'App', owner: 'you',
    status: 'draft', version: 'v1', draft_revision: 1, health_score: null, health_status: '—',
    model: 'Claude Sonnet 5', mode: 'agentic', app_type: 'workflow', runs_7d: 0,
    cost_7d: '$0.00', first_party: false, tags: [], description: '',
    updated_at: '2026-01-01T00:00:00', metrics: {},
    flow: { nodes: [{ id: 'n1', type: 'trigger', x: 0, y: 0, sub: '', config: {} }], edges: [] },
  } as Agent;
}

const node = { id: 'n1', type: 'trigger', x: 0, y: 0, sub: '', config: {} } as FlowNode;

/**
 * A trigger input field's name is the variable's NAME, not a reference
 * to it. Typing "{{document}}" created a field literally called
 * "{{document}}", so {{document}} downstream resolved to nothing — the
 * flow ran green and every Output field came back empty, which is
 * indistinguishable from a broken upload.
 */
describe('trigger input field names', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(api.updateNodeConfig).mockResolvedValue(makeAgent());
  });

  const renderModal = () => render(
    <MemoryRouter>
      <AppProvider>
        <AddInputFieldModal agent={makeAgent()} node={node} onSaved={() => {}} onClose={() => {}} />
        <ToastHost />
      </AppProvider>
    </MemoryRouter>
  );

  const type = (value: string) => {
    fireEvent.change(screen.getByPlaceholderText('e.g. topic'), { target: { value } });
  };

  it('strips braces someone types around the name', async () => {
    renderModal();
    type('{{document}}');
    fireEvent.click(screen.getByText('Save'));
    await waitFor(() => expect(api.updateNodeConfig).toHaveBeenCalled());
    const cfg = vi.mocked(api.updateNodeConfig).mock.calls[0][2] as any;
    expect(cfg.fields[0].name).toBe('document');
  });

  it('tolerates spaces inside the braces', async () => {
    renderModal();
    type('{{  document  }}');
    fireEvent.click(screen.getByText('Save'));
    await waitFor(() => expect(api.updateNodeConfig).toHaveBeenCalled());
    const cfg = vi.mocked(api.updateNodeConfig).mock.calls[0][2] as any;
    expect(cfg.fields[0].name).toBe('document');
  });

  it('leaves a plain name untouched', async () => {
    renderModal();
    type('document');
    fireEvent.click(screen.getByText('Save'));
    await waitFor(() => expect(api.updateNodeConfig).toHaveBeenCalled());
    const cfg = vi.mocked(api.updateNodeConfig).mock.calls[0][2] as any;
    expect(cfg.fields[0].name).toBe('document');
  });

  it('refuses a name that is only braces', async () => {
    renderModal();
    type('{{}}');
    fireEvent.click(screen.getByText('Save'));
    await waitFor(() => expect(screen.getByText(/Enter a field name/)).toBeInTheDocument());
    expect(api.updateNodeConfig).not.toHaveBeenCalled();
  });

  it('shows the reference the rest of the flow will use', () => {
    renderModal();
    type('{{document}}');
    expect(screen.getByText('{{document}}')).toBeInTheDocument();
  });
});
