import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, fireEvent, waitFor, cleanup } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { AppProvider } from '../context/AppContext';
import PublishModal from '../modals/PublishModal';
import { api } from '../api';

// TOP-LEVEL role catalogue stub: PublishModal loads it on mount, and
// this file has several describe blocks with their own beforeEach, so
// stubbing inside one of them leaves the others throwing.
beforeEach(() => {
  vi.mocked(api.getAgentRoleCatalogue).mockResolvedValue([]);
});
import type { Agent, AgentVersion } from '../types';
import { openSelect, optionTexts } from './pickOption';

vi.mock('../api');
vi.mock('react-router-dom', async () => {
  const actual = await vi.importActual('react-router-dom');
  return { ...actual, useNavigate: () => mockNavigate };
});
const mockNavigate = vi.fn();

afterEach(() => cleanup());

function makeAgent(overrides?: Partial<Agent>): Agent {
  return {
    id: 'a6', name: 'Invoice Approval Flow', type: 'App', subtype: 'Task-Automation', plane: 'App',
    owner: 'you', status: 'published', version: 'v3', health_score: 97, health_status: 'pass',
    model: 'Claude Haiku 4.5', mode: 'fixed', app_type: 'workflow', runs_7d: 390, cost_7d: '$14.30', first_party: false,
    tags: [], description: 'Routes an invoice through approval.', updated_at: '2026-01-01T00:00:00',
    metrics: {}, flow: { nodes: [{ id: 'n1', type: 'trigger', x: 0, y: 0, sub: '', config: {} }], edges: [] },
    features: {},
    ...overrides,
  } as Agent;
}

function makeVersion(overrides?: Partial<AgentVersion>): AgentVersion {
  return {
    id: 2, agent_id: 'a6', version: 'v4', flow: { nodes: [], edges: [] }, app_type: 'workflow',
    features: {}, entitlement_plan: {}, published_by: 'user:you', created_at: '2026-01-01T00:00:00', is_current: true,
    ...overrides,
  } as AgentVersion;
}

function isoNoZ(msAgo: number): string {
  return new Date(Date.now() - msAgo).toISOString().replace('Z', '');
}

function renderPanel(agentId = 'a6') {
  return render(
    <MemoryRouter>
      <AppProvider><PublishModal agentId={agentId} /></AppProvider>
    </MemoryRouter>
  );
}

describe('PublishModal — real publish-status banner with three genuine states', () => {
  beforeEach(() => { vi.clearAllMocks(); });

  it('never published: shows "Current draft unpublished" with real auto-save time, Publish enabled, no Restore', async () => {
    vi.mocked(api.getAgent).mockResolvedValue(makeAgent({ status: 'draft', updated_at: '2026-01-01T00:00:00' }));
    vi.mocked(api.listAgentVersions).mockResolvedValue([]);
    renderPanel();
    await waitFor(() => expect(screen.getByText('Current draft unpublished')).toBeInTheDocument());
    expect(screen.getByText(/Auto-Saved ·/)).toBeInTheDocument();
    expect(screen.queryByText('Restore')).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Publish Update' })).not.toBeDisabled();
  });

  it('published before, no changes since: shows "Latest published", a real Restore link, and Publish genuinely disabled', async () => {
    const publishedAt = isoNoZ(3600_000);
    vi.mocked(api.getAgent).mockResolvedValue(makeAgent({ status: 'published', updated_at: publishedAt }));
    vi.mocked(api.listAgentVersions).mockResolvedValue([makeVersion({ created_at: publishedAt })]);
    renderPanel();
    await waitFor(() => expect(screen.getByText('Latest published')).toBeInTheDocument());
    expect(screen.getByText(/Published .* ago/)).toBeInTheDocument();
    expect(screen.getByText('Restore')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Publish Update' })).toBeDisabled();
  });

  it('published before, with real changes since: shows unpublished-draft state again, Publish genuinely re-enabled', async () => {
    const publishedAt = isoNoZ(7200_000);
    const editedAt = isoNoZ(60_000);
    vi.mocked(api.getAgent).mockResolvedValue(makeAgent({ status: 'published', updated_at: editedAt }));
    vi.mocked(api.listAgentVersions).mockResolvedValue([makeVersion({ created_at: publishedAt })]);
    renderPanel();
    await waitFor(() => expect(screen.getByText('Current draft unpublished')).toBeInTheDocument());
    expect(screen.getByRole('button', { name: 'Publish Update' })).not.toBeDisabled();
    expect(screen.getByText('Restore')).toBeInTheDocument();
  });

  it('clicking Restore on the status banner calls the real restore endpoint with the real latest version id', async () => {
    const publishedAt = isoNoZ(3600_000);
    vi.mocked(api.getAgent).mockResolvedValue(makeAgent({ status: 'published', updated_at: publishedAt }));
    vi.mocked(api.listAgentVersions).mockResolvedValue([makeVersion({ id: 7, created_at: publishedAt })]);
    vi.mocked(api.restoreAgentVersion).mockResolvedValue(makeAgent({ status: 'draft' }));
    renderPanel();
    await waitFor(() => expect(screen.getByText('Restore')).toBeInTheDocument());
    fireEvent.click(screen.getByText('Restore'));
    await waitFor(() => expect(api.restoreAgentVersion).toHaveBeenCalledWith('a6', 7));
  });

  it('publishing calls the real endpoint and shows the real resolved entitlement plan', async () => {
    vi.mocked(api.getAgent).mockResolvedValueOnce(makeAgent({ status: 'draft' })).mockResolvedValueOnce(makeAgent({ status: 'published' }));
    vi.mocked(api.listAgentVersions).mockResolvedValue([]);
    vi.mocked(api.publishAgent).mockResolvedValue({ version: 'v4', entitlement_plan: { objects_checked: ['a', 'b'] }, audited_as: 'agent:svc-a6' });
    renderPanel();
    await waitFor(() => expect(screen.getByRole('button', { name: 'Publish Update' })).toBeInTheDocument());
    fireEvent.click(screen.getByRole('button', { name: 'Publish Update' }));
    await waitFor(() => {
      // The publishing identity is sent explicitly now: publish runs a
      // UCS entitlement check for the invoking user as well as the
      // agent's service principal, and with nothing sent the backend
      // fell back to "user:demo" — a principal that can hold no grant.
      expect(api.publishAgent).toHaveBeenCalledWith('a6', expect.any(String));
      expect(screen.getByText(/Published — v4 is now live/)).toBeInTheDocument();
    });
  });
});

describe('PublishModal — the rest of the real comprehensive Publish panel', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(api.listAgentVersions).mockResolvedValue([]);
  });

  it('shows the real Web App, Backend Service API, and MCP Server cards', async () => {
    vi.mocked(api.getAgent).mockResolvedValue(makeAgent());
    renderPanel();
    await waitFor(() => {
      expect(screen.getByText('Web App')).toBeInTheDocument();
      expect(screen.getByText('Backend Service API')).toBeInTheDocument();
      expect(screen.getByText('MCP Server')).toBeInTheDocument();
    });
  });

  it('Web App and Backend Service API toggle independently via real, separate calls', async () => {
    vi.mocked(api.getAgent).mockResolvedValue(makeAgent());
    vi.mocked(api.setPublishSurface).mockImplementation((id, surface, enabled) =>
      Promise.resolve(makeAgent({ features: { publish: { [surface]: enabled } } }))
    );
    renderPanel();
    await waitFor(() => expect(screen.getByText('Web App')).toBeInTheDocument());

    const webAppToggle = screen.getByRole('switch', { name: 'Web App' });
    fireEvent.click(webAppToggle);
    await waitFor(() => expect(api.setPublishSurface).toHaveBeenCalledWith('a6', 'web_app', false));

    const apiToggle = screen.getByRole('switch', { name: 'Backend Service API' });
    fireEvent.click(apiToggle);
    await waitFor(() => expect(api.setPublishSurface).toHaveBeenCalledWith('a6', 'api', false));
  });

  it('enabling MCP Server shows the real generated URL and Regenerate button', async () => {
    vi.mocked(api.getAgent)
      .mockResolvedValueOnce(makeAgent())
      .mockResolvedValueOnce(makeAgent({ mcp_enabled: true }));
    vi.mocked(api.setMcp).mockResolvedValue({ enabled: true, url: '/mcp/a6/real-token-xyz' });
    renderPanel();
    await waitFor(() => expect(screen.getByText('MCP Server')).toBeInTheDocument());

    const mcpToggle = screen.getByRole('switch', { name: 'MCP Server' });
    fireEvent.click(mcpToggle);

    await waitFor(() => {
      expect(api.setMcp).toHaveBeenCalledWith('a6', true);
      expect(screen.getByDisplayValue(/real-token-xyz/)).toBeInTheDocument();
      expect(screen.getByText('Regenerate')).toBeInTheDocument();
    });
  });

  it('Export DSL calls the real export endpoint with the real agent id', async () => {
    vi.mocked(api.getAgent).mockResolvedValue(makeAgent());
    vi.mocked(api.exportDsl).mockResolvedValue({ dsl_version: '1', name: 'Invoice Approval Flow', flow: { nodes: [], edges: [] } });
    global.URL.createObjectURL = vi.fn(() => 'blob:mock');
    global.URL.revokeObjectURL = vi.fn();
    renderPanel();
    await waitFor(() => expect(screen.getByText('Export DSL')).toBeInTheDocument());
    fireEvent.click(screen.getByText('Export DSL'));
    await waitFor(() => expect(api.exportDsl).toHaveBeenCalledWith('a6'));
  });

  it('Duplicate calls the real endpoint and navigates to the new real agent', async () => {
    vi.mocked(api.getAgent).mockResolvedValue(makeAgent());
    vi.mocked(api.duplicateAgent).mockResolvedValue(makeAgent({ id: 'a99', name: 'Invoice Approval Flow (Copy)' }));
    renderPanel();
    await waitFor(() => expect(screen.getByText('Duplicate')).toBeInTheDocument());
    fireEvent.click(screen.getByText('Duplicate'));
    await waitFor(() => {
      expect(api.duplicateAgent).toHaveBeenCalledWith('a6');
      expect(mockNavigate).toHaveBeenCalledWith('/builder?agent=a99');
    });
  });

  it('a real generated QR code image appears after clicking QR', async () => {
    vi.mocked(api.getAgent).mockResolvedValue(makeAgent());
    renderPanel();
    await waitFor(() => expect(screen.getByText('QR')).toBeInTheDocument());
    fireEvent.click(screen.getByText('QR'));
    await waitFor(() => {
      const img = screen.getByAltText('QR code for the real public URL') as HTMLImageElement;
      expect(img.src.startsWith('data:image/')).toBe(true);
    });
  });

  it('a real embed snippet, containing the real per-agent URL, shows when Embedded is clicked', async () => {
    vi.mocked(api.getAgent).mockResolvedValue(makeAgent());
    renderPanel();
    await waitFor(() => expect(screen.getByText('Embedded')).toBeInTheDocument());
    fireEvent.click(screen.getByText('Embedded'));
    await waitFor(() => {
      expect(screen.getByText(/<iframe/)).toBeInTheDocument();
      expect(screen.getByText(/#\/app\/a6/)).toBeInTheDocument();
    });
  });

  it('Version History section shows the real fetched versions when expanded', async () => {
    vi.mocked(api.getAgent).mockResolvedValue(makeAgent());
    vi.mocked(api.listAgentVersions).mockResolvedValue([
      makeVersion({ id: 2, version: 'v4', created_at: '2026-02-01T10:00:00', is_current: true }),
      makeVersion({ id: 1, version: 'v3', created_at: '2026-01-15T09:00:00', is_current: false }),
    ]);
    renderPanel();
    await waitFor(() => expect(screen.getByText('Version History')).toBeInTheDocument());
    fireEvent.click(screen.getByText('Version History'));
    await waitFor(() => {
      expect(screen.getByText('v4')).toBeInTheDocument();
      expect(screen.getByText('v3')).toBeInTheDocument();
      expect(screen.getByText('current')).toBeInTheDocument();
    });
  });

  it('in Version History, only prior (non-current) versions get their own Restore button', async () => {
    vi.mocked(api.getAgent).mockResolvedValue(makeAgent());
    vi.mocked(api.listAgentVersions).mockResolvedValue([
      makeVersion({ id: 2, version: 'v4', created_at: '2026-02-01T10:00:00', is_current: true }),
      makeVersion({ id: 1, version: 'v3', created_at: '2026-01-15T09:00:00', is_current: false }),
    ]);
    renderPanel();
    await waitFor(() => expect(screen.getByText('Version History')).toBeInTheDocument());
    fireEvent.click(screen.getByText('Version History'));
    await waitFor(() => {
      const restores = screen.getAllByText('Restore');
      expect(restores.length).toBeGreaterThanOrEqual(1);
    });
  });

  it('an honest empty state shows when no versions have ever been published', async () => {
    vi.mocked(api.getAgent).mockResolvedValue(makeAgent({ status: 'draft' }));
    vi.mocked(api.listAgentVersions).mockResolvedValue([]);
    renderPanel();
    await waitFor(() => expect(screen.getByText('Version History')).toBeInTheDocument());
    fireEvent.click(screen.getByText('Version History'));
    await waitFor(() => expect(screen.getByText(/No published versions yet/)).toBeInTheDocument());
  });
});

describe('PublishModal — real UCS Access card', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(api.listAgentVersions).mockResolvedValue([]);
  });

  it('shows real "synced" status and the real registered fqn when the agent synced successfully', async () => {
    vi.mocked(api.getAgent).mockResolvedValue(makeAgent({ ucs_synced: true, ucs_fqn: 'agent_catalog.agents.a6', ucs_sync_error: null }));
    renderPanel();
    await waitFor(() => expect(screen.getByText('UCS Access')).toBeInTheDocument());
    expect(screen.getByText('synced')).toBeInTheDocument();
    expect(screen.getByText('agent_catalog.agents.a6')).toBeInTheDocument();
    expect(screen.queryByText('Retry sync')).not.toBeInTheDocument();
  });

  it('shows an honest "not synced" status with the real error and a real Retry sync button when sync failed', async () => {
    vi.mocked(api.getAgent).mockResolvedValue(makeAgent({
      ucs_synced: false, ucs_fqn: null,
      ucs_sync_error: 'UCS_ADMIN_TOKEN is not set — syncing agents into UCS\'s catalog requires a real platform-admin bearer token from your deployment\'s IdP. Nothing was synced.',
    }));
    renderPanel();
    await waitFor(() => expect(screen.getByText('not synced')).toBeInTheDocument());
    expect(screen.getByText(/UCS_ADMIN_TOKEN is not set/)).toBeInTheDocument();
    expect(screen.getByText('Retry sync')).toBeInTheDocument();
  });

  it('Retry sync calls the real endpoint and updates the real status on success', async () => {
    vi.mocked(api.getAgent).mockResolvedValue(makeAgent({ ucs_synced: false, ucs_sync_error: 'not yet synced' }));
    vi.mocked(api.retryUcsSync).mockResolvedValue(makeAgent({ ucs_synced: true, ucs_fqn: 'agent_catalog.agents.a6', ucs_sync_error: null }));
    renderPanel();
    await waitFor(() => expect(screen.getByText('Retry sync')).toBeInTheDocument());
    fireEvent.click(screen.getByText('Retry sync'));
    await waitFor(() => {
      expect(api.retryUcsSync).toHaveBeenCalledWith('a6');
      expect(screen.getByText('synced')).toBeInTheDocument();
    });
  });

  it('Grant calls the real endpoint with the real group name and agent role, and shows the real result', async () => {
    vi.mocked(api.getAgent).mockResolvedValue(makeAgent({ ucs_synced: true, ucs_fqn: 'agent_catalog.agents.a6' }));
    vi.mocked(api.grantUcsAccess).mockResolvedValue({ status: 'bound', group: 'support-team', role: 'agent-a6-select' });
    renderPanel();
    await waitFor(() => expect(screen.getByPlaceholderText('group name')).toBeInTheDocument());

    fireEvent.change(screen.getByPlaceholderText('group name'), { target: { value: 'support-team' } });
    fireEvent.click(screen.getByText('Grant'));

    await waitFor(() => {
      expect(api.grantUcsAccess).toHaveBeenCalledWith('a6', { group_name: 'support-team', role: 'use_agent' });
      expect(screen.getByText(/Granted use_agent on this agent to real group "support-team"/)).toBeInTheDocument();
    });
  });

  it('Revoke calls the real revoke endpoint with the same real group and agent role', async () => {
    vi.mocked(api.getAgent).mockResolvedValue(makeAgent({ ucs_synced: true }));
    vi.mocked(api.revokeUcsAccess).mockResolvedValue({ status: 'revoked', group: 'support-team', role: 'agent-a6-select' });
    renderPanel();
    await waitFor(() => expect(screen.getByPlaceholderText('group name')).toBeInTheDocument());

    fireEvent.change(screen.getByPlaceholderText('group name'), { target: { value: 'support-team' } });
    fireEvent.click(screen.getByText('Revoke'));

    await waitFor(() => {
      expect(api.revokeUcsAccess).toHaveBeenCalledWith('a6', { group_name: 'support-team', role: 'use_agent' });
      expect(screen.getByText(/Revoked use_agent on this agent from real group "support-team"/)).toBeInTheDocument();
    });
  });

  it('Grant is refused with a real client-side check when no group name is entered', async () => {
    vi.mocked(api.getAgent).mockResolvedValue(makeAgent({ ucs_synced: true }));
    renderPanel();
    await waitFor(() => expect(screen.getByText('Grant')).toBeInTheDocument());
    expect(screen.getByText('Grant').closest('button')).toBeDisabled();
    expect(api.grantUcsAccess).not.toHaveBeenCalled();
  });

  it("offers the agent roles the server defines — UCS's own agent privileges", async () => {
    // The roles are served from app/agent_roles.py and are exactly what
    // UCS shows for an agent: USE_AGENT / CREATE_AGENT_VERSION / CREATE_AGENT.
    vi.mocked(api.getAgentRoleCatalogue).mockResolvedValue([
      { name: 'use_agent', label: 'USE_AGENT', summary: 'See and run the agent.',
        inherits: null, ucs_access_level: 'select',
        permissions: [{ key: 'agent:invoke', summary: 'Open and run the agent', enforced: true }] },
      { name: 'create_agent_version', label: 'CREATE_AGENT_VERSION', summary: 'Edit and publish.',
        inherits: 'use_agent', ucs_access_level: 'writer',
        permissions: [{ key: 'agent:write', summary: "Edit the agent's flow", enforced: false }] },
    ] as never);
    vi.mocked(api.getAgent).mockResolvedValue(makeAgent({ ucs_synced: true }));
    renderPanel();
    await waitFor(() => expect(screen.getByText('UCS Access')).toBeInTheDocument());

    const select = await screen.findByRole('button', { name: 'Access level' });
    await waitFor(() => expect(select).toHaveTextContent('USE_AGENT'));
    // The dropdown shows each role by its label; one entry per served role.
    openSelect(select);
    expect(optionTexts()).toEqual(['USE_AGENT', 'CREATE_AGENT_VERSION']);

    // The permissions are shown, and one that gates nothing yet says so
    // rather than reading as protection.
    expect(await screen.findByText(/agent:invoke/)).toBeTruthy();
    expect(screen.getByText(/Sent to UCS as/)).toBeTruthy();
  });
});
