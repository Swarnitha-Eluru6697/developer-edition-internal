import { describe, expect, it } from "vitest";
import { storageKeyFromUri } from "./artifactPersistence";

describe("storageKeyFromUri", () => {
  it("extracts S3 keys", () => expect(storageKeyFromUri("s3://bucket/trained-models/a/model.joblib", "bucket")).toBe("trained-models/a/model.joblib"));
  it("extracts GCS keys", () => expect(storageKeyFromUri("gs://bucket/trained-models/a/model.joblib", "bucket")).toBe("trained-models/a/model.joblib"));
  it("removes the ADLS container from HTTPS paths", () => expect(storageKeyFromUri("https://acct.blob.core.windows.net/models/trained-models/a/model.joblib", "models")).toBe("trained-models/a/model.joblib"));
});
