import { describe, expect, it, vi } from "vitest";
import { openExternal, safeExternalUrl } from "../src/shared/externalUrl";

// W-023: external tabs are opened without an opener/referrer, and only for URLs that are plainly safe.
describe("safeExternalUrl", () => {
  it.each([
    ["https://github.com/acme/repo/pull/7", "https://github.com/acme/repo/pull/7"],
    ["  https://gitlab.com/a/b/-/merge_requests/1  ", "https://gitlab.com/a/b/-/merge_requests/1"],
  ])("accepts %s", (input, expected) => { expect(safeExternalUrl(input)).toBe(expected); });

  it.each([
    "javascript:alert(1)", "data:text/html,<script>1</script>", "file:///etc/passwd", "ftp://x/y", "//evil.example/x", "/relative/path",
    "http://github.com/acme/repo/pull/7", "https://user:pw@github.com/x", "https://:pw@github.com/x", "", "   ", null, undefined, 42, {},
  ])("refuses %p", (input) => { expect(safeExternalUrl(input)).toBeNull(); });

  it("with an expected host, only that host (case-insensitive, port included)", () => {
    expect(safeExternalUrl("https://GitHub.com/a/b/pull/1", { expectedHost: "github.com" })).not.toBeNull();
    expect(safeExternalUrl("https://github.com.evil.example/a", { expectedHost: "github.com" })).toBeNull();
    expect(safeExternalUrl("https://evil.example/github.com", { expectedHost: "github.com" })).toBeNull();
    expect(safeExternalUrl("https://github.com:8443/a", { expectedHost: "github.com" })).toBeNull();
  });
});

describe("openExternal", () => {
  it("opens a new tab with noopener,noreferrer", () => {
    const open = vi.fn();
    expect(openExternal("https://github.com/a/b/pull/1", {}, open as unknown as typeof window.open)).toBe(true);
    expect(open).toHaveBeenCalledWith("https://github.com/a/b/pull/1", "_blank", "noopener,noreferrer");
  });

  it("opens nothing for an unsafe URL", () => {
    const open = vi.fn();
    expect(openExternal("javascript:alert(1)", {}, open as unknown as typeof window.open)).toBe(false);
    expect(open).not.toHaveBeenCalled();
  });
});
