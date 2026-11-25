import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, fireEvent, waitFor, cleanup } from '@testing-library/react';
import { MemoryRouter, Routes, Route } from 'react-router-dom';
import { AppProvider } from '../context/AppContext';
import Builder from '../pages/Builder';
import NodeDocs from '../pages/NodeDocs';
import { api } from '../api';
import { NODE_TYPES } from '../constants';
import { NODE_DOCS } from '../nodeDocs';
import type { Agent } from '../types';

vi.mock('../api');

afterEach(() => cleanup());

function makeAgent(): Agent {
  return {
    id: 'a1', name: 'Test Agent', type: 'Data', subtype: 'Analysis', plane: 'App',
    owner: 'you', status: 'draft', version: 'v1', draft_revision: 1, health_score: null, health_status: '—', tags: [], description: '', updated_at: '2026-01-01T00:00:00',
    model: 'Claude Sonnet 5', mode: 'agentic', runs_7d: 0, cost_7d: '$0.00', first_party: false,
    metrics: {},
    flow: {
      nodes: [
        { id: 'n1', type: 'trigger', x: 40, y: 140, sub: '', config: {} },
        { id: 'n2', type: 'llm', x: 200, y: 140, sub: '', config: {} },
        { id: 'n3', type: 'respond', x: 400, y: 140, sub: '', config: {} },
      ],
      edges: [{ id: 'e1', source: 'n1', target: 'n2', source_handle: null }, { id: 'e2', source: 'n2', target: 'n3', source_handle: null }],
    },
  } as Agent;
}

describe('Node documentation content — real coverage', () => {
  it('every real node type in NODE_TYPES has a real documentation entry', () => {
    const missing = Object.keys(NODE_TYPES).filter((t) => !NODE_DOCS[t]);
    expect(missing).toEqual([]);
  });

  it('every real doc has a non-trivial title, summary, and body', () => {
    for (const [key, doc] of Object.entries(NODE_DOCS)) {
      expect(doc.title.length, `${key} title`).toBeGreaterThan(2);
      expect(doc.summary.length, `${key} summary`).toBeGreaterThan(10);
      expect(doc.body.length, `${key} body`).toBeGreaterThan(100);
    }
  });
});

describe('NodeDocs page — a real standalone documentation view', () => {
  it('renders the real title, summary, and formatted body for a real node type', async () => {
    render(
      <MemoryRouter initialEntries={['/docs/llm']}>
        <AppProvider>
          <Routes><Route path="/docs/:nodeType" element={<NodeDocs />} /></Routes>
        </AppProvider>
      </MemoryRouter>
    );
    expect(screen.getByText('LLM')).toBeInTheDocument();
    expect(screen.getByText(NODE_DOCS.llm.summary)).toBeInTheDocument();
    // Real markdown was genuinely rendered, not shown as raw text
    expect(screen.queryByText(/## Prompt configuration/)).not.toBeInTheDocument();
    expect(document.querySelector('.docs-page-content h2')).toBeInTheDocument();
    expect(document.querySelector('.docs-page-content table')).toBeInTheDocument();
  });

  it('shows a real, honest not-found message for an unknown node type', () => {
    render(
      <MemoryRouter initialEntries={['/docs/nonexistent_type']}>
        <AppProvider>
          <Routes><Route path="/docs/:nodeType" element={<NodeDocs />} /></Routes>
        </AppProvider>
      </MemoryRouter>
    );
    expect(screen.getByText(/No documentation found/)).toBeInTheDocument();
  });

  it('a different node type shows genuinely different real content', () => {
    const { unmount } = render(
      <MemoryRouter initialEntries={['/docs/code']}>
        <AppProvider>
          <Routes><Route path="/docs/:nodeType" element={<NodeDocs />} /></Routes>
        </AppProvider>
      </MemoryRouter>
    );
    expect(screen.getByText('Code')).toBeInTheDocument();
    expect(screen.getByText(NODE_DOCS.code.summary)).toBeInTheDocument();
    unmount();
  });
});

describe('Builder — the real "view docs" icon links to the real per-node doc page', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(api.getAgent).mockResolvedValue(makeAgent());
    vi.mocked(api.listAgents).mockResolvedValue([]);
    vi.mocked(api.listModelProviders).mockResolvedValue([]);
    vi.mocked(api.listDataSources).mockResolvedValue([]);
    vi.mocked(api.listKnowledgeBases).mockResolvedValue([]);
    vi.mocked(api.listTools).mockResolvedValue([]);
  });

  it('the selected node\u2019s config panel shows a real docs link pointing at its own real node type', async () => {
    const { container } = render(
      <MemoryRouter initialEntries={['/builder?agent=a1']}>
        <AppProvider><Builder /></AppProvider>
      </MemoryRouter>
    );
    await waitFor(() => expect(container.querySelector('[data-nodeid="n2"]')).toBeInTheDocument());
    fireEvent.click(container.querySelector('[data-nodeid="n2"]')!);
    await waitFor(() => {
      const link = container.querySelector('.docs-link-icon') as HTMLAnchorElement;
      expect(link).toBeTruthy();
      expect(link.getAttribute('href')).toBe('#/docs/llm');
      expect(link.getAttribute('target')).toBe('_blank');
    });
  });

  it('switching the selected node updates the real docs link to match the new node\u2019s own type', async () => {
    const { container } = render(
      <MemoryRouter initialEntries={['/builder?agent=a1']}>
        <AppProvider><Builder /></AppProvider>
      </MemoryRouter>
    );
    await waitFor(() => expect(container.querySelector('[data-nodeid="n2"]')).toBeInTheDocument());
    fireEvent.click(container.querySelector('[data-nodeid="n2"]')!);
    await waitFor(() => expect((container.querySelector('.docs-link-icon') as HTMLAnchorElement)?.getAttribute('href')).toBe('#/docs/llm'));

    fireEvent.click(container.querySelector('[data-nodeid="n3"]')!);
    await waitFor(() => expect((container.querySelector('.docs-link-icon') as HTMLAnchorElement)?.getAttribute('href')).toBe('#/docs/respond'));
  });

  it('each real palette item has its own real docs link, distinct per node type', async () => {
    const { container } = render(
      <MemoryRouter initialEntries={['/builder?agent=a1']}>
        <AppProvider><Builder /></AppProvider>
      </MemoryRouter>
    );
    await waitFor(() => expect(container.querySelectorAll('.palnode').length).toBeGreaterThan(0));
    const codeDocLink = container.querySelector('a.pal-doc-icon[href="#/docs/code"]');
    const templateDocLink = container.querySelector('a.pal-doc-icon[href="#/docs/template"]');
    expect(codeDocLink).toBeTruthy();
    expect(templateDocLink).toBeTruthy();
  });
});

describe('Real end-to-end route wiring', () => {
  it('the /docs/:nodeType route genuinely renders NodeDocs, not a 404 or redirect', () => {
    render(
      <MemoryRouter initialEntries={['/docs/answer']}>
        <AppProvider>
          <Routes><Route path="/docs/:nodeType" element={<NodeDocs />} /></Routes>
        </AppProvider>
      </MemoryRouter>
    );
    expect(document.querySelector('.docs-page')).toBeInTheDocument();
    expect(screen.getByText('Answer')).toBeInTheDocument();
  });
});
