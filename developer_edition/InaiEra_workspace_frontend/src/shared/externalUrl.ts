/**
 * Opening a URL the backend or a provider handed us (W-023).
 *
 * `window.open(url, "_blank")` without `noopener,noreferrer` leaves the new page with a handle back to ours (`window.opener`) and sends
 * our address as the Referer. Every external open goes through here instead: the URL must be absolute https (http only for localhost in
 * development), optionally on an expected host, and the tab is opened with `noopener,noreferrer`.
 */

export interface ExternalUrlOptions {
  /** If set, the URL's host must equal this host (case-insensitive), e.g. the host of the repository's own URL. */
  expectedHost?: string;
}

/** The normalised URL when it is safe to open in a new tab, else null. */
export function safeExternalUrl(value: unknown, opts: ExternalUrlOptions = {}): string | null {
  if (typeof value !== "string" || !value.trim()) return null;
  let url: URL;
  try {
    url = new URL(value.trim());
  } catch {
    return null;
  }
  const local = url.hostname === "localhost" || url.hostname === "127.0.0.1";
  if (url.protocol !== "https:" && !(url.protocol === "http:" && local && import.meta.env.DEV === true)) return null;
  if (url.username || url.password) return null;                       // never a URL that carries credentials
  if (opts.expectedHost && url.host.toLowerCase() !== opts.expectedHost.toLowerCase()) return null;
  return url.toString();
}

/** Open `value` in a new tab without an opener or referrer. Returns false (and opens nothing) when the URL is not safe. */
export function openExternal(value: unknown, opts: ExternalUrlOptions = {}, open: typeof window.open = window.open.bind(window)): boolean {
  const url = safeExternalUrl(value, opts);
  if (!url) return false;
  open(url, "_blank", "noopener,noreferrer");
  return true;
}
