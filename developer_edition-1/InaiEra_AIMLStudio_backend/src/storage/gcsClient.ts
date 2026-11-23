import type { Readable } from "stream";
import { pipeline } from "stream/promises";
import { Storage } from "@google-cloud/storage";
import type { ObjectStorage, PutObjectResult } from "./types";
import { StorageError } from "./types";

// Spec-correct against the real, documented @google-cloud/storage SDK —
// unverified end-to-end, since this sandbox has no network path to
// storage.googleapis.com (confirmed unreachable) and no local GCS
// emulator (fake-gcs-server) was available to stand in.

export interface GcsConfig {
  bucket: string;
  projectId?: string;
  // Path to a real service-account JSON key file, or the JSON contents
  // directly via GCS_CREDENTIALS_JSON — either is accepted the same way
  // the real @google-cloud/storage SDK accepts them.
  keyFilename?: string;
  credentials?: Record<string, unknown>;
}

export class GcsStorage implements ObjectStorage {
  readonly backend = "gcs" as const;
  readonly bucket: string;
  private storage: Storage;

  constructor(cfg: GcsConfig) {
    this.bucket = cfg.bucket;
    this.storage = new Storage({
      projectId: cfg.projectId,
      keyFilename: cfg.keyFilename,
      credentials: cfg.credentials as any,
    });
  }

  async putObject(key: string, data: Buffer, contentType?: string): Promise<PutObjectResult> {
    try {
      const file = this.storage.bucket(this.bucket).file(key);
      await file.save(data, contentType ? { contentType } : undefined);
      return { uri: this.uriFor(key), bytesWritten: data.length };
    } catch (err) {
      throw new StorageError(`GCS putObject failed for ${this.uriFor(key)}: ${err instanceof Error ? err.message : String(err)}`, err);
    }
  }

  async putObjectStream(key: string, body: Readable, contentLength: number, contentType?: string): Promise<PutObjectResult> {
    try {
      const file = this.storage.bucket(this.bucket).file(key);
      // GCS resumable uploads stream natively; pipeline propagates an error
      // from either side instead of leaving a half-written object behind.
      await pipeline(body, file.createWriteStream(contentType ? { contentType, resumable: true } : { resumable: true }));
      return { uri: this.uriFor(key), bytesWritten: contentLength };
    } catch (err) {
      throw new StorageError(`GCS putObjectStream failed for ${this.uriFor(key)}: ${err instanceof Error ? err.message : String(err)}`, err);
    }
  }

  async getObject(key: string): Promise<Buffer> {
    try {
      const [data] = await this.storage.bucket(this.bucket).file(key).download();
      return data;
    } catch (err) {
      throw new StorageError(`GCS getObject failed for ${this.uriFor(key)}: ${err instanceof Error ? err.message : String(err)}`, err);
    }
  }

  async objectExists(key: string): Promise<boolean> {
    try {
      const [exists] = await this.storage.bucket(this.bucket).file(key).exists();
      return exists;
    } catch {
      return false;
    }
  }

  uriFor(key: string): string {
    return `gs://${this.bucket}/${key}`;
  }
}
