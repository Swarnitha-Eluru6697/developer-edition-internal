import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { api } from '../api';

/**
 * Every request must carry Content-Type: application/json, including the
 * ones that also send a header of their own.
 *
 * req() built its options as
 *
 *   { headers: { 'Content-Type': 'application/json' }, ...opts }
 *
 * so `opts.headers` replaced the whole headers object rather than adding
 * to it. Each optimistic-locking mutation passes If-Match, and therefore
 * sent no content type at all — FastAPI then declined to parse the JSON
 * body and answered
 *
 *   422  "Input should be a valid dictionary or object to extract fields from"
 *
 * The visible symptom was a Builder where a node could not be added,
 * moved or configured and no connection could be drawn, while deleting a
 * node still worked, because a DELETE sends no body and so loses nothing
 * by having no content type. Confirmed in a real browser: the drag was
 * never the problem, the request it produced was unparseable.
 */
describe('api request headers', () => {
  let calls: { url: string; init: RequestInit }[];

  beforeEach(() => {
    calls = [];
    vi.stubGlobal('fetch', vi.fn(async (url: string, init: RequestInit) => {
      calls.push({ url, init });
      return { ok: true, status: 200, json: async () => ({}) } as Response;
    }));
  });

  afterEach(() => vi.unstubAllGlobals());

  const headerOf = (i: number, name: string) => {
    const h = calls[i].init.headers as Record<string, string>;
    return h?.[name];
  };

  it('sends Content-Type on a plain JSON mutation', async () => {
    await api.addNode('a1', { type: 'llm', x: 0, y: 0 });
    expect(headerOf(0, 'Content-Type')).toBe('application/json');
  });

  it('keeps Content-Type when the caller also sends If-Match', async () => {
    await api.addNode('a1', { type: 'llm', x: 0, y: 0 }, 3);
    expect(headerOf(0, 'If-Match')).toBe('3');
    expect(headerOf(0, 'Content-Type')).toBe('application/json');
  });

  it('every revision-carrying Builder mutation keeps both headers', async () => {
    await api.addNode('a1', { type: 'llm', x: 0, y: 0 }, 1);
    await api.addEdge('a1', 'n1', 'n2', null, 2);
    await api.moveNode('a1', 'n1', 10, 20, 3);
    await api.updateNodeConfig('a1', 'n1', { foo: 'bar' }, 4);

    expect(calls).toHaveLength(4);
    calls.forEach((_, i) => {
      expect(headerOf(i, 'Content-Type')).toBe('application/json');
      expect(headerOf(i, 'If-Match')).toBe(String(i + 1));
    });
  });

  it('a body is still actually sent alongside the extra header', async () => {
    await api.addNode('a1', { type: 'llm', x: 5, y: 6 }, 1);
    expect(JSON.parse(String(calls[0].init.body))).toMatchObject({ type: 'llm', x: 5, y: 6 });
  });

  it('does not force a content type on a FormData upload', async () => {
    // The browser has to set multipart/form-data itself, with the
    // boundary — overriding it breaks the upload.
    const file = new File(['x'], 'x.txt', { type: 'text/plain' });
    await api.uploadFile(file).catch(() => {});
    // The size check reads the upload limit first; the upload is the POST.
    const upload = calls.find((c) => c.init?.method === 'POST')!;
    const h = (upload.init.headers || {}) as Record<string, string>;
    expect(h['Content-Type']).toBeUndefined();
  });
});
