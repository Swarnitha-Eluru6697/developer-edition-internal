import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen } from '@testing-library/react';

vi.mock('../api', () => ({
  api: {
    listAgents: vi.fn(),
    getMetrics: vi.fn(),
    getEvals: vi.fn(),
    getEvalHealth: vi.fn(),
  },
}));
vi.mock('../components/EvalManager', () => ({ default: () => null }));
vi.mock('../context/AppContext', () => ({ useApp: () => ({ toast: vi.fn() }) }));

import { api } from '../api';
import Monitoring from '../pages/Monitoring';

const AGENTS = [{ id: 'ag1', name: 'Support Bot', plane: 'Control' }];

function setup(metrics: Record<string, unknown>) {
  (api.listAgents as ReturnType<typeof vi.fn>).mockResolvedValue(AGENTS);
  (api.getMetrics as ReturnType<typeof vi.fn>).mockResolvedValue({ scope: 'ag1', metrics });
  (api.getEvals as ReturnType<typeof vi.fn>).mockResolvedValue([]);
  (api.getEvalHealth as ReturnType<typeof vi.fn>).mockResolvedValue(null);
  return render(<Monitoring />);
}

describe('per-agent monitoring numbers', () => {
  beforeEach(() => vi.clearAllMocks());

  it('renders the computed numbers the scoped endpoint returns', async () => {
    setup({ success: 80, p50: '2.0s', p95: '9.0s', stepLimit: 10, guardrail: 0,
            entPass: 90, costAvg: '$0.0500', retries: null, loopIter: null,
            chainDepth: null, cacheHit: null, sample_size: 10, window_days: 7 });
    expect(await screen.findByText('80%')).toBeTruthy();
    expect(screen.getByText('$0.0500')).toBeTruthy();
    expect(screen.getByText('10%')).toBeTruthy();
    expect(screen.getByText('90%')).toBeTruthy();
    expect(screen.getByText(/Computed from 10 runs in the last 7 days/)).toBeTruthy();
  });

  it('says a scope has no runs rather than leaving unexplained dashes', async () => {
    setup({ success: null, p50: null, p95: null, stepLimit: null, guardrail: null,
            entPass: null, costAvg: null, retries: null, loopIter: null,
            chainDepth: null, cacheHit: null, sample_size: 0, window_days: 7 });
    expect(await screen.findByText(/No runs .* in the last 7 days/)).toBeTruthy();
  });

  it('still shows the untracked note for metrics that have no instrumentation', async () => {
    setup({ success: 80, p50: '2.0s', p95: '9.0s', stepLimit: 10, guardrail: 0,
            entPass: 90, costAvg: '$0.0500', retries: null, loopIter: null,
            chainDepth: null, cacheHit: null, sample_size: 10, window_days: 7 });
    await screen.findByText('80%');
    // A dash from "no instrumentation" must stay distinguishable from a
    // dash caused by an empty window.
    // Every remaining dash now names its own cause instead of pointing
    // at a GAP_REPORT section, so this asserts the specific reason
    // rather than the old generic "not tracked" wording.
    expect(screen.getByText(/no per-run attempt count is persisted/)).toBeTruthy();
    expect(screen.queryByText(/No runs/)).toBeNull();
  });
});
