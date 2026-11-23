/**
 * Untrusted rich HTML (W-019).
 *
 * A notebook's `text/html` output (a pandas DataFrame, a plot, anything a kernel or a shared .ipynb file
 * carries) is attacker-controllable content. Injecting it into our own DOM would run its event handlers
 * (`<img onerror>`, `<svg onload>`, `javascript:` links) in the Workspace origin, where they can call the API
 * as the signed-in user. So it is never injected: it is rendered inside an iframe that
 *
 *   - has an EMPTY `sandbox` attribute: no scripts, no same-origin (an opaque origin, so it cannot reach the parent's
 *     DOM, cookies or storage), no forms, popups, modals or top-level navigation; and
 *   - carries a Content-Security-Policy that blocks every network load (images may only be `data:` URIs), as a
 *     second layer: nothing can be exfiltrated to a remote host, not even by CSS.
 *
 * Without scripts the frame cannot report its own height, so the height is estimated from the content and the
 * user can drag the frame bigger (`resize`). Nothing here sanitizes or rewrites the markup: it is shown as the
 * kernel produced it, just unable to do anything.
 */
import { useMemo } from "react";
import { htmlOutputDocument, htmlOutputHeight } from "../htmlOutput";

export function SandboxedHtml({ html }: { html: string }) {
  const doc = useMemo(() => htmlOutputDocument(html), [html]);
  const height = useMemo(() => htmlOutputHeight(html), [html]);
  return (
    <iframe
      title="HTML output"
      sandbox=""
      referrerPolicy="no-referrer"
      srcDoc={doc}
      style={{ display: "block", width: "100%", height, border: 0, resize: "vertical", overflow: "auto", background: "#fff" }}
    />
  );
}
