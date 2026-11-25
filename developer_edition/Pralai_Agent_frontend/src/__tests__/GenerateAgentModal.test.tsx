import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, fireEvent, waitFor, cleanup } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { AppProvider } from '../context/AppContext';
import GenerateAgentModal from '../modals/GenerateAgentModal';
import { api } from '../api';
import type { Agent } from '../types';

vi.mock('../api');
vi.mock('react-router-dom', async () => {
  const actual = await vi.importActual('react-router-dom');
  return { ...actual, useNavigate: () => mockNavigate };
});
const mockNavigate = vi.fn();

afterEach(() => cleanup());

function makeAgent(overrides?: Partial<Agent>): Agent {
  return {
    id: 'a-gen-1', name: 'Generated Agent', type: 'App', subtype: 'Custom', plane: 'App',
    owner: 'you', status: 'draft', version: 'v1', health_score: null, health_status: '-',
    model: 'Claude Sonnet 5', mode: 'agentic', app_type: 'workflow', runs_7d: 0, cost_7d: '$0.00', first_party: false,
    tags: [], description: '', updated_at: '2026-01-01T00:00:00', metrics: {}, flow: { nodes: [], edges: [] },
    ...overrides,
  } as Agent;
}

function renderModal() {
  return render(
    <MemoryRouter>
      <AppProvider><GenerateAgentModal /></AppProvider>
    </MemoryRouter>
  );
}

describe('GenerateAgentModal - real workflow generation from a plain-language prompt (§0.46, GAP_REPORT)', () => {
  beforeEach(() => { vi.clearAllMocks(); });

  it('is honest up front that this creates a draft, never publishes automatically', () => {
    renderModal();
    expect(screen.getByText(/nothing is ever published automatically/)).toBeInTheDocument();
  });

  it('refuses to generate with an empty prompt, without ever calling the real API', () => {
    renderModal();
    fireEvent.click(screen.getByText('Generate'));
    expect(screen.getByText(/Describe what you want/)).toBeInTheDocument();
    expect(api.generateAgent).not.toHaveBeenCalled();
  });

  it('calls the real API with the real prompt, name, and app_type', async () => {
    vi.mocked(api.generateAgent).mockResolvedValue(makeAgent());
    renderModal();
    fireEvent.change(screen.getByPlaceholderText(/Classify support tickets/), { target: { value: 'summarize incoming emails' } });
    fireEvent.change(screen.getByPlaceholderText('e.g. Support Ticket Triage'), { target: { value: 'Email Summarizer' } });
    fireEvent.click(screen.getByText('Chatflow'));
    fireEvent.click(screen.getByText('Generate'));

    await waitFor(() => {
      expect(api.generateAgent).toHaveBeenCalledWith({
        prompt: 'summarize incoming emails', name: 'Email Summarizer', app_type: 'chatflow',
      });
    });
  });

  it('a real, honest default name is used when none is given', async () => {
    vi.mocked(api.generateAgent).mockResolvedValue(makeAgent());
    renderModal();
    fireEvent.change(screen.getByPlaceholderText(/Classify support tickets/), { target: { value: 'do something' } });
    fireEvent.click(screen.getByText('Generate'));
    await waitFor(() => {
      expect(vi.mocked(api.generateAgent).mock.calls[0][0].name).toBe('Untitled generated agent');
    });
  });

  it('shows a real loading state while the generation request is in flight', async () => {
    let resolveFn: (a: Agent) => void = () => {};
    vi.mocked(api.generateAgent).mockReturnValue(new Promise((res) => { resolveFn = res; }));
    renderModal();
    fireEvent.change(screen.getByPlaceholderText(/Classify support tickets/), { target: { value: 'do something' } });
    fireEvent.click(screen.getByText('Generate'));
    expect(screen.getByText('Generating…')).toBeInTheDocument();
    resolveFn(makeAgent());
    await waitFor(() => expect(mockNavigate).toHaveBeenCalled());
  });

  it('a real 503 (unreachable LLM endpoint) shows the real, honest backend message, not a generic failure', async () => {
    vi.mocked(api.generateAgent).mockRejectedValue(new Error("Couldn't reach the coding agent's LLM endpoint at http://127.0.0.1:8080/v1 — is it running? (Connection error.)"));
    renderModal();
    fireEvent.change(screen.getByPlaceholderText(/Classify support tickets/), { target: { value: 'do something' } });
    fireEvent.click(screen.getByText('Generate'));
    await waitFor(() => expect(screen.getByText(/Couldn't reach the coding agent's LLM endpoint/)).toBeInTheDocument());
  });

  it('a real 422 (invalid generated flow) shows the real, honest validation message', async () => {
    vi.mocked(api.generateAgent).mockRejectedValue(new Error("Couldn't generate a real, valid workflow from that prompt: The model referenced an unknown node type: 'made_up_type'."));
    renderModal();
    fireEvent.change(screen.getByPlaceholderText(/Classify support tickets/), { target: { value: 'do something' } });
    fireEvent.click(screen.getByText('Generate'));
    await waitFor(() => expect(screen.getByText(/unknown node type/)).toBeInTheDocument());
  });

  it('on real success, navigates straight into the Builder for the new draft agent', async () => {
    vi.mocked(api.generateAgent).mockResolvedValue(makeAgent({ id: 'a-real-42' }));
    renderModal();
    fireEvent.change(screen.getByPlaceholderText(/Classify support tickets/), { target: { value: 'do something' } });
    fireEvent.click(screen.getByText('Generate'));
    await waitFor(() => expect(mockNavigate).toHaveBeenCalledWith('/builder?agent=a-real-42'));
  });
});
