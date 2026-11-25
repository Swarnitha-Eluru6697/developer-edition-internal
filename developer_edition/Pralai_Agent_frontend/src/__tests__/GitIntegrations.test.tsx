import { describe, it, expect, vi, afterEach, beforeEach } from 'vitest';
import { render, screen, waitFor, cleanup, fireEvent } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { AppProvider } from '../context/AppContext';
import Settings from '../pages/Settings';
import { downloadAgentYaml } from '../components/GitCommitPanel';
import { api } from '../api';
import type { Agent, GitConnection } from '../types';

vi.mock('../api');

afterEach(() => cleanup());

const conn = (over: Partial<GitConnection> = {}): GitConnection => ({
  id: 'git_1', name: 'Team GitHub', provider: 'github', provider_label: 'GitHub', base_url: null, username: null,
  account: 'octo', token: '••••••••', default_repo: 'acme/agents', default_branch: 'main', default_folder: 'agents',
  commit_on_download: false, created_at: null, last_checked_at: '2026-10-16T10:00:00', last_error: null,
  web_url: 'https://github.com', ...over,
});

function renderGitTab() {
  return render(<MemoryRouter initialEntries={['/settings?tab=git']}><AppProvider><Settings /></AppProvider></MemoryRouter>);
}

describe('Settings → Git integrations', () => {
  beforeEach(() => {
    vi.mocked(api.listModelProviders).mockResolvedValue([]);
    vi.mocked(api.getRuntimeConfig).mockRejectedValue(new Error('n/a'));
  });

  it('offers GitHub, GitLab and Bitbucket and lists connections without the token', async () => {
    vi.mocked(api.listGitConnections).mockResolvedValue({ connections: [conn({ commit_on_download: true })], can_store_credentials: true });
    renderGitTab();
    await waitFor(() => expect(screen.getByText('Team GitHub')).toBeInTheDocument());
    for (const p of ['GitHub', 'GitLab', 'Bitbucket']) expect(screen.getAllByText(p).length).toBeGreaterThan(0);
    expect(screen.getByText('connected')).toBeInTheDocument();
    expect(screen.getByText('commits on download')).toBeInTheDocument();
    expect(screen.getByText('acme/agents@main/agents')).toBeInTheDocument();
  });

  it('connects with a token, then continues to choosing the repository', async () => {
    vi.mocked(api.listGitConnections).mockResolvedValue({ connections: [], can_store_credentials: true });
    vi.mocked(api.createGitConnection).mockResolvedValue(conn({ default_repo: null, default_branch: null }));
    vi.mocked(api.listGitRepos).mockResolvedValue([{ full_name: 'acme/agents', default_branch: 'main', private: true }]);
    renderGitTab();
    fireEvent.click(await screen.findByRole('button', { name: /GitLab/ }));
    fireEvent.change(screen.getByPlaceholderText('Paste the token'), { target: { value: 'glpat-xyz' } });
    fireEvent.click(screen.getByRole('button', { name: 'Connect' }));
    await waitFor(() => expect(api.createGitConnection).toHaveBeenCalledWith(
      expect.objectContaining({ provider: 'gitlab', token: 'glpat-xyz' })));
    await waitFor(() => expect(screen.getByText('Default repository')).toBeInTheDocument());
    expect(api.listGitRepos).toHaveBeenCalledWith('git_1');
  });

  it('refuses to connect when the server cannot store credentials', async () => {
    vi.mocked(api.listGitConnections).mockResolvedValue({ connections: [], can_store_credentials: false });
    renderGitTab();
    await waitFor(() => expect(screen.getByText(/SECRETS_ENCRYPTION_KEY/)).toBeInTheDocument());
    expect(screen.getByRole('button', { name: /GitHub/ })).toBeDisabled();
  });
});

describe('Download YAML', () => {
  const agent = { id: 'ag1', name: 'Invoice Bot' } as Agent;
  beforeEach(() => {
    URL.createObjectURL = vi.fn(() => 'blob:x');
    URL.revokeObjectURL = vi.fn();
    vi.mocked(api.exportAgentYaml).mockResolvedValue({ text: 'name: Invoice Bot\n', filename: 'invoice-bot.yaml' });
  });

  it('only downloads when no connection commits on download', async () => {
    vi.mocked(api.listGitConnections).mockResolvedValue({ connections: [conn()], can_store_credentials: true });
    expect(await downloadAgentYaml(agent)).toBe('Downloaded invoice-bot.yaml');
    expect(api.commitAgentToGit).not.toHaveBeenCalled();
  });

  it('also commits to every "commit on download" connection and reports each', async () => {
    vi.mocked(api.commitAgentToGit).mockReset();
    vi.mocked(api.listGitConnections).mockResolvedValue({
      connections: [conn({ commit_on_download: true }), conn({ id: 'git_2', name: 'GL', commit_on_download: true })],
      can_store_credentials: true,
    });
    vi.mocked(api.commitAgentToGit)
      .mockResolvedValueOnce({ repo: 'acme/agents', branch: 'main' } as never)
      .mockRejectedValueOnce(new Error('GitLab rejected the token'));
    const msg = await downloadAgentYaml(agent);
    expect(api.commitAgentToGit).toHaveBeenCalledWith('ag1', { connection_id: 'git_1' });
    expect(api.commitAgentToGit).toHaveBeenCalledWith('ag1', { connection_id: 'git_2' });
    expect(msg).toContain('committed to acme/agents@main');
    expect(msg).toContain('GL: GitLab rejected the token');
  });
});
