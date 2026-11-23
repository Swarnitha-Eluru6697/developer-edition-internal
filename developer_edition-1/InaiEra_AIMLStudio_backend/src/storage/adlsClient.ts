import type { Readable } from "stream";
import { BlobServiceClient, StorageSharedKeyCredential } from "@azure/storage-blob";
import type { ObjectStorage, PutObjectResult } from "./types";
import { StorageError } from "./types";

// ADLS Gen2 is Azure Blob Storage with the hierarchical-namespace feature
// enabled on the storage account — the Blob SDK's data-plane API (upload/
// download/exists on a container) works against it exactly as it does
// against a plain blob container, which is what this uses. A dedicated
// Data Lake SDK (@azure/storage-file-datalake) exists for directory-level
// operations (ACLs, atomic renames) this app doesn't need — object
// put/get/exists is the whole surface here.
//
// Spec-correct against the real, documented Azure SDK — unverified
// end-to-end, since this sandbox has no network path to any Azure
// endpoint (confirmed: blob.core.windows.net is unreachable here) and no
// local Azure Storage emulator (Azurite) was available to stand in.

export interface AdlsConfig {
  // "container" in Blob terms — the customer's ADLS filesystem/container name.
  container: string;
  accountName: string;
  accountKey?: string;
  // Full connection string, if provided, takes precedence over accountName/accountKey.
  connectionString?: string;
}

export class AdlsStorage implements ObjectStorage {
  readonly backend = "adls" as const;
  readonly bucket: string; // the container name, kept as `bucket` to satisfy the common interface
  private serviceClient: BlobServiceClient;

  constructor(cfg: AdlsConfig) {
    this.bucket = cfg.container;
    if (cfg.connectionString) {
      this.serviceClient = BlobServiceClient.fromConnectionString(cfg.connectionString);
    } else {
      if (!cfg.accountKey) throw new StorageError("ADLS config needs either connectionString or accountName+accountKey");
      const credential = new StorageSharedKeyCredential(cfg.accountName, cfg.accountKey);
      this.serviceClient = new BlobServiceClient(`https://${cfg.accountName}.blob.core.windows.net`, credential);
    }
  }

  private container() {
    return this.serviceClient.getContainerClient(this.bucket);
  }

  async putObject(key: string, data: Buffer, contentType?: string): Promise<PutObjectResult> {
    try {
      const blockBlobClient = this.container().getBlockBlobClient(key);
      await blockBlobClient.uploadData(data, contentType ? { blobHTTPHeaders: { blobContentType: contentType } } : undefined);
      return { uri: this.uriFor(key), bytesWritten: data.length };
    } catch (err) {
      throw new StorageError(`ADLS putObject failed for ${this.uriFor(key)}: ${err instanceof Error ? err.message : String(err)}`, err);
    }
  }

  async putObjectStream(key: string, body: Readable, contentLength: number, contentType?: string): Promise<PutObjectResult> {
    try {
      const blockBlobClient = this.container().getBlockBlobClient(key);
      // 4 MiB buffers, 5 concurrent — the SDK's own streaming upload, so the
      // blob is assembled from blocks rather than from one in-memory body.
      await blockBlobClient.uploadStream(
        body, 4 * 1024 * 1024, 5,
        contentType ? { blobHTTPHeaders: { blobContentType: contentType } } : undefined
      );
      return { uri: this.uriFor(key), bytesWritten: contentLength };
    } catch (err) {
      throw new StorageError(`ADLS putObjectStream failed for ${this.uriFor(key)}: ${err instanceof Error ? err.message : String(err)}`, err);
    }
  }

  async getObject(key: string): Promise<Buffer> {
    try {
      const blobClient = this.container().getBlobClient(key);
      const res = await blobClient.download();
      const chunks: Uint8Array[] = [];
      if (!res.readableStreamBody) throw new Error("empty response body");
      for await (const chunk of res.readableStreamBody) chunks.push(chunk as Uint8Array);
      return Buffer.concat(chunks);
    } catch (err) {
      throw new StorageError(`ADLS getObject failed for ${this.uriFor(key)}: ${err instanceof Error ? err.message : String(err)}`, err);
    }
  }

  async objectExists(key: string): Promise<boolean> {
    try {
      return await this.container().getBlobClient(key).exists();
    } catch {
      return false;
    }
  }

  uriFor(key: string): string {
    return `https://${this.serviceClient.accountName}.blob.core.windows.net/${this.bucket}/${key}`;
  }
}
