import { PYTHON_BIN } from "./pythonBin";
import { createHash } from "crypto";
import { promises as fs } from "fs";
import path from "path";
import { spawn } from "child_process";

/**
 * The real interpreter and library versions training actually ran under.
 *
 * A reproducibility record that names the dataset, the seed and the code commit
 * but not the Python stack is not reproducible: scikit-learn changes default
 * hyperparameters between minor versions, and the same seed on a different
 * numpy gives different splits. This asks the SAME `python3` the trainers spawn
 * what it is, rather than recording a version string from configuration that
 * could describe a different interpreter than the one that ran.
 *
 * Probed once per process and cached — this runs on every registration, and the
 * answer cannot change while the process lives. A probe that fails (no python3,
 * an import error) records null, never a guess.
 */
let pythonEnvProbe: Promise<{ pythonVersion: string | null; libraryVersions: string | null }> | null = null;

function probePythonEnv(): Promise<{ pythonVersion: string | null; libraryVersions: string | null }> {
  return new Promise((resolve) => {
    const script = [
      "import json,sys,platform",
      "libs={}",
      "import importlib.metadata as md",
      "for name in ('scikit-learn','numpy','pandas','joblib','torch','transformers','peft','accelerate','datasets','xgboost','lightgbm','catboost','statsmodels','prophet','pmdarima','umap-learn'):",
      "    try: libs[name]=md.version(name)",
      "    except Exception: pass",
      "print(json.dumps({'python':platform.python_version(),'libs':libs}))",
    ].join("\n");
    let out = "";
    const child = spawn(PYTHON_BIN, ["-c", script], { stdio: ["ignore", "pipe", "ignore"] });
    const timer = setTimeout(() => child.kill(), 10000);
    child.stdout.on("data", (d) => (out += d));
    child.on("error", () => { clearTimeout(timer); resolve({ pythonVersion: null, libraryVersions: null }); });
    child.on("close", () => {
      clearTimeout(timer);
      try {
        const parsed = JSON.parse(out.trim());
        const libs = Object.entries(parsed.libs as Record<string, string>).map(([k, v]) => `${k}==${v}`);
        resolve({ pythonVersion: parsed.python ?? null, libraryVersions: libs.length ? libs.join(" · ") : null });
      } catch {
        resolve({ pythonVersion: null, libraryVersions: null });
      }
    });
  });
}

async function sha256File(filePath:string):Promise<string|null>{try{const b=await fs.readFile(filePath);return createHash("sha256").update(b).digest("hex")}catch{return null}}
export async function captureReproducibility(extra:Record<string,unknown>={}):Promise<Record<string,unknown>>{
  const root=path.resolve(__dirname,"..","..");
  const packageLockHash=await sha256File(path.join(root,"package-lock.json"));
  const requirementsHash=await sha256File(path.join(root,"requirements-training.txt"));
  if(!pythonEnvProbe) pythonEnvProbe=probePythonEnv();
  const pythonEnv=await pythonEnvProbe;
  return {
    capturedAt:new Date().toISOString(),
    gitCommit:process.env.GIT_COMMIT || process.env.SOURCE_VERSION || null,
    gitRepository:process.env.GIT_REPOSITORY || null,
    containerImage:process.env.CONTAINER_IMAGE || process.env.IMAGE_NAME || null,
    containerImageDigest:process.env.CONTAINER_IMAGE_DIGEST || process.env.IMAGE_DIGEST || null,
    trainerVersion:process.env.TRAINER_VERSION || process.env.npm_package_version || "2.37.0",
    nodeVersion:process.version,
    platform:process.platform,
    arch:process.arch,
    packageLockSha256:packageLockHash,
    trainingRequirementsSha256:requirementsHash,
    pythonVersion:pythonEnv.pythonVersion,
    libraryVersions:pythonEnv.libraryVersions,
    computeProfile:extra.computeProfile ?? null,
    seed:extra.seed ?? null,
    baseModelId:extra.baseModelId ?? null,
    baseModelUri:extra.baseModelUri ?? null,
    datasetName:extra.datasetName ?? null,
    datasetSnapshotVersion:extra.datasetSnapshotVersion ?? null,
    datasetChecksum:extra.datasetChecksum ?? null,
    preprocessing:extra.preprocessing ?? null,
    actualFramework:extra.actualFramework ?? null,
    actualArchitecture:extra.actualArchitecture ?? null,
    ...extra,
  };
}

/** The libraries a model of this kind actually trains with. The probe records every ML
 *  library in the environment (kept whole in the stored record), but listing torch and
 *  transformers on a scikit-learn model — or xgboost on an LLM — says nothing true about
 *  what produced it. */
export function relevantLibraries(all: string | null, domain?: string | null, framework?: string | null): string | null {
  if (!all) return all;
  const fw = (framework || "").toLowerCase();
  const wanted =
    domain === "llm" ? ["torch", "transformers", "peft", "accelerate", "datasets"]
    : fw === "pytorch" ? ["torch", "numpy", "pandas", "scikit-learn"]
    : ["statsmodels", "prophet", "pmdarima"].includes(fw) ? [fw, "numpy", "pandas", "scikit-learn"]
    : ["scikit-learn", "numpy", "pandas", "joblib", ...(["xgboost", "lightgbm", "catboost", "umap-learn"].includes(fw) ? [fw] : [])];
  const byName = new Map(all.split(" · ").map((s) => [s.split("==")[0], s] as [string, string]));
  const kept = wanted.map((n) => byName.get(n)).filter((s): s is string => !!s);
  return kept.length ? kept.join(" · ") : all;
}
