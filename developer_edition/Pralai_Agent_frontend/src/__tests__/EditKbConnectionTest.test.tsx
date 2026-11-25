import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, fireEvent, waitFor, cleanup } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { AppProvider } from '../context/AppContext';
import EditKbConnectionModal from '../modals/EditKbConnectionModal';
import { api } from '../api';
import type { KnowledgeBase } from '../types';

vi.mock('../api');
afterEach(() => cleanup());

const kb = {
  id: 'kb1', name: 'Docs KB', provider: 'pgvector',
  config: { host: 'db', port: 5432, dbname: 'kb', user: 'u', password: '••••', table: 't' },
  status: 'untested', last_error: null, last_checked_at: null, created_at: '2026-01-01T00:00:00',
} as unknown as KnowledgeBase;

describe('Edit connection — a saved credential is tested straight away', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(api.listKnowledgeBases).mockResolvedValue([kb]);
    vi.mocked(api.updateKbConnection).mockResolvedValue({ ...kb } as never);
  });

  it('runs the connection test after saving, so the pipeline does not sit as "untested"', async () => {
    vi.mocked(api.testKnowledgeBase).mockResolvedValue({ ...kb, status: 'connected', test_result: { ok: true, detail: 'Connected.' } } as never);
    render(<MemoryRouter><AppProvider><EditKbConnectionModal kbId="kb1" /></AppProvider></MemoryRouter>);
    await waitFor(() => expect(screen.getByText('Edit connection — Docs KB')).toBeInTheDocument());

    fireEvent.change(screen.getByPlaceholderText('Leave blank to keep the stored one'), { target: { value: 'new-secret' } });
    fireEvent.click(screen.getByText('Save connection'));

    await waitFor(() => expect(api.updateKbConnection).toHaveBeenCalledWith('kb1', expect.objectContaining({ password: 'new-secret' }), false));
    await waitFor(() => expect(api.testKnowledgeBase).toHaveBeenCalledWith('kb1'));
  });
});
