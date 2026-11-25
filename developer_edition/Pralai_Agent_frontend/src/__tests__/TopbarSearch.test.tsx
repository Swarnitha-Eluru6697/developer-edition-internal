import { describe, it, expect, vi, afterEach } from 'vitest';
import { render, screen, fireEvent, waitFor, cleanup } from '@testing-library/react';
import { MemoryRouter, Route, Routes, useLocation } from 'react-router-dom';
import { AppProvider } from '../context/AppContext';
import Topbar from '../components/Topbar';
import { api } from '../api';
import type { Agent, KnowledgeBase } from '../types';
import { SearchNote } from '../components/ui';
import { grantPermissions } from './grantPermissions';
vi.mock('../api');

function Where() {
  const l = useLocation();
  return <div data-testid="where">{l.pathname + l.search}</div>;
}

const agents = [
  { id: 'a1', name: 'Support Triage Router' },
  { id: 'a2', name: 'Support FAQ Bot' },
] as Agent[];
const kbs = [{ id: 'kb_mx_large', name: 'MX large files' }] as KnowledgeBase[];

async function search(term: string) {
  grantPermissions();
  vi.mocked(api.listAllAgents).mockResolvedValue(agents);
  vi.mocked(api.listKnowledgeBases).mockResolvedValue(kbs);
  render(
    <MemoryRouter initialEntries={['/monitoring']}>
      <AppProvider>
        <Topbar />
        <Routes><Route path="*" element={<Where />} /></Routes>
      </AppProvider>
    </MemoryRouter>,
  );
  // The box appears once /api/me says the user may read agents or KBs.
  const box = await screen.findByLabelText('Search agents or RAG pipelines');
  fireEvent.change(box, { target: { value: term } });
  fireEvent.keyDown(box, { key: 'Enter' });
}

describe('top bar search', () => {
  afterEach(() => { cleanup(); vi.resetAllMocks(); });

  it('an exact agent name opens that agent, from any page', async () => {
    await search('support triage router');
    await waitFor(() => expect(screen.getByTestId('where').textContent).toBe('/builder?agent=a1'));
  });

  it('an exact RAG pipeline name opens that pipeline', async () => {
    await search('MX large files');
    await waitFor(() => expect(screen.getByTestId('where').textContent).toBe('/rag?open=kb_mx_large'));
  });

  it('a partial agent name lands on Agents filtered to it', async () => {
    await search('support');
    await waitFor(() => expect(screen.getByTestId('where').textContent).toBe('/agents?q=support'));
  });

  it('a partial name only pipelines match lands on RAG filtered to it', async () => {
    await search('large');
    await waitFor(() => expect(screen.getByTestId('where').textContent).toBe('/rag?q=large'));
  });

  it("a page's Clear also empties the top-bar box", async () => {
    await search('support');
    await waitFor(() => expect(screen.getByTestId('where').textContent).toBe('/agents?q=support'));
    render(<SearchNote term="support" onClear={() => {}} />);
    fireEvent.click(screen.getByRole('button', { name: 'Clear search' }));
    expect(screen.getByLabelText('Search agents or RAG pipelines')).toHaveValue('');
  });
});
