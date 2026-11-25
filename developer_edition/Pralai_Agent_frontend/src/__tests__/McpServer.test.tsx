import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, fireEvent, waitFor, cleanup } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { AppProvider } from '../context/AppContext';
import AgentProfileDrawer from '../drawers/AgentProfileDrawer';
import { api } from '../api';
import type { Agent, Role, AgentLog, Trace } from '../types';

vi.mock('../api');

afterEach(() => cleanup());

function makeAgent(overrides?: Partial<Agent>): Agent {
  return {
    id: 'a5', name: 'Support Knowledge Bot', type: 'App', subtype: 'Conversational', plane: 'App',
    owner: 'team-support', status: 'published', version: 'v9', health_score: 95, health_status: 'pass',
    model: 'Claude Sonnet 5', mode: 'agentic', app_type: 'chatbot', runs_7d: 0, cost_7d: '$0.00', first_party: false,
    tags: [], description: 'Answers customer support questions.', updated_at: '2026-01-01T00:00:00',
    metrics: {}, flow: { nodes: [], edges: [] }, features: {},
    ...overrides,
  } as Agent;
}

function renderDrawer(agentId = 'a5') {
  return render(
    <MemoryRouter>
      <AppProvider><AgentProfileDrawer agentId={agentId} /></AppProvider>
    </MemoryRouter>
  );
}

async function openApiAccess() {
  await waitFor(() => expect(screen.getByText(/Agent ·/)).toBeInTheDocument());
  fireEvent.click(screen.getByText('Api Access'));
  await waitFor(() => expect(screen.getByText('MCP Server')).toBeInTheDocument());
}

describe('AgentProfileDrawer — real MCP Server publishing', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(api.getAgentRoles).mockResolvedValue([] as Role[]);
    vi.mocked(api.getAgentLogs).mockResolvedValue([] as AgentLog[]);
    vi.mocked(api.getAgentTraces).mockResolvedValue([] as Trace[]);
    vi.mocked(api.getMetrics).mockResolvedValue({ scope: 'a1', metrics: {} });
  });

  it('MCP is off by default, with no URL shown until enabled', async () => {
    vi.mocked(api.getAgent).mockResolvedValue(makeAgent());
    renderDrawer();
    await openApiAccess();
    expect(screen.queryByText('Regenerate')).not.toBeInTheDocument();
    expect(screen.queryByText(/\/mcp\//)).not.toBeInTheDocument();
  });

  it('enabling MCP calls the real toggle endpoint and shows the real generated URL', async () => {
    vi.mocked(api.getAgent).mockResolvedValue(makeAgent());
    vi.mocked(api.setMcp).mockResolvedValue({ enabled: true, url: '/mcp/a5/real-token-abc123' });
    renderDrawer();
    await openApiAccess();

    const toggles = document.querySelectorAll('.tog');
    const mcpToggle = Array.from(toggles).find((t) => t.parentElement?.textContent?.includes('Enable MCP Server'));
    fireEvent.click(mcpToggle!);

    await waitFor(() => {
      expect(api.setMcp).toHaveBeenCalledWith('a5', true);
      expect(screen.getByDisplayValue(/\/mcp\/a5\/real-token-abc123/)).toBeInTheDocument();
    });
  });

  it('a real Cursor config snippet is shown, containing the actual generated URL', async () => {
    vi.mocked(api.getAgent).mockResolvedValue(makeAgent());
    vi.mocked(api.setMcp).mockResolvedValue({ enabled: true, url: '/mcp/a5/real-token-abc123' });
    renderDrawer();
    await openApiAccess();
    const toggles = document.querySelectorAll('.tog');
    const mcpToggle = Array.from(toggles).find((t) => t.parentElement?.textContent?.includes('Enable MCP Server'));
    fireEvent.click(mcpToggle!);

    await waitFor(() => {
      expect(screen.getByText(/mcpServers/)).toBeInTheDocument();
      expect(screen.getByText(/real-token-abc123/)).toBeInTheDocument();
    });
  });

  it('Regenerate calls the real regenerate endpoint and updates the displayed URL', async () => {
    vi.mocked(api.getAgent).mockResolvedValue(makeAgent({ mcp_enabled: true }));
    vi.mocked(api.regenerateMcpToken).mockResolvedValue({ enabled: true, url: '/mcp/a5/brand-new-token' });
    renderDrawer();
    await openApiAccess();

    // §0.101, GAP_REPORT test fix — the real, plaintext MCP URL is
    // never recoverable from a plain GET (mcp_token_encrypted is
    // always masked there) — a real, already-enabled agent shows a
    // "Regenerate URL" prompt instead of a stale, no-longer-real URL.
    await waitFor(() => expect(screen.getByText(/Regenerate URL/)).toBeInTheDocument());
    fireEvent.click(screen.getByText(/Regenerate URL/));

    await waitFor(() => {
      expect(api.regenerateMcpToken).toHaveBeenCalledWith('a5');
      expect(screen.getByDisplayValue(/brand-new-token/)).toBeInTheDocument();
    });
  });

  it('Copy on the MCP URL copies the real generated URL to the clipboard', async () => {
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.assign(navigator, { clipboard: { writeText } });
    vi.mocked(api.getAgent).mockResolvedValue(makeAgent());
    vi.mocked(api.setMcp).mockResolvedValue({ enabled: true, url: '/mcp/a5/copy-me-token' });
    renderDrawer();
    await openApiAccess();
    const toggles = document.querySelectorAll('.tog');
    const mcpToggle = Array.from(toggles).find((t) => t.parentElement?.textContent?.includes('Enable MCP Server'));
    fireEvent.click(mcpToggle!);
    await waitFor(() => expect(screen.getByDisplayValue(/copy-me-token/)).toBeInTheDocument());

    const mcpInput = screen.getByDisplayValue(/copy-me-token/);
    const mcpCopyBtn = mcpInput.closest('.row-split')!.querySelector('button');
    fireEvent.click(mcpCopyBtn!);

    await waitFor(() => {
      expect(writeText).toHaveBeenCalledWith(expect.stringContaining('copy-me-token'));
    });
  });
});
