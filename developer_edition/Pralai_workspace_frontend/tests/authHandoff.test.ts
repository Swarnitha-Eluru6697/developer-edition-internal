import { describe, expect, it, vi } from "vitest";
import { establishSessionFromUrl, takeHandoffToken } from "../src/api/auth";

// WS-AUTH-010: the handoff token is read once and removed from the URL; the
// fragment form never reaches a server.
describe("takeHandoffToken", () => {
  it("reads the fragment token and strips it", () => {
    const url = new URL("https://ws.example/?handoff=1#token=abc.def.ghi");
    expect(takeHandoffToken(url)).toBe("abc.def.ghi");
    expect(url.toString()).toBe("https://ws.example/");
  });

  it("keeps other fragment params", () => {
    const url = new URL("https://ws.example/app#token=t1&tab=sql");
    expect(takeHandoffToken(url)).toBe("t1");
    expect(url.hash).toBe("#tab=sql");
  });

  // W-005: a query-string token has already been sent to the server (access logs, proxies,
  // analytics, history) by the time JS runs, so it is never accepted — only scrubbed.
  it("never accepts a query-string token, but still scrubs it from the address bar", () => {
    const url = new URL("https://ws.example/?token=t2&x=1");
    expect(takeHandoffToken(url)).toBeNull();
    expect(url.search).toBe("?x=1");
  });

  it("ignores the query token even when a fragment token is also present", () => {
    const url = new URL("https://ws.example/?token=from-query#token=from-fragment");
    expect(takeHandoffToken(url)).toBe("from-fragment");
    expect(url.toString()).toBe("https://ws.example/");
  });

  it("does not turn a query token into a session exchange", async () => {
    const fetchMock = vi.fn().mockResolvedValue({ ok: true });
    vi.stubGlobal("fetch", fetchMock);
    window.history.replaceState({}, "", "/?token=leaky.jwt.value");
    try {
      expect(await establishSessionFromUrl()).toBe(false);
      expect(fetchMock).not.toHaveBeenCalled();
      expect(window.location.search).toBe("");
    } finally {
      vi.unstubAllGlobals();
    }
  });

  it("exchanges a fragment token exactly once and clears it from the URL", async () => {
    const fetchMock = vi.fn().mockResolvedValue({ ok: true });
    vi.stubGlobal("fetch", fetchMock);
    window.history.replaceState({}, "", "/#token=good.jwt.value");
    try {
      expect(await establishSessionFromUrl()).toBe(true);
      expect(fetchMock).toHaveBeenCalledTimes(1);
      expect(fetchMock.mock.calls[0][1].headers.Authorization).toBe("Bearer good.jwt.value");
      expect(window.location.hash).toBe("");
    } finally {
      vi.unstubAllGlobals();
    }
  });

  it("returns null when there is no token", () => {
    expect(takeHandoffToken(new URL("https://ws.example/"))).toBeNull();
  });
});
