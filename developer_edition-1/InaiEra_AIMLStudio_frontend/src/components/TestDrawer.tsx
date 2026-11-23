import { useEffect, useState } from "react";
import { Icon } from "./Icons";
import type { Domain, TestPrediction } from "../types/domain";
import { api } from "../api/client";

/**
 * Ad-hoc model testing, as a side drawer so it can be opened from anywhere a
 * specific model needs trying out (a catalog version drawer, a promotion
 * candidate) without navigating away and losing that context.
 *
 * Every result here comes from a real HTTP call to the backend's test-predict
 * route. The backend answers either from real weights loaded out of object
 * storage or, in development, from a deterministic stand-in — and it says
 * which in `dataSource`. This component renders that distinction prominently
 * instead of presenting both as "the model's prediction", because they are not
 * the same claim.
 */
export function TestDrawer({
  label, task, domain, modelId, onRun, onClose,
}: {
  label: string;
  task: string;
  domain: Domain;
  /** When given, the body is prefilled with this model's REAL column contract
   *  instead of the task-shaped guess below. */
  modelId?: string;
  onRun: (input: unknown) => Promise<TestPrediction>;
  onClose: () => void;
}) {
  const [input, setInput] = useState(() => defaultInputFor(task, domain));
  // Speech models take audio (speech-to-text) or text + a voice (text-to-speech), not JSON.
  const speech = /speech-to-text/i.test(task) ? "stt" : /text-to-speech/i.test(task) ? "tts" : null;

  const [schemaNote, setSchemaNote] = useState<string | null>(null);

  // The task-shaped default is a guess: it was a fraud-shaped body for every
  // tabular model, so running it against anything else failed the column
  // contract and fell back to the stand-in — which reads as a broken model
  // rather than a wrong request. Replaced with the deployed artifact's actual
  // columns, filled from its own training data.
  useEffect(() => {
    if (!modelId) return;
    let alive = true;
    api.getModelInputSchema(modelId)
      .then((schema) => {
        if (!alive) return;
        if (schema.available && schema.example) {
          setInput(JSON.stringify(schema.example, null, 2));
          setSchemaNote(schema.exampleSource ?? null);
        } else if (schema.reason) {
          setSchemaNote(`Using a generic example — ${schema.reason}`);
        }
      })
      .catch(() => { /* keep the generic default; the run itself reports any real problem */ });
    return () => { alive = false; };
  }, [modelId]);
  const [result, setResult] = useState<TestPrediction | null>(null);
  const [sentAs, setSentAs] = useState<{ how: string; body: unknown } | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [running, setRunning] = useState(false);
  const isLlm = domain === "llm" || /chat|text generation|code generation/i.test(task);
  const [maxTokens, setMaxTokens] = useState(DEFAULT_MAX_TOKENS);

  async function run() {
    setRunning(true);
    setError(null);
    setResult(null);

    const read = readTestInput(input, isLlm);
    if ("error" in read) {
      setError(read.error);
      setRunning(false);
      return;
    }
    const parsed = isLlm ? withMaxTokens(read.body, maxTokens) : read.body;
    setSentAs({ how: read.how, body: parsed });
    try {
      setResult(await onRun(parsed));
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setRunning(false);
    }
  }

  return (
    <div className="drawer-overlay open" onClick={(e) => { if (e.target === e.currentTarget) onClose(); }}>
      <div className="drawer">
        <div className="drawer-head">
          <div>
            <div style={{ fontWeight: 700, fontSize: 16, color: "var(--navy)" }}>Test this model</div>
            <div style={{ fontSize: 12, color: "var(--text-faint)", marginTop: 3, fontFamily: "var(--font-mono)" }}>
              {label}
            </div>
          </div>
          <button className="drawer-close" onClick={onClose}><Icon name="x" /></button>
        </div>
        <div className="drawer-body">
          {speech ? <SpeechTester task={task} onRun={onRun} /> : (<>
          <div className="drawer-section">
            <div className="field">
              <label>Input</label>
              <textarea
                className="textarea"
                style={{ minHeight: 140 }}
                value={input}
                onChange={(e) => setInput(e.target.value)}
              />
              {schemaNote && <div className="hint" style={{ marginTop: 6 }}>{schemaNote}</div>}
              <div className="hint" style={{ marginTop: 4 }}>
                Accepts JSON, <code>column = value</code> lines (or <code>a=1, b=2</code>), CSV (a header row and a values row),
                or plain text — sent to an LLM as a chat message.
              </div>
            </div>
            {isLlm && <MaxTokensField value={maxTokens} onChange={setMaxTokens} />}
            <button className="btn btn-primary btn-block" onClick={run} disabled={running}>
              <Icon name="play" />
              {running ? (isLlm ? "Generating… (long answers take a minute or two)" : "Running…") : "Run prediction"}
            </button>
          </div>
          <div className="drawer-section">
            <div className="card card-pad" style={{ minHeight: 140 }}>
              <div className="drawer-section-title">Result</div>
              {sentAs && (
                <details style={{ marginBottom: 8 }}>
                  <summary className="hint" style={{ cursor: "pointer", margin: 0 }}>Sent as {sentAs.how}</summary>
                  <pre className="step-log" style={{ whiteSpace: "pre-wrap", margin: "6px 0 0" }}>{JSON.stringify(sentAs.body, null, 2)}</pre>
                </details>
              )}
              {!result && !error && (
                <div className="empty-hint">Run a prediction to see the response here.</div>
              )}
              {error && <div className="callout callout-amber">{error}</div>}
              {result && (
                <>
                  {/* A fine-tuned LLM answers in text; show it as text, not a JSON blob. */}
                  {typeof (result.prediction as { prediction?: unknown } | null)?.prediction === "string" ? (<>
                    <div className="step-log" style={{ whiteSpace: "pre-wrap", fontFamily: "inherit", fontSize: 13.5, lineHeight: 1.6 }}>
                      {(result.prediction as { prediction: string }).prediction}
                    </div>
                    <TruncatedNote prediction={result.prediction} />
                  </>) : (
                    <div className="step-log" style={{ whiteSpace: "pre-wrap" }}>
                      {JSON.stringify(result.prediction, null, 2)}
                    </div>
                  )}
                  {result.deployed === false && (
                    <div className="kv-row" style={{ marginTop: 10 }}>
                      <span className="k">Deployment</span>
                      <span className="v">Not deployed — testing v{result.version} before you deploy it</span>
                    </div>
                  )}
                  <div className="kv-row" style={{ marginTop: 10 }}>
                    <span className="k">Latency</span>
                    <span className="v">{result.latencyMs} ms</span>
                  </div>
                  <div className="kv-row">
                    <span className="k">Source</span>
                    <span className="v" style={{ color: result.dataSource === "measured" ? "var(--teal-700)" : "var(--amber-700)" }}>
                      {result.dataSource === "measured" ? "Measured — real weights" : "Development stand-in"}
                    </span>
                  </div>
                  <div
                    className={result.dataSource === "measured" ? "callout callout-navy" : "callout callout-amber"}
                    style={{ marginTop: 10 }}
                  >
                    {result.servedBy}
                  </div>
                </>
              )}
            </div>
          </div>
          </>)}
        </div>
      </div>
    </div>
  );
}

/** A speech result — `{transcript}` or `{audioBase64}` — whether the model answered
 *  directly (base model) or wrapped in `{prediction: …}` (a trained version). */
function speechOutput(pred: unknown): Record<string, any> | null {
  const p = pred as Record<string, any> | null;
  const inner = p && typeof p.prediction === "object" && p.prediction ? p.prediction : p;
  return inner && (typeof inner.transcript === "string" || typeof inner.audioBase64 === "string") ? inner : null;
}

function SpeechResult({ p }: { p: Record<string, any> }) {
  if (typeof p.transcript === "string") {
    return (
      <>
        <div className="step-log" style={{ whiteSpace: "pre-wrap", fontFamily: "inherit", fontSize: 13.5, lineHeight: 1.6 }}>
          {p.transcript || "(no speech recognised)"}
        </div>
        <div className="kv-row" style={{ marginTop: 10 }}><span className="k">Audio length</span><span className="v">{p.durationSeconds}s · {p.chunks} window(s) · {p.language}</span></div>
      </>
    );
  }
  const src = `data:${p.mimeType || "audio/wav"};base64,${p.audioBase64}`;
  return (
    <>
      <audio controls src={src} style={{ width: "100%" }} />
      <div className="kv-row" style={{ marginTop: 10 }}><span className="k">Generated</span><span className="v">{p.durationSeconds}s · {p.sampleRate / 1000} kHz · voice {p.voice}</span></div>
      <a className="btn btn-secondary btn-sm" style={{ marginTop: 8 }} href={src} download="speech.wav"><Icon name="download" />Download WAV</a>
    </>
  );
}

/**
 * "Test this model" for speech models: an audio file in, a transcript out
 * (speech-to-text), or text and a voice in, playable audio out (text-to-speech).
 * Used by the test drawer and the base-model page alike.
 */
export function SpeechTester({ task, onRun }: { task: string; onRun: (input: unknown) => Promise<TestPrediction> }) {
  const stt = /speech-to-text/i.test(task);
  const [audioFile, setAudioFile] = useState<File | null>(null);
  const [language, setLanguage] = useState("en");
  const [text, setText] = useState("A program is a sequence of instructions that specifies how to perform a computation.");
  const [voice, setVoice] = useState("default");
  const [speed, setSpeed] = useState(1);
  const [result, setResult] = useState<TestPrediction | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [running, setRunning] = useState(false);

  async function run() {
    setRunning(true); setError(null); setResult(null);
    try {
      if (stt && !audioFile) throw new Error("Choose an audio file (WAV, FLAC or OGG) to transcribe.");
      if (!stt && !text.trim()) throw new Error("Type the text to speak.");
      const body = stt ? { audioBase64: await fileToBase64(audioFile!), language } : { text: text.trim(), voice, speed };
      setResult(await onRun(body));
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setRunning(false);
    }
  }
  const out = result ? speechOutput(result.prediction) : null;

  return (
    <>
      <div className="drawer-section">
        {stt ? (
          <>
            <div className="field">
              <label>Audio file</label>
              <input className="input" type="file" accept=".wav,.flac,.ogg,audio/*" onChange={(e) => setAudioFile(e.target.files?.[0] ?? null)} />
              <div className="hint">WAV, FLAC or OGG. Long recordings are transcribed in 30-second windows and joined.</div>
              {audioFile && <audio controls src={URL.createObjectURL(audioFile)} style={{ width: "100%", marginTop: 8 }} />}
            </div>
            <div className="field">
              <label>Language</label>
              <input className="input" value={language} onChange={(e) => setLanguage(e.target.value)} style={{ maxWidth: 120 }} />
            </div>
          </>
        ) : (
          <>
            <div className="field">
              <label>Text to speak</label>
              <textarea className="textarea" style={{ minHeight: 120 }} value={text} maxLength={5000} onChange={(e) => setText(e.target.value)} />
              <div className="hint">Up to 5,000 characters per request — paste a paragraph or a page.</div>
            </div>
            <div className="field-row">
              <div className="field">
                <label>Voice</label>
                <select className="input" value={voice} onChange={(e) => setVoice(e.target.value)}>
                  <option value="default">default</option><option value="warm">warm</option><option value="narrator">narrator</option>
                </select>
              </div>
              <div className="field">
                <label>Speed</label>
                <input className="input" type="number" min={0.5} max={2} step={0.1} value={speed} onChange={(e) => setSpeed(Number(e.target.value))} />
              </div>
            </div>
          </>
        )}
        <button className="btn btn-primary btn-block" onClick={run} disabled={running}>
          <Icon name="play" />{running ? (stt ? "Transcribing…" : "Generating…") : (stt ? "Transcribe" : "Generate speech")}
        </button>
      </div>
      <div className="drawer-section">
        <div className="card card-pad" style={{ minHeight: 120 }}>
          <div className="drawer-section-title">Result</div>
          {!result && !error && <div className="empty-hint">{stt ? "Transcribe a file to see the text here." : "Generate speech to play it here."}</div>}
          {error && <div className="callout callout-amber">{error}</div>}
          {result && (
            <>
              {out ? <SpeechResult p={out} /> : <div className="step-log" style={{ whiteSpace: "pre-wrap" }}>{JSON.stringify(result.prediction, null, 2)}</div>}
              <div className="kv-row" style={{ marginTop: 10 }}><span className="k">Latency</span><span className="v">{result.latencyMs} ms</span></div>
              <div className="kv-row">
                <span className="k">Source</span>
                <span className="v" style={{ color: result.dataSource === "measured" ? "var(--teal-700)" : "var(--amber-700)" }}>
                  {result.dataSource === "measured" ? "Measured — real weights" : "Development stand-in"}
                </span>
              </div>
              <div className={result.dataSource === "measured" ? "callout callout-navy" : "callout callout-amber"} style={{ marginTop: 10 }}>{result.servedBy}</div>
            </>
          )}
        </div>
      </div>
    </>
  );
}

function fileToBase64(file: File): Promise<string> {
  return new Promise((resolve, reject) => {
    const r = new FileReader();
    r.onload = () => resolve(String(r.result).split(",")[1] ?? "");
    r.onerror = () => reject(new Error("couldn't read the file"));
    r.readAsDataURL(file);
  });
}

/**
 * A starting request body shaped like what this task actually takes, so the
 * first Run exercises the right contract rather than making you guess it.
 */
export function defaultInputFor(task: string, domain: Domain): string {
  const t = task.toLowerCase();
  if (t.includes("embedding")) {
    return JSON.stringify({ input: "Card declined due to suspected fraud — customer disputes the transaction." }, null, 2);
  }
  if (t.includes("speech-to-text")) {
    return JSON.stringify({ audio_url: "s3://acme-corp/uploads/support-call-042.wav" }, null, 2);
  }
  if (t.includes("text-to-speech")) {
    return JSON.stringify({ text: "Your claim has been approved and payment will arrive within three business days." }, null, 2);
  }
  if (t.includes("chat") || domain === "llm") {
    return JSON.stringify({ messages: [{ role: "user", content: "Summarize this quarter's fraud detection risks in 3 bullets." }] }, null, 2);
  }
  if (t.includes("forecast")) {
    return JSON.stringify({ series: [120, 134, 141, 129, 156], horizon: 30 }, null, 2);
  }
  return JSON.stringify({ features: { amount_usd: 1240, merchant_category: "electronics" } }, null, 2);
}

/** Read the test box however it was written. JSON is used as-is; otherwise
 *  `column = value` / `column: value` lines or `a=1, b=2`, a CSV header + values row,
 *  and finally plain text — a chat message for an LLM, {"text": …} for anything else.
 *  Values that look like numbers or booleans become them. */
export function readTestInput(raw: string, isLlm: boolean): { body: unknown; how: string } | { error: string } {
  const text = raw.trim();
  if (!text) return { error: "Type something to send — JSON, column = value lines, CSV, or plain text." };
  const coerce = (v: string): unknown => {
    const s = v.trim().replace(/^(["'])(.*)\1$/, "$2");
    if (/^-?\d+(\.\d+)?([eE][-+]?\d+)?$/.test(s)) return Number(s);
    if (/^(true|false)$/i.test(s)) return s.toLowerCase() === "true";
    if (/^null$/i.test(s)) return null;
    return s;
  };
  try {
    const j = JSON.parse(text);
    if (isLlm && typeof j === "string") return { body: { messages: [{ role: "user", content: j }] }, how: "a chat message" };
    return { body: j, how: "JSON" };
  } catch { /* not JSON — try the other shapes */ }
  if (isLlm) return { body: { messages: [{ role: "user", content: text }] }, how: "a chat message" };

  const lines = text.split(/\r?\n/).map((l) => l.trim()).filter(Boolean);
  const kv = /^([A-Za-z_][\w .-]*?)\s*[:=]\s*(.*)$/;
  const pairs = lines.length === 1 && lines[0].includes(",") && lines[0].split(",").every((p) => kv.test(p.trim()))
    ? lines[0].split(",").map((p) => p.trim())
    : lines;
  if (pairs.every((l) => kv.test(l))) {
    return { body: Object.fromEntries(pairs.map((l) => { const m = l.match(kv)!; return [m[1].trim(), coerce(m[2])]; })), how: "column = value pairs" };
  }
  if (lines.length === 2) {
    const head = lines[0].split(",").map((s) => s.trim());
    const vals = lines[1].split(",");
    if (head.length > 1 && head.length === vals.length && head.every((h) => /^[A-Za-z_][\w.-]*$/.test(h))) {
      return { body: Object.fromEntries(head.map((h, i) => [h, coerce(vals[i])])), how: "a CSV row" };
    }
  }
  return { body: { text }, how: 'plain text ({"text": …})' };
}

/* ---------------- LLM answer length ---------------- */

export const DEFAULT_MAX_TOKENS = 1024;
const MAX_TOKEN_CHOICES = [256, 512, 1024, 2048, 4096];

/** Adds max_new_tokens to an LLM request body unless the caller already set it in their JSON. */
export function withMaxTokens(body: unknown, maxTokens: number): unknown {
  if (body && typeof body === "object" && !Array.isArray(body) && !("max_new_tokens" in body)) {
    return { ...(body as Record<string, unknown>), max_new_tokens: maxTokens };
  }
  return body;
}

export function MaxTokensField({ value, onChange }: { value: number; onChange: (n: number) => void }) {
  return (
    <div className="field" style={{ marginTop: 4 }}>
      <label>Max answer length</label>
      <select className="select" value={value} onChange={(e) => onChange(Number(e.target.value))}>
        {MAX_TOKEN_CHOICES.map((n) => (
          <option key={n} value={n}>{n.toLocaleString()} tokens (≈ {Math.round(n * 0.75).toLocaleString()} words)</option>
        ))}
      </select>
      <div className="hint" style={{ marginTop: 4 }}>
        The model stops here even mid-answer. Use 1,024+ for code with an explanation; longer answers take longer to generate.
      </div>
    </div>
  );
}

/** Says so when an LLM answer was cut off by the length limit. */
export function TruncatedNote({ prediction }: { prediction: unknown }) {
  const p = prediction as { truncated?: boolean; maxNewTokens?: number } | null;
  if (!p?.truncated) return null;
  return (
    <div className="callout callout-amber" style={{ marginTop: 8 }}>
      The answer stopped at the {p.maxNewTokens?.toLocaleString() ?? "length"}-token limit — choose a longer
      Max answer length and run it again for the rest.
    </div>
  );
}
