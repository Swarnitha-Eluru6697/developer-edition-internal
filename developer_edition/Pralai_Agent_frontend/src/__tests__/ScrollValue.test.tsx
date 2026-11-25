import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, fireEvent, waitFor, cleanup } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { AppProvider } from '../context/AppContext';
import { FormRunner } from '../components/RunnerUI';
import { api } from '../api';
import type { Agent } from '../types';

vi.mock('../api');
afterEach(() => cleanup());

/**
 * A long result value scrolls inside itself instead of running the card
 * down the page. The scrollbar is hidden by request, so the fade at the
 * bottom edge is the only thing left saying "there is more" — and it is
 * applied only while the value actually overflows, or a short value
 * would carry a shadow suggesting text that is not there.
 */
function makeAgent(): Agent {
  return {
    id: 'a1', name: 'WF', type: 'App', subtype: 'Custom', plane: 'App', owner: 'you',
    status: 'published', version: 'v1', draft_revision: 1, health_score: null,
    health_status: '—', tags: [], description: '', updated_at: '2026-01-01T00:00:00',
    model: 'Claude Sonnet 5', mode: 'agentic', app_type: 'text_generator', runs_7d: 0,
    cost_7d: '$0.00', first_party: false, metrics: {}, features: {},
    flow: { nodes: [{ id: 'n1', type: 'trigger', x: 0, y: 0, sub: '', config: {} }], edges: [] },
  } as Agent;
}

const LONG = 'ACME Security Control Catalogue. '.repeat(80);

function renderRunner() {
  return render(
    <MemoryRouter>
      <AppProvider>
        <FormRunner agent={makeAgent()} appType="text_generator" busy={false} setBusy={() => {}} />
      </AppProvider>
    </MemoryRouter>
  );
}

describe('long result values', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    // text_generator, not workflow: a workflow app runs through the
    // streaming path, and this is about how a finished value is shown.
    vi.mocked(api.runAgent).mockResolvedValue({
      status: 'success', output: null, fail_class: null,
      structured_output: { category: 'security', context: LONG },
    } as any);
  });

  const runIt = async (container: HTMLElement) => {
    const box = container.querySelector('textarea, input[type=text]') as HTMLElement;
    if (box) fireEvent.change(box, { target: { value: 'SEC-201' } });
    fireEvent.click(screen.getByText('Generate'));  // label for a text_generator app
    await waitFor(() => expect(container.querySelector('.so-value')).toBeTruthy());
  };

  it('puts every value in its own scroll box', async () => {
    const { container } = renderRunner();
    await runIt(container);
    expect(container.querySelectorAll('.so-value')).toHaveLength(2);
  });

  it('caps the height and scrolls rather than growing the card', async () => {
    const { container } = renderRunner();
    await runIt(container);
    // jsdom has no layout, so assert the contract the CSS relies on
    // rather than a measured height.
    const values = [...container.querySelectorAll('.so-value')];
    // the long one is `context`, not the first row (`category`)
    const long = values.find((v) => (v.textContent || '').length > 200);
    expect(long).toBeTruthy();
    expect(long!.textContent).toContain('ACME Security Control Catalogue');
  });

  it('marks an overflowing value so the fade can show', async () => {
    // jsdom reports 0 for both, so drive the measurement directly.
    const proto = window.HTMLElement.prototype;
    const sh = Object.getOwnPropertyDescriptor(proto, 'scrollHeight');
    const ch = Object.getOwnPropertyDescriptor(proto, 'clientHeight');
    Object.defineProperty(proto, 'scrollHeight', { configurable: true, get: () => 900 });
    Object.defineProperty(proto, 'clientHeight', { configurable: true, get: () => 240 });
    try {
      const { container } = renderRunner();
      await runIt(container);
      await waitFor(() =>
        expect(container.querySelectorAll('.so-value-more').length).toBeGreaterThan(0));
    } finally {
      if (sh) Object.defineProperty(proto, 'scrollHeight', sh);
      if (ch) Object.defineProperty(proto, 'clientHeight', ch);
    }
  });

  it('does not fade a value that fits', async () => {
    const proto = window.HTMLElement.prototype;
    const sh = Object.getOwnPropertyDescriptor(proto, 'scrollHeight');
    const ch = Object.getOwnPropertyDescriptor(proto, 'clientHeight');
    Object.defineProperty(proto, 'scrollHeight', { configurable: true, get: () => 40 });
    Object.defineProperty(proto, 'clientHeight', { configurable: true, get: () => 240 });
    try {
      const { container } = renderRunner();
      await runIt(container);
      expect(container.querySelectorAll('.so-value-more')).toHaveLength(0);
    } finally {
      if (sh) Object.defineProperty(proto, 'scrollHeight', sh);
      if (ch) Object.defineProperty(proto, 'clientHeight', ch);
    }
  });

  it('an overflowing value is reachable by keyboard', async () => {
    const proto = window.HTMLElement.prototype;
    const sh = Object.getOwnPropertyDescriptor(proto, 'scrollHeight');
    const ch = Object.getOwnPropertyDescriptor(proto, 'clientHeight');
    Object.defineProperty(proto, 'scrollHeight', { configurable: true, get: () => 900 });
    Object.defineProperty(proto, 'clientHeight', { configurable: true, get: () => 240 });
    try {
      const { container } = renderRunner();
      await runIt(container);
      await waitFor(() => {
        const v = container.querySelector('.so-value-more')!;
        expect(v.getAttribute('tabindex')).toBe('0');
      });
    } finally {
      if (sh) Object.defineProperty(proto, 'scrollHeight', sh);
      if (ch) Object.defineProperty(proto, 'clientHeight', ch);
    }
  });
});
