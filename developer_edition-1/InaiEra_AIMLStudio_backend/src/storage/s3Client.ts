import { S3Client, PutObjectCommand, GetObjectCommand, HeadObjectCommand } from "@aws-sdk/client-s3";
import type { Readable } from "stream";
import type { ObjectStorage, PutObjectResult } from "./types";
import { StorageError } from "./types";

export interface S3Config {
  bucket: string;
  region: string;
  accessKeyId?: string;
  secretAccessKey?: string;
  // Set for local/self-hosted S3-compatible testing (moto, MinIO) — real
  // AWS is reached with this left unset.
  endpoint?: string;
  forcePathStyle?: boolean;
}

export class S3Storage implements ObjectStorage {
  readonly backend = "s3" as const;
  readonly bucket: string;
  private client: S3Client;

  constructor(cfg: S3Config) {
    this.bucket = cfg.bucket;
    this.client = new S3Client({
      region: cfg.region,
      endpoint: cfg.endpoint,
      forcePathStyle: cfg.forcePathStyle,
      credentials: cfg.accessKeyId && cfg.secretAccessKey ? { accessKeyId: cfg.accessKeyId, secretAccessKey: cfg.secretAccessKey } : undefined,
      // Flexible checksums, but only against real AWS.
      //
      // The SDK defaults this to WHEN_SUPPORTED, which appends a trailing CRC32
      // and therefore sends a streaming body as `aws-chunked`. Real S3 handles
      // that natively; the S3-compatible servers used locally (moto, MinIO) parse
      // it in application code, and moto is pathologically slow at it — a 316 MB
      // streamed upload took 386 seconds, against 1.6 seconds with this set to
      // WHEN_REQUIRED. That is the whole reason a large dataset upload appeared
      // to hang.
      //
      // Scoped to `cfg.endpoint` so only a custom (local/self-hosted) endpoint
      // opts out; a real AWS deployment keeps the SDK's integrity checks. Nothing
      // is left unverified either way: the upload path computes its own SHA-256
      // over the bytes and stores it on the snapshot row, which is what dataset
      // integrity is actually checked against here.
      ...(cfg.endpoint ? { requestChecksumCalculation: "WHEN_REQUIRED" as const } : {}),
    });
  }

  async putObject(key: string, data: Buffer, contentType?: string): Promise<PutObjectResult> {
    try {
      await this.client.send(new PutObjectCommand({ Bucket: this.bucket, Key: key, Body: data, ContentType: contentType }));
      return { uri: this.uriFor(key), bytesWritten: data.length };
    } catch (err) {
      throw new StorageError(`S3 putObject failed for s3://${this.bucket}/${key}: ${err instanceof Error ? err.message : String(err)}`, err);
    }
  }

  async putObjectStream(key: string, body: Readable, contentLength: number, contentType?: string): Promise<PutObjectResult> {
    try {
      // ContentLength is mandatory here: a stream has no length the SDK can
      // discover, and without it the SDK would try to buffer the whole body to
      // find one — which is the exact thing this method exists to avoid.
      await this.client.send(new PutObjectCommand({
        Bucket: this.bucket, Key: key, Body: body, ContentLength: contentLength, ContentType: contentType,
      }));
      return { uri: this.uriFor(key), bytesWritten: contentLength };
    } catch (err) {
      throw new StorageError(`S3 putObjectStream failed for s3://${this.bucket}/${key}: ${err instanceof Error ? err.message : String(err)}`, err);
    }
  }

  async getObject(key: string): Promise<Buffer> {
    try {
      const res = await this.client.send(new GetObjectCommand({ Bucket: this.bucket, Key: key }));
      const chunks: Uint8Array[] = [];
      // @ts-expect-error — Body is a real Node.js Readable at runtime under the Node SDK build
      for await (const chunk of res.Body) chunks.push(chunk);
      return Buffer.concat(chunks);
    } catch (err) {
      throw new StorageError(`S3 getObject failed for s3://${this.bucket}/${key}: ${err instanceof Error ? err.message : String(err)}`, err);
    }
  }

  async objectExists(key: string): Promise<boolean> {
    try {
      await this.client.send(new HeadObjectCommand({ Bucket: this.bucket, Key: key }));
      return true;
    } catch {
      return false;
    }
  }

  async statObject(key: string): Promise<{ sizeBytes: number; lastModified: string | null } | null> {
    try {
      const r = await this.client.send(new HeadObjectCommand({ Bucket: this.bucket, Key: key }));
      return { sizeBytes: Number(r.ContentLength ?? 0), lastModified: r.LastModified ? r.LastModified.toISOString() : null };
    } catch {
      return null;
    }
  }

  uriFor(key: string): string {
    return `s3://${this.bucket}/${key}`;
  }
}
