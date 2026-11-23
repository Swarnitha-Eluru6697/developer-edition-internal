import { Router, Request, Response } from "express";
import { getStorage } from "../storage";
import { storageKeyFromUri } from "../services/artifactPersistence";
import { PERM, requirePermission } from "../middleware/permissions";

/**
 * Where a model artifact actually lives, and a way to get it.
 *
 * A version records its weights as a storage URI ("s3://bucket/trained-models/..."),
 * which pages used to print as-is. Locally that bucket is MinIO, not AWS, so the URI
 * opened nowhere and said nothing about whether the file was still there (artifacts
 * written to the old in-memory store were gone while the URI remained). This answers
 * with the real endpoint, bucket and path, whether the object exists and its size,
 * plus a console link and a download.
 *
 * Only model artifacts are reachable: keys under trained-models/ and base-models/.
 */
export const artifactsRouter = Router();

const ALLOWED_PREFIXES = ["trained-models/", "base-models/"];

function resolve(req: Request): { key: string; storage: NonNullable<ReturnType<typeof getStorage>> } | { error: string; status: number } {
  const uri = String(req.query.uri ?? "");
  const storage = getStorage();
  if (!storage) return { error: "no object storage is configured (STORAGE_BACKEND)", status: 503 };
  if (!uri) return { error: "pass ?uri=<artifact uri>", status: 400 };
  const key = storageKeyFromUri(uri, storage.bucket);
  if (key.includes("..") || !ALLOWED_PREFIXES.some((p) => key.startsWith(p))) {
    return { error: "only model artifacts (trained-models/, base-models/) can be located here", status: 400 };
  }
  return { key, storage };
}

function describeBackend() {
  const backend = (process.env.STORAGE_BACKEND || "").toLowerCase();
  const endpoint = process.env.S3_ENDPOINT || null;
  const local = !!endpoint && /localhost|127\.0\.0\.1/.test(endpoint);
  const name = backend === "s3" ? (endpoint ? (local ? "MinIO (local, S3-compatible)" : "S3-compatible storage") : "Amazon S3")
    : backend === "gcs" ? "Google Cloud Storage" : backend === "adls" ? "Azure Data Lake Storage" : backend || "unknown";
  // The MinIO console runs next to the API (compose maps 9010 -> API, 9011 -> console).
  const consoleBase = process.env.S3_CONSOLE_URL || (local && endpoint ? endpoint.replace(/:9010\b/, ":9011") : null);
  return { name, endpoint, consoleBase };
}

artifactsRouter.get("/artifacts/locate", requirePermission(PERM.train), async (req: Request, res: Response) => {
  const r = resolve(req);
  if ("error" in r) return res.status(r.status).json({ error: r.error });
  const { key, storage } = r;
  const stat = storage.statObject ? await storage.statObject(key) : ((await storage.objectExists(key)) ? { sizeBytes: null, lastModified: null } : null);
  const be = describeBackend();
  const folder = key.slice(0, key.lastIndexOf("/") + 1);
  res.json({
    uri: storage.uriFor(key),
    storage: be.name,
    endpoint: be.endpoint,
    bucket: storage.bucket,
    key,
    fileName: key.slice(key.lastIndexOf("/") + 1),
    exists: !!stat,
    sizeBytes: stat?.sizeBytes ?? null,
    lastModified: stat?.lastModified ?? null,
    // MinIO's object browser addresses a folder by its base64-encoded prefix.
    consoleUrl: be.consoleBase ? `${be.consoleBase}/browser/${encodeURIComponent(storage.bucket)}/${Buffer.from(folder).toString("base64")}` : null,
    downloadPath: `/api/artifacts/download?uri=${encodeURIComponent(storage.uriFor(key))}`,
  });
});

artifactsRouter.get("/artifacts/download", requirePermission(PERM.train), async (req: Request, res: Response) => {
  const r = resolve(req);
  if ("error" in r) return res.status(r.status).json({ error: r.error });
  const { key, storage } = r;
  if (!(await storage.objectExists(key))) {
    return res.status(404).json({ error: `artifact not found in storage: ${storage.uriFor(key)} — it may have been written to an earlier, non-persistent store; re-train to regenerate it` });
  }
  const data = await storage.getObject(key);
  res.setHeader("Content-Type", "application/octet-stream");
  res.setHeader("Content-Disposition", `attachment; filename="${key.split("/").slice(-3).join("__")}"`);
  res.send(data);
});
