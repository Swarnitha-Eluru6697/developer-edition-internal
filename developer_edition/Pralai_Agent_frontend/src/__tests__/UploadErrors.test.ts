import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';

// The upload limit is cached per module load, so each test imports a fresh copy.
async function freshApi() {
  vi.resetModules();
  return (await import('../api')).api;
}

const json = (status: number, body: unknown) =>
  Promise.resolve(new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } }));

describe('file uploads — refused early with the limit named, and server errors carry their reference', () => {
  let posts = 0;
  beforeEach(() => { posts = 0; });
  afterEach(() => vi.unstubAllGlobals());

  it('refuses a file over the server limit before sending it', async () => {
    vi.stubGlobal('fetch', vi.fn((url: string, init?: RequestInit) => {
      if (String(url).endsWith('/api/uploads/limits')) return json(200, { max_upload_bytes: 10 });
      if (init?.method === 'POST') posts += 1;
      return json(200, {});
    }));
    const api = await freshApi();
    const big = new File(['x'.repeat(20)], 'big.pdf');
    await expect(api.uploadFile(big)).rejects.toThrow(/big\.pdf.*too large to upload\. Files must be/);
    expect(posts).toBe(0);
  });

  it('a server crash shows its correlation reference instead of "Request failed: 500"', async () => {
    vi.stubGlobal('fetch', vi.fn((url: string) => {
      if (String(url).endsWith('/api/uploads/limits')) return json(200, { max_upload_bytes: 1024 });
      return json(500, { error_code: 'AGENT_EXECUTION_FAILED', correlation_id: 'unhandled-abc123' });
    }));
    const api = await freshApi();
    await expect(api.uploadFile(new File(['ok'], 'ok.txt'))).rejects.toThrow(/reference unhandled-abc123/);
  });

  it('a 413 from the server shows its reason', async () => {
    vi.stubGlobal('fetch', vi.fn((url: string) => {
      if (String(url).endsWith('/api/uploads/limits')) return json(200, { max_upload_bytes: 1024 });
      return json(413, { detail: 'This file is too large to upload. Files must be 20 MB or smaller.' });
    }));
    const api = await freshApi();
    await expect(api.uploadFile(new File(['ok'], 'ok.txt'))).rejects.toThrow(/Files must be 20 MB or smaller/);
  });
});
