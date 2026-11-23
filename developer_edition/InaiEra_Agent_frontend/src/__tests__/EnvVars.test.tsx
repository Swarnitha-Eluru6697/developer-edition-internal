import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, fireEvent, waitFor, cleanup, within } from '@testing-library/react';
import { useEffect } from 'react';
import EnvVarsDrawer from '../drawers/EnvVarsDrawer';
import AddEnvVarModal from '../modals/AddEnvVarModal';
import { AppProvider, useApp } from '../context/AppContext';
import { api } from '../api';
import type { EnvVar } from '../types';

vi.mock('../api');

afterEach(() => cleanup());

function DrawerHarness({ agentId }: { agentId: string }) {
  const { drawer, drawerArg, modal, modalArg, openDrawer } = useApp();
  useEffect(() => {
    openDrawer('envVars', agentId);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);
  return (
    <>
      {drawer === 'envVars' && drawerArg && <EnvVarsDrawer agentId={drawerArg} />}
      {modal === 'addEnvVar' && modalArg && <AddEnvVarModal agentId={modalArg} />}
    </>
  );
}

function renderHarness(agentId = 'a1') {
  return render(<AppProvider><DrawerHarness agentId={agentId} /></AppProvider>);
}

const secretVar: EnvVar = {
  id: 2, agent_id: 'a1', key: 'API_TOKEN', type: 'secret', description: 'Auth token',
  value: '••••••••', created_at: '2026-01-01T00:00:00',
};
const stringVar: EnvVar = {
  id: 1, agent_id: 'a1', key: 'API_BASE_URL', type: 'string', description: 'Base URL',
  value: 'https://api.github.com', created_at: '2026-01-01T00:00:00',
};

describe('Environment Variables — drawer + add modal', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(api.listEnvVars).mockResolvedValue([stringVar, secretVar]);
  });

  it('lists existing variables, showing secret values masked and string values in plaintext', async () => {
    renderHarness();
    await waitFor(() => expect(api.listEnvVars).toHaveBeenCalledWith('a1'));
    await waitFor(() => expect(screen.getByText('API_BASE_URL')).toBeInTheDocument());
    expect(screen.getByText('https://api.github.com')).toBeInTheDocument();
    expect(screen.getByText('API_TOKEN')).toBeInTheDocument();
    expect(screen.getByText('••••••••')).toBeInTheDocument();
    // the real secret value must never appear anywhere in the rendered DOM
    expect(screen.queryByText(/sk-|secret-value|plaintext/i)).not.toBeInTheDocument();
  });

  it('deleting a variable calls the real API with the correct id and refreshes the list', async () => {
    vi.mocked(api.deleteEnvVar).mockResolvedValue({ deleted: 1 });
    renderHarness();
    await waitFor(() => expect(screen.getByText('API_BASE_URL')).toBeInTheDocument());

    const deleteButtons = screen.getAllByRole('button', { name: '' }); // icon-only buttons
    const row = screen.getByText('API_BASE_URL').closest('.card')!;
    const delBtn = row.querySelector('button.icon-action.danger')!;
    fireEvent.click(delBtn);

    await waitFor(() => {
      expect(api.deleteEnvVar).toHaveBeenCalledWith('a1', 1);
    });
    expect(api.listEnvVars).toHaveBeenCalledTimes(2); // initial load + refresh after delete
  });

  it('"Add Variable" opens the modal, and saving calls createEnvVar with the real form values', async () => {
    vi.mocked(api.createEnvVar).mockResolvedValue({ ...stringVar, id: 3 });
    renderHarness();
    await waitFor(() => expect(screen.getByText('Add Variable')).toBeInTheDocument());
    fireEvent.click(screen.getByText('Add Variable'));

    await waitFor(() => expect(screen.getByText('Add Environment Variable')).toBeInTheDocument());

    const modal = screen.getByText('Add Environment Variable').closest('.modal') as HTMLElement;
    fireEvent.click(within(modal).getByText('Secret'));
    fireEvent.change(screen.getByPlaceholderText('env name'), { target: { value: 'my_token' } });
    fireEvent.change(screen.getByPlaceholderText('env value'), { target: { value: 'sk-real-secret-value' } });
    fireEvent.change(screen.getByPlaceholderText('Describe the variable'), { target: { value: 'test token' } });
    fireEvent.click(screen.getByText('Save'));

    await waitFor(() => {
      expect(api.createEnvVar).toHaveBeenCalledWith('a1', {
        key: 'MY_TOKEN', value: 'sk-real-secret-value', type: 'secret', description: 'test token',
      });
    });
  });

  it('rejects a name with invalid characters before ever calling the API', async () => {
    renderHarness();
    await waitFor(() => expect(screen.getByText('Add Variable')).toBeInTheDocument());
    fireEvent.click(screen.getByText('Add Variable'));
    await waitFor(() => expect(screen.getByText('Add Environment Variable')).toBeInTheDocument());

    fireEvent.change(screen.getByPlaceholderText('env name'), { target: { value: 'bad name!' } });
    fireEvent.click(screen.getByText('Save'));

    await new Promise((r) => setTimeout(r, 50));
    expect(api.createEnvVar).not.toHaveBeenCalled();
  });
});
