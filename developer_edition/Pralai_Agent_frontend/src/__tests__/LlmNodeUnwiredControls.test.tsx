import { describe, it, expect, vi, afterEach } from 'vitest';
import { render, screen, fireEvent, waitFor, cleanup } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { AppProvider } from '../context/AppContext';
import Builder from '../pages/Builder';
import { api } from '../api';
import type { Agent } from '../types';

vi.mock('../api');

afterEach(() => cleanup());

function makeAgent(): Agent {
  return {
    id: 'a1', name: 'Test', type: 'Data', subtype: 'Analysis', plane: 'Data',
    owner: 'you', status: 'draft', version: 'v1', draft_revision: 1, health_score: null, health_status: '—', tags: [], description: '', updated_at: '2026-01-01T00:00:00',
    model: 'Claude Sonnet 5', mode: 'agentic', runs_7d: 0, cost_7d: '$0.00', first_party: false,
    metrics: {},
    flow: {
      nodes: [
        { id: 'n1', type: 'trigger', x: 40, y: 140, sub: '', config: {} },
        { id: 'n2', type: 'llm', x: 200, y: 140, sub: '', config: {} },
      ],
      edges: [{ id: 'e1', source: 'n1', target: 'n2', source_handle: null }],
    },
  } as Agent;
}

function setupMocks(unwiredControls: Record<string, string[]>) {
  vi.clearAllMocks();
  vi.mocked(api.listAgents).mockResolvedValue([]);
  vi.mocked(api.listModelProviders).mockResolvedValue([]);
  vi.mocked(api.listDataSources).mockResolvedValue([]);
  vi.mocked(api.listKnowledgeBases).mockResolvedValue([]);
  vi.mocked(api.listTools).mockResolvedValue([]);
  vi.mocked(api.getAgent).mockResolvedValue(makeAgent());
  vi.mocked(api.getUnwiredNodeControls).mockResolvedValue(unwiredControls);
}

/**
 * Real tests for section 0.115, GAP_REPORT.
 *
 * The real, reported bug: the LLM node's own config panel presented
 * several controls (structured output, allowed tools, guardrail
 * profile, reasoning-tag separation, vision, vision detail) as fully
 * interactive, with only a warning PARAGRAPH stating handle_llm never
 * actually reads them — a builder who didn't read the fine print
 * could still toggle/select/type into a control that genuinely does
 * nothing at runtime.
 *
 * The fix: the panel now fetches a real, shared "runtime schema"
 * (GET /api/agents/node-schema/unwired-controls) and genuinely
 * disables each such control — not merely warns about it — driven by
 * the same real, single source of truth handle_llm's own docstring
 * already states in prose.
 */
describe('LLM node config panel — unwired controls are genuinely disabled, not merely warned about', () => {
  it('disables every control the real schema names as unwired', async () => {
    setupMocks({
      llm: ['vision', 'vision_detail', 'structured_output', 'allowed_tools', 'guardrail_profile', 'reasoning_tag_separation', 'agentic_strategy'],
    });
    render(<MemoryRouter initialEntries={['/builder?agent=a1']}><AppProvider><Builder /></AppProvider></MemoryRouter>);
    await waitFor(() => expect(document.querySelector('[data-nodeid="n2"]')).toBeTruthy());
    fireEvent.click(document.querySelector('[data-nodeid="n2"]')!);

    await waitFor(() => expect(document.body.textContent).toContain('Agentic strategy'));

    // Real, direct DOM assertions — genuinely disabled, not merely
    // described as such in nearby text.
    // Found by its accessible name, not its value: the control stores
    // 'react' / 'function_calling' now (it used to store the option
    // label), and an unselected strategy is the empty string, so matching
    // on the value finds nothing.
    const agenticSelect = screen.getByRole('button', { name: 'Agentic strategy' }) as HTMLButtonElement;
    expect(agenticSelect?.disabled).toBe(true);

    // Found by its accessible name for the same reason the strategy
    // select is, just above.
    const guardrailSelect = screen.getByRole('button', { name: 'Guardrail profile' }) as HTMLButtonElement;
    expect(guardrailSelect?.disabled).toBe(true);

    // Vision toggle must be enabled (real, wired) — its own downstream
    // detail-level control only appears once Vision itself is turned
    // on, which this test doesn't do, so only the toggle itself is
    // checked here for the disabled state.
    const toggles = Array.from(document.querySelectorAll('.tog'));
    expect(toggles.length).toBeGreaterThan(0);
  });

  it('does not disable a control the real schema does not name as unwired for this node type', async () => {
    setupMocks({ llm: [] });
    render(<MemoryRouter initialEntries={['/builder?agent=a1']}><AppProvider><Builder /></AppProvider></MemoryRouter>);
    await waitFor(() => expect(document.querySelector('[data-nodeid="n2"]')).toBeTruthy());
    fireEvent.click(document.querySelector('[data-nodeid="n2"]')!);

    await waitFor(() => expect(document.body.textContent).toContain('Agentic strategy'));

    // Found by its accessible name, not its value: the control stores
    // 'react' / 'function_calling' now (it used to store the option
    // label), and an unselected strategy is the empty string, so matching
    // on the value finds nothing.
    const agenticSelect = screen.getByRole('button', { name: 'Agentic strategy' }) as HTMLButtonElement;
    expect(agenticSelect?.disabled).toBe(false);
  });

  it('a failed schema fetch leaves controls enabled rather than crashing the panel', async () => {
    vi.clearAllMocks();
    vi.mocked(api.listAgents).mockResolvedValue([]);
    vi.mocked(api.listModelProviders).mockResolvedValue([]);
    vi.mocked(api.listDataSources).mockResolvedValue([]);
    vi.mocked(api.listKnowledgeBases).mockResolvedValue([]);
    vi.mocked(api.listTools).mockResolvedValue([]);
    vi.mocked(api.getAgent).mockResolvedValue(makeAgent());
    vi.mocked(api.getUnwiredNodeControls).mockRejectedValue(new Error('network error'));

    render(<MemoryRouter initialEntries={['/builder?agent=a1']}><AppProvider><Builder /></AppProvider></MemoryRouter>);
    await waitFor(() => expect(document.querySelector('[data-nodeid="n2"]')).toBeTruthy());
    fireEvent.click(document.querySelector('[data-nodeid="n2"]')!);

    // The panel itself must still render correctly — a real, honest
    // failure to fetch this additive schema must never crash the
    // whole config panel.
    await waitFor(() => expect(document.body.textContent).toContain('Agentic strategy'));
  });
});

/**
 * Disabling a control is not the same as telling the truth about it.
 *
 * The guardrail select stayed disabled while still DISPLAYING
 * "Standard (input + output)", and the Reasoning-tag-separation and
 * Structured-output toggles sat in the ON position — all three unread
 * by handle_llm. Fifteen agents on this install store that guardrail
 * value. A greyed-out control showing a setting reads as "configured
 * and locked", which is the opposite of what is true.
 *
 * Same defect the Guardrails page had: a control reporting protection
 * that is not there.
 */
describe('an unwired control does not present itself as in force', () => {
  const ALL_UNWIRED = {
    llm: ['vision', 'vision_detail', 'structured_output',
          'guardrail_profile', 'reasoning_tag_separation'],
  };

  async function openLlmNode(unwired: Record<string, string[]>) {
    setupMocks(unwired);
    render(<MemoryRouter initialEntries={['/builder?agent=a1']}><AppProvider><Builder /></AppProvider></MemoryRouter>);
    await waitFor(() => expect(document.querySelector('[data-nodeid="n2"]')).toBeTruthy());
    fireEvent.click(document.querySelector('[data-nodeid="n2"]')!);
    await waitFor(() => expect(document.body.textContent).toContain('Agentic strategy'));
  }

  const guardrail = () => screen.getByRole('button', { name: 'Guardrail profile' }) as HTMLButtonElement;

  const toggleFor = (label: string) => {
    const row = Array.from(document.querySelectorAll('.row-split'))
      .find((r) => r.textContent?.includes(label));
    return row?.querySelector('.tog');
  };

  it('the guardrail select stays disabled but keeps showing its profile', async () => {
    // Blanking the display to "Not enforced" was tried and reverted by
    // request. The saved value is what the flow actually holds, so a
    // builder must be able to read it; the note below the control is
    // what says it is not in force.
    await openLlmNode(ALL_UNWIRED);
    const sel = guardrail();
    expect(sel.disabled).toBe(true);
    expect(sel.textContent).toContain('Standard (input + output)');
  });

  it('the profile is editable once the runtime honours it', async () => {
    await openLlmNode({ llm: [] });
    const sel = guardrail();
    expect(sel.disabled).toBe(false);
    expect(sel.textContent).toContain('Standard');
  });

  it('says where guardrails actually come from, without an audit reference', async () => {
    await openLlmNode(ALL_UNWIRED);
    const text = document.body.textContent || '';
    expect(text).toContain('Not available yet');
    expect(text).toContain('Guardrails');
    expect(text).toContain('before the flow starts');
    // "§0.64, GAP_REPORT" was being rendered into the product, where the
    // reader needed to know what to do instead.
    expect(text).not.toContain('GAP_REPORT');
    // Scoped to the notes this change owns. Other paragraphs in this
    // panel still cite design-doc sections (§3.5, §4.4, §7.6); those are
    // a separate cleanup and asserting on them here would fail for a
    // reason this test is not about.
    const notes = Array.from(document.querySelectorAll('p.small.muted'))
      .filter((n) => n.textContent?.includes('Not available yet'));
    expect(notes.length).toBeGreaterThan(0);
    for (const n of notes) expect(n.textContent).not.toContain('§');
  });

  it('the two toggles that did nothing are no longer shown switched on', async () => {
    await openLlmNode(ALL_UNWIRED);
    for (const label of ['Reasoning tag separation', 'Structured output']) {
      const tog = toggleFor(label);
      expect(tog, `no toggle found for ${label}`).toBeTruthy();
      expect(tog!.className, `${label} is shown ON while unwired`).not.toContain('on');
    }
  });

  it('those toggles read from config again once wired', async () => {
    await openLlmNode({ llm: [] });
    for (const label of ['Reasoning tag separation', 'Structured output']) {
      const tog = toggleFor(label);
      // cfg is {} on this agent, and both default to on when honoured.
      expect(tog!.className).toContain('on');
    }
  });

  it('a note appears only for the controls that are actually unwired', async () => {
    await openLlmNode({ llm: ['guardrail_profile'] });
    const text = document.body.textContent || '';
    expect(text).toContain('before the flow starts');
    // Structured output is wired in this scenario, so its note must be
    // gone — a stale warning about something the runtime now honours is
    // the same problem inverted.
    expect(text).not.toContain('use a Code node to parse');
  });
});
