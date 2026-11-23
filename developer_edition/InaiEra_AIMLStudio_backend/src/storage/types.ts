import type { Readable } from "stream";

// A real, pluggable object-storage abstraction — S3, ADLS Gen2 (via the
// Blob API, which ADLS Gen2 is built on), and GCS all implement the same
// interface, selected at runtime via STORAGE_BACKEND. This is what "the
// customer's preferred storage" means concretely: one env var picks the
// backend, everything else in this app (weight downloads, trained-model
// uploads) is written against ObjectStorage, not against any one cloud's
// SDK directly.

export interface PutObjectResult {
  // The URI ML Studio registers as a model version's real location —
  // replaces the placeholder strings (training-run://..., pvc://...)
  // used before this existed.
  uri: string;
  bytesWritten: number;
}

export interface ObjectStorage {
  readonly backend: "s3" | "adls" | "gcs";
  readonly bucket: string;

  // key is a path within the bucket/container — e.g.
  // "base-models/llama-3-8b-instruct/model-00001-of-00004.safetensors"
  // or "trained-models/fraud-detector-dl/v3/model.joblib".
  putObject(key: string, data: Buffer, contentType?: string): Promise<PutObjectResult>;
  // Same write, without ever holding the object in memory. `contentLength` is
  // required because S3 needs it up front for a non-replayable stream, and
  // because knowing it lets every backend verify what it actually wrote.
  //
  // This exists for dataset uploads, which are routinely hundreds of megabytes
  // and were previously buffered whole — once by the body parser, once for the
  // profiler's temp file, and once again here.
  putObjectStream(key: string, body: Readable, contentLength: number, contentType?: string): Promise<PutObjectResult>;
  getObject(key: string): Promise<Buffer>;
  objectExists(key: string): Promise<boolean>;
  // Size and time of one object, or null if it isn't there. Optional: backends that
  // can't answer cheaply leave it out and callers fall back to objectExists.
  statObject?(key: string): Promise<{ sizeBytes: number; lastModified: string | null } | null>;
  // The URI form this backend's own SDK/consumers expect for this key —
  // s3://bucket/key, https://account.blob.core.windows.net/container/key,
  // gs://bucket/key — without actually reading the object.
  uriFor(key: string): string;
}

export class StorageError extends Error {
  constructor(message: string, public readonly cause?: unknown) {
    super(message);
  }
}
