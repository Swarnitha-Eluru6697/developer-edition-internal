/**
 * Test harness for RTK Query endpoint definitions: runs an endpoint through a
 * real store + fetchBaseQuery, with fetch stubbed, and reports the HTTP
 * request it produced (method, path, JSON body) plus the transformed result.
 */
import { configureStore } from "@reduxjs/toolkit";
import { vi } from "vitest";
import { workspaceApi } from "../../src/api/client";

export interface CapturedRequest {
  method: string;
  url: string;
  body: unknown;
}

// fetchBaseQuery builds `new Request(url)` from the relative "/api/v1" base;
// Node's Request rejects relative URLs, so stand in a minimal one.
export class FakeRequest {
  url: string;
  method: string;
  headers: Headers;
  body: unknown;
  signal?: AbortSignal;
  constructor(input: string, init: RequestInit = {}) {
    this.url = input;
    this.method = (init.method ?? "GET").toUpperCase();
    this.headers = new Headers(init.headers);
    this.body = init.body;
    this.signal = init.signal ?? undefined;
  }
  clone() { return this; }
}

export function makeStore() {
  return configureStore({
    reducer: { [workspaceApi.reducerPath]: workspaceApi.reducer },
    middleware: (gDM) => gDM({ serializableCheck: false }).concat(workspaceApi.middleware),
  });
}

/** Dispatch `initiate(arg)` for an endpoint and return what went over the wire. */
export async function callEndpoint<A>(
  endpoint: { initiate: (arg: A) => unknown },
  arg: A,
  response: unknown = {},
  status = 200,
): Promise<{ request: CapturedRequest; data: unknown; error: unknown }> {
  const requests: CapturedRequest[] = [];
  vi.stubGlobal("Request", FakeRequest);
  vi.stubGlobal("fetch", vi.fn(async (req: FakeRequest) => {
    const raw = req.body;
    requests.push({
      method: req.method,
      url: req.url,
      body: typeof raw === "string" && raw ? JSON.parse(raw) : raw ?? undefined,
    });
    return new Response(status === 204 ? null : JSON.stringify(response), {
      status,
      headers: { "content-type": "application/json" },
    });
  }));
  try {
    const store = makeStore();
    // initiate() returns a thunk; its precise RTK type isn't needed here.
    const result: unknown = await store.dispatch(endpoint.initiate(arg) as never);
    const r = result as { data?: unknown; error?: unknown };
    if (typeof (result as { unsubscribe?: () => void }).unsubscribe === "function") {
      (result as { unsubscribe: () => void }).unsubscribe();
    }
    return { request: requests[0], data: r.data, error: r.error };
  } finally {
    vi.unstubAllGlobals();
  }
}
