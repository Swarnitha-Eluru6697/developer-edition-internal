import { S3Storage } from "./s3Client";
import { AdlsStorage } from "./adlsClient";
import { GcsStorage } from "./gcsClient";
import type { ObjectStorage } from "./types";
import { StorageError } from "./types";

export type { ObjectStorage, PutObjectResult } from "./types";
export { StorageError } from "./types";

// One env var picks the customer's preferred backend — everything else in
// this app (weight downloads, trained-model uploads) is written against
// the ObjectStorage interface, never against a specific cloud's SDK.
let cached: ObjectStorage | null | undefined;

export function getStorage(): ObjectStorage | null {
  if (cached !== undefined) return cached;

  const backend = (process.env.STORAGE_BACKEND || "").toLowerCase();
  if (backend === "s3") {
    if (!process.env.S3_BUCKET) throw new StorageError("STORAGE_BACKEND=s3 requires S3_BUCKET");
    cached = new S3Storage({
      bucket: process.env.S3_BUCKET,
      region: process.env.S3_REGION || "us-east-1",
      accessKeyId: process.env.S3_ACCESS_KEY_ID,
      secretAccessKey: process.env.S3_SECRET_ACCESS_KEY,
      endpoint: process.env.S3_ENDPOINT, // local/self-hosted testing only — unset for real AWS
      forcePathStyle: process.env.S3_FORCE_PATH_STYLE === "true",
    });
  } else if (backend === "adls") {
    if (!process.env.ADLS_CONTAINER) throw new StorageError("STORAGE_BACKEND=adls requires ADLS_CONTAINER");
    cached = new AdlsStorage({
      container: process.env.ADLS_CONTAINER,
      accountName: process.env.ADLS_ACCOUNT_NAME || "",
      accountKey: process.env.ADLS_ACCOUNT_KEY,
      connectionString: process.env.ADLS_CONNECTION_STRING,
    });
  } else if (backend === "gcs") {
    if (!process.env.GCS_BUCKET) throw new StorageError("STORAGE_BACKEND=gcs requires GCS_BUCKET");
    cached = new GcsStorage({
      bucket: process.env.GCS_BUCKET,
      projectId: process.env.GCS_PROJECT_ID,
      keyFilename: process.env.GCS_KEY_FILENAME,
      credentials: process.env.GCS_CREDENTIALS_JSON ? JSON.parse(process.env.GCS_CREDENTIALS_JSON) : undefined,
    });
  } else {
    // Unconfigured is a real, valid state — not every deployment has
    // object storage wired up yet. Callers must handle null explicitly
    // (weight downloads/uploads become a clear "storage not configured"
    // error rather than silently no-op-ing).
    cached = null;
  }
  return cached;
}

// Test-only: clears the cached client so a test can reconfigure env vars
// and get a fresh instance. Never called from application code.
export function _resetStorageForTests(): void {
  cached = undefined;
}
