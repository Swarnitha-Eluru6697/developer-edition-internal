import { describe, it, expect, vi, afterEach } from 'vitest';

describe('api req() — a real, diagnosable message when the backend is genuinely unreachable', () => {
  afterEach(() => { vi.unstubAllGlobals(); vi.resetModules(); });

  it('a raw fetch-level network failure becomes a clear, actionable message naming the backend URL', async () => {
    vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new TypeError('Failed to fetch')));
    const { api } = await import('../api');
    // BASE is now window.location.origin (§0.44, GAP_REPORT) — genuinely
    // correct behind a same-origin deployment, rather than a hardcoded
    // 127.0.0.1:8000 that would break anywhere else. The real origin
    // value itself varies by test environment (jsdom's own default),
    // so this asserts the real message shape rather than a specific
    // hardcoded host.
    await expect(api.listAgents()).rejects.toThrow(/Couldn't reach the backend at .*is it running.*Failed to fetch/);
  });

  it('a real HTTP error response still surfaces the backend\'s own detail message, unaffected by the network-failure handling', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({
      ok: false, status: 503, json: async () => ({ detail: 'Could not resolve app access — UCS unreachable' }),
    }));
    const { api } = await import('../api');
    await expect(api.listAgents()).rejects.toThrow('Could not resolve app access — UCS unreachable');
  });

  it('a successful response still parses and returns real data as before', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({ ok: true, json: async () => [{ id: 'a1' }] }));
    const { api } = await import('../api');
    await expect(api.listAgents()).resolves.toEqual([{ id: 'a1' }]);
  });
});
