import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, fireEvent, waitFor, cleanup } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { AppProvider } from '../context/AppContext';
import Integrations from '../pages/Integrations';
import { api, ApiError } from '../api';
import type { MlStudioDeployment } from '../types';

vi.mock('../api', async (orig) => {
  const real = await orig<typeof import('../api')>();
  return { ...real, api: Object.fromEntries(Object.keys(real.api).map((k) => [k, vi.fn()])) };
});

const deployment = (over: Partial<MlStudioDeployment> = {}): MlStudioDeployment => ({
  modelId: 'churn-xgboost', task: 'Classification', domain: 'ml', framework: 'xgboost',
  currentVersion: 3, previousVersion: 2, healthStatus: 'healthy', runtime: 'Local (dev)', replicas: '2–8',
  canRollback: true, deployedBy: 'neha', deployedAt: '2026-11-04T10:00:00Z',
  endpointUrl: 'http://localhost:4001/api/serve/churn-xgboost/predict', endpointStatus: 'active',
  ...over,
});

const signedInAs = (user: string | null) =>
  vi.mocked(api.me).mockResolvedValue({ user, userId: user, tenant: 't1', permissions: ['agent:integrations:manage'] });

function renderPage() {
  render(<MemoryRouter><AppProvider><Integrations /></AppProvider></MemoryRouter>);
}

function openMlStudioTab() {
  renderPage();
  fireEvent.click(screen.getByRole('tab', { name: 'ML Studio models' }));
}

describe('Integrations — Model Providers sub-tabs', () => {
  beforeEach(() => {
    vi.mocked(api.listModelProviders).mockResolvedValue([]);
    signedInAs('neha');
  });
  afterEach(() => { cleanup(); vi.clearAllMocks(); });

  it('shows External providers by default, on the same row as the page tabs', async () => {
    renderPage();
    const external = screen.getByRole('tab', { name: 'External providers' });
    expect(external).toHaveAttribute('aria-selected', 'true');
    expect(external.closest('.tabs')).toBe(screen.getByText('Model Providers').closest('.tabs'));
    await waitFor(() => expect(screen.getByText(/using platform AI credits/)).toBeInTheDocument());
    expect(api.listMlStudioDeployments).not.toHaveBeenCalled();
  });

  it("lists the signed-in user's ML Studio deployments, asked for by gateway user name", async () => {
    vi.mocked(api.listMlStudioDeployments).mockResolvedValue([deployment()]);
    openMlStudioTab();
    await waitFor(() => expect(screen.getByText('churn-xgboost', { selector: 'b' })).toBeInTheDocument());
    expect(api.listMlStudioDeployments).toHaveBeenCalledWith('neha');
    expect(screen.getByText('v3')).toBeInTheDocument();
    expect(screen.getByText('http://localhost:4001/api/serve/churn-xgboost/predict')).toBeInTheDocument();
  });

  it('says so when the user has deployed nothing', async () => {
    vi.mocked(api.listMlStudioDeployments).mockResolvedValue([]);
    openMlStudioTab();
    await waitFor(() => expect(screen.getByText(/neha hasn't deployed any models/)).toBeInTheDocument());
  });

  it('hides the sub-tabs on other integration tabs', async () => {
    vi.mocked(api.listTools).mockResolvedValue([]);
    renderPage();
    fireEvent.click(screen.getByText('Tools'));
    expect(screen.queryByRole('tab', { name: 'ML Studio models' })).not.toBeInTheDocument();
  });

  it('LLM cards carry icon buttons whose text shows on hover, and save/remove the client secret', async () => {
    vi.mocked(api.listMlStudioDeployments).mockResolvedValue([deployment({ modelId: 'ticket-triage', domain: 'llm', task: 'Chat' })]);
    vi.mocked(api.listMlStudioCredentials).mockResolvedValue({ models: [] });
    vi.mocked(api.listLlmModels).mockResolvedValue({ models: [] });
    vi.mocked(api.setMlStudioCredential).mockResolvedValue({ model_id: 'ticket-triage', configured: true });
    openMlStudioTab();
    const add = await screen.findByRole('button', { name: 'Add the client secret for ticket-triage' });
    expect(add).toHaveAttribute('data-tip', 'Add the client secret for ticket-triage');
    expect(screen.getByText('Needs client secret')).toBeInTheDocument();
    fireEvent.click(add);
    fireEvent.change(screen.getByLabelText('Client secret for ticket-triage'), { target: { value: 'cs_live_abc' } });
    vi.mocked(api.listMlStudioCredentials).mockResolvedValue({ models: ['ticket-triage'] });
    fireEvent.click(screen.getByRole('button', { name: 'Save' }));
    await waitFor(() => expect(api.setMlStudioCredential).toHaveBeenCalledWith('ticket-triage', 'cs_live_abc'));
    await waitFor(() => expect(screen.getByText('Usable in agents')).toBeInTheDocument());
    expect(screen.getByRole('button', { name: /Remove the client secret for ticket-triage/ })).toHaveAttribute('data-tip');
  });

  it('says so when ML Studio is unreachable', async () => {
    vi.mocked(api.listMlStudioDeployments).mockRejectedValue(new ApiError("Couldn't reach ML Studio", 0));
    openMlStudioTab();
    await waitFor(() => expect(screen.getByText(/Couldn't reach ML Studio/)).toBeInTheDocument());
  });
});
