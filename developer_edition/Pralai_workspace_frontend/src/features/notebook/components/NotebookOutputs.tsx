/** Jupyter (Python) cell outputs: streams, rich mime bundles, and errors. */
import { mimeText, stripAnsi, type NbOutput } from "../notebookFormat";
import { SandboxedHtml } from "./SandboxedHtml";

type MimeBundleData = Record<string, unknown>;

/** Fields a Jupyter output payload may carry (by output_type). */
interface OutputPayload {
  name?: string;                   // stream: stdout | stderr
  text?: string | string[];        // stream
  mime_bundle?: MimeBundleData;    // display_data / execute_result
  traceback?: string[];            // error (Jupyter)
  ename?: string;
  evalue?: string;
  error_message?: string;          // error (infra)
  error_code?: string;
}

const preStyle = { margin: 0, fontSize: 11.5, fontFamily: "monospace", whiteSpace: "pre-wrap" } as const;

function MimeBundle({ bundle }: { bundle: MimeBundleData }) {
  const png = bundle["image/png"];
  if (typeof png === "string" && png) {
    return <img alt="output" style={{ maxWidth: "100%", display: "block", padding: 8 }} src={`data:image/png;base64,${png.replace(/\s/g, "")}`} />;
  }
  const jpeg = bundle["image/jpeg"];
  if (typeof jpeg === "string" && jpeg) {
    return <img alt="output" style={{ maxWidth: "100%", display: "block", padding: 8 }} src={`data:image/jpeg;base64,${jpeg.replace(/\s/g, "")}`} />;
  }
  const html = mimeText(bundle, "text/html");
  if (html) {
    // Kernel-produced HTML (e.g. pandas DataFrame._repr_html_) is UNTRUSTED (W-019): it runs in a sandboxed,
    // no-same-origin iframe, never in our own DOM.
    return <SandboxedHtml html={html} />;
  }
  if (bundle["text/plain"]) {
    return <pre style={{ ...preStyle, padding: "6px 10px", color: "#14201B" }}>{mimeText(bundle, "text/plain")}</pre>;
  }
  return null;
}

export function PythonOutputs({ outputs, error }: { outputs?: NbOutput[]; error?: string | null }) {
  if (!outputs || outputs.length === 0) {
    if (error) {
      return <pre style={{ ...preStyle, padding: "6px 10px", color: "#b23b3b" }}>{error}</pre>;
    }
    return <div style={{ padding: "4px 10px", fontSize: 11.5, color: "#73827a" }}>No output.</div>;
  }
  return (
    <div style={{ maxHeight: 360, overflow: "auto" }}>
      {outputs.map((o, i) => {
        const p = (o.payload || {}) as OutputPayload;
        if (o.output_type === "stream") {
          const isErr = p.name === "stderr";
          return <pre key={i} style={{ ...preStyle, padding: "4px 10px", color: isErr ? "#b23b3b" : "#2E3D36", background: isErr ? "#fbe9e9" : undefined }}>{Array.isArray(p.text) ? p.text.join("") : p.text}</pre>;
        }
        if (o.output_type === "display_data" || o.output_type === "execute_result") {
          return <MimeBundle key={i} bundle={p.mime_bundle || {}} />;
        }
        if (o.output_type === "error") {
          // Jupyter error → {ename, evalue, traceback}. Infra error → {error_code, error_message}.
          const tb = Array.isArray(p.traceback) ? stripAnsi(p.traceback.join("\n")) : null;
          const text = tb || `${p.ename ? p.ename + ": " : ""}${p.evalue || p.error_message || p.error_code || "Error"}`;
          return <pre key={i} style={{ ...preStyle, padding: "6px 10px", color: "#b23b3b" }}>{text}</pre>;
        }
        return null;
      })}
    </div>
  );
}
