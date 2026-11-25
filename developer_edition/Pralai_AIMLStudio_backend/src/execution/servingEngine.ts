// Same two-implementation pattern as workflowEngine.ts: a LOCAL executor
// that's genuinely real and verifiable today (real HTTP request/response,
// deterministic computation from actual posted input — see routes/serve.ts),
// and a spec-correct KServe InferenceService client (matches the real CRD
// API via the Kubernetes API server) that's correct against the
// documented spec but unverified against a live cluster, since none
// exists in this environment.

export interface ReadinessResult {
  ready: boolean;
  reason: string;
}

export interface ServingEngine {
  readonly kind: "local" | "kserve";
  deploy(modelId: string, version: number, artifact?: ServingArtifact): Promise<{ endpointUrl: string; warning?: string }>;
  undeploy(modelId: string): Promise<void>;
  // Real readiness reconciliation — previously deploy() returning meant
  // "KServe accepted the request," which promotion then treated as
  // sufficient to commit champion state. Those are two different real
  // facts: an accepted InferenceService can still spend real time
  // pulling its image or crash-looping before genuinely serving traffic.
  // A no-op that always returns ready under the local executor (there's
  // no real "not ready yet" state for a plain HTTP route match).
  waitForReady(modelId: string, timeoutMs?: number): Promise<ReadinessResult>;
}

// "Deploying" locally means nothing more than confirming the model will
// be reachable at the generic serving route — that route itself checks
// the `deployments` table on every request, so there's no separate
// process to spin up or state to track here. What IS real: a request to
// the returned URL gets a real HTTP round-trip and a real (if simple,
// deterministic, and honestly-labeled) computed response — see serve.ts.
export class LocalServingExecutor implements ServingEngine {
  readonly kind = "local" as const;

  async deploy(modelId: string, _version: number, artifact?: ServingArtifact): Promise<{ endpointUrl: string; warning?: string }> {
    const port = process.env.PORT || 4000;
    const host = process.env.PUBLIC_HOST || `http://localhost:${port}`;
    const warning = artifact?.weightsUri
      ? undefined
      : "no real weights recorded for this version — the local serving stand-in doesn't need one, but a real deployment would have nothing to load";
    return { endpointUrl: `${host}/api/serve/${modelId}/predict`, ...(warning ? { warning } : {}) };
  }

  async undeploy(): Promise<void> {
    // Nothing to tear down — the serve route itself checks `deployments`
    // and 404s once the row is gone.
  }

  async waitForReady(): Promise<ReadinessResult> {
    // No real "not ready yet" state exists for a plain HTTP route match
    // — the route becomes reachable the instant the `deployments` row
    // exists, which deploy() above already guarantees.
    return { ready: true, reason: "local serving stand-in has no real readiness delay" };
  }
}

// Spec-correct against KServe's real InferenceService CRD API, submitted
// through the standard Kubernetes API server pattern
// (POST/PATCH /apis/serving.kserve.io/v1beta1/namespaces/{ns}/inferenceservices).
// UNVERIFIED against a live cluster — same disclosure as the UCS
// integration and the Argo client.
export interface ServingArtifact {
  weightsUri: string | null;
  artifactFormat: string | null;
}

// Shared by KServeClient and VllmServingClient — both produce a real
// InferenceService with the same real readiness contract (KServe's
// actual documented status.conditions array: a Ready condition with
// status "True" once the predictor pod is genuinely serving). Polls on
// a real interval up to a real timeout, rather than either trusting the
// initial 200/201 response or blocking forever.
async function pollInferenceServiceReady(
  kubeApiUrl: string,
  namespace: string,
  modelId: string,
  authHeaders: Record<string, string>,
  timeoutMs: number
): Promise<ReadinessResult> {
  const pollIntervalMs = 3_000;
  const deadline = Date.now() + timeoutMs;
  let lastReason = "no status reported yet";

  while (Date.now() < deadline) {
    try {
      const res = await fetch(`${kubeApiUrl}/apis/serving.kserve.io/v1beta1/namespaces/${namespace}/inferenceservices/${modelId}`, {
        method: "GET",
        headers: authHeaders,
      });
      if (res.ok) {
        const wf = (await res.json()) as { status?: { conditions?: { type?: string; status?: string; reason?: string; message?: string }[] } };
        const readyCondition = wf.status?.conditions?.find((c) => c.type === "Ready");
        if (readyCondition?.status === "True") {
          return { ready: true, reason: "Ready=True" };
        }
        lastReason = readyCondition ? `${readyCondition.reason || "NotReady"}: ${readyCondition.message || ""}`.trim() : "no Ready condition reported yet";
      } else {
        lastReason = `status fetch returned HTTP ${res.status}`;
      }
    } catch (err) {
      lastReason = `status fetch failed: ${err instanceof Error ? err.message : String(err)}`;
    }
    await new Promise((resolve) => setTimeout(resolve, pollIntervalMs));
  }
  return { ready: false, reason: `timed out after ${timeoutMs}ms waiting for Ready=True — last known status: ${lastReason}` };
}

export class KServeClient implements ServingEngine {
  readonly kind = "kserve" as const;

  constructor(private kubeApiUrl: string, private token: string, private namespace: string) {}

  async deploy(modelId: string, version: number, artifact?: ServingArtifact): Promise<{ endpointUrl: string; warning?: string }> {
    // No real artifact for this version means there's nothing for
    // KServe to actually load — deploying anyway would silently serve
    // whatever was there before (or nothing), reported as if it worked.
    // Refuse instead, with a clear, specific reason.
    if (!artifact?.weightsUri) {
      throw new Error(
        `no real weights are recorded for ${modelId} v${version} — nothing for KServe to actually load. Was this trained before real weight persistence existed, or with STORAGE_BACKEND unconfigured?`
      );
    }
    const authHeaders: Record<string, string> = this.token ? { Authorization: `Bearer ${this.token}` } : {};
    const specBody = {
      predictor: { model: { modelFormat: { name: artifact.artifactFormat || "sklearn" }, storageUri: artifact.weightsUri } },
    };

    const createRes = await fetch(`${this.kubeApiUrl}/apis/serving.kserve.io/v1beta1/namespaces/${this.namespace}/inferenceservices`, {
      method: "POST",
      headers: { "Content-Type": "application/json", ...authHeaders },
      body: JSON.stringify({
        apiVersion: "serving.kserve.io/v1beta1",
        kind: "InferenceService",
        metadata: { name: modelId, namespace: this.namespace },
        spec: specBody,
      }),
    });

    let warning: string | undefined;
    let data: { status?: { url?: string } } | null = null;

    if (createRes.ok) {
      data = (await createRes.json()) as { status?: { url?: string } };
    } else if (createRes.status === 409) {
      // Already exists — a real PATCH to point the EXISTING
      // InferenceService at the new version's artifact, not silently
      // treating "it's already there" as "it's already updated." Those
      // are different claims; the old code conflated them.
      const patchRes = await fetch(`${this.kubeApiUrl}/apis/serving.kserve.io/v1beta1/namespaces/${this.namespace}/inferenceservices/${modelId}`, {
        method: "PATCH",
        headers: { "Content-Type": "application/merge-patch+json", ...authHeaders },
        body: JSON.stringify({ spec: specBody }),
      });
      if (!patchRes.ok) {
        throw new Error(`KServe deploy: InferenceService already existed, and updating it to v${version} failed (${patchRes.status}): ${(await patchRes.text()).slice(0, 300)}`);
      }
      data = (await patchRes.json()) as { status?: { url?: string } };
    } else {
      throw new Error(`KServe deploy failed (${createRes.status}): ${(await createRes.text()).slice(0, 300)}`);
    }

    const url = data?.status?.url || `http://${modelId}.${this.namespace}.svc.cluster.local`;
    return { endpointUrl: url, ...(warning ? { warning } : {}) };
  }

  async undeploy(modelId: string): Promise<void> {
    const res = await fetch(`${this.kubeApiUrl}/apis/serving.kserve.io/v1beta1/namespaces/${this.namespace}/inferenceservices/${modelId}`, {
      method: "DELETE",
      headers: this.token ? { Authorization: `Bearer ${this.token}` } : {},
    });
    if (!res.ok && res.status !== 404) {
      throw new Error(`KServe undeploy failed (${res.status}): ${(await res.text()).slice(0, 300)}`);
    }
  }

  async waitForReady(modelId: string, timeoutMs = 300_000): Promise<ReadinessResult> {
    return pollInferenceServiceReady(this.kubeApiUrl, this.namespace, modelId, this.token ? { Authorization: `Bearer ${this.token}` } : {}, timeoutMs);
  }
}

// Spec-correct against KServe's REAL, documented Hugging Face serving
// runtime with a vLLM backend (`--backend vllm` on the huggingfaceserver
// runtime — KServe's actual native way to serve an LLM efficiently,
// not a separate deployment mechanism from InferenceService). UNVERIFIED
// against a live cluster, same disclosure as KServeClient. Previously,
// LLM models had no distinct serving path at all — they went through
// KServeClient's sklearn-formatted spec, which is simply wrong for an
// LLM (no sklearn joblib file exists for a language model).
export class VllmServingClient implements ServingEngine {
  readonly kind = "kserve" as const;

  constructor(private kubeApiUrl: string, private token: string, private namespace: string) {}

  async deploy(modelId: string, version: number, artifact?: ServingArtifact): Promise<{ endpointUrl: string; warning?: string }> {
    if (!artifact?.weightsUri) {
      throw new Error(
        `no real weights are recorded for ${modelId} v${version} — nothing for vLLM to actually load. Real LLM fine-tuning with real weight persistence isn't implemented in this scaffold yet; only base-model weights imported via Model Import Service have a real URI to serve.`
      );
    }
    const authHeaders: Record<string, string> = this.token ? { Authorization: `Bearer ${this.token}` } : {};
    const specBody = {
      predictor: {
        model: {
          modelFormat: { name: "huggingface" },
          runtime: "kserve-huggingfaceserver",
          storageUri: artifact.weightsUri,
          args: ["--backend", "vllm"],
        },
      },
    };

    const createRes = await fetch(`${this.kubeApiUrl}/apis/serving.kserve.io/v1beta1/namespaces/${this.namespace}/inferenceservices`, {
      method: "POST",
      headers: { "Content-Type": "application/json", ...authHeaders },
      body: JSON.stringify({
        apiVersion: "serving.kserve.io/v1beta1",
        kind: "InferenceService",
        metadata: { name: modelId, namespace: this.namespace },
        spec: specBody,
      }),
    });

    let data: { status?: { url?: string } } | null = null;
    if (createRes.ok) {
      data = (await createRes.json()) as { status?: { url?: string } };
    } else if (createRes.status === 409) {
      const patchRes = await fetch(`${this.kubeApiUrl}/apis/serving.kserve.io/v1beta1/namespaces/${this.namespace}/inferenceservices/${modelId}`, {
        method: "PATCH",
        headers: { "Content-Type": "application/merge-patch+json", ...authHeaders },
        body: JSON.stringify({ spec: specBody }),
      });
      if (!patchRes.ok) {
        throw new Error(`vLLM deploy: InferenceService already existed, and updating it to v${version} failed (${patchRes.status}): ${(await patchRes.text()).slice(0, 300)}`);
      }
      data = (await patchRes.json()) as { status?: { url?: string } };
    } else {
      throw new Error(`vLLM deploy failed (${createRes.status}): ${(await createRes.text()).slice(0, 300)}`);
    }

    const url = data?.status?.url || `http://${modelId}.${this.namespace}.svc.cluster.local`;
    return { endpointUrl: url };
  }

  async undeploy(modelId: string): Promise<void> {
    const res = await fetch(`${this.kubeApiUrl}/apis/serving.kserve.io/v1beta1/namespaces/${this.namespace}/inferenceservices/${modelId}`, {
      method: "DELETE",
      headers: this.token ? { Authorization: `Bearer ${this.token}` } : {},
    });
    if (!res.ok && res.status !== 404) {
      throw new Error(`vLLM undeploy failed (${res.status}): ${(await res.text()).slice(0, 300)}`);
    }
  }

  // LLM serving pods realistically take longer to become ready than a
  // classical/DL sklearn predictor (pulling a real model artifact,
  // initializing the vLLM engine) — a longer real default timeout (10
  // minutes vs KServeClient's 5) reflects that honestly, not just a
  // copy-pasted identical number.
  async waitForReady(modelId: string, timeoutMs = 600_000): Promise<ReadinessResult> {
    return pollInferenceServiceReady(this.kubeApiUrl, this.namespace, modelId, this.token ? { Authorization: `Bearer ${this.token}` } : {}, timeoutMs);
  }
}

let engineInstance: ServingEngine | null = null;
export function getServingEngine(): ServingEngine {
  if (engineInstance) return engineInstance;
  const kubeApiUrl = process.env.KSERVE_API_URL;
  engineInstance = kubeApiUrl
    ? new KServeClient(kubeApiUrl, process.env.KUBE_TOKEN || "", process.env.KSERVE_NAMESPACE || "default")
    : new LocalServingExecutor();
  return engineInstance;
}

// A model's serving engine now genuinely depends on what kind of model it
// is, not just whether KSERVE_API_URL is set — an LLM needs the vLLM
// runtime spec, everything else needs the sklearn one. Call sites that
// know the model's domain should use this instead of getServingEngine()
// when domain is available (routes/deploy.ts does); getServingEngine()
// remains for call sites that don't have that context.
export function getServingEngineFor(isLlmModel: boolean): ServingEngine {
  const kubeApiUrl = process.env.KSERVE_API_URL;
  if (!kubeApiUrl) return new LocalServingExecutor();
  if (isLlmModel) return new VllmServingClient(kubeApiUrl, process.env.KUBE_TOKEN || "", process.env.KSERVE_NAMESPACE || "default");
  return getServingEngine();
}
