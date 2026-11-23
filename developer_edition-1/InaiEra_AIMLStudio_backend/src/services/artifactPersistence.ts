import { getStorage } from "../storage";

export class ArtifactPersistenceError extends Error {
  constructor(message: string) {
    super(`ARTIFACT_PERSISTENCE_FAILED: ${message}`);
    this.name = "ArtifactPersistenceError";
  }
}

export function storageKeyFromUri(uri: string, bucketOrContainer: string): string {
  try {
    const parsed = new URL(uri);
    if (parsed.protocol === "s3:" || parsed.protocol === "gs:") {
      return parsed.pathname.replace(/^\//, "");
    }
    if (parsed.protocol === "http:" || parsed.protocol === "https:") {
      const parts = parsed.pathname.replace(/^\//, "").split("/");
      if (parts[0] === bucketOrContainer) parts.shift();
      return parts.join("/");
    }
  } catch {
    // Fall through for non-URL legacy values.
  }
  return uri.replace(/^[a-z0-9]+:\/\/[^/]+\//i, "");
}

function uriBelongsToConfiguredStorage(uri: string, bucketOrContainer: string): boolean {
  try {
    const parsed = new URL(uri);
    if (parsed.protocol === "s3:" || parsed.protocol === "gs:") return parsed.hostname === bucketOrContainer;
    if (parsed.protocol === "http:" || parsed.protocol === "https:") {
      return parsed.pathname.replace(/^\//, "").split("/")[0] === bucketOrContainer;
    }
  } catch {
    return false;
  }
  return false;
}

export async function verifyDurableArtifact(uri: string | null): Promise<{ ok: true; key: string } | { ok: false; reason: string }> {
  if (!uri) return { ok: false, reason: "model version has no weights_uri" };
  const storage = getStorage();
  if (!storage) return { ok: false, reason: "STORAGE_BACKEND is not configured" };
  if (!uriBelongsToConfiguredStorage(uri, storage.bucket)) {
    return { ok: false, reason: `artifact URI '${uri}' does not belong to configured ${storage.backend} bucket/container '${storage.bucket}'` };
  }
  const key = storageKeyFromUri(uri, storage.bucket);
  if (!key) return { ok: false, reason: `could not derive an object key from '${uri}'` };
  try {
    const exists = await storage.objectExists(key);
    return exists ? { ok: true, key } : { ok: false, reason: `artifact '${uri}' does not exist in configured ${storage.backend} storage` };
  } catch (err) {
    return { ok: false, reason: `artifact existence check failed: ${err instanceof Error ? err.message : String(err)}` };
  }
}
