import { useEffect, useMemo, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { api, ApiRequestError } from "../api/client";
import { useApi } from "../api/useApi";
import { Icon } from "../components/Icons";
import IconAction from "../components/IconAction";
import SearchSelect from "../components/SearchSelect";
import { StatusPill } from "../components/StatusPill";
import { CATEGORY_LABELS, matchesDomainView } from "../components/modelBadges";
import { matchesQuery, usePageSearch } from "../context/PageSearchContext";
import type { AlgorithmField, Dataset, HpoSearchRequest, PipelineDetail, PipelineLog, ProfileColumn, TimeSeriesConfig } from "../types/domain";
import { usePermissions } from "../context/PermissionsContext";
import { PERM } from "../permissions";

const STEPS = ["Base model", "Dataset", "Features & split", "Configure", "Review & launch"];

// Clustering / anomaly detection / dimensionality reduction / embedding models
// have no target column at all — asking for one is a real mismatch, not a
// cosmetic gap.
const NO_TARGET_TASKS = new Set(["Anomaly detection", "Clustering", "Dimensionality reduction", "Embedding"]);

const DOMAIN_VIEWS = [
  { key: "all", label: "All" },
  { key: "ml", label: "Classical ML" },
  { key: "deep-learning", label: "Deep Learning" },
  { key: "llm", label: "LLM" },
] as const;

/** Strategies offered per column kind, and their labels. Mirrors the trainer's
 *  supported set exactly — anything offered here is something
 *  classical_trainer.py can actually build a transformer for. */
const PREPROCESS_OPTIONS: Record<"numeric" | "categorical", string[]> = {
  numeric: ["standard_scale", "min_max_scale", "none"],
  categorical: ["one_hot", "ordinal", "none"],
};
const PREPROCESS_LABELS: Record<string, string> = {
  standard_scale: "Standard scale (z-score)",
  min_max_scale: "Min-max scale (0–1)",
  one_hot: "One-hot encode",
  ordinal: "Ordinal encode",
  passthrough: "Pass through",
  none: "None",
};
const defaultStrategy = (kind: string) => (kind === "numeric" ? "standard_scale" : "one_hot");

const PROFILES = [
  { id: "cpu-small", spec: "2 vCPU · 8 GB", gated: false },
  { id: "cpu-large", spec: "16 vCPU · 64 GB", gated: false },
  { id: "gpu-1x", spec: "1× GPU", gated: false },
  { id: "gpu-4x", spec: "4× GPU", gated: true },
  { id: "gpu-8x", spec: "8× GPU", gated: true },
];

/** True time-series models: they fit a chronological series, not X/y rows, so
 *  the server requires a timestamp/value contract instead of a target column
 *  (routes/trainingRuns.ts). The xgboost/lightgbm forecasters are NOT here —
 *  they train on lag features like any tabular regressor. */
const DEEP_TS_IDS = new Set(["lstm-forecaster", "gru-forecaster", "temporal-fusion-transformer", "nbeats-forecaster"]);
const TS_CONTRACT_IDS = new Set([
  "prophet-forecaster", "arima-forecaster", "sarima-forecaster", "auto-arima-forecaster",
  "exponential-smoothing", "holt-winters", ...DEEP_TS_IDS,
]);

/** Objectives a search can optimize, per category — exactly the metrics the
 *  trainers report (routes/hpo.ts rejects anything else). Search is offered
 *  only where there's a supervised split with a standard objective, as in the
 *  mock; other categories need a different framing, so it's disabled there. */
const SEARCH_OBJECTIVES: Record<string, { value: HpoSearchRequest["objectiveMetric"]; label: string; direction: "maximize" | "minimize" }[]> = {
  classification: [
    { value: "accuracy", label: "val_accuracy (maximize)", direction: "maximize" },
    { value: "f1", label: "val_f1 (maximize)", direction: "maximize" },
  ],
  regression: [
    { value: "r2", label: "val_r2 (maximize)", direction: "maximize" },
    { value: "mse", label: "val_mse (minimize)", direction: "minimize" },
  ],
};
const MAX_TRIALS = 20;

/** Default search values for one field: the documented range's two ends and
 *  the default between them, or every option for a select. Pre-filled (as in
 *  the mock) so a search is launchable without typing anything. */
function defaultSearchValues(f: AlgorithmField): string {
  if (f.type === "select") return (f.options ?? []).join(", ");
  const m = String(f.range ?? "").match(/(-?[\d.]+)\s*[–-]\s*(-?[\d.]+)/);
  if (!m) return "";
  const lo = Number(m[1]), hi = Number(m[2]), def = Number(f.default);
  const vals = [lo, ...(Number.isFinite(def) && def > lo && def < hi ? [def] : []), hi];
  return [...new Set(vals)].join(", ");
}

/** Identifier-looking columns are left out of the features by default, like
 *  the mock's `id` columns — a row id carries no signal and invites leakage. */
const looksLikeId = (name: string) => /(^id$|_id$|^id_|uuid)/i.test(name);

const FT_METHODS = [
  { key: "lora", label: "LoRA / PEFT", hint: "Trains a small adapter on top of frozen base weights — cheapest, and servable via multi-LoRA alongside other adapters on one deployment." },
  { key: "qlora", label: "QLoRA (4-bit)", hint: "LoRA against a 4-bit-quantized base — fits a larger model on the same GPU at some quality cost." },
  { key: "full", label: "Full fine-tune", hint: "Updates every weight. Far more GPU memory and time, and produces its own full-size model rather than a small adapter." },
] as const;

/** Base-model tasks that can't be fine-tuned here, and why. */
const NO_TRAINER_TASKS: Record<string, string> = {
  "Text-to-speech": "Kokoro has no supported fine-tuning method. It does generate speech: open it in the Model catalog and use “Test this model” (type or paste text, pick a voice).",
};

export default function TrainPage() {
  const { can } = usePermissions();
  const [params] = useSearchParams();
  const baseModels = useApi(api.getBaseModels, []);
  const experiments = useApi(api.getExperiments, []);
  const datasets = useApi(api.getDatasets, []);
  const runs = useApi(api.getTrainingRuns, []);

  const [step, setStep] = useState(0);
  const [domainView, setDomainView] = useState<string>("all");
  const [categoryFilter, setCategoryFilter] = useState("all");
  const [baseModelId, setBaseModelId] = useState(params.get("base") ?? "");
  const [modelName, setModelName] = useState("");
  // No picker in step 5 — the mock's wizard has none. The capability is kept
  // reachable instead: Experiments can deep-link in as /train?experiment=<id>,
  // so a run launched from an experiment is still attached to it. Without this
  // the field would be permanently "" and runs could never join an experiment.
  const [experimentId, setExperimentId] = useState(params.get("experiment") ?? "");
  const [datasetName, setDatasetName] = useState("");
  const [computeProfile, setComputeProfile] = useState("cpu-small");
  const [targetColumn, setTargetColumn] = useState("");
  const [excludedFeatures, setExcludedFeatures] = useState<string[]>([]);
  const [preprocessing, setPreprocessing] = useState<Record<string, string>>({});
  const [splitTrain, setSplitTrain] = useState(70);
  const [splitVal, setSplitVal] = useState(15);
  const [splitTest, setSplitTest] = useState(15);
  const [trainMode, setTrainMode] = useState<"fixed" | "search">("fixed");
  const [hyperparams, setHyperparams] = useState<Record<string, string>>({});
  const [searchSpace, setSearchSpace] = useState<Record<string, string>>({});
  const [numTrials, setNumTrials] = useState(MAX_TRIALS);
  const [objectiveMetric, setObjectiveMetric] = useState<HpoSearchRequest["objectiveMetric"]>("accuracy");
  const [fineTuneMethod, setFineTuneMethod] = useState<"lora" | "qlora" | "full">("lora");
  const [lora, setLora] = useState({ rank: "16", alpha: "32", dropout: "0.05", learningRate: "1e-4", epochs: "3" });
  const [full, setFull] = useState({ learningRate: "2e-5", epochs: "2", warmupSteps: "100" });
  // Which columns hold the fine-tuning text: a prompt/completion pair, or one
  // column that already holds whole examples. Auto-filled from the dataset.
  const [llmCols, setLlmCols] = useState({ mode: "pair" as "pair" | "text", prompt: "", completion: "", text: "", maxSeq: "1024" });
  // Time-series contract for the true forecasting models — auto-filled from
  // the dataset's real columns, editable in step 3.
  const [ts, setTs] = useState({
    timestamp: "", value: "", entity: "", entityValue: "", frequency: "",
    horizon: "7", missing: "ffill" as NonNullable<TimeSeriesConfig["missingPeriodPolicy"]>,
  });

  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [launched, setLaunched] = useState<{ id: string; kind: "run" | "search" } | null>(null);

  const base = baseModels.data?.find((b) => b.id === baseModelId);
  // gpu-4x / gpu-8x need GPU quota (the backend checks the same permission).
  const gpuAllowed = can(PERM.gpuLarge);
  const isLlm = base?.domain === "llm";
  // Whisper fine-tunes on audio + transcripts (speech_finetune_trainer.py), not on text columns.
  const isStt = base?.task === "Speech-to-text";
  // The top-bar search filters base models on step 1 and datasets on step 2 (where the
  // step's own search box shows the same term); it starts empty on each step.
  const [dsQuery, setDsQuery] = usePageSearch(step === 0 ? "Search base models…" : step === 1 ? "Search datasets…" : "Search…");
  useEffect(() => setDsQuery(""), [step, setDsQuery]);
  const filteredDatasets = useMemo(() => {
    const q = dsQuery.trim().toLowerCase();
    const all = datasets.data ?? [];
    if (!q) return all;
    return all.filter((d) => [d.name, d.structure_type, d.source, d.detail, d.description ?? "", ...(d.tags ?? [])]
      .join(" ").toLowerCase().includes(q));
  }, [datasets.data, dsQuery]);
  const [sttMaxSamples, setSttMaxSamples] = useState("300");
  // An LLM on an unstructured dataset trains on its documents: text is extracted
  // from the PDFs / Markdown / text files and split into passages.
  const [maxPassages, setMaxPassages] = useState("3000");
  const maxPassagesNum = Number(maxPassages);
  const [sttLanguage, setSttLanguage] = useState("en");
  // ML Studio's fine-tuner trains text-generation (causal LM) models only. Speech models
  // need an audio trainer that doesn't exist here, so the wizard says so up front instead
  // of letting the run fail — or blaming the dataset — later.
  const noTrainerReason = base && NO_TRAINER_TASKS[base.task]
    ? `${base.title} can't be fine-tuned — ${NO_TRAINER_TASKS[base.task]}`
    : null;
  const isUnsupervised = !!base && NO_TARGET_TASKS.has(base.task);
  const needsImageData = base?.task === "Image classification";
  // True forecasters take a timestamp/value contract instead of a target.
  const needsTs = !!base && TS_CONTRACT_IDS.has(base.id);
  const dataset = datasets.data?.find((d) => d.name === datasetName);
  // An LLM on an unstructured dataset trains on its documents (see maxPassages above).
  const isDocLlm = isLlm && !isStt && dataset?.structure_type === "unstructured";

  // The real uploaded snapshot behind the chosen dataset — this is where the
  // genuine column names, row count and per-column types come from. Without a
  // snapshot there is no schema to show, and the wizard says so rather than
  // inventing column names from the dataset's title.
  const snapshots = useApi(
    () => (datasetName ? api.getDatasetSnapshots(datasetName) : Promise.resolve([])),
    [datasetName]
  );
  const latestSnapshotVersion = (snapshots.data ?? []).reduce((m, s) => Math.max(m, s.version), 0) || null;
  const latestSnapshotFormat = (snapshots.data ?? []).find((s) => s.version === latestSnapshotVersion)?.format ?? null;
  // Speech-to-text needs a ZIP of audio clips plus metadata.csv (file, transcript).
  const latestSnap = (snapshots.data ?? []).find((s) => s.version === latestSnapshotVersion);
  const sttTypes = (latestSnap?.fileTypes ?? (latestSnap?.profile as any)?.fileTypes ?? {}) as Record<string, number>;
  const sttAudio = ["wav", "flac", "ogg", "mp3"].reduce((n, e) => n + (sttTypes[e] ?? 0), 0);
  const DOC_TYPES = ["pdf", "md", "mdx", "txt", "rst", "html", "htm", "docx", "pptx"];
  const docCounts = (snapshots.data ?? []).filter((sn) => sn.isLatest ?? sn.version === latestSnapshotVersion).reduce((acc, sn) => {
    const types = sn.fileTypes ?? { [sn.format]: 1 };
    for (const [ext, n] of Object.entries(types)) if (DOC_TYPES.includes(ext)) acc[ext] = (acc[ext] ?? 0) + n;
    return acc;
  }, {} as Record<string, number>);
  const docTotal = Object.values(docCounts).reduce((a, b) => a + b, 0);
  const sttReady = latestSnapshotFormat === "zip" && !!sttTypes.csv && sttAudio >= 10;
  const sttError = !latestSnapshotVersion
    ? `${datasetName} has no uploaded file yet — upload a ZIP of audio files with a metadata.csv of transcripts.`
    : latestSnapshotFormat === "wav"
    ? `${datasetName} is a single WAV file — it has no transcript to learn from. Fine-tuning needs a ZIP of audio clips plus metadata.csv (file, transcript). A single WAV is for testing: open the model and use “Test this model”.`
    : `${datasetName} v${latestSnapshotVersion} needs to be a ZIP of at least 10 audio files plus metadata.csv (file, transcript) — this one is ${String(latestSnapshotFormat ?? "a file").toUpperCase()}${latestSnapshotFormat === "zip" ? ` with ${sttAudio} audio file(s)${sttTypes.csv ? "" : " and no metadata.csv"}` : ""}.`;
  // An uploaded file with no columns (a ZIP of audio/images, a PDF…) is not "nothing uploaded".
  const noTextColumnsMessage = latestSnapshotVersion
    ? `${datasetName} v${latestSnapshotVersion} is a ${String(latestSnapshotFormat ?? "file").toUpperCase()} with no text columns — LLM fine-tuning needs a CSV or JSONL with prompt/completion (or one text) column.`
    : `${datasetName} has no uploaded file yet — upload a prompt/completion CSV or JSONL first.`;
  const snapshot = useApi(
    () => (datasetName && latestSnapshotVersion ? api.getDatasetSnapshot(datasetName, latestSnapshotVersion) : Promise.resolve(null)),
    [datasetName, latestSnapshotVersion]
  );
  // Which columns can actually BE the target, graded server-side by the very
  // same function the train-time quality gate uses. Fetched rather than
  // recomputed here on purpose: a second copy of those rules in the browser
  // would drift, and a wizard that offers a target the gate later refuses is
  // exactly the failure this closes.
  const targetCandidates = useApi(
    () =>
      datasetName && latestSnapshotVersion && base?.task && !isUnsupervised && !isLlm && !needsTs
        ? api.getTargetCandidates(datasetName, base.task, latestSnapshotVersion)
        : Promise.resolve(null),
    [datasetName, latestSnapshotVersion, base?.task, isUnsupervised, isLlm, needsTs]
  );
  const candidateFor = (col: string) => targetCandidates.data?.columns.find((c) => c.column === col) ?? null;
  const targetVerdict = targetColumn ? candidateFor(targetColumn) : null;
  const targetBlocked = targetVerdict?.severity === "blocker";

  const profileColumns: ProfileColumn[] = (snapshot.data?.profile as any)?.columns ?? [];
  const columnNames: string[] = snapshot.data?.columnNames ?? dataset?.column_names ?? [];
  const rowCount: number | null = snapshot.data?.rowCount ?? null;

  // Pre-select the usual instruction-tuning column names so a standard
  // prompt/completion file needs no clicks; anything else is picked below.
  const pickCol = (names: string[]) => columnNames.find((c) => names.includes(c.toLowerCase())) ?? "";
  const guessPrompt = pickCol(["prompt", "instruction", "input", "question", "query"]);
  const guessCompletion = pickCol(["completion", "response", "output", "answer", "target"]);
  const guessText = pickCol(["text", "content", "document", "messages", "conversation"]);
  // The mock's "Dataset format check": fine-tuning needs instruction/chat
  // shaped examples — a prompt + completion pair, or one column of whole
  // examples. A tabular file (sensor readings, churn rows…) has neither, and
  // the wizard stops here rather than spending GPU time on it.
  const llmChatShaped = columnNames.length > 0 && ((!!guessPrompt && !!guessCompletion && guessPrompt !== guessCompletion) || !!guessText);
  const llmSample = (snapshot.data?.profile as any)?.sampleRows?.[0] as Record<string, unknown> | undefined;

  useEffect(() => {
    if (!isLlm) return;
    const prompt = guessPrompt, completion = guessCompletion, text = guessText;
    setLlmCols((c) => ({
      ...c,
      mode: prompt && completion ? "pair" : text ? "text" : c.mode,
      prompt: columnNames.includes(c.prompt) ? c.prompt : prompt,
      completion: columnNames.includes(c.completion) ? c.completion : completion,
      text: columnNames.includes(c.text) ? c.text : text,
    }));
  }, [isLlm, columnNames.join("|")]);
  const llmColsReady = llmCols.mode === "pair"
    ? !!llmCols.prompt && !!llmCols.completion && llmCols.prompt !== llmCols.completion
    : !!llmCols.text;
  const maxSeqNum = Number(llmCols.maxSeq);
  const maxSeqValid = Number.isInteger(maxSeqNum) && maxSeqNum >= 64 && maxSeqNum <= 8192;

  // Default the target once a real schema arrives — never guessed when there is
  // no schema.
  //
  // This used to take the LAST column unconditionally, which is a convention,
  // not a fact about the data. On a dataset whose last column happens to be a
  // URL, the wizard opened pre-set to a target no classifier can predict, and
  // the run failed after launch. Once grading is available, prefer a column the
  // gate will actually accept; the positional guess stays as the fallback for
  // an unprofiled snapshot.
  useEffect(() => {
    if (!columnNames.length) { setTargetColumn(""); return; }
    const graded = targetCandidates.data?.graded ? targetCandidates.data.columns : null;
    const firstUsable = graded
      ? [...columnNames].reverse().find((c) => graded.find((g) => g.column === c)?.severity == null)
      : null;
    const fallback = firstUsable ?? columnNames[columnNames.length - 1];
    setTargetColumn((t) => {
      if (!t || !columnNames.includes(t)) return fallback;
      // Already-chosen target turned out to be unusable (grading arrived after
      // the pick) — move off it rather than leaving a doomed selection in place.
      if (graded && graded.find((g) => g.column === t)?.severity === "blocker") return fallback;
      return t;
    });
  }, [columnNames.join("|"), targetCandidates.data]);

  // LLM fine-tuning always needs a GPU; nudge off a CPU profile once we know.
  useEffect(() => {
    if (isLlm) setComputeProfile((p) => (p.startsWith("cpu") ? "gpu-1x" : p));
  }, [isLlm]);

  // The mock opens with the first library entry already selected, so step 1 has
  // a valid answer before you touch anything. Now that the API preserves the
  // curated order, "first" is xgboost-classifier — the recommended default —
  // rather than whatever sorts first alphabetically.
  useEffect(() => {
    if (baseModelId || !baseModels.data?.length) return;
    const fromQuery = params.get("base");
    setBaseModelId(fromQuery && baseModels.data.some((b) => b.id === fromQuery) ? fromQuery : baseModels.data[0].id);
  }, [baseModels.data, baseModelId, params]);

  const categories = useMemo(
    () => [...new Set((baseModels.data ?? []).map((b) => b.category).filter((c): c is string => !!c))],
    [baseModels.data]
  );
  const filteredBase = useMemo(
    () =>
      (baseModels.data ?? []).filter(
        (b) => matchesDomainView(b, domainView) && (categoryFilter === "all" || b.category === categoryFilter)
          && (step !== 0 || matchesQuery(dsQuery, b.id, b.title, b.task, b.framework, b.category, b.domain))
      ),
    [baseModels.data, domainView, categoryFilter, step, dsQuery]
  );

  // The tunable surface of whatever trainer this base model dispatches to.
  // Several library entries share one spec, so it's keyed by algorithm_key
  // rather than by base model id.
  const algorithm = useApi(
    () => (base?.algorithm_key ? api.getAlgorithm(base.algorithm_key) : Promise.resolve(null)),
    [base?.algorithm_key]
  );
  const variant = base?.algorithm_variant ?? algorithm.data?.defaultAppliesTo ?? null;
  const algoFields: AlgorithmField[] = (algorithm.data?.fields ?? []).filter(
    (f) => !f.showIf || (variant !== null && f.showIf.includes(variant))
  );
  const algorithmLabel = base
    ? `${base.algorithm_key ?? base.framework}${variant ? ` (${variant})` : ""}`
    : "—";
  const features = columnNames.filter((c) => c !== targetColumn && !excludedFeatures.includes(c));

  // Hyperparameter search: only where the trainers report a standard
  // objective (classification / regression), and only over fields with a real
  // range or choice list — required ones first, capped at four like the mock
  // so the form stays usable for algorithms with a dozen knobs.
  const searchCategory = base && base.task !== "Ranking" && !needsImageData && base.category && SEARCH_OBJECTIVES[base.category]
    ? base.category : null;
  const searchFields = algoFields
    .filter((f) => (f.type === "number" && f.range) || f.type === "select")
    .sort((a, b) => Number(!!b.required) - Number(!!a.required))
    .slice(0, 4);
  const searchEligible = !isLlm && !!searchCategory && searchFields.length > 0;
  const searchIneligibleReason = !base || isLlm ? "" : !searchCategory
    ? `Hyperparameter search isn't offered for ${base.task} — it needs a different objective than a train/validation split provides. Use Fixed hyperparameters.`
    : "This algorithm doesn't have enough tunable hyperparameters for a meaningful search — use Fixed hyperparameters.";
  const objectives = searchCategory ? SEARCH_OBJECTIVES[searchCategory] : [];
  const objective = objectives.find((o) => o.value === objectiveMetric) ?? objectives[0];

  // A new base model means a different tunable surface: start its fields from
  // their defaults, pre-fill the search ranges, and leave search mode if it
  // doesn't apply. Without this, values typed for one algorithm carried over
  // to another that happens to share a field name.
  useEffect(() => {
    setHyperparams({});
    setSearchSpace(Object.fromEntries(searchFields.map((f) => [f.name, defaultSearchValues(f)])));
    if (objectives.length) setObjectiveMetric(objectives[0].value);
    if (!searchEligible) setTrainMode("fixed");
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [baseModelId, algorithm.data]);

  // A new dataset means new columns: forget the old exclusions/strategies and
  // leave identifier columns out by default.
  useEffect(() => {
    setExcludedFeatures(columnNames.filter(looksLikeId));
    setPreprocessing({});
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [datasetName, columnNames.join("|")]);

  // Pre-fill the time-series contract from the real columns: a date-like
  // column for time, the last numeric one for the value.
  useEffect(() => {
    if (!needsTs || !columnNames.length) return;
    const isTimeLike = (c: string) => /(date|timestamp|^ds$|^time$|_at$|_time$|^period$|^month$|^week$)/i.test(c)
      || /date|time/i.test(profileColumns.find((p) => p.name === c)?.dtype ?? "");
    const timestamp = columnNames.find(isTimeLike) ?? "";
    const numeric = profileColumns.filter((p) => p.kind === "numeric" && p.name !== timestamp && !looksLikeId(p.name)).map((p) => p.name);
    const value = numeric[numeric.length - 1] ?? "";
    setTs((t) => ({
      ...t,
      timestamp: columnNames.includes(t.timestamp) ? t.timestamp : timestamp,
      value: columnNames.includes(t.value) ? t.value : value,
      entity: columnNames.includes(t.entity) ? t.entity : "",
    }));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [needsTs, columnNames.join("|"), profileColumns.length]);
  const tsReady = !!ts.timestamp && !!ts.value && ts.timestamp !== ts.value
    && (!ts.entity || DEEP_TS_IDS.has(baseModelId) || !!ts.entityValue.trim());
  const tsError = !ts.timestamp || !ts.value ? "Pick the timestamp column and the value column to forecast."
    : ts.timestamp === ts.value ? "The timestamp and value columns have to be different."
    : "This model trains one series at a time — give the entity value to train on, or clear the entity column.";
  const timeSeriesConfig: TimeSeriesConfig | undefined = needsTs && columnNames.length
    ? {
        timestampColumn: ts.timestamp,
        valueColumn: ts.value,
        ...(ts.entity ? { entityColumn: ts.entity } : {}),
        ...(ts.entity && ts.entityValue.trim() ? { entityValue: ts.entityValue.trim() } : {}),
        ...(ts.frequency.trim() ? { frequency: ts.frequency.trim() } : {}),
        missingPeriodPolicy: ts.missing,
        ...(Number(ts.horizon) > 0 ? { predictionHorizon: Math.min(365, Math.round(Number(ts.horizon))) } : {}),
      }
    : undefined;
  const splitTotal = splitTrain + splitVal + splitTest;

  /**
   * What the trained model gets registered as.
   *
   * POST /api/training-runs requires a name; the mock never asks for one
   * because its Launch button calls nothing. Rather than add a field the design
   * doesn't have, the name is derived from the two things that actually
   * identify the run — the base model and the dataset — and shown read-only in
   * the Run summary, so it is visible before you commit to it.
   *
   * `modelName` stays state so an explicit choice (a rerun, or a future rename
   * control) still wins over the derived value.
   */
  const derivedModelName = base && datasetName ? `${base.id}-${datasetName}` : "";
  const effectiveModelName = modelName.trim() || derivedModelName;

  function next() {
    setError(null);
    if (step === 0 && !baseModelId) return setError("Pick a base model first.");
    if (step === 2 && noTrainerReason) return setError(noTrainerReason);
    if (step === 1 && !datasetName) return setError("Pick a dataset first.");
    // Documents (PDF/DOCX/TXT…) have no rows or columns to fit a tabular,
    // forecasting or deep model on — catch it here, not as a failed run.
    if (step === 1 && !isLlm && dataset?.structure_type === "unstructured")
      return setError(`${datasetName} is an unstructured dataset — ${base?.id} trains on tabular rows. Pick a structured dataset.`);
    if (step === 2 && needsTs && columnNames.length && !tsReady) return setError(tsError);
    if (step === 2 && !isLlm && !isUnsupervised && !needsTs && columnNames.length) {
      if (!targetColumn) return setError("Pick a target column.");
      if (targetBlocked) return setError(`'${targetColumn}' can't be a target — ${targetVerdict?.reason ?? "pick a different column."}`);
      if (!features.length) return setError("Include at least one feature column.");
    }
    if (step === 3 && trainMode === "search" && searchEligible) {
      if (!latestSnapshotVersion) return setError("Hyperparameter search needs an uploaded dataset snapshot — every trial trains on the same pinned version.");
      if (!Object.values(searchSpace).some((v) => v.split(",").some((x) => x.trim()))) return setError("Give at least one hyperparameter a list of values to search over.");
      if (!Number.isInteger(numTrials) || numTrials < 1 || numTrials > MAX_TRIALS) return setError(`Number of trials has to be between 1 and ${MAX_TRIALS}.`);
    }
    if (step === 2 && isStt && !sttReady) return setError(sttError);
    if (step === 3 && isStt && !(Number(sttMaxSamples) >= 10 && Number(sttMaxSamples) <= 20000)) return setError("Max clips has to be between 10 and 20,000.");
    if (step === 2 && isDocLlm && !docTotal) return setError(`${datasetName} has no documents to learn from — upload PDF, Markdown, text, HTML, Word or PowerPoint files.`);
    if (step === 2 && isDocLlm && !maxSeqValid) return setError("Max sequence length has to be a whole number between 64 and 8192.");
    if (step === 2 && isDocLlm && !(Number.isInteger(maxPassagesNum) && maxPassagesNum >= 10)) return setError("Max passages has to be a whole number of at least 10.");
    if (step === 2 && isLlm && !isStt && !isDocLlm && !columnNames.length) return setError(noTextColumnsMessage);
    if (step === 2 && isLlm && !isStt && !isDocLlm && !llmChatShaped)
      return setError(`${datasetName} doesn't look chat-shaped — it has a fixed tabular schema (${columnNames.slice(0, 4).join(", ")}${columnNames.length > 4 ? ", …" : ""}) and no prompt/completion or text column. Pick a different dataset in step 2.`);
    if (step === 2 && isLlm && !isStt && !isDocLlm && !llmColsReady) return setError(llmCols.mode === "pair" ? "Pick two different columns for the prompt and the completion." : "Pick the column that holds the training text.");
    if (step === 2 && isLlm && !isStt && !maxSeqValid) return setError("Max sequence length has to be a whole number between 64 and 8192.");
    if (step === 2 && !isLlm && splitTotal !== 100) return setError(`The split has to total 100% — it currently totals ${splitTotal}%.`);
    setStep((s) => Math.min(s + 1, STEPS.length - 1));
  }
  function back() { setError(null); setStep((s) => Math.max(s - 1, 0)); }

  /** Numbers typed into hyperparameter fields come back as strings; send the
   *  declared type so server-side range checks compare like with like. */
  function coerceHp(): Record<string, string | number | boolean> | undefined {
    const out: Record<string, string | number | boolean> = {};
    for (const field of algoFields) {
      const raw = hyperparams[field.name];
      if (raw === "" || raw === undefined) continue;
      out[field.name] = field.type === "number" ? Number(raw) : raw;
    }
    return Object.keys(out).length ? out : undefined;
  }

  async function launch() {
    if (!effectiveModelName) {
      setError("Pick a base model and a dataset first — the registered model name is derived from them.");
      return;
    }
    setSubmitting(true);
    setError(null);
    try {
      if (trainMode === "search" && searchEligible) {
        const space: Record<string, (string | number | number[])[]> = {};
        for (const [k, raw] of Object.entries(searchSpace)) {
          if (!searchFields.some((f) => f.name === k)) continue;
          const values = raw.split(",").map((v) => v.trim()).filter(Boolean);
          if (!values.length) continue;
          space[k] = values.map((v) => (Number.isNaN(Number(v)) ? v : Number(v)));
        }
        if (!Object.keys(space).length) throw new Error("Give at least one hyperparameter a comma-separated list of values to search over.");
        const search = await api.createHpoSearch({
          experimentId: experimentId || undefined,
          baseModelId,
          datasetName,
          datasetVersion: latestSnapshotVersion ?? undefined,
          targetColumn: isUnsupervised ? undefined : targetColumn,
          searchSpace: space,
          numTrials,
          objectiveMetric: objective.value,
          objectiveDirection: objective.direction,
        });
        setLaunched({ id: search.id, kind: "search" });
      } else {
        const run = await api.createTrainingRun({
          baseModelId,
          modelName: effectiveModelName,
          datasetName,
          datasetVersion: latestSnapshotVersion ?? undefined,
          computeProfile,
          experimentId: experimentId || undefined,
          ...(isStt
            ? {
                fineTuneMethod: "lora" as const,
                loraParams: {
                  rank: Number(lora.rank), alpha: Number(lora.alpha), dropout: Number(lora.dropout),
                  learningRate: lora.learningRate, epochs: Number(lora.epochs),
                },
                speechConfig: { maxSamples: Number(sttMaxSamples), language: sttLanguage.trim() || "en" },
              }
            : isLlm
            ? {
                fineTuneMethod,
                llmFineTuneConfig: isDocLlm ? { textColumn: "text", fromDocuments: true, maxSeqLength: maxSeqNum, maxPassages: maxPassagesNum } : {
                  ...(llmCols.mode === "pair"
                    ? { promptColumn: llmCols.prompt, completionColumn: llmCols.completion }
                    : { textColumn: llmCols.text }),
                  maxSeqLength: maxSeqNum,
                },
                ...(fineTuneMethod === "full"
                  ? { fullParams: { learningRate: full.learningRate, epochs: Number(full.epochs), warmupSteps: Number(full.warmupSteps) } }
                  : {
                      loraParams: {
                        rank: Number(lora.rank), alpha: Number(lora.alpha), dropout: Number(lora.dropout),
                        learningRate: lora.learningRate, epochs: Number(lora.epochs),
                      },
                    }),
              }
            : needsTs
            ? {
                // A forecaster's "target" is the value column in its
                // contract; it takes no feature list or column transforms.
                ...(timeSeriesConfig ? { timeSeriesConfig } : {}),
                splitTrain, splitVal, splitTest,
                hyperparams: coerceHp(),
              }
            : {
                ...(isUnsupervised ? {} : { targetColumn, features }),
                splitTrain, splitVal, splitTest,
                hyperparams: coerceHp(),
                // Only the included features — sending a strategy for an
                // excluded column would be rejected as "not in the training set".
                preprocessing: Object.fromEntries(
                  Object.entries(preprocessing).filter(
                    ([col]) => col !== targetColumn && !excludedFeatures.includes(col) && columnNames.includes(col)
                  )
                ),
              }),
        });
        setLaunched({ id: run.id, kind: "run" });
      }
      runs.reload();
    } catch (e) {
      setError(e instanceof ApiRequestError || e instanceof Error ? e.message : "Failed to launch");
    } finally {
      setSubmitting(false);
    }
  }

  function reset() {
    setStep(0); setBaseModelId(""); setModelName(""); setDatasetName("");
    setLaunched(null); setError(null); setHyperparams({}); setSearchSpace({}); setPreprocessing({});
    setExperimentId(params.get("experiment") ?? "");
  }

  return (
    <div>
      <div className="page-fixed">
        <div className="page-head">
          <div>
            <h1>Train wizard</h1>
            <p>Pick a base model, point at a dataset, choose how much compute — the rest runs as one Argo Workflow.</p>
          </div>
        </div>

        <div className="wizard-steps">
          {STEPS.map((s, i) => (
            <div key={s} style={{ display: "contents" }}>
              {i > 0 && <div className={"wizard-connector" + (i <= step ? " done" : "")} />}
              <div className={"wizard-step" + (i === step ? " current" : i < step ? " done" : "")}>
                <div className="wizard-step-num">{i + 1}</div>
                <div className="wizard-step-label">{s}</div>
              </div>
            </div>
          ))}
        </div>

        {/* Step 1's domain/category filter stays pinned with the wizard bar. */}
        {step === 0 && (
          <>
            <div className="tabs tabs-with-tools" role="tablist" aria-label="Filter base models by domain">
              {DOMAIN_VIEWS.map((d) => (
                <button
                  type="button" role="tab" key={d.key} className={"tab" + (domainView === d.key ? " active" : "")} aria-selected={domainView === d.key}
                  onClick={() => { setDomainView(d.key); if (d.key !== "ml") setCategoryFilter("all"); }}
                >
                  {d.label}
                </button>
              ))}
              {/* Category only makes sense for Classical ML — LLM entries aren't
                  categorized this way, and "All" would mix uncategorized entries
                  in with a filter that can't apply to them. A dropdown on the
                  row's right end rather than a second tab row: it has up to
                  seven options. A div, not a <label>: .view-body label is
                  display:block with margins, which would undo margin-left:auto. */}
              {domainView === "ml" && (
                <div className="inline-filter tabs-tool-end">
                  <span aria-hidden="true">Category</span>
                  <select className="sm" aria-label="Category" value={categoryFilter} onChange={(e) => setCategoryFilter(e.target.value)}>
                    <option value="all">All categories</option>
                    {categories.map((c) => (
                      <option key={c} value={c}>{CATEGORY_LABELS[c] ?? c}</option>
                    ))}
                  </select>
                </div>
              )}
            </div>

            <div style={{ fontSize: 12, color: "var(--text-faint)", margin: "-6px 0 14px" }}>
              {filteredBase.length} base model{filteredBase.length === 1 ? "" : "s"}
            </div>
          </>
        )}
      </div>

      {error && <div className="callout callout-amber" style={{ marginBottom: 14 }}>{error}</div>}

      {/* ---------------- STEP 1 — base model ---------------- */}
      {step === 0 && (
        <>
          <div className="opt-grid">
            {filteredBase.map((b) => (
              <div
                key={b.id}
                className={"opt-card" + (baseModelId === b.id ? " selected" : "")}
                onClick={() => setBaseModelId(b.id)}
              >
                <div className="opt-card-head">
                  <span className="opt-card-title">{b.title}</span>
                  <span className="opt-radio" />
                </div>
                <div className="opt-card-desc">{b.description}</div>
              </div>
            ))}
            {filteredBase.length === 0 && <div className="empty-hint" style={{ gridColumn: "1/-1" }}>No base models match this filter.</div>}
          </div>

        </>
      )}

      {/* ---------------- STEP 2 — dataset ---------------- */}
      {step === 1 && (
        <>
          <div style={{ display: "flex", alignItems: "center", gap: 12, marginBottom: 12 }}>
            <div className="search-box" style={{ flex: 1, maxWidth: 520 }}>
              <Icon name="search" />
              <input type="text" value={dsQuery} onChange={(e) => setDsQuery(e.target.value)} autoFocus
                placeholder="Search datasets by name, tag, description or type…" aria-label="Search datasets" />
              {dsQuery && (
                <IconAction icon="x" label="Clear dataset search" size="sm" align="end" onClick={() => setDsQuery("")} />
              )}
            </div>
            <span className="cell-sub">
              {datasets.data ? `${filteredDatasets.length} of ${datasets.data.length} datasets` : ""}
              {datasetName && <> · selected <strong>{datasetName}</strong></>}
            </span>
          </div>
          <div className="table-wrap" style={{ marginBottom: 16 }}>
            <table>
              <thead>
                <tr>
                  <th style={{ width: "34%" }}>Dataset</th>
                  <th style={{ width: "16%" }}>Source</th>
                  <th style={{ width: "16%" }}>Size</th>
                  <th style={{ width: "34%" }}>Details</th>
                </tr>
              </thead>
              <tbody>
                {filteredDatasets.map((d: Dataset) => (
                  <tr
                    key={d.name}
                    onClick={() => setDatasetName(d.name)}
                    style={datasetName === d.name ? { background: "var(--amber-100)" } : undefined}
                  >
                    <td>
                      <div className="cell-name">{d.name}</div>
                      <div className="cell-sub">{d.rows} rows · {d.structure_type}</div>
                    </td>
                    <td><span className="tag">{d.source}</span></td>
                    <td>{d.size}</td>
                    <td style={{ fontSize: 12, color: "var(--text-muted)" }}>{d.detail}</td>
                  </tr>
                ))}
                {!datasets.data?.length && (
                  <tr><td colSpan={4}><div className="empty-hint">No datasets registered yet — add one from the Datasets page first.</div></td></tr>
                )}
                {!!datasets.data?.length && !filteredDatasets.length && (
                  <tr><td colSpan={4}><div className="empty-hint">No datasets match “{dsQuery}”.</div></td></tr>
                )}
              </tbody>
            </table>
          </div>
          <div className="callout callout-navy">
            Datasets up to <strong>20&nbsp;GB</strong> are supported. <strong>Iceberg-backed</strong> datasets
            can pin an exact snapshot, so what trained this model stays reproducible even if the live
            table changes later.
          </div>
        </>
      )}

      {/* ---------------- STEP 3 — features & split ---------------- */}
      {step === 2 && (
        <>
          {isStt && (
            <div className="card card-pad" style={{ marginBottom: 16 }}>
              <div className="drawer-section-title">Audio and transcripts</div>
              <p className="hint" style={{ marginBottom: 14 }}>
                Whisper learns from audio paired with what was said: a ZIP of audio clips (WAV, FLAC, OGG) plus
                <code> metadata.csv</code> with a <code>file</code> column and a <code>transcript</code> column.
              </p>
              {sttReady ? (
                <div className="callout callout-teal">
                  ✓ {datasetName} v{latestSnapshotVersion}: {sttAudio.toLocaleString()} audio clips with metadata.csv — ready to fine-tune.
                </div>
              ) : (
                <div className="callout callout-amber">⚠ {sttError}</div>
              )}
            </div>
          )}

          {isLlm && !isStt && columnNames.length > 0 && (
            <div className="card card-pad" style={{ marginBottom: 16 }}>
              <div className="drawer-section-title">Dataset format check</div>
              <p className="hint" style={{ marginBottom: 14 }}>
                Fine-tuning uses chat/instruction-formatted examples, not a target column — this checks the
                dataset actually looks like that before you spend compute on it.
              </p>
              {llmChatShaped ? (
                <>
                  <div className="callout callout-teal" style={{ marginBottom: 0 }}>
                    <strong>✓ Looks chat/instruction-shaped.</strong> {datasetName} has{" "}
                    {guessPrompt && guessCompletion
                      ? <>a <strong>{guessPrompt}</strong> → <strong>{guessCompletion}</strong> pair</>
                      : <>a <strong>{guessText}</strong> column of whole examples</>}
                    , consistent with a fine-tuning corpus rather than tabular data.
                  </div>
                  {llmSample && (
                    <div className="card card-pad" style={{ marginTop: 14, background: "var(--surface-sunken)" }}>
                      <div style={{ fontSize: 11, color: "var(--text-faint)", textTransform: "uppercase", letterSpacing: ".04em", marginBottom: 8 }}>
                        Sample record · first row of v{latestSnapshotVersion}
                      </div>
                      <pre style={{ margin: 0, fontFamily: "var(--font-mono)", fontSize: 11.5, whiteSpace: "pre-wrap", wordBreak: "break-word" }}>
                        {JSON.stringify(
                          Object.fromEntries(Object.entries(llmSample).map(([k, v]) => [k, typeof v === "string" && v.length > 280 ? v.slice(0, 280) + "…" : v])),
                          null, 2
                        )}
                      </pre>
                    </div>
                  )}
                </>
              ) : (
                <div className="callout callout-amber" style={{ marginBottom: 0 }}>
                  <strong>⚠ Doesn't look chat-shaped.</strong> {datasetName} has a fixed tabular column schema — that's
                  normally a sign this dataset was built for a classical ML task, not fine-tuning. Pick a different
                  dataset in step 2 — one with prompt/completion columns or a single text column.
                </div>
              )}
            </div>
          )}

          {noTrainerReason && (
            <div className="card card-pad" style={{ marginBottom: 16 }}>
              <div className="drawer-section-title">Fine-tuning not available</div>
              <div className="callout callout-amber">⚠ {noTrainerReason}</div>
            </div>
          )}

          {isDocLlm && (
            <div className="card card-pad" style={{ marginBottom: 16 }}>
              <div className="drawer-section-title">Training text — from documents</div>
              <p className="hint" style={{ marginBottom: 14 }}>
                The text of every document in the latest version of each file is extracted and split into passages
                (about 500 tokens each, at paragraph breaks). The model is fine-tuned on those passages, so it learns their
                content, terms and code style.
              </p>
              {docTotal ? (
                <div className="callout callout-teal" style={{ marginBottom: 14 }}>
                  ✓ {docTotal.toLocaleString()} documents found — {Object.entries(docCounts).sort((a, b) => b[1] - a[1]).map(([k, v]) => `${v.toLocaleString()} ${k.toUpperCase()}`).join(", ")}
                </div>
              ) : (
                <div className="callout callout-amber" style={{ marginBottom: 14 }}>⚠ No PDF, Markdown, text, HTML, Word or PowerPoint documents in {datasetName}.</div>
              )}
              <div className="field-row">
                <div className="field">
                  <label>Max passages</label>
                  <input className="input" inputMode="numeric" value={maxPassages} onChange={(e) => setMaxPassages(e.target.value)} />
                  <div className="hint">A large book set can produce tens of thousands of passages; this takes an even sample across all documents. Higher = longer training.</div>
                </div>
                <div className="field">
                  <label>Max sequence length (tokens)</label>
                  <input className="input" inputMode="numeric" value={llmCols.maxSeq} onChange={(e) => setLlmCols((c) => ({ ...c, maxSeq: e.target.value }))} />
                  <div className="hint">Passages are ~500 tokens, so 512–1024 fits them.</div>
                </div>
              </div>
            </div>
          )}

          {!noTrainerReason && isLlm && !isStt && !isDocLlm && (!columnNames.length || llmChatShaped) && (
            <div className="card card-pad" style={{ marginBottom: 16 }}>
              <div className="drawer-section-title">Training text</div>
              <p className="hint" style={{ marginBottom: 14 }}>
                Fine-tuning learns from text, not a target column. Choose the columns that hold
                each example — a prompt and the completion the model should produce, or one
                column that already contains whole examples.
              </p>
              {!columnNames.length ? (
                <div className="callout callout-amber">
                  ⚠ {noTextColumnsMessage}
                </div>
              ) : (
                <>
                  <div className="tabs" role="tablist" style={{ marginBottom: 12 }}>
                    <button type="button" role="tab" className={"tab" + (llmCols.mode === "pair" ? " active" : "")} aria-selected={llmCols.mode === "pair"} onClick={() => setLlmCols((c) => ({ ...c, mode: "pair" }))}>Prompt + completion</button>
                    <button type="button" role="tab" className={"tab" + (llmCols.mode === "text" ? " active" : "")} aria-selected={llmCols.mode === "text"} onClick={() => setLlmCols((c) => ({ ...c, mode: "text" }))}>Single text column</button>
                  </div>
                  <div className="field-row">
                    {llmCols.mode === "pair" ? (
                      <>
                        <div className="field"><label id="llm-prompt-col-label">Prompt column</label>
                          <SearchSelect labelledBy="llm-prompt-col-label" noun="column" placeholder="Choose…" value={llmCols.prompt}
                            options={columnNames.map((c) => ({ value: c, label: c }))}
                            onChange={(v) => setLlmCols((c) => ({ ...c, prompt: v }))} />
                        </div>
                        <div className="field"><label id="llm-completion-col-label">Completion column</label>
                          <SearchSelect labelledBy="llm-completion-col-label" noun="column" placeholder="Choose…" value={llmCols.completion}
                            options={columnNames.map((c) => ({ value: c, label: c }))}
                            onChange={(v) => setLlmCols((c) => ({ ...c, completion: v }))} />
                        </div>
                      </>
                    ) : (
                      <div className="field"><label id="llm-text-col-label">Text column</label>
                        <SearchSelect labelledBy="llm-text-col-label" noun="column" placeholder="Choose…" value={llmCols.text}
                          options={columnNames.map((c) => ({ value: c, label: c }))}
                          onChange={(v) => setLlmCols((c) => ({ ...c, text: v }))} />
                      </div>
                    )}
                    <div className="field"><label>Max sequence length (tokens)</label>
                      <input className="input" inputMode="numeric" value={llmCols.maxSeq} onChange={(e) => setLlmCols((c) => ({ ...c, maxSeq: e.target.value }))} />
                    </div>
                  </div>
                  <div className="hint">Longer examples are cut at this length. Longer lengths cost more memory and time.</div>
                  {llmColsReady ? (
                    <div className="callout callout-teal" style={{ marginTop: 12, marginBottom: 0 }}>
                      ✓ {rowCount != null ? `${rowCount.toLocaleString()} examples` : "Examples"} will train on{" "}
                      {llmCols.mode === "pair" ? <><strong>{llmCols.prompt}</strong> → <strong>{llmCols.completion}</strong></> : <strong>{llmCols.text}</strong>}.
                    </div>
                  ) : (
                    <div className="callout callout-amber" style={{ marginTop: 12, marginBottom: 0 }}>
                      {llmCols.mode === "pair" ? "Pick two different columns for the prompt and the completion." : "Pick the column that holds the training text."}
                    </div>
                  )}
                </>
              )}
            </div>
          )}

          {!isLlm && needsImageData && (
            <div className="card card-pad" style={{ marginBottom: 16, border: "1px solid var(--amber)" }}>
              <div className="drawer-section-title" style={{ color: "var(--amber-700)" }}>⚠ No image datasets available</div>
              <p className="hint" style={{ marginBottom: 0 }}>
                {base?.id} expects image input, but every dataset registered here is tabular or text.
                There's no way to train this model meaningfully against them.
              </p>
            </div>
          )}

          {!isLlm && isUnsupervised && (
            <div className="card card-pad" style={{ marginBottom: 16 }}>
              <div className="drawer-section-title">No target column needed</div>
              <p className="hint" style={{ marginBottom: 0 }}>
                {base?.id} is unsupervised — it works directly on the dataset's features with no
                target/label to predict. Every column in the dataset is used as input; there's nothing
                to select here. Split and preprocessing below still apply.
              </p>
            </div>
          )}

          {needsTs && (
            <div className="card card-pad" style={{ marginBottom: 16 }}>
              <div className="drawer-section-title">Time-series setup</div>
              <p className="hint" style={{ marginBottom: 14 }}>
                {base?.id} fits one chronological series rather than independent rows — there's no
                target column, it forecasts the value column forward in time.
              </p>
              {!columnNames.length ? (
                <div className="empty-hint">
                  {datasetName} has no uploaded snapshot yet, so there are no real columns to map. Upload a
                  file with a date column and a numeric value column on the Datasets page first.
                </div>
              ) : (
                <>
                  <div className="field-row">
                    <div className="field"><label id="ts-time-label">Timestamp column</label>
                      <SearchSelect labelledBy="ts-time-label" noun="column" placeholder="Choose…" value={ts.timestamp}
                        options={columnNames.map((c) => ({ value: c, label: c, hint: profileColumns.find((p) => p.name === c)?.dtype }))}
                        onChange={(v) => setTs((t) => ({ ...t, timestamp: v }))} />
                    </div>
                    <div className="field"><label id="ts-value-label">Value to forecast</label>
                      <SearchSelect labelledBy="ts-value-label" noun="column" placeholder="Choose…" value={ts.value}
                        options={columnNames.map((c) => ({ value: c, label: c, hint: profileColumns.find((p) => p.name === c)?.dtype }))}
                        onChange={(v) => setTs((t) => ({ ...t, value: v }))} />
                    </div>
                  </div>
                  <div className="field-row">
                    <div className="field"><label id="ts-entity-label">Entity column <span style={{ color: "var(--text-faint)", fontWeight: 400 }}>(optional)</span></label>
                      <SearchSelect labelledBy="ts-entity-label" noun="column" placeholder="None — one series" value={ts.entity}
                        options={[{ value: "", label: "None — one series" }, ...columnNames.map((c) => ({ value: c, label: c }))]}
                        onChange={(v) => setTs((t) => ({ ...t, entity: v, entityValue: v ? t.entityValue : "" }))} />
                      <div className="hint">For files holding several series (e.g. one per store).</div>
                    </div>
                    {ts.entity ? (
                      <div className="field"><label>Entity value {DEEP_TS_IDS.has(baseModelId) && <span style={{ color: "var(--text-faint)", fontWeight: 400 }}>(blank = train across all)</span>}</label>
                        <input className="input" value={ts.entityValue} placeholder="e.g. store-042" onChange={(e) => setTs((t) => ({ ...t, entityValue: e.target.value }))} />
                        <div className="hint">
                          {DEEP_TS_IDS.has(baseModelId) ? "Leave blank to fit one global model over every series." : "This model trains one series at a time — name the one to train on."}
                        </div>
                      </div>
                    ) : <div />}
                  </div>
                  <div className="field-row">
                    <div className="field"><label>Frequency <span style={{ color: "var(--text-faint)", fontWeight: 400 }}>(optional)</span></label>
                      <input className="input" value={ts.frequency} placeholder="Inferred — e.g. D, W, H, MS" onChange={(e) => setTs((t) => ({ ...t, frequency: e.target.value }))} />
                    </div>
                    <div className="field"><label>Forecast horizon (periods)</label>
                      <input className="input" inputMode="numeric" value={ts.horizon} onChange={(e) => setTs((t) => ({ ...t, horizon: e.target.value }))} />
                    </div>
                  </div>
                  <div className="field" style={{ marginBottom: 0 }}><label id="ts-missing-label">Missing periods</label>
                    <SearchSelect labelledBy="ts-missing-label" noun="policy" value={ts.missing}
                      options={[
                        { value: "ffill", label: "Carry the last value forward" },
                        { value: "interpolate", label: "Interpolate" },
                        { value: "zero", label: "Fill with zero" },
                        { value: "drop", label: "Drop missing periods" },
                      ]}
                      onChange={(v) => setTs((t) => ({ ...t, missing: v as typeof t.missing }))} />
                  </div>
                  {tsReady ? (
                    <div className="callout callout-teal" style={{ marginTop: 12, marginBottom: 0 }}>
                      ✓ Forecasting <strong>{ts.value}</strong> over <strong>{ts.timestamp}</strong>
                      {ts.entity ? <> · {ts.entityValue.trim() ? <>series <strong>{ts.entityValue.trim()}</strong></> : <>one global model across <strong>{ts.entity}</strong></>}</> : null}.
                    </div>
                  ) : (
                    <div className="callout callout-amber" style={{ marginTop: 12, marginBottom: 0 }}>{tsError}</div>
                  )}
                </>
              )}
            </div>
          )}

          {!isLlm && !isUnsupervised && !needsTs && (
            <div className="card card-pad" style={{ marginBottom: 16 }}>
              <div className="drawer-section-title">Target &amp; features</div>
              {columnNames.length ? (
                <>
                  <div className="field">
                    <label id="target-column-label">Target column</label>
                    <SearchSelect labelledBy="target-column-label" noun="column" value={targetColumn} onChange={(v) => setTargetColumn(v)}
                      options={columnNames.map((c) => {
                        const v = candidateFor(c);
                        // The verdict is shown as a hint chip because a disabled
                        // option can't say why it's disabled, and "why" is
                        // the whole point. They stay selectable so you can read
                        // the full reason below rather than wonder.
                        const hint =
                          v?.severity === "blocker" ? "can't be a target"
                          : v?.severity === "warning" ? `${v.cardinality} classes, thin`
                          : undefined;
                        return { value: c, label: c, hint };
                      })} />
                    <div className="hint">
                      Read from {datasetName} v{latestSnapshotVersion} — the real uploaded snapshot's own header.
                      {targetCandidates.data?.graded
                        ? " Each column is graded by the same quality gate that runs at train time."
                        : ""}
                    </div>
                    {/* The full reason, in the words the gate itself would use.
                        Shown here so a bad target is caught while you're
                        choosing, not thirty seconds into a run. */}
                    {targetVerdict?.reason && (
                      <div
                        className={targetVerdict.severity === "blocker" ? "callout callout-red" : "callout callout-amber"}
                        style={{ marginTop: 10 }}
                      >
                        <strong>{targetVerdict.severity === "blocker" ? "Not a usable target." : "Usable, but noisy."}</strong>{" "}
                        {targetVerdict.reason}
                      </div>
                    )}
                    {targetCandidates.data && !targetCandidates.data.graded && targetCandidates.data.reason && (
                      <div className="hint" style={{ marginTop: 8 }}>{targetCandidates.data.reason}</div>
                    )}
                  </div>
                  <div className="field" style={{ marginBottom: 0 }}>
                    <label>Features</label>
                    <div className="hint" style={{ marginBottom: 8 }}>
                      Included by default — toggle off anything that shouldn't be fed to the model
                      (ids, leakage-prone columns, …).
                    </div>
                    {columnNames.filter((c) => c !== targetColumn).map((c) => (
                      <label
                        key={c}
                        style={{ display: "flex", alignItems: "center", gap: 8, padding: "6px 0", fontSize: 12.5, cursor: "pointer" }}
                      >
                        <input
                          type="checkbox"
                          checked={!excludedFeatures.includes(c)}
                          onChange={(e) =>
                            setExcludedFeatures((x) => (e.target.checked ? x.filter((n) => n !== c) : [...x, c]))
                          }
                        />
                        <span style={{ fontFamily: "var(--font-mono)" }}>{c}</span>
                        <span className="tag" style={{ marginLeft: "auto" }}>
                          {profileColumns.find((pc) => pc.name === c)?.dtype ?? "—"}
                        </span>
                      </label>
                    ))}
                  </div>
                </>
              ) : (
                // No invented column list: with no uploaded snapshot there is
                // genuinely no schema to read, and guessing one from the
                // dataset's name would be fabricating the contract training runs on.
                <div className="empty-hint">
                  {datasetName} has no uploaded snapshot yet, so there's no real column schema to pick
                  a target from. Upload a file on the Datasets page first — training resolves columns
                  from the snapshot, not from the dataset's name.
                </div>
              )}
            </div>
          )}

          {(!isLlm || (columnNames.length > 0 && !llmChatShaped)) && !needsTs && profileColumns.length > 0 && (
            <div className="card card-pad" style={{ marginBottom: 16 }}>
              <div className="drawer-section-title">Preprocessing</div>
              {/* Editable, and genuinely applied: the strategy chosen here is sent
                  on the run, and classical_trainer.py builds that column's
                  transformer from it. The fitted transformer is saved alongside
                  the model, so changing one changes what the deployed model
                  expects at serving time — not just what this page displays. */}
              <div className="hint" style={{ marginBottom: 10 }}>
                Defaults are inferred from each column's real dtype in the uploaded snapshot —
                override any of them. The fitted transformer is saved alongside the model, so
                serving preprocesses identically.
              </div>
              {profileColumns.filter((c) => c.name !== targetColumn && !excludedFeatures.includes(c.name)).map((c) => {
                const opts = PREPROCESS_OPTIONS[c.kind === "numeric" ? "numeric" : "categorical"];
                return (
                  <div className="field-row" style={{ marginBottom: 8, alignItems: "center" }} key={c.name}>
                    <div>
                      <span style={{ fontFamily: "var(--font-mono)", fontSize: 12 }}>{c.name}</span>
                      <span className="tag" style={{ marginLeft: 8 }}>{c.dtype}</span>
                    </div>
                    <SearchSelect
                      ariaLabel={`Preprocessing for ${c.name}`}
                      noun="method"
                      value={preprocessing[c.name] ?? defaultStrategy(c.kind)}
                      options={opts.map((o) => ({ value: o, label: PREPROCESS_LABELS[o] }))}
                      onChange={(v) => setPreprocessing({ ...preprocessing, [c.name]: v })}
                    />
                  </div>
                );
              })}
              {profileColumns.filter((c) => c.name !== targetColumn && !excludedFeatures.includes(c.name)).length === 0 && (
                <div className="empty-hint">No features included — switch at least one on above.</div>
              )}
            </div>
          )}

          {/* Speech fine-tuning uses its own 85 / 15 clip split (see the Configure step); a model
              that can't be fine-tuned has no split to choose. */}
          {!isStt && !noTrainerReason && !isDocLlm && (
          <div className="card card-pad">
            <div className="drawer-section-title">Train / validation / test split</div>
            <div className="hint" style={{ marginBottom: 10 }}>
              {rowCount !== null ? `${rowCount.toLocaleString()} rows in ${datasetName} v${latestSnapshotVersion}` : "Row count unavailable until a snapshot is uploaded"}
            </div>
            <div className="field-row">
              {([["Train %", splitTrain, setSplitTrain], ["Validation %", splitVal, setSplitVal], ["Test %", splitTest, setSplitTest]] as const).map(
                ([label, value, setter]) => (
                  <div className="field" key={label}>
                    <label>{label}</label>
                    <input className="input" value={value} onChange={(e) => setter(Number(e.target.value) || 0)} />
                    <div className="field-range">
                      {rowCount !== null ? `${Math.round((rowCount * value) / 100).toLocaleString()} rows` : "—"}
                    </div>
                  </div>
                )
              )}
            </div>
            <div className="hint" style={{ color: splitTotal === 100 ? undefined : "var(--amber-700)" }}>
              {splitTotal === 100 ? "Totals 100% ✓" : `Totals ${splitTotal}% — has to be 100%`}
            </div>

            <div className="table-wrap" style={{ marginTop: 14 }}>
              <table>
                <thead><tr><th style={{ width: "22%" }}>Split</th><th style={{ width: "38%" }}>Purpose</th><th style={{ width: "40%" }}>Used by</th></tr></thead>
                <tbody>
                  <tr><td className="cell-name">Training</td><td>Fit model parameters</td><td style={{ color: "var(--text-muted)", fontSize: 12 }}>Ray Train, this run</td></tr>
                  <tr><td className="cell-name">Validation</td><td>Model / hyperparameter selection</td><td style={{ color: "var(--text-muted)", fontSize: 12 }}>Batch Evaluation's regression gate</td></tr>
                  <tr><td className="cell-name">Test</td><td>Final, unbiased evaluation</td><td style={{ color: "var(--text-muted)", fontSize: 12 }}>Held out entirely — touched once, for the reported metrics</td></tr>
                </tbody>
              </table>
            </div>
          </div>
          )}
        </>
      )}

      {/* ---------------- STEP 4 — configure ---------------- */}
      {step === 3 && (
        <>
          {isStt && (
            <div className="card card-pad" style={{ marginBottom: 16 }}>
              <div className="drawer-section-title">Speech fine-tuning</div>
              <div className="hint" style={{ marginBottom: 12 }}>
                LoRA on Whisper's decoder; the audio encoder stays frozen. Word error rate is measured on held-out clips
                before and after, so the run shows whether fine-tuning helped. Each clip is encoded as a 30-second window,
                so on a laptop GPU expect roughly a minute per 25–50 clips per epoch.
              </div>
              <div className="field-row">
                <div className="field">
                  <label>Max clips</label>
                  <input className="input" value={sttMaxSamples} onChange={(e) => setSttMaxSamples(e.target.value)} />
                  <div className="hint">Clips used from the dataset (85% train / 15% eval). Lower = faster run.</div>
                </div>
                <div className="field">
                  <label>Language</label>
                  <input className="input" value={sttLanguage} onChange={(e) => setSttLanguage(e.target.value)} />
                  <div className="hint">Spoken language code, e.g. en.</div>
                </div>
              </div>
            </div>
          )}
          {isLlm ? (
            <div className="card card-pad" style={{ marginBottom: 16 }}>
              {!isStt && <div className="field">
                <label>Fine-tuning method</label>
                <div className="tabs" role="tablist" style={{ marginBottom: 0 }}>
                  {FT_METHODS.map((m) => (
                    <button type="button" role="tab" key={m.key} className={"tab" + (fineTuneMethod === m.key ? " active" : "")} aria-selected={fineTuneMethod === m.key} onClick={() => setFineTuneMethod(m.key)}>
                      {m.label}
                    </button>
                  ))}
                </div>
                <div className="hint" style={{ marginTop: 6 }}>{FT_METHODS.find((m) => m.key === fineTuneMethod)?.hint}</div>
              </div>}

              {isStt || fineTuneMethod !== "full" ? (
                <>
                  <div className="field-row">
                    <div className="field">
                      <label>Rank (r)</label>
                      <input className="input" value={lora.rank} onChange={(e) => setLora({ ...lora, rank: e.target.value })} />
                      <div className="hint">Higher = more capacity, more memory. 8–16 covers most tone/style adapters.</div>
                    </div>
                    <div className="field">
                      <label>Alpha</label>
                      <input className="input" value={lora.alpha} onChange={(e) => setLora({ ...lora, alpha: e.target.value })} />
                      <div className="hint">Scaling factor — commonly 2× rank.</div>
                    </div>
                  </div>
                  <div className="field-row">
                    <div className="field">
                      <label>Dropout</label>
                      <input className="input" value={lora.dropout} onChange={(e) => setLora({ ...lora, dropout: e.target.value })} />
                    </div>
                    <div className="field">
                      <label>Learning rate</label>
                      <input className="input" value={lora.learningRate} onChange={(e) => setLora({ ...lora, learningRate: e.target.value })} />
                    </div>
                  </div>
                  <div className="field" style={{ marginBottom: 0 }}>
                    <label>Epochs</label>
                    <input className="input" value={lora.epochs} onChange={(e) => setLora({ ...lora, epochs: e.target.value })} />
                  </div>
                </>
              ) : (
                <>
                  <div className="callout callout-amber" style={{ marginBottom: 14 }}>
                    Full fine-tuning updates every weight — far more GPU memory and time than LoRA, and
                    produces its own full-size model rather than a small adapter. Only worth it when
                    LoRA genuinely isn't enough.
                  </div>
                  <div className="field-row">
                    <div className="field">
                      <label>Learning rate</label>
                      <input className="input" value={full.learningRate} onChange={(e) => setFull({ ...full, learningRate: e.target.value })} />
                    </div>
                    <div className="field">
                      <label>Epochs</label>
                      <input className="input" value={full.epochs} onChange={(e) => setFull({ ...full, epochs: e.target.value })} />
                    </div>
                  </div>
                  <div className="field" style={{ marginBottom: 0 }}>
                    <label>Warmup steps</label>
                    <input className="input" value={full.warmupSteps} onChange={(e) => setFull({ ...full, warmupSteps: e.target.value })} />
                  </div>
                </>
              )}
            </div>
          ) : (
            <div className="card card-pad" style={{ marginBottom: 16 }}>
              <div className="field">
                <label>Training mode</label>
                <div className="tabs" role="tablist" style={{ marginBottom: 0 }}>
                  <button type="button" role="tab" className={"tab" + (trainMode === "fixed" ? " active" : "")} aria-selected={trainMode === "fixed"} onClick={() => setTrainMode("fixed")}>
                    Fixed hyperparameters
                  </button>
                  <button
                    type="button" role="tab" className={"tab" + (trainMode === "search" ? " active" : "")} aria-selected={trainMode === "search"}
                    aria-disabled={!searchEligible} title={searchEligible ? undefined : searchIneligibleReason}
                    style={searchEligible ? undefined : { opacity: 0.45, cursor: "not-allowed" }}
                    onClick={() => {
                      if (!searchEligible) { setError(searchIneligibleReason); return; }
                      setError(null);
                      setTrainMode("search");
                    }}
                  >
                    Hyperparameter search (Ray Tune + Optuna)
                  </button>
                </div>
                {!searchEligible && searchIneligibleReason && (
                  <div className="hint" style={{ marginTop: 6 }}>{searchIneligibleReason}</div>
                )}
                {trainMode === "search" && (
                  <div className="hint" style={{ marginTop: 6 }}>
                    Launches an HPO search rather than a single run — it trains one trial per sampled
                    combination and registers the best. Track it from Experiments.
                  </div>
                )}
              </div>

              <div className="field">
                <label>Algorithm</label>
                <input className="input" value={algorithmLabel} disabled style={{ background: "var(--surface-sunken)", color: "var(--text-muted)" }} />
                <div className="hint">
                  Determined by the base model chosen in step 1 — pick a different base model to train
                  a different algorithm.
                </div>
              </div>

              {!algoFields.length ? (
                <div className="empty-hint">
                  {base?.id} doesn't have a cataloged hyperparameter schema, so there's nothing to
                  override here — the run uses its trainer's own defaults.
                </div>
              ) : trainMode === "fixed" ? (
                <div className="field-row" style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 14 }}>
                  {algoFields.map((f) => (
                    <div className="field" key={f.name}>
                      <label>
                        {f.label}
                        {f.required && <span className="field-required">*</span>}{" "}
                        <span className="field-info" title={f.tooltip}>ⓘ</span>
                      </label>
                      {f.type === "yesno" ? (
                        <SearchSelect
                          ariaLabel={f.label}
                          noun="choice"
                          value={hyperparams[f.name] ?? String(f.default)}
                          options={[{ value: "yes", label: "Yes" }, { value: "no", label: "No" }]}
                          onChange={(v) => setHyperparams({ ...hyperparams, [f.name]: v })}
                        />
                      ) : f.type === "select" ? (
                        <SearchSelect
                          ariaLabel={f.label}
                          noun="value"
                          value={hyperparams[f.name] ?? String(f.default)}
                          options={(f.options ?? []).map((o) => ({ value: o, label: o }))}
                          onChange={(v) => setHyperparams({ ...hyperparams, [f.name]: v })}
                        />
                      ) : (
                        <input
                          className="input"
                          value={hyperparams[f.name] ?? String(f.default)}
                          onChange={(e) => setHyperparams({ ...hyperparams, [f.name]: e.target.value })}
                        />
                      )}
                      {f.range && <div className="field-range">Range: {f.range}</div>}
                    </div>
                  ))}
                </div>
              ) : (
                <>
                  <div className="field">
                    <label id="hpo-objective-label">Optimize for</label>
                    <SearchSelect labelledBy="hpo-objective-label" noun="metric" value={objective?.value ?? ""}
                      options={objectives.map((o) => ({ value: o.value, label: o.label }))}
                      onChange={(v) => setObjectiveMetric(v as HpoSearchRequest["objectiveMetric"])} />
                    <div className="hint">The metrics a {CATEGORY_LABELS[searchCategory ?? ""] ?? searchCategory} trainer reports — each trial is scored on the validation split.</div>
                  </div>
                  <div className="field-row" style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 14 }}>
                    {searchFields.map((f) => (
                      <div className="field" key={f.name}>
                        <label>
                          {f.label} {f.type === "select" ? "choices" : "values"} <span className="field-info" title={f.tooltip}>ⓘ</span>
                        </label>
                        <input
                          className="input"
                          placeholder={f.type === "select" ? (f.options ?? []).join(", ") : `e.g. ${f.default}`}
                          value={searchSpace[f.name] ?? ""}
                          onChange={(e) => setSearchSpace({ ...searchSpace, [f.name]: e.target.value })}
                        />
                        <div className="field-range">
                          Comma-separated values to try{f.range ? ` · range ${f.range}` : ""} · leave blank to skip
                        </div>
                      </div>
                    ))}
                  </div>
                  <div className="field">
                    <label>Number of trials</label>
                    <input className="input" inputMode="numeric" value={numTrials} onChange={(e) => setNumTrials(Number(e.target.value) || 0)} />
                    <div className="field-range">1–{MAX_TRIALS} trials · each samples a distinct combination of the values above</div>
                  </div>
                  <div className="callout callout-teal">
                    Optuna prunes clearly-bad trials early (median stopping) instead of running every
                    trial to completion — this rarely uses the full trial budget in wall-clock time.
                  </div>
                </>
              )}
            </div>
          )}

          <div className="card card-pad">
            <div className="field" style={{ marginBottom: 0 }}>
              <label>Compute profile</label>
              <div className="profile-grid">
                {PROFILES.map((p) => (
                  <div
                    key={p.id}
                    className={
                      "profile-card" +
                      (computeProfile === p.id ? " selected" : "") +
                      (p.gated ? " gpu-gated" : "") +
                      (p.gated && !gpuAllowed ? " locked" : "")
                    }
                    onClick={() => {
                      // Enforced here too, not just by pointer-events: the
                      // server rejects gpu-4x/gpu-8x for a non-admin anyway
                      // (routes/trainingRuns.ts), so silently selecting it
                      // would only produce a 403 at launch.
                      if (p.gated && !gpuAllowed) {
                        setError(`${p.id} needs GPU quota approval — ask a Platform Admin, or pick a CPU profile.`);
                        return;
                      }
                      setError(null);
                      setComputeProfile(p.id);
                    }}
                  >
                    <div className="p-name">{p.id}</div>
                    <div className="p-spec">{p.spec}</div>
                    {p.gated && <div className="p-lock">🔒 Needs quota approval</div>}
                  </div>
                ))}
              </div>
              <div className="hint">
                Resolved into RayJob worker count and resource requests by mlops-training-service — no
                raw Kubernetes syntax to write.
              </div>
            </div>
          </div>
        </>
      )}

      {/* ---------------- STEP 5 — review & launch ---------------- */}
      {step === 4 && (
        <>
          <div className="card card-pad" style={{ marginBottom: 16 }}>
            <div className="drawer-section-title">Run summary</div>
            <div className="kv-row"><span className="k">Base model</span><span className="v">{baseModelId}</span></div>
            <div className="kv-row"><span className="k">Registering as</span><span className="v">{effectiveModelName || "—"}</span></div>
            <div className="kv-row">
              <span className="k">Dataset</span>
              <span className="v">{datasetName}{latestSnapshotVersion ? ` · pinned to v${latestSnapshotVersion}` : " · no snapshot pinned"}</span>
            </div>
            {needsTs && (
              <div className="kv-row">
                <span className="k">Series</span>
                <span className="v">
                  {timeSeriesConfig
                    ? `${ts.value} over ${ts.timestamp}${ts.entity ? ` · ${ts.entityValue.trim() ? `${ts.entity}=${ts.entityValue.trim()}` : `global across ${ts.entity}`}` : ""} · horizon ${ts.horizon}`
                    : "no snapshot — synthetic series"}
                </span>
              </div>
            )}
            {!isLlm && !isUnsupervised && !needsTs && (
              <>
                <div className="kv-row"><span className="k">Target column</span><span className="v">{targetColumn || "—"}</span></div>
                <div className="kv-row">
                  <span className="k">Features</span>
                  {/* Count, with the names on hover — the full list runs to six
                      or more columns and swamped the summary row. */}
                  <span className="v" title={features.join(", ")}>
                    {features.length ? `${features.length} included` : "—"}
                  </span>
                </div>
              </>
            )}
            {isUnsupervised && <div className="kv-row"><span className="k">Target column</span><span className="v">none — unsupervised</span></div>}
            {isLlm && (
              <>
                <div className="kv-row"><span className="k">Fine-tuning method</span><span className="v">{isStt ? "LoRA (decoder) — Whisper speech fine-tune" : FT_METHODS.find((m) => m.key === fineTuneMethod)?.label}</span></div>
                {isStt ? (
                  <div className="kv-row"><span className="k">Audio data</span><span className="v">{sttAudio.toLocaleString()} clips · up to {sttMaxSamples} used · language {sttLanguage}</span></div>
                ) : isDocLlm ? (
                  <div className="kv-row"><span className="k">Training text</span><span className="v">{docTotal.toLocaleString()} documents → up to {maxPassagesNum.toLocaleString()} passages · max {llmCols.maxSeq} tokens</span></div>
                ) : <div className="kv-row"><span className="k">Training text</span><span className="v">{llmCols.mode === "pair" ? `${llmCols.prompt || "—"} → ${llmCols.completion || "—"}` : llmCols.text || "—"} · max {llmCols.maxSeq} tokens</span></div>}
                <div className="kv-row">
                  <span className="k">Parameters</span>
                  <span className="v" style={{ textAlign: "right" }}>
                    {fineTuneMethod === "full" && !isStt
                      ? `lr=${full.learningRate}, epochs=${full.epochs}, warmup=${full.warmupSteps}`
                      : `r=${lora.rank}, α=${lora.alpha}, dropout=${lora.dropout}, lr=${lora.learningRate}, epochs=${lora.epochs}`}
                  </span>
                </div>
              </>
            )}
            {!isLlm && <div className="kv-row"><span className="k">Split (train / val / test)</span><span className="v">{splitTrain}% / {splitVal}% / {splitTest}%</span></div>}
            {!isLlm && <div className="kv-row"><span className="k">Mode</span><span className="v">{trainMode === "search" && searchEligible ? `Hyperparameter search · ${numTrials} trials · optimizing ${objective?.label}` : "Fixed hyperparameters"}</span></div>}
            <div className="kv-row"><span className="k">Compute profile</span><span className="v">{computeProfile}</span></div>
            <div className="kv-row">
              <span className="k">Experiment</span>
              <span className="v">{experiments.data?.find((e) => e.id === experimentId)?.name ?? "None — standalone run"}</span>
            </div>
          </div>

          <div className="card card-pad" style={{ marginBottom: 16 }}>
            <div className="drawer-section-title">What happens after you launch</div>
            <div className="step-log">
              <div className="ln">Model training — Ray Train, on the compute profile chosen in step 4</div>
              <div className="ln">→ Hyperparameter optimization <span style={{ color: "#73827A" }}>(only in Hyperparameter search mode — the run above trains once, this loops trials)</span></div>
              <div className="ln">→ Best model selected from the run / trials</div>
              <div className="ln">→ Validation gate — checked against the split configured in step 3</div>
              {/* PASS/FAIL is a semantic pair, so it does NOT follow the brand.
                  --teal is aliased to the brand orange, which put PASS at
                  #FF4500 next to FAIL at #EF4444 — two shades of red-orange on
                  a dark panel, so the PASS line read as a second error. Green
                  vs red is the one encoding nobody has to decode. */}
              <div className="ln" style={{ paddingLeft: 20, color: "var(--log-pass)" }}>PASS → final evaluation on the held-out test split → registered in Catalog as a new version</div>
              <div className="ln" style={{ paddingLeft: 20, color: "var(--log-fail)" }}>FAIL → run marked failed, no new version registered — adjust and retrain</div>
            </div>
          </div>

          {launched ? (
            launched.kind === "run" ? (
              <LaunchedRun runId={launched.id} modelName={effectiveModelName} onReset={reset} />
            ) : (
              <div className="card card-pad">
                <div className="callout callout-teal" style={{ marginBottom: 12 }}>
                  Hyperparameter search <span style={{ fontFamily: "var(--font-mono)" }}>{launched.id.slice(0, 8)}</span> queued —{" "}
                  {numTrials} trials, optimizing {objective?.label}. The best trial is selected when they finish.
                </div>
                <div style={{ display: "flex", gap: 8 }}>
                  <Link className="btn btn-primary" style={{ flex: 1 }} to="/experiments">Follow trials in Experiments</Link>
                  <button className="btn btn-secondary" style={{ flex: 1 }} onClick={reset}>Start another</button>
                </div>
              </div>
            )
          ) : (
            <>
              {/* Launching with a target the gate will refuse only buys a
                  failed run a minute later. The server still re-checks this
                  independently — this is a courtesy, not the enforcement. */}
              {targetBlocked && (
                <div className="callout callout-red" style={{ marginBottom: 12 }}>
                  <strong>Pick a different target column.</strong> '{targetColumn}' won't pass the dataset
                  quality gate, so this run would fail rather than train. {targetVerdict?.reason}
                </div>
              )}
              <button hidden={!can(PERM.train)}
                className="btn btn-primary btn-block"
                disabled={submitting || targetBlocked}
                onClick={launch}
              >
                <Icon name="wand" />
                {submitting ? "Launching…"
                  : targetBlocked ? "Can't launch — choose a usable target"
                  : trainMode === "search" && searchEligible ? "Launch hyperparameter search"
                  : "Launch training run"}
              </button>
            </>
          )}
        </>
      )}

      {!launched && (
        <div className="wizard-actions">
          <button className="btn btn-secondary" onClick={back} style={{ visibility: step === 0 ? "hidden" : "visible" }}>Back</button>
          {/* A model that can't be fine-tuned has nowhere further to go here — offer its Test tab instead. */}
          {step === 2 && noTrainerReason && base
            ? <Link className="btn btn-primary" to={`/base-models/${encodeURIComponent(base.id)}?tab=test`}><Icon name="play" />Test this model</Link>
            : step < STEPS.length - 1 && <button className="btn btn-primary" onClick={next}>Continue</button>}
        </div>
      )}

    </div>
  );
}

/**
 * Live view of the run that was just launched.
 *
 * Polls the same projected-pipeline endpoint the Pipelines page uses, so the
 * stages and log lines here ARE the run's recorded state — not a scripted
 * animation. Polling stops as soon as the run reaches a terminal status.
 *
 * The mock also shows a "val_accuracy — live" curve. There is deliberately no
 * equivalent here: the trainers report metrics once, at completion, so a
 * per-step curve would be drawn rather than measured. The final metrics are
 * shown instead, when they actually exist.
 */
function LaunchedRun({ runId, modelName, onReset }: { runId: string; modelName: string; onReset: () => void }) {
  const [detail, setDetail] = useState<PipelineDetail | null>(null);
  // Dataset warnings the quality gate raised but didn't block on. These are
  // the difference between "f1 100" meaning a good model and it meaning a
  // leaking feature, so they belong next to the metrics, not in a log nobody
  // reads.
  const [warnings, setWarnings] = useState<string[]>([]);

  useEffect(() => {
    let alive = true;
    let timer: ReturnType<typeof setInterval> | null = null;
    const tick = async () => {
      try {
        const d = await api.getPipeline(runId);
        if (!alive) return;
        setDetail(d);
        if (d.status === "succeeded" || d.status === "failed") {
          if (timer) clearInterval(timer);
          // Only fetched once the run is finished — the gate runs as part of
          // completion, so there is nothing to show before then.
          try {
            const run = await api.getTrainingRun(runId);
            if (alive) setWarnings(run.qualityGateWarnings ?? []);
          } catch {
            /* the run detail is supplementary — a failure here must not break the panel */
          }
        }
      } catch {
        /* the run exists — a transient read failure shouldn't kill the poll */
      }
    };
    tick();
    timer = setInterval(tick, 2000);
    return () => { alive = false; if (timer) clearInterval(timer); };
  }, [runId]);

  const stages = detail?.stages ?? [];
  const doneCount = stages.filter((s) => s.state === "ok").length;
  const pct = stages.length ? Math.round((doneCount / stages.length) * 100) : 0;

  // Relative offsets from the first recorded line, like the mock's "+0s/+2s".
  // Derived from real timestamps, so the gaps are the real gaps — including
  // sub-second ones, which matter because a small run finishes in ~3s.
  const logs = detail?.logs ?? [];
  const baseMs = logs.length && logs[0].atMs != null ? logs[0].atMs : null;
  const offset = (l: PipelineLog) => {
    if (baseMs == null || l.atMs == null) return "";
    const d = (l.atMs - baseMs) / 1000;
    return `+${d < 10 ? d.toFixed(1) : Math.round(d)}s`;
  };

  return (
    <div className="card" style={{ marginBottom: 16 }}>
      <div className="card-pad" style={{ borderBottom: "1px solid var(--border)" }}>
        <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", marginBottom: 12 }}>
          <div>
            <span style={{ fontWeight: 700, fontSize: 13.5, fontFamily: "var(--font-mono)" }}>{runId.slice(0, 8)}</span>
            <span style={{ fontWeight: 400, color: "var(--text-faint)", marginLeft: 8, fontSize: 12 }}>· {modelName}</span>
          </div>
          <StatusPill status={detail?.status ?? "queued"} />
        </div>
        <div className="progress-track" style={{ marginBottom: 12 }}>
          <div className="progress-fill" style={{ width: `${pct}%` }} />
        </div>

        <div className="dag" style={{ marginBottom: 12 }}>
          {stages.map((s, i) => (
            <div key={s.order} style={{ display: "contents" }}>
              {i > 0 && <div className="dag-arrow" />}
              <div className={"dag-node " + (s.state === "ok" ? "done" : s.state === "running" ? "running" : s.state === "failed" ? "failed" : "")}>
                <div className="dn-title">{s.title}</div>
                <div className="dn-sub">{s.sub}</div>
              </div>
            </div>
          ))}
        </div>

        <div className="step-log" style={{ maxHeight: 220 }}>
          {logs.map((l, i) => (
            <div className={"ln " + (l.level === "error" ? "err" : l.level === "ok" ? "ok" : "")} key={i}>
              <span className="t">{offset(l) || l.t}</span>
              <span className="msg">{l.msg}</span>
            </div>
          ))}
          {!logs.length && <div className="ln"><span className="msg">Queued — waiting for a worker to claim it…</span></div>}
        </div>

        {warnings.length > 0 && (
          <div className="callout callout-amber" style={{ marginTop: 12 }}>
            <strong>{warnings.length} dataset warning{warnings.length > 1 ? "s" : ""}</strong> — the run completed, but these
            are worth reading before trusting its metrics:
            <ul style={{ margin: "8px 0 0", paddingLeft: 18 }}>
              {warnings.map((w, i) => <li key={i} style={{ marginBottom: 4 }}>{w}</li>)}
            </ul>
          </div>
        )}
      </div>
      <div className="card-pad">
        <button className="btn btn-secondary btn-block" onClick={onReset}>Start another</button>
      </div>
    </div>
  );
}
