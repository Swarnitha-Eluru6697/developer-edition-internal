import { describe, expect, it } from "vitest";
import { requestFromRun, trainingRunRequestSchema } from "../src/routes/trainingRuns";

// The stored row of a real failed LoRA run (training_runs, as pg returns it).
const FAILED_LORA_RUN = {
  id: "f09a24c6-2b60-4c51-861c-b67ef8b4f6f7", status: "failed", error: "TRAINING_EXECUTION_FAILED: …",
  base_model_id: "qwen2.5-0.5b-instruct", model_name: "qwen2.5-0.5b-instruct-dbt-runner", dataset_name: "dbt-runner",
  compute_profile: "gpu-1x", experiment_id: null, target_column: null, features: null,
  split_train: null, split_val: null, split_test: null, fine_tune_method: "lora",
  lora_params: { rank: 16, alpha: 32, epochs: 3, dropout: 0.05, learningRate: "1e-4" }, full_params: null,
  hyperparams: null, preprocessing: null, requested_dataset_version: 4, dataset_snapshot_version: null,
  time_series_config: null, llm_fine_tune_config: { maxSeqLength: 1024, promptColumn: "source", completionColumn: "text" },
};

describe("re-train request", () => {
  it("rebuilds the failed run's own configuration, and it passes the create-run schema", () => {
    const body = requestFromRun(FAILED_LORA_RUN);
    expect(body).toEqual({
      baseModelId: "qwen2.5-0.5b-instruct", modelName: "qwen2.5-0.5b-instruct-dbt-runner", datasetName: "dbt-runner",
      datasetVersion: 4, computeProfile: "gpu-1x", fineTuneMethod: "lora",
      loraParams: FAILED_LORA_RUN.lora_params, llmFineTuneConfig: FAILED_LORA_RUN.llm_fine_tune_config,
    });
    const parsed = trainingRunRequestSchema.safeParse(body);
    expect(parsed.success, JSON.stringify(!parsed.success && parsed.error.flatten())).toBe(true);
  });

  it("pins the snapshot the run actually read over the one it asked for", () => {
    expect(requestFromRun({ ...FAILED_LORA_RUN, requested_dataset_version: null, dataset_snapshot_version: 7 }).datasetVersion).toBe(7);
  });

  it("a classical run keeps its target, features, splits and hyperparameters", () => {
    const body = requestFromRun({
      ...FAILED_LORA_RUN, base_model_id: "xgboost-regressor", fine_tune_method: null, lora_params: null,
      llm_fine_tune_config: null, target_column: "Salary", features: ["YearsExperience"],
      split_train: 70, split_val: 15, split_test: 15, hyperparams: { n_estimators: 200 },
    });
    expect(body).toMatchObject({ targetColumn: "Salary", features: ["YearsExperience"], splitTrain: 70, hyperparams: { n_estimators: 200 } });
    expect(trainingRunRequestSchema.safeParse(body).success).toBe(true);
  });
});
