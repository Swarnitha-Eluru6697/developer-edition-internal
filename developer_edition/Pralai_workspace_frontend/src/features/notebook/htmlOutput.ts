/**
 * The pure parts of how untrusted rich HTML is framed (W-019): the Content-Security-Policy, the document placed in the
 * sandboxed iframe, and a height estimate. See components/SandboxedHtml.tsx for the why.
 */

/** Applied inside the frame. Scripts, fetch, frames and fonts fall under `default-src 'none'`. */
export const HTML_OUTPUT_CSP = [
  "default-src 'none'",
  "img-src data:",                 // embedded images (matplotlib, plotly's static export) but never a URL
  "style-src 'unsafe-inline'",     // pandas emits <style scoped> and inline styles
  "font-src data:",
  "form-action 'none'",
  "base-uri 'none'",
].join("; ");

const MIN_HEIGHT = 40;
const MAX_HEIGHT = 340;            // the outputs panel itself scrolls at 360px
const ROW_HEIGHT = 24;

/** The complete document placed in the frame. The CSP is first, so nothing in `html` can precede or remove it. */
export function htmlOutputDocument(html: string): string {
  return `<!doctype html><html><head><meta charset="utf-8">`
    + `<meta http-equiv="Content-Security-Policy" content="${HTML_OUTPUT_CSP}">`
    + `<base target="_blank">`
    + `<style>html,body{margin:0}body{padding:6px 10px;font:11.5px/1.4 system-ui,sans-serif;color:#14201B}`
    + `table{border-collapse:collapse}th,td{padding:2px 8px;border:0.5px solid #e3e8e2;text-align:right}`
    + `th{background:#f8faf9}</style></head><body>${html}</body></html>`;
}

/** A height for the frame, since it cannot measure itself: roughly one line per row / break / block element. */
export function htmlOutputHeight(html: string): number {
  const blocks = html.match(/<(tr|br|p|li|div|h[1-6]|pre)[\s>/]/gi)?.length ?? 0;
  const rows = blocks + 1;                     // +1: trailing inline content (an image, a link) below the last block
  return Math.min(MAX_HEIGHT, Math.max(MIN_HEIGHT, rows * ROW_HEIGHT + 16));
}
