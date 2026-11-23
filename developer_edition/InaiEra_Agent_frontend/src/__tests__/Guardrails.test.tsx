import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, fireEvent, waitFor, cleanup } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { AppProvider } from '../context/AppContext';
import Guardrails from '../pages/Guardrails';
import { api } from '../api';
import type { GuardrailsOverview, Agent } from '../types';

vi.mock('../api');

afterEach(() => cleanup());

function makeOverview(overrides?: Partial<GuardrailsOverview>): GuardrailsOverview {
  return {
    agents: [
      {
        id: 'a1', name: 'Revenue Analysis Agent', status: 'published', app_type: 'workflow',
        moderation: { enabled: false, provider: null, input_enabled: true, output_enabled: true,
      effective: false, reason: 'off' },
        require_authenticated_user: false,
        ucs: { touches_checked_objects: true, checked_object_count: 2 },
        safety_caps: { llm_agent_node_count: 1, llm_agent_nodes_with_custom_step_limit: 0, loop_node_count: 0, loop_nodes_with_custom_max_loops: 0 },
      },
      {
        id: 'a2', name: 'Support Bot', status: 'draft', app_type: 'chatbot',
        moderation: { enabled: true, provider: 'keywords', input_enabled: true, output_enabled: true,
      effective: true, reason: '' },
        require_authenticated_user: true,
        ucs: { touches_checked_objects: false, checked_object_count: 0 },
        safety_caps: { llm_agent_node_count: 2, llm_agent_nodes_with_custom_step_limit: 1, loop_node_count: 1, loop_nodes_with_custom_max_loops: 1 },
      },
    ],
    platform_constants: {
      max_graph_steps: 50, sub_agent_hop_cap: 5, step_limit_default: 12, max_loops_default: 10,
      code_sandbox_timeout_seconds: 5, code_sandbox_memory_mb: 128, chat_history_max_turns: 20,
    },
    ...overrides,
  };
}

function renderPage() {
  return render(<MemoryRouter><AppProvider><Guardrails /></AppProvider></MemoryRouter>);
}

describe('Guardrails - real, enforced controls, not a policy document (section 0.49, GAP_REPORT)', () => {
  beforeEach(() => { vi.clearAllMocks(); });

  it('fetches and displays real per-agent guardrail status', async () => {
    vi.mocked(api.getGuardrailsOverview).mockResolvedValue(makeOverview());
    renderPage();
    await waitFor(() => expect(screen.getAllByText('Revenue Analysis Agent')[0]).toBeInTheDocument());
    expect(screen.getAllByText('Support Bot')[0]).toBeInTheDocument();
  });

  it('shows the real, live platform-wide safety constants, not placeholder numbers', async () => {
    vi.mocked(api.getGuardrailsOverview).mockResolvedValue(makeOverview());
    renderPage();
    await waitFor(() => expect(screen.getByText(/50 — fixed, not configurable/)).toBeInTheDocument());
    expect(screen.getByText(/5 — fixed, shared with UCS's own chain_depth_cap/)).toBeInTheDocument();
    expect(screen.getByText(/5s timeout · 128MB memory · no network/)).toBeInTheDocument();
  });

  it('search genuinely filters the agent lists', async () => {
    vi.mocked(api.getGuardrailsOverview).mockResolvedValue(makeOverview());
    renderPage();
    await waitFor(() => expect(screen.getAllByText('Support Bot')[0]).toBeInTheDocument());
    fireEvent.change(screen.getByPlaceholderText('Search agents…'), { target: { value: 'Revenue' } });
    expect(screen.queryByText('Support Bot')).not.toBeInTheDocument();
    expect(screen.getAllByText('Revenue Analysis Agent')[0]).toBeInTheDocument();
  });

  it('UCS Entitlement section only lists agents that genuinely touch a checked object', async () => {
    vi.mocked(api.getGuardrailsOverview).mockResolvedValue(makeOverview());
    renderPage();
    await waitFor(() => expect(screen.getByText('2 real catalog objects')).toBeInTheDocument());
    const ucsCard = screen.getByText('UCS Entitlement').closest('.card')!;
    expect(ucsCard.textContent).not.toContain('Support Bot');
  });

  it('toggling Require authenticated user calls the real API with the real plain-boolean shape', async () => {
    vi.mocked(api.getGuardrailsOverview).mockResolvedValue(makeOverview());
    vi.mocked(api.setAgentFeatures).mockResolvedValue({} as Agent);
    renderPage();
    await waitFor(() => expect(screen.getAllByText('Revenue Analysis Agent')[0]).toBeInTheDocument());

    const authCard = screen.getByText('Gateway-trust Authentication').closest('.card')!;
    const revenueRow = [...authCard.querySelectorAll('.row-split')].find((r) => r.textContent?.includes('Revenue Analysis Agent'))!;
    fireEvent.click(revenueRow.querySelector('input[type="checkbox"]')!);

    await waitFor(() => {
      expect(api.setAgentFeatures).toHaveBeenCalledWith('a1', { require_authenticated_user: true });
    });
  });

  it('opening the moderation config modal fetches the real full agent for its existing config', async () => {
    vi.mocked(api.getGuardrailsOverview).mockResolvedValue(makeOverview());
    vi.mocked(api.getAgent).mockResolvedValue({ features: { moderation: { keywords: ['spam', 'scam'] } } } as unknown as Agent);
    renderPage();
    await waitFor(() => expect(screen.getAllByText('Revenue Analysis Agent')[0]).toBeInTheDocument());

    const modCard = screen.getByText('Content Moderation').closest('.card')!;
    const configButtons = modCard.querySelectorAll('button');
    fireEvent.click(configButtons[0]);

    await waitFor(() => expect(api.getAgent).toHaveBeenCalledWith('a1'));
    await waitFor(() => expect(screen.getByDisplayValue('spam, scam')).toBeInTheDocument());
  });

  it('saving moderation config sends only keywords or custom_endpoint_url based on the real selected provider', async () => {
    vi.mocked(api.getGuardrailsOverview).mockResolvedValue(makeOverview());
    vi.mocked(api.getAgent).mockResolvedValue({ features: {} } as unknown as Agent);
    vi.mocked(api.setAgentFeatures).mockResolvedValue({} as Agent);
    renderPage();
    await waitFor(() => expect(screen.getAllByText('Revenue Analysis Agent')[0]).toBeInTheDocument());

    const modCard = screen.getByText('Content Moderation').closest('.card')!;
    fireEvent.click(modCard.querySelectorAll('button')[0]);
    await waitFor(() => expect(screen.getByText(/Content moderation — Revenue Analysis Agent/)).toBeInTheDocument());

    fireEvent.click(screen.getByText(/Keywords — a real, deterministic/));
    fireEvent.change(screen.getByPlaceholderText(/e.g. password, ssn/), { target: { value: 'foo, bar' } });
    fireEvent.click(screen.getByText('Save'));

    await waitFor(() => {
      expect(api.setAgentFeatures).toHaveBeenCalledWith('a1', {
        moderation: { enabled: false, provider: 'keywords', keywords: ['foo', 'bar'], custom_endpoint_url: undefined },
      });
    });
  });

  it('an honest error state is shown when the real fetch fails, not a silent blank page', async () => {
    vi.mocked(api.getGuardrailsOverview).mockRejectedValue(new Error('Could not reach the backend'));
    renderPage();
    await waitFor(() => expect(screen.getByText(/Couldn't load guardrails/)).toBeInTheDocument());
  });

  it('build/publish-time validation section lists the real, structural checks', async () => {
    vi.mocked(api.getGuardrailsOverview).mockResolvedValue(makeOverview());
    renderPage();
    await waitFor(() => expect(screen.getByText(/ends with a real Output node/)).toBeInTheDocument());
  });
});
