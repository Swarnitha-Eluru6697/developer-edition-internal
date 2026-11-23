// Domain types shared conceptually with the frontend (frontend/src/types/domain.ts
// mirrors this file — see README for why they're not a single shared package yet).

export type Domain = "ml" | "llm";
export type ModelKind = "base" | "adapter";

export interface BaseModel {
  id: string;
  domain: Domain;
  title: string;
  task: string;
  description: string;
  framework: string;
  license: string;
  uri: string;
  gpu: boolean;
  compute_profile: string;
  long_description: string;
}

export interface ModelVersion {
  v: number;
  alias: "champion" | "challenger" | "staging" | "archived" | "training";
  note: string;
}

export interface CatalogModel {
  id: string;
  task: string;
  domain: Domain;
  kind: ModelKind;
  framework: string;
  baseRef?: string;
  updated: string;
  versions: ModelVersion[];
}

export interface Dataset {
  name: string;
  source: "upload" | "iceberg";
  structureType: "structured" | "unstructured";
  size: string;
  rows: string;
  owner: string;
  tags: string[];
  detail: string;
}

export interface RateLimits {
  rpm: number;
  burst: number;
  concurrent: number;
}

export interface Endpoint {
  modelId: string;
  title: string;
  url: string;
  status: "healthy" | "scaled to 0" | "callable";
  authMethod: "OAuth 2.0 (Client Credentials)";
  tokenEndpoint: string;
  clientId: string;
  clientSecretMasked: string;
  rateLimits: RateLimits | null;
}

export type FineTuneMethod = "lora" | "full";

export interface LoraParams {
  rank: number;
  alpha: number;
  dropout: number;
  learningRate: string;
  epochs: number;
}

export interface FullFineTuneParams {
  learningRate: string;
  epochs: number;
  warmupSteps: number;
}

export interface TrainingRunRequest {
  baseModelId: string;
  datasetName: string;
  computeProfile: string;
  // Classical ML fields — omitted entirely for LLM base models
  targetColumn?: string;
  features?: string[];
  splitTrain?: number;
  splitVal?: number;
  splitTest?: number;
  // LLM fields — omitted entirely for classical ML base models
  fineTuneMethod?: FineTuneMethod;
  loraParams?: LoraParams;
  fullParams?: FullFineTuneParams;
}

export type TrainingRunStatus = "queued" | "running" | "succeeded" | "failed";

export interface TrainingRun extends TrainingRunRequest {
  id: string;
  status: TrainingRunStatus;
  createdAt: string;
}

export interface AlertPolicy {
  id: string;
  name: string;
  label: string;
  severity: "low" | "medium" | "high";
  operator: ">" | "<";
  unit: string;
  window: string;
  action: string;
  enabled: boolean;
  appliesTo: Domain[] | "classification"[] | string[];
}
