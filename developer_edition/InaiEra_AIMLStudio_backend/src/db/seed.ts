import { pool, initSchema } from "./connection";
import { ALL_BASE_MODELS } from "./baseModelsData";
import { ALGORITHM_SPECS } from "./algorithmSpecs";
import {
  ALGORITHM_SPECS_TABLE, BASE_MODELS_TABLE, COMPUTE_POOLS, COMPUTE_POOLS_TABLE,
  algorithmSpecValues, baseModelValues, computePoolValues, insertSql,
} from "./referenceData";
import { hashSecret } from "../utils/secretHash";

// A representative slice of the mockup's data, not the full 80-base-model
// library — enough to exercise every code path (classical ML + LLM, adapter,
// fired + building alerts, OAuth) without hand-typing 80 rows here.
// Safe to re-run: truncates and reseeds every table each time.
//
// DEV ONLY, DESTRUCTIVE: this drops every table, user data included. Never run
// it against a real deployment — the reference catalog a deployment needs
// (algorithm specs, base models, compute pools) is loaded non-destructively at
// server start by referenceData.ts, from the same column lists used below.
async function seed() {
  // Drop everything first — schema.sql uses CREATE IF NOT EXISTS, which
  // doesn't add new columns to an already-existing table from a previous
  // run. A dev seed script should be safe to re-run even after a schema
  // change, not just after a fresh empty database.
  await pool.query(
    "DROP TABLE IF EXISTS seed_markers, algorithm_specs, compute_pools, audit_log, dataset_access, dataset_audit_log, dataset_comments, model_monitoring, deployments, pipeline_logs, pipeline_stages, pipelines, pipeline_definitions, hpo_trials, hpo_searches, training_runs, experiments, alert_history, alert_state, alert_policies, notification_channels, endpoints, dataset_snapshots, datasets, model_versions, catalog_models, base_models CASCADE"
  );
  await initSchema();

  // All 80 base models from the mockup — both ML and LLM domains, not just
  // a representative sample. Real, individual insert per row (not a bulk
  // multi-row VALUES) so a single malformed entry fails loudly and doesn't
  // silently corrupt the whole seed.
  for (const a of ALGORITHM_SPECS) {
    await pool.query(insertSql(ALGORITHM_SPECS_TABLE), algorithmSpecValues(a));
  }

  // Index carries the curated order through to sort_order — the array's own
  // sequence IS the intended presentation order.
  for (const [i, b] of ALL_BASE_MODELS.entries()) {
    await pool.query(insertSql(BASE_MODELS_TABLE), baseModelValues(b, i));
  }

  await pool.query(
    `INSERT INTO catalog_models (id, task, domain, kind, framework, base_ref, updated) VALUES
     ('fraud-detector','Classification','ml','trained','xgboost',NULL,'2h ago'),
     ('fraud-detector-dl','Classification','ml','trained','pytorch','deep-mlp-classifier','5h ago'),
     ('llama-3-8b-instruct','Chat','llm','fine-tuned','huggingface',NULL,'18 days ago'),
     ('support-tone-adapter','Chat','llm','adapter','huggingface','llama-3-8b-instruct','1 day ago')`
  );
  await pool.query(
    `INSERT INTO model_versions (model_id, v, alias, note) VALUES
     ('fraud-detector', 4, 'champion', 'promoted 2h ago · run_071'),
     ('fraud-detector', 3, 'archived', 'retired — superseded by v4'),
     ('fraud-detector-dl', 1, 'challenger', 'deep-mlp-classifier trained on the same dataset — evaluating as a challenger to the xgboost champion'),
     ('llama-3-8b-instruct', 1, 'champion', 'base model — registered from library'),
     ('support-tone-adapter', 3, 'challenger', 'canarying 10% of traffic'),
     ('support-tone-adapter', 2, 'champion', 'promoted 9 days ago')`
  );

  await pool.query(
    `INSERT INTO datasets (name, source, structure_type, size, rows, owner, tags, detail) VALUES
     ('fraud-transactions-2026','upload','structured','4.2 GB','1.2M','Jordan Reyes', $1, '1.2M rows · checksum verified'),
     ('support-tickets-corpus','upload','unstructured','1.8 GB','42,000','Jordan Reyes', $2, '42,000 chat-formatted support conversations')`,
    [
      JSON.stringify(["fraud-detection", "finance", "classification", "tabular"]),
      JSON.stringify(["support", "chat", "fine-tuning"]),
    ]
  );

  await pool.query(
    `INSERT INTO endpoints (model_id, title, url, status, token_endpoint, client_id, client_secret_masked, client_secret_hash, rpm, burst, concurrent) VALUES
     ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11), ($12,$13,$14,$15,$16,$17,$18,$19,$20,$21,$22)`,
    [
      "fraud-detector", "fraud-detector", "https://ml-gateway.acme-corp.internal/v1/models/fraud-detector", "healthy",
      "https://ml-gateway.acme-corp.internal/oauth2/token", "mlp-svc-fraud-detector", "cs_live_••••••••••••7f2a",
      hashSecret("cs_live_seed7f2a0000000000000000000000000000000000000000000000"), 3000, 300, 60,
      "llama-3-8b-instruct", "llama-3-8b-instruct", "https://ml-gateway.acme-corp.internal/v1/models/llama-3-8b", "healthy",
      "https://ml-gateway.acme-corp.internal/oauth2/token", "mlp-svc-llama-3-8b-instruct", "cs_live_••••••••••••2b90",
      hashSecret("cs_live_seed2b900000000000000000000000000000000000000000000"), 1200, 60, 24,
    ]
  );

  await pool.query(
    `INSERT INTO notification_channels (id, name, type, enabled, webhook_url, webhook_masked, channel_dest) VALUES
     ('ch-1', 'ML Alerts', 'slack', TRUE, '', 'https://hooks.slack.com/services/T02••••••••/B08••••••••/••••••••••••••••••••', '#ml-alerts')`
  );

  const policies: [string, string, string, string, string, string, string, string, string, number, string][] = [
    ["f1-drop", "f1-drop", "F1 drops", "high", ">", "%", "1h", "Notify + retrain candidate", JSON.stringify(["classification"]), 10, "ch-1"],
    ["recall-drop", "recall-drop", "Recall drops", "high", ">", "%", "1h", "Notify + retrain candidate", JSON.stringify(["classification"]), 10, "ch-1"],
    ["psi", "feature-drift", "PSI (any tracked feature)", "high", ">", "", "24h", "Notify + retrain candidate", JSON.stringify(["classification", "regression", "generation"]), 0.2, "ch-1"],
    ["low-conf", "low-confidence", "Low-confidence predictions", "medium", ">", "%", "1h", "Notify", JSON.stringify(["classification"]), 10, "ch-1"],
    ["p95-latency", "latency-high", "P95 latency", "medium", ">", "ms", "5m", "Scale + notify", JSON.stringify(["classification", "regression", "generation"]), 500, "ch-1"],
    ["error-rate", "error-rate-high", "Error rate", "high", ">", "%", "5m", "Page on-call", JSON.stringify(["classification", "regression", "generation"]), 1, "ch-1"],
    ["missing-values", "missing-values-high", "Missing values", "medium", ">", "%", "1h", "Notify", JSON.stringify(["classification", "regression", "generation"]), 5, "ch-1"],
    ["gt-coverage", "gt-coverage-low", "Ground-truth coverage", "low", "<", "%", "24h", "Notify", JSON.stringify(["classification", "regression"]), 30, "ch-1"],
  ];
  for (const p of policies) {
    await pool.query(
      `INSERT INTO alert_policies (id, name, label, severity, operator, unit, window_duration, action, enabled, applies_to, default_threshold, channel_id)
       VALUES ($1, $2, $3, $4, $5, $6, $7, $8, TRUE, $9, $10, $11)`,
      p
    );
  }

  // fraud-detector: PSI genuinely fired (3/3 windows), low-confidence building (2/3)
  await pool.query(
    `INSERT INTO alert_state (model_id, policy_id, windows_exceeded, current_value, ack_state) VALUES
     ('fraud-detector', 'psi', 3, '0.26 — merchant_category', 'none'),
     ('fraud-detector', 'low-conf', 2, '7.1%', 'none')`
  );

  // A couple of real past resolutions, so History isn't empty on first load.
  await pool.query(
    `INSERT INTO alert_history (model_id, policy_id, final_value, windows_at_resolution, resolved_by, resolved_at) VALUES
     ('fraud-detector', 'p95-latency', '612ms', 3, 'arjun', NOW() - INTERVAL '2 days'),
     ('fraud-detector', 'error-rate', '1.4%', 3, 'sneha', NOW() - INTERVAL '6 hours')`
  );

  // A genuinely failed pipeline (OOM at trial 19/24) — matches the mockup's
  // real restart-flow example, not a fabricated always-green demo.
  await pool.query(
    `INSERT INTO pipelines (id, title, status, compute_profile, meta, started_at) VALUES
     ('fraud-detector-hpo', 'tune-fraud-detector-hpo', 'failed', 'gpu-4x', 'Ray Tune search — failed, trial 19/24', NOW() - INTERVAL '1 day')`
  );
  await pool.query(
    `INSERT INTO pipeline_stages (pipeline_id, stage_order, title, state, sub) VALUES
     ('fraud-detector-hpo', 1, 'Validate dataset', 'ok', '1s'),
     ('fraud-detector-hpo', 2, 'Ray Tune search', 'failed', 'failed · trial 19/24'),
     ('fraud-detector-hpo', 3, 'Batch evaluate', 'pending', 'not reached'),
     ('fraud-detector-hpo', 4, 'Register', 'pending', 'not reached')`
  );
  const logLines: [string, number, string, string, string, string][] = [
    ["fraud-detector-hpo", 1, "14:22:01", "argo-workflow", "info", "Workflow tune-fraud-detector-hpo started — step validate-dataset"],
    ["fraud-detector-hpo", 2, "14:22:03", "argo-workflow", "ok", "validate-dataset succeeded — 1.2M rows, checksum OK"],
    ["fraud-detector-hpo", 3, "14:22:04", "argo-workflow", "info", "Step ray-tune-search started — RayJob submitted to KubeRay, gpu-4x profile"],
    ["fraud-detector-hpo", 4, "14:22:11", "kuberay", "info", "RayCluster provisioned — 1 head, 4 workers"],
    ["fraud-detector-hpo", 5, "14:26:44", "ray-tune", "info", "Trial 19/24 started — n_estimators=512, max_depth=6"],
    ["fraud-detector-hpo", 6, "14:30:09", "ray-tune", "error", "Trial 19/24 killed — OOM: worker exceeded memory limit (gpu-4x, 16Gi)"],
    ["fraud-detector-hpo", 7, "14:30:10", "argo-workflow", "error", "Step ray-tune-search failed — trial 19/24 OOMKilled"],
  ];
  for (const l of logLines) {
    await pool.query(
      `INSERT INTO pipeline_logs (pipeline_id, seq, t, src, level, msg) VALUES ($1, $2, $3, $4, $5, $6)`,
      l
    );
  }

  // Real deployments — fraud-detector is DEGRADED (matches its real fired PSI
  // alert elsewhere), support-tone-adapter is healthy with a real pending
  // promotion candidate (its v3 challenger, already seeded).
  await pool.query(
    `INSERT INTO deployments (model_id, current_version, previous_version, health_status, runtime, replicas) VALUES
     ('fraud-detector', 4, 3, 'degraded', 'KServe', '2–8'),
     ('support-tone-adapter', 2, NULL, 'healthy', 'vLLM (multi-LoRA)', 'shared')`
  );

  // fraud-detector's full monitoring payload — every number here matches
  // what was established across the mockup (health 87/WARNING, merchant_category
  // PSI 0.26 CRITICAL, the same confusion matrix, the same fired-alert state).
  const overview = {
    predictions1h: "50,400", accuracy: 94.2, f1: 91.7, avgConfidence: 87.2,
    p95Latency: "38ms", errorRate: "0.02%", driftedFeatures: 1,
  };
  const performance = {
    current: { accuracy: 94.2, precision: 92.8, recall: 90.7, f1: 91.7, rocAuc: 96.4, prAuc: 93.1, logLoss: 0.183 },
    baseline: { accuracy: 94.1, precision: 92.7, recall: 91.8, f1: 92.2, rocAuc: 96.4, prAuc: 94.5, logLoss: 0.152 },
    confusionMatrix: { labels: ["Not fraud", "Fraud"], matrix: [[72481, 1829], [2104, 9482]] },
  };
  const drift = {
    featureDrift: [
      { feature: "amount_usd", psi: 0.07, ks: 0.08, status: "healthy" },
      { feature: "transaction_hour", psi: 0.05, ks: 0.05, status: "healthy" },
      { feature: "card_present", psi: 0.03, ks: 0.03, status: "healthy" },
      { feature: "merchant_category", psi: 0.26, ks: 0.31, status: "critical" },
    ],
    predictionDrift: { psi: 0.24, ks: 0.19, baselineMean: "11%", currentMean: "28%" },
    targetDrift: { psi: 0.18, ks: 0.15, baselineMean: "9%", currentMean: "15%", status: "watch" },
    conceptDrift: { note: "Recall dropped 1.2pp over the last 7 days while inputs (feature drift) stayed mostly stable — a small early signal, not yet conclusive." },
  };
  const predictions = {
    traces: [
      { input: "amount_usd=1240, merchant_category=electronics", prediction: "fraud", confidence: 0.91, groundTruth: "confirmed fraud", correct: true, latencyMs: 14, version: 4 },
      { input: "amount_usd=42, merchant_category=groceries", prediction: "not fraud", confidence: 0.98, groundTruth: "confirmed not fraud", correct: true, latencyMs: 11, version: 4 },
      { input: "amount_usd=3100, merchant_category=electronics", prediction: "fraud", confidence: 0.67, groundTruth: "pending", correct: null, latencyMs: 16, version: 4 },
      { input: "amount_usd=88, merchant_category=restaurants", prediction: "not fraud", confidence: 0.95, groundTruth: "confirmed not fraud", correct: true, latencyMs: 9, version: 4 },
      { input: "amount_usd=560, merchant_category=travel", prediction: "not fraud", confidence: 0.58, groundTruth: "confirmed fraud", correct: false, latencyMs: 18, version: 4 },
      { input: "amount_usd=15, merchant_category=groceries", prediction: "not fraud", confidence: 0.99, groundTruth: "pending", correct: null, latencyMs: 8, version: 4 },
    ],
  };
  const explainability = {
    globalImportance: [
      { feature: "amount_usd", importance: 100 },
      { feature: "merchant_category", importance: 72 },
      { feature: "transaction_hour", importance: 41 },
      { feature: "card_present", importance: 29 },
    ],
    attributionDrift: [
      { feature: "merchant_category", baselineRank: 3, currentRank: 2, flagged: true },
      { feature: "amount_usd", baselineRank: 1, currentRank: 1, flagged: false },
    ],
  };
  const infrastructure = {
    throughput: "16/s", p50: "19ms", p95: "38ms", p99: "84ms",
    cpu: "61%", memory: "54%", gpu: null, errorRate: "0.02%",
    timeouts: "3 (0.006%)", replicas: "2–6 (KEDA, on QPS)",
  };

  await pool.query(
    `INSERT INTO model_monitoring (model_id, model_type, health_score, overview, performance, drift, predictions, explainability, infrastructure)
     VALUES ('fraud-detector', 'classification', 87, $1, $2, $3, $4, $5, $6)`,
    [JSON.stringify(overview), JSON.stringify(performance), JSON.stringify(drift), JSON.stringify(predictions), JSON.stringify(explainability), JSON.stringify(infrastructure)]
  );

  // fraud-detector-dl — a deep-mlp-classifier trained on the SAME dataset
  // as fraud-detector, evaluated as a challenger. Same classification
  // shape as fraud-detector (accuracy/F1/confusion-matrix are still valid
  // for a deep classifier), PLUS training_metrics — the piece a classical
  // ML model like xgboost genuinely has no equivalent of (no epochs, no
  // gradient descent, no loss curve).
  const dlOverview = {
    predictions1h: "48,900", accuracy: 95.1, f1: 93.4, avgConfidence: 89.6,
    p95Latency: "61ms", errorRate: "0.03%", driftedFeatures: 1,
  };
  const dlPerformance = {
    current: { accuracy: 95.1, precision: 93.8, recall: 93.0, f1: 93.4, rocAuc: 97.2, prAuc: 94.6, logLoss: 0.151 },
    baseline: { accuracy: 94.8, precision: 93.4, recall: 92.9, f1: 93.1, rocAuc: 97.0, prAuc: 94.2, logLoss: 0.158 },
    confusionMatrix: { labels: ["Not fraud", "Fraud"], matrix: [[72890, 1420], [1610, 9976]] },
  };
  const dlDrift = {
    featureDrift: [
      { feature: "amount_usd", psi: 0.06, ks: 0.07, status: "healthy" },
      { feature: "transaction_hour", psi: 0.04, ks: 0.04, status: "healthy" },
      { feature: "card_present", psi: 0.03, ks: 0.03, status: "healthy" },
      { feature: "merchant_category", psi: 0.21, ks: 0.27, status: "critical" },
    ],
    predictionDrift: { psi: 0.19, ks: 0.15, baselineMean: "10%", currentMean: "24%" },
    targetDrift: { psi: 0.18, ks: 0.15, baselineMean: "9%", currentMean: "15%", status: "watch" },
    conceptDrift: { note: "Same merchant_category drift affecting fraud-detector also shows up here — expected, since both models see the same input distribution regardless of architecture." },
  };
  const dlPredictions = {
    traces: [
      { input: "amount_usd=1240, merchant_category=electronics", prediction: "fraud", confidence: 0.94, groundTruth: "confirmed fraud", correct: true, latencyMs: 22, version: 1 },
      { input: "amount_usd=42, merchant_category=groceries", prediction: "not fraud", confidence: 0.99, groundTruth: "confirmed not fraud", correct: true, latencyMs: 19, version: 1 },
      { input: "amount_usd=560, merchant_category=travel", prediction: "fraud", confidence: 0.71, groundTruth: "confirmed fraud", correct: true, latencyMs: 24, version: 1 },
    ],
  };
  const dlExplainability = {
    globalImportance: [
      { feature: "amount_usd", importance: 100 },
      { feature: "merchant_category", importance: 81 },
      { feature: "transaction_hour", importance: 38 },
      { feature: "card_present", importance: 24 },
    ],
    attributionDrift: [
      { feature: "merchant_category", baselineRank: 3, currentRank: 2, flagged: true },
      { feature: "amount_usd", baselineRank: 1, currentRank: 1, flagged: false },
    ],
  };
  const dlInfrastructure = {
    throughput: "14/s", p50: "31ms", p95: "61ms", p99: "118ms",
    cpu: null, memory: "62%", gpu: "41%", errorRate: "0.03%",
    timeouts: "1 (0.002%)", replicas: "1–4 (KEDA, on QPS)",
  };
  // Matches the mockup's fraud-detector-dl training metrics exactly, number
  // for number — both artifacts show the same story for the same model.
  const dlTrainingMetrics = {
    dataSource: "example",
    architecture: "3-layer MLP (256→128→64), ReLU, dropout 0.15",
    progress: {
      epoch: 28, totalEpochs: 30, step: 43820, totalSteps: 46950, progressPct: 93.3,
      samplesProcessed: "11.22M", totalSamples: "12.01M", batchesProcessed: "43,820",
      elapsed: "41 min", eta: "3 min 10 sec",
    },
    loss: {
      type: "Binary cross entropy", training: 0.142, validation: 0.168,
      bestValidation: 0.161, bestEpoch: 26,
      trainCurve: [0.612,0.541,0.472,0.414,0.365,0.329,0.301,0.278,0.258,0.241,0.226,0.214,0.204,0.195,0.187,0.180,0.173,0.168,0.163,0.159,0.155,0.151,0.148,0.146,0.144,0.143,0.1425,0.142],
      valCurve:   [0.598,0.526,0.459,0.402,0.355,0.321,0.295,0.273,0.254,0.237,0.224,0.212,0.202,0.194,0.187,0.181,0.176,0.172,0.169,0.166,0.164,0.163,0.162,0.162,0.1615,0.161,0.164,0.168],
    },
    generalization: {
      trainF1: 96.8, validationF1: 94.1, gap: 2.7, status: "HEALTHY",
      overfittingStarted: "Not detected", bestEpoch: 26, epochsWithoutImprovement: 2,
    },
    optimization: {
      optimizer: "AdamW", learningRate: "1.8e-4", initialLearningRate: "1e-3",
      scheduler: "Cosine decay", gradientNorm: 0.84, meanGradient: "2.1e-4", maxGradient: 0.091,
      nanGradients: 0, infGradients: 0, explodingGradientEvents: 0, vanishingGradientWarning: "No",
      lrCurve: [0.001,0.00099,0.00098,0.00096,0.00093,0.00090,0.00086,0.00082,0.00077,0.00072,0.00067,0.00062,0.00057,0.00052,0.00047,0.00042,0.00038,0.00034,0.00030,0.00027,0.00024,0.00022,0.00020,0.00019,0.000185,0.000182,0.000181,0.00018],
      gradCurve: [1.42,1.31,1.26,1.18,1.12,1.08,1.04,1.01,0.98,0.96,0.95,0.93,0.91,0.90,0.89,0.88,0.87,0.86,0.86,0.85,0.85,0.85,0.84,0.84,0.84,0.84,0.84,0.84],
    },
    parameters: {
      total: "24.8M", trainable: "22.1M", frozen: "2.7M",
      weightNorm: 84.21, weightMean: "0.0021", weightStdDev: "0.118",
    },
    throughput: { batchSize: 256, samplesPerSec: "18,400", batchesPerSec: "71.9" },
    compute: { gpuUtilization: "78%", gpuMemory: "9.2 / 16 GB", gpuTemperature: "67°C", cpu: "36%", memory: "62%" },
    earlyStopping: { enabled: "Yes", monitor: "val_loss", patience: "5 epochs", minDelta: "0.001", bestScore: 0.161, bestEpoch: 26, epochsSinceImprovement: "2 / 5" },
    checkpoints: [
      { epoch: 10, valLoss: 0.237, f1: 91.8, status: "saved" },
      { epoch: 20, valLoss: 0.166, f1: 93.5, status: "saved" },
      { epoch: 26, valLoss: 0.161, f1: 94.2, status: "best" },
      { epoch: 28, valLoss: 0.168, f1: 94.1, status: "latest" },
    ],
    health: { overall: 94, lossConvergence: 96, generalization: 92, gradientHealth: 99, learningRate: 94, computeHealth: 95, dataQuality: 97, status: "HEALTHY" },
  };
  const dlInputQuality = {
    "Input quality score": "97 / 100", "Expected input shape": "[batch, 18]",
    "Shape violation rate": "0.003%", "NaN input rate": "0.000%", "Inf input rate": "0.000%",
    "Missing value rate": "0.02%", "Invalid input rate": "0.01%", "Out-of-range rate": "0.09%",
  };

  await pool.query(
    `INSERT INTO model_monitoring (model_id, model_type, health_score, overview, performance, drift, predictions, explainability, infrastructure, training_metrics, dl_input_quality)
     VALUES ('fraud-detector-dl', 'classification', 89, $1, $2, $3, $4, $5, $6, $7, $8)`,
    [
      JSON.stringify(dlOverview), JSON.stringify(dlPerformance), JSON.stringify(dlDrift), JSON.stringify(dlPredictions),
      JSON.stringify(dlExplainability), JSON.stringify(dlInfrastructure), JSON.stringify(dlTrainingMetrics), JSON.stringify(dlInputQuality),
    ]
  );

  // support-tone-adapter — full LLM monitoring, covering the 8 categories
  // requested: response quality, hallucination/groundedness, latency/
  // serving, infrastructure, cost, safety, LLM drift, fine-tuning. Every
  // number here is taken directly from the reference doc's own examples,
  // not invented, so it's internally consistent (e.g. cost's token counts
  // match overview's, fine-tuning's base model matches the real
  // support-tone-adapter -> llama-3-8b-instruct relationship already in
  // catalog_models).
  const llmOverview = {
    requests1h: "3,420", answerRelevance: 94.2, correctness: 92.7, completeness: 90.1,
    instructionFollowing: 96.3, taskSuccess: 91.8, userRating: 4.6, p95Latency: "4.7s", p95Ttft: "890ms",
  };
  const llmPerformance = {
    current: { answerRelevance: 94.2, correctness: 92.7, completeness: 90.1, coherence: 95.5, instructionFollowing: 96.3, conciseness: 88.9, semanticSimilarity: 90.4, taskSuccess: 91.8 },
    baseline: { answerRelevance: 95.0, correctness: 93.5, completeness: 91.0, coherence: 95.8, instructionFollowing: 96.8, conciseness: 90.1, semanticSimilarity: 91.2, taskSuccess: 92.6 },
  };
  const llmDrift = {
    promptLengthDrift: "+18%", inputTokenPsi: 0.12, embeddingDrift: "WARNING",
    newTopicCluster: "Equipment compliance", newTopicShare: "12.4%",
    topicDrift: "watch", languageDrift: "stable", outputLengthDrift: "+4%",
    groundednessDrift: "-1.1pp", hallucinationDrift: "+0.3pp", userFeedbackDrift: "stable",
  };
  const llmPredictions = {
    traces: [
      { input: "\"What's our refund window for damaged equipment?\"", prediction: "Cites 30-day policy §4.2, offers RMA steps", confidence: 0.94, groundTruth: "correct — matches policy doc", correct: true, latencyMs: 1820, version: 3 },
      { input: "\"Can I get a refund after 45 days if it's still broken?\"", prediction: "States no refund after 30 days, offers repair credit", confidence: 0.81, groundTruth: "pending human review", correct: null, latencyMs: 2210, version: 3 },
      { input: "\"Is your support line open on weekends?\"", prediction: "Confirms Sat 9-5, cites hours page", confidence: 0.97, groundTruth: "correct", correct: true, latencyMs: 640, version: 3 },
    ],
  };
  const llmInfrastructure = {
    p50Latency: "1.8s", p95Latency: "4.7s", p99Latency: "8.2s",
    p50Ttft: "420ms", p95Ttft: "890ms", outputThroughput: "74 tokens/sec",
    queueTime: "120ms", gpuUtilization: "87%", vram: "71 / 80 GB",
    kvCacheUtilization: "82%", tokensPerSec: "1,284", queueDepth: 14, oomEvents: 0,
  };
  const llmHallucination = {
    groundedness: 96.8, faithfulness: 95.4, unsupportedClaimRate: 1.8,
    hallucinationRate: 1.2, citationCorrectness: 98.1, citationCompleteness: 94.5,
  };
  const llmSafety = {
    toxicity: 0.3, unsafeResponseRate: 0.4, refusalRate: 2.1, falseRefusalRate: 0.6,
    promptInjectionDetections: 3, jailbreakDetections: 1, policyViolations: 0, sensitiveDataLeakage: 0,
  };
  const llmCost = {
    today: "$482", last7Days: "$3,184", costPer1kRequests: "$2.64", costPer1mTokens: "$1.82",
    avgCostPerRequest: "$0.0026", weekOverWeek: "-8.4%",
    inputTokens: "124.8M", outputTokens: "32.1M", avgInputPerRequest: 684, avgOutputPerRequest: 176,
    contextUtilization: "61%", truncatedRequests: "0.4%",
  };
  const llmFineTuning = {
    baseModel: "llama-3-8b-instruct", method: "LoRA / PEFT", epoch: "2 / 3", step: 8240,
    trainingLoss: 0.821, validationLoss: 0.904, learningRate: "2e-5", gradientNorm: 1.18,
    tokensPerSec: 8420, gpuUtilization: "94%", gpuMemory: "72 / 80GB",
    checkpoint: "checkpoint-8000", perplexity: 4.1,
  };

  await pool.query(
    `INSERT INTO model_monitoring
      (model_id, model_type, health_score, overview, performance, drift, predictions, explainability, infrastructure, hallucination, safety, cost, fine_tuning)
     VALUES ('support-tone-adapter', 'generation', 91, $1, $2, $3, $4, NULL, $5, $6, $7, $8, $9)`,
    [
      JSON.stringify(llmOverview), JSON.stringify(llmPerformance), JSON.stringify(llmDrift),
      JSON.stringify(llmPredictions), JSON.stringify(llmInfrastructure), JSON.stringify(llmHallucination),
      JSON.stringify(llmSafety), JSON.stringify(llmCost), JSON.stringify(llmFineTuning),
    ]
  );

  await pool.query(
    `INSERT INTO dataset_comments (dataset_name, author, body) VALUES
     ('fraud-transactions-2026', 'arjun', 'Heads up — merchant_category has been drifting on the live model. Worth a fresh pull before the next retrain.')`
  );
  await pool.query(
    `INSERT INTO dataset_audit_log (dataset_name, actor, action) VALUES
     ('fraud-transactions-2026', 'sneha', 'uploaded'),
     ('fraud-transactions-2026', 'arjun', 'commented')`
  );

  // Same five pools a deployment gets, with demo occupancy numbers on top.
  const demoCounters: Record<string, { inUse: number; queuedJobs: number }> = {
    "cpu-small": { inUse: 12, queuedJobs: 0 },
    "cpu-large": { inUse: 3, queuedJobs: 0 },
    "gpu-1x": { inUse: 9, queuedJobs: 1 },
    "gpu-4x": { inUse: 4, queuedJobs: 2 },
    "gpu-8x": { inUse: 0, queuedJobs: 0 },
  };
  for (const p of COMPUTE_POOLS) {
    await pool.query(insertSql(COMPUTE_POOLS_TABLE), computePoolValues(p, demoCounters[p.id]));
  }

  const counts = await Promise.all(
    ["algorithm_specs", "base_models", "catalog_models", "datasets", "endpoints", "alert_policies", "notification_channels", "pipelines", "deployments", "model_monitoring", "compute_pools"].map(async (t) => {
      const r = await pool.query(`SELECT COUNT(*) FROM ${t}`);
      return [t, Number(r.rows[0].count)];
    })
  );
  // eslint-disable-next-line no-console
  console.log("Seed complete:", Object.fromEntries(counts));
  await pool.end();
}

seed().catch((err) => {
  // eslint-disable-next-line no-console
  console.error("Seed failed:", err);
  process.exit(1);
});
