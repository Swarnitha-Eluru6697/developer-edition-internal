import { PYTHON_BIN } from "./pythonBin";
import { spawn } from "child_process";
import path from "path";

const SCRIPT = path.join(__dirname, "..", "training", "llm_gpu_certify.py");
export interface LlmGpuCertificationResult { ok:boolean; certified:boolean; error?:string; [k:string]:unknown }

export function runLlmGpuCertification(configPath?: string): Promise<LlmGpuCertificationResult> {
  return new Promise((resolve) => {
    const args = [SCRIPT, ...(configPath ? ["--config", configPath] : ["--preflight"])];
    const child = spawn(PYTHON_BIN, args);
    let stdout="", stderr="";
    child.stdout.on("data", d => stdout += d.toString()); child.stderr.on("data", d => stderr += d.toString());
    child.on("error", e => resolve({ok:false,certified:false,error:e.message}));
    child.on("close", code => {
      try { const out=JSON.parse(stdout.trim()); resolve(out); }
      catch { resolve({ok:false,certified:false,error:`certification exited ${code}: ${stderr.slice(-1200)}`}); }
    });
  });
}
