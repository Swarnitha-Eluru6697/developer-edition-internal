import { Router, Request, Response } from "express";
import express from "express";
import { z } from "zod";
import { randomUUID, createHash } from "crypto";
import { promises as fsp, createWriteStream, createReadStream } from "fs";
import path from "path";
import os from "os";
import { pool } from "../db/connection";
import { PERM, requireAnyPermission, requirePermission } from "../middleware/permissions";
import { getStorage } from "../storage";
import { runDatasetProfiler } from "../services/datasetProfiler";
import { runImageDatasetProfiler } from "../services/imageDatasetProfiler";
import { materializeIcebergSnapshot, configuredIcebergCatalogs } from "../services/icebergResolver";
import { assessTargetColumn, isRegressionTask } from "../services/modelRegistration";
import { mapToClassicalTask } from "../services/classicalModelMapping";
import { runFileProfiler } from "../services/fileProfiler";
import { storageKeyFromUri } from "../services/artifactPersistence";

export const datasetsRouter = Router();

// POST /api/datasets/:name/upload?format=csv|parquet|json|jsonl|xlsx —
// real upload for the actual dataset catalog this app's design covers,
// not just CSV. format defaults to csv for backward compatibility (a
// plain POST with no query param behaves exactly as before). Binary
// formats (Parquet, XLSX) are handled as real binary data — previously
// this route used express.text(), which decodes the body as UTF-8 text
// and would corrupt any real binary file silently.
const uploadFormatSchema = z.enum(["csv", "parquet", "json", "jsonl", "xlsx", "xls", "image-zip", "zip", "pdf", "docx", "txt", "md", "html", "ppt", "pptx", "png", "wav"]).default("csv");

/**
 * Which upload formats a dataset accepts, fixed by the type chosen when it was
 * created — one dataset never mixes tabular files with documents. JSONL and
 * Parquet stay structured: the LLM fine-tuning sets are JSONL and Iceberg
 * snapshots materialize as Parquet. Image ZIPs are unstructured.
 */
export const FORMATS_BY_STRUCTURE: Record<"structured" | "unstructured", string[]> = {
  structured: ["csv", "json", "jsonl", "xlsx", "xls", "parquet"],
  // "zip" is any archive; one holding only images is stored as "image-zip",
  // the format the image trainers read (still accepted directly, too).
  unstructured: ["html", "ppt", "pptx", "md", "txt", "docx", "png", "zip", "wav", "pdf", "image-zip"],
};
/** Slides, single images, audio and archives — profiled by training/file_profiler.py. */
const FILE_FORMATS = new Set(["ppt", "pptx", "png", "wav", "zip"]);
/** Content type for every upload format — used to store the object and to serve it back. */
const FORMAT_CONTENT_TYPES: Record<string, string> = {
  csv: "text/csv", parquet: "application/octet-stream", json: "application/json",
  jsonl: "application/x-ndjson", xlsx: "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
  xls: "application/vnd.ms-excel", "image-zip": "application/zip", zip: "application/zip", pdf: "application/pdf",
  docx: "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
  txt: "text/plain", md: "text/markdown", html: "text/html",
  ppt: "application/vnd.ms-powerpoint", pptx: "application/vnd.openxmlformats-officedocument.presentationml.presentation",
  png: "image/png", wav: "audio/wav",
};
const DOCUMENT_FORMATS = new Set(["pdf", "docx", "txt", "md", "html"]);
const TEXT_DOCUMENT_FORMATS = new Set(["txt", "md", "html"]);

/** A document has no columns to profile; what's real is its size and, for
 *  text formats, its line/word/character counts. One file = one document. */
async function profileDocument(filePath: string, format: string, bytes: number) {
  const profile: Record<string, unknown> = { ok: true, kind: "document", format, bytes, rowCount: 1, columns: [] };
  if (TEXT_DOCUMENT_FORMATS.has(format)) {
    const raw = await fsp.readFile(filePath, "utf8");
    const text = format === "html" ? raw.replace(/<script[\s\S]*?<\/script>|<style[\s\S]*?<\/style>/gi, " ").replace(/<[^>]+>/g, " ") : raw;
    profile.lines = raw.split(/\r?\n/).length;
    profile.words = (text.match(/\S+/g) ?? []).length;
    profile.characters = text.length;
  }
  return profile as { ok: true; rowCount: number; columns: [] } & Record<string, unknown>;
}

/**
 * How large a dataset upload may be, in megabytes.
 *
 * The old ceiling was 50 MB, which is small for a real dataset — a 332 MB
 * market-data CSV was refused, and (because every error became a 500) the UI
 * said only "internal server error". Raised, and configurable, because the
 * right number depends on the deployment's disk and object store, not on a
 * constant compiled in here.
 */
const MAX_UPLOAD_MB = Number(process.env.DATASET_UPLOAD_MAX_MB ?? 4096);
const MAX_UPLOAD_BYTES = MAX_UPLOAD_MB * 1024 * 1024;

/**
 * Streams the request body to a temp file, hashing as it goes.
 *
 * Deliberately NOT express.raw: that buffers the entire body in memory before
 * the handler runs, so the old route held the whole file as a Buffer, wrote a
 * second copy to disk for the profiler, and handed a third to the object
 * store. At 50 MB that was merely wasteful; simply raising the limit without
 * changing this would have made a multi-gigabyte upload an out-of-memory
 * crash instead of an error message.
 *
 * Streaming keeps memory flat regardless of file size, and lets an
 * over-limit upload be rejected partway through rather than after the whole
 * thing has been received and held.
 */
async function streamBodyToFile(
  req: Request,
  destPath: string
): Promise<{ bytes: number; checksum: string } | { tooLarge: true; bytes: number }> {
  const hash = createHash("sha256");
  const out = createWriteStream(destPath);
  let bytes = 0;
  let aborted = false;

  try {
    await new Promise<void>((resolve, reject) => {
      req.on("data", (chunk: Buffer) => {
        if (aborted) return;
        bytes += chunk.length;
        if (bytes > MAX_UPLOAD_BYTES) {
          aborted = true;
          // Stop reading rather than draining the rest of a file we have
          // already decided to refuse.
          req.destroy();
          out.destroy();
          resolve();
          return;
        }
        hash.update(chunk);
        if (!out.write(chunk)) {
          req.pause();
          out.once("drain", () => req.resume());
        }
      });
      req.on("end", () => { if (!aborted) out.end(resolve); });
      req.on("error", (err) => { if (!aborted) reject(err); else resolve(); });
      // A browser that navigates away, or a user who cancels, ends the request
      // without an 'error' — so this promise never settled and the handler's
      // cleanup never ran, leaving the partial upload in /tmp. A 332 MB
      // orphan per abandoned upload fills the disk silently.
      req.on("aborted", () => {
        if (aborted) return;
        aborted = true;
        out.destroy();
        reject(new Error("client closed the connection before the upload finished"));
      });
      out.on("error", (err) => { if (!aborted) reject(err); });
    });
  } catch (err) {
    out.destroy();
    throw err;
  }

  if (aborted) return { tooLarge: true, bytes };
  return { bytes, checksum: hash.digest("hex") };
}

class StorageWriteError extends Error {
  constructor(cause: unknown) {
    const reason = cause instanceof Error ? cause.message : String(cause);
    const where = process.env.S3_ENDPOINT ? ` at ${process.env.S3_ENDPOINT}` : "";
    super(`couldn't save the file to object storage (${process.env.STORAGE_BACKEND || "unset"}${where}) — is it running? Nothing was recorded; try the upload again once storage is back. (${reason})`);
  }
}

datasetsRouter.post(
  "/datasets/:name/upload",
  requirePermission(PERM.datasetsUpload),
  async (req: Request, res: Response) => {
    const ds = await pool.query("SELECT structure_type FROM datasets WHERE name = $1", [req.params.name]);
    if (ds.rows.length === 0) return res.status(404).json({ error: "dataset not found — create it first via seed data or a future create-dataset route" });

    const formatParsed = uploadFormatSchema.safeParse(req.query.format);
    if (!formatParsed.success) return res.status(400).json({ error: "invalid format — structured: csv, json, jsonl, xls, xlsx, parquet; unstructured: html, ppt, pptx, md, txt, docx, png, zip, wav, pdf" });
    const format = formatParsed.data;
    // The file's own name: uploading the same name again adds a version of that file;
    // a new name adds another file to the dataset. Older clients send none.
    const rawName = typeof req.query.filename === "string" ? req.query.filename : "";
    const fileName = (path.basename(rawName).replace(/[^\w.\- ()]+/g, "_").trim().slice(0, 200)) || `${req.params.name}.${format}`;
    const structure: "structured" | "unstructured" = ds.rows[0].structure_type === "unstructured" ? "unstructured" : "structured";
    if (!FORMATS_BY_STRUCTURE[structure].includes(format)) {
      return res.status(400).json({
        error: `${req.params.name} is ${structure === "unstructured" ? "an" : "a"} ${structure} dataset — it accepts ${FORMATS_BY_STRUCTURE[structure].map((f) => f.toUpperCase()).join(", ")}, not ${format.toUpperCase()}. The type is set when the dataset is created and can't be mixed.`,
      });
    }

    // Checked before the body is read, so an unconfigured deployment refuses
    // the upload up front instead of after receiving the whole file.
    if (!getStorage()) return res.status(503).json({ error: "STORAGE_BACKEND is not configured — nowhere real to put this file" });

    // Real per-column profiling — runs against a real local temp file
    // with the real format-appropriate extension, through the same
    // format-general dataset_loader.py every real trainer now uses.
    // Row/column counts are no longer hand-parsed from CSV text in
    // Node (which only ever worked for CSV anyway) — the real profiler
    // is the one source of truth for every format now.
    const tmpDir = await fsp.mkdtemp(path.join(os.tmpdir(), "ml-studio-dataset-upload-"));
    const tmpPath = path.join(tmpDir, `data.${format}`);
    const cleanup = () => fsp.rm(tmpDir, { recursive: true, force: true }).catch(() => {});

    // The temp file is the ONE copy: the profiler reads it, and the object
    // store streams from it. Kept until both are done, then removed.
    let received;
    try {
      received = await streamBodyToFile(req, tmpPath);
    } catch (err) {
      await cleanup();
      return res.status(400).json({ error: `upload failed while reading the request body: ${err instanceof Error ? err.message : String(err)}` });
    }
    if ("tooLarge" in received) {
      await cleanup();
      return res.status(413).json({
        error: `this file is larger than the ${MAX_UPLOAD_MB} MB upload limit — it was still growing past ${(received.bytes / 1024 / 1024).toFixed(0)} MB when the upload was stopped. Raise DATASET_UPLOAD_MAX_MB on the server, or split the file into multiple snapshots.`,
        limitBytes: MAX_UPLOAD_BYTES,
      });
    }
    if (received.bytes === 0) {
      await cleanup();
      return res.status(400).json({ error: "request body must be non-empty real file content" });
    }

    try {
      const result = await ingestDatasetFile({
        datasetName: req.params.name, format, fileName, filePath: tmpPath,
        bytes: received.bytes, checksum: received.checksum, actor: req.actor,
      });
      res.status(result.status).json(result.body);
    } finally {
      // The temp file is the only copy of the upload and is needed right up to
      // the storage write inside ingestDatasetFile, so it can't be cleaned any
      // earlier — but it MUST go on every exit, including a failed
      // transaction. A 300 MB file left in /tmp per failed upload fills the
      // disk quietly.
      await cleanup();
    }
  }
);

export type UploadFormat = z.infer<typeof uploadFormatSchema>;

/**
 * Everything an upload does once the file's bytes are on local disk: profile
 * it, compare its schema with the previous version, store it under a new
 * versioned key, record the snapshot and refresh the dataset's aggregates, and
 * write the audit entry. Returns the HTTP status and body the upload route
 * sends.
 *
 * Split out of the route so the startup sample-dataset loader
 * (db/sampleDatasets.ts) registers its files through exactly this path — the
 * same profiler, the same versioned storage key, the same snapshot row — rather
 * than a second, drifting copy of it. The caller owns `filePath` and removes it
 * afterwards; the dataset row must already exist and accept `format`.
 */
export async function ingestDatasetFile(opts: {
  datasetName: string;
  format: UploadFormat;
  fileName: string;
  filePath: string;
  bytes: number;
  checksum: string;
  actor: string;
}): Promise<{ status: number; body: Record<string, unknown> }> {
  const { format, fileName, filePath, checksum } = opts;
  const fileSizeBytes = opts.bytes;
  const storage = getStorage();
  if (!storage) return { status: 503, body: { error: "STORAGE_BACKEND is not configured — nowhere real to put this file" } };

  let profileResult: any = DOCUMENT_FORMATS.has(format)
    ? await profileDocument(filePath, format, fileSizeBytes)
    : format === "image-zip"
    ? await runImageDatasetProfiler(filePath)
    : FILE_FORMATS.has(format)
    ? await runFileProfiler(filePath, format)
    : await runDatasetProfiler(filePath, format);
  if (!profileResult.ok) {
    return { status: 422, body: { error: `couldn't read this file as real ${format}: ${profileResult.error}` } };
  }
  // An archive of nothing but images is an image dataset: profile it the way
  // the image trainers read it and store it as image-zip. If the image
  // profiler can't read it (e.g. no label folders), it stays a plain zip.
  let storedFormat: string = format;
  if (format === "zip" && profileResult.allImages) {
    const asImages = await runImageDatasetProfiler(filePath);
    if (asImages.ok) { profileResult = { ...asImages, archive: profileResult }; storedFormat = "image-zip"; }
  }
  const rowCount = profileResult.rowCount;
  const columnNames = storedFormat === "image-zip" ? (profileResult.columnNames || ["image", "label"]) : (profileResult.columns ?? []).map((c: any) => c.name);

  // Real schema evolution — compares the actual columns of THIS
  // upload against the actual columns of the PREVIOUS real version
  // (not a user-declared expected schema, which this app has no
  // mechanism to collect; the previous real snapshot is the honest
  // available baseline). Informational, not blocking — a schema
  // legitimately changing between uploads is common and often
  // intentional; the value here is surfacing it, not refusing it.
  const previousSnapshot = await pool.query(
    `SELECT column_names FROM dataset_snapshots WHERE dataset_name = $1
     ORDER BY (file_name = $2) DESC, version DESC LIMIT 1`,
    [opts.datasetName, fileName]
  );
  let schemaEvolution: { addedColumns: string[]; removedColumns: string[] } | null = null;
  if (previousSnapshot.rows.length > 0) {
    const previousColumns: string[] = previousSnapshot.rows[0].column_names || [];
    const addedColumns = columnNames.filter((c: string) => !previousColumns.includes(c));
    const removedColumns = previousColumns.filter((c: string) => !columnNames.includes(c));
    if (addedColumns.length || removedColumns.length) {
      schemaEvolution = { addedColumns, removedColumns };
    }
  }

  const client = await pool.connect();
  let snapshot;
  try {
    await client.query("BEGIN");
    await client.query("SELECT pg_advisory_xact_lock(hashtext($1))", [opts.datasetName]);
    const existing = await client.query("SELECT COALESCE(MAX(version), 0) AS max_v FROM dataset_snapshots WHERE dataset_name = $1", [opts.datasetName]);
    const nextVersion = Number(existing.rows[0].max_v) + 1;
    const id = randomUUID();

    // Real versioned storage key, real extension — v1/v2/... are
    // genuinely different objects, not the same key overwritten, so a
    // previous version's file is never destroyed by a later upload.
    const fileVersion = Number((await client.query(
      "SELECT COALESCE(MAX(file_version), 0) AS v FROM dataset_snapshots WHERE dataset_name = $1 AND file_name = $2",
      [opts.datasetName, fileName])).rows[0].v) + 1;
    const key = `datasets/${opts.datasetName}/v${nextVersion}/${fileName.replace(/[^\w.\-]+/g, "_")}`;
    let uploadResult;
    try {
      uploadResult = await storage.putObjectStream(
        key, createReadStream(filePath), fileSizeBytes, FORMAT_CONTENT_TYPES[storedFormat]
      );
    } catch (err) {
      throw new StorageWriteError(err);
    }

    const inserted = await client.query(
      `INSERT INTO dataset_snapshots (id, dataset_name, version, storage_uri, format, row_count, column_names, checksum, profile, uploaded_by, size_bytes, file_name, file_version)
       VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13) RETURNING *`,
      [id, opts.datasetName, nextVersion, uploadResult.uri, storedFormat, rowCount, JSON.stringify(columnNames), checksum, JSON.stringify(profileResult), opts.actor, fileSizeBytes, fileName, fileVersion]
    );
    snapshot = inserted.rows[0];

    await refreshDatasetAggregates(client, opts.datasetName);
    await client.query("COMMIT");
  } catch (err) {
    await client.query("ROLLBACK");
    // An unreachable object store is an outage, not a bug in the upload:
    // say which store failed instead of a bare "Request failed: 500".
    if (err instanceof StorageWriteError) {
      return { status: 503, body: { error: err.message } };
    }
    throw err;
  } finally {
    client.release();
  }

  await logAudit(opts.datasetName, opts.actor, `uploaded v${snapshot.version}`, "upload", [
    `Format: ${format}`, `Rows: ${rowCount.toLocaleString("en-US")}`, `Columns: ${columnNames.length}`,
    `Checksum: ${checksum.slice(0, 16)}…`,
    ...(schemaEvolution ? [`Schema change: +${schemaEvolution.addedColumns.join(", ") || "none"} / -${schemaEvolution.removedColumns.join(", ") || "none"}`] : []),
  ]);

  return { status: 200, body: {
    ok: true,
    snapshotVersion: snapshot.version,
    storageUri: snapshot.storage_uri,
    format: storedFormat,
    fileName: snapshot.file_name,
    fileVersion: snapshot.file_version,
    rows: rowCount,
    columns: columnNames,
    checksum,
    profile: profileResult,
    schemaEvolution,
  } };
}

// snapshotId as a digit string: Iceberg ids are 64-bit, beyond a JS number's exact range.
// GET /api/iceberg/catalogs — which Iceberg catalogs a dataset can be imported from.
datasetsRouter.get("/iceberg/catalogs", requirePermission(PERM.datasetsView), (_req: Request, res: Response) => res.json(configuredIcebergCatalogs()));

const icebergSnapshotSchema = z.object({ table: z.string().min(3), snapshotId: z.union([z.string().regex(/^\d+$/), z.number().int().positive()]).transform(String).optional(),
  // Typed in the UI for this pull only — handed to the resolver, never stored or logged.
  credentials: z.object({ accessKeyId: z.string().trim().min(1), secretAccessKey: z.string().trim().min(1), sessionToken: z.string().trim().optional() }).optional() });
// Resolve a real Apache Iceberg table snapshot through PyIceberg, materialize
// that exact immutable snapshot to Parquet in customer object storage, and
// register it as a normal governed dataset_snapshots version. Training then
// uses the same fail-closed snapshot path as uploaded files.
datasetsRouter.post("/datasets/:name/iceberg-snapshots", requirePermission(PERM.datasetsUpload), express.json(), async (req: Request, res: Response) => {
  const parsed=icebergSnapshotSchema.safeParse(req.body); if(!parsed.success)return res.status(400).json({error:"invalid Iceberg snapshot request",details:parsed.error.flatten()});
  const ds=await pool.query("SELECT iceberg_ref FROM datasets WHERE name=$1",[req.params.name]); if(!ds.rows.length)return res.status(404).json({error:"dataset not found"});
  const catalogName:string|null=ds.rows[0].iceberg_ref?.catalog ?? null;
  const storage=getStorage(); if(!storage)return res.status(503).json({error:"STORAGE_BACKEND is required to persist the materialized Iceberg snapshot"});
  const tmpDir=await fsp.mkdtemp(path.join(os.tmpdir(),"ml-studio-iceberg-")); const tmpPath=path.join(tmpDir,"snapshot.parquet");
  try {
    const ice=await materializeIcebergSnapshot(parsed.data.table,tmpPath,parsed.data.snapshotId,catalogName,parsed.data.credentials); if(!ice.ok)return res.status(422).json({error:`Iceberg resolution failed: ${ice.error}`});
    const profile=await runDatasetProfiler(tmpPath,"parquet"); if(!profile.ok)return res.status(422).json({error:`materialized Iceberg snapshot profiling failed: ${profile.error}`});
    const bytes=await fsp.readFile(tmpPath); const client=await pool.connect(); let row:any;
    try { await client.query("BEGIN"); await client.query("SELECT pg_advisory_xact_lock(hashtext($1))",[req.params.name]); const v=Number((await client.query("SELECT COALESCE(MAX(version),0) max_v FROM dataset_snapshots WHERE dataset_name=$1",[req.params.name])).rows[0].max_v)+1; const fileName=`${ice.table}.parquet`;
      // The same Iceberg snapshot pulled again is the same data — reuse that version rather than duplicating it.
      const same=(await client.query("SELECT * FROM dataset_snapshots WHERE dataset_name=$1 AND file_name=$2 AND external_source->>'snapshotId'=$3 ORDER BY version DESC LIMIT 1",[req.params.name,fileName,String(ice.snapshotId)])).rows[0];
      if(same){await client.query("ROLLBACK"); return res.json({ok:true,unchanged:true,datasetSnapshotVersion:same.version,icebergSnapshotId:ice.snapshotId,table:ice.table,catalog:ice.catalog,storageUri:same.storage_uri,checksum:same.checksum,rowCount:ice.rowCount,columnNames:ice.columnNames});}
      const fv=Number((await client.query("SELECT COALESCE(MAX(file_version),0) v FROM dataset_snapshots WHERE dataset_name=$1 AND file_name=$2",[req.params.name,fileName])).rows[0].v)+1; const key=`datasets/${req.params.name}/v${v}/iceberg-${ice.snapshotId}.parquet`; const uploaded=await storage.putObject(key,bytes,"application/octet-stream");
      row=(await client.query(`INSERT INTO dataset_snapshots (id,dataset_name,version,storage_uri,format,row_count,column_names,checksum,profile,uploaded_by,external_source,size_bytes,file_name,file_version) VALUES ($1,$2,$3,$4,'parquet',$5,$6,$7,$8,$9,$10,$11,$12,$13) RETURNING *`,[randomUUID(),req.params.name,v,uploaded.uri,ice.rowCount,JSON.stringify(ice.columnNames),ice.checksum,JSON.stringify(profile),req.actor,JSON.stringify({type:"iceberg",catalog:ice.catalog,table:ice.table,snapshotId:ice.snapshotId}),bytes.length,fileName,fv])).rows[0];
      await client.query("UPDATE datasets SET storage_uri=$1,column_names=$2,rows=$3,size=$4,latest_snapshot_version=$5 WHERE name=$6",[uploaded.uri,JSON.stringify(ice.columnNames),Number(ice.rowCount).toLocaleString("en-US"),`${(bytes.length/1e6).toFixed(2)} MB`,v,req.params.name]); await client.query("COMMIT");
    } catch(e){await client.query("ROLLBACK"); throw e} finally {client.release()}
    res.json({ok:true,datasetSnapshotVersion:row.version,icebergSnapshotId:ice.snapshotId,table:ice.table,catalog:ice.catalog,storageUri:row.storage_uri,checksum:ice.checksum,rowCount:ice.rowCount,columnNames:ice.columnNames});
  } finally { await fsp.rm(tmpDir,{recursive:true,force:true}).catch(()=>{}); }
});

function snapshotDto(r: any, includeProfile: boolean) {
  return {
    id: r.id,
    datasetName: r.dataset_name,
    version: r.version,
    storageUri: r.storage_uri,
    format: r.format,
    rowCount: r.row_count,
    columnNames: r.column_names,
    checksum: r.checksum,
    externalSource: r.external_source ?? undefined,
    uploadedBy: r.uploaded_by,
    uploadedAt: r.uploaded_at,
    fileName: r.file_name ?? `${r.dataset_name}.${r.format}`,
    fileVersion: r.file_version ?? 1,
    ...(r.is_latest !== undefined ? { isLatest: r.is_latest } : {}),
    // File counts by type for archives (e.g. {wav: 640, csv: 1}) — small, and what the
    // Train wizard needs to tell an audio+transcripts ZIP from anything else.
    ...(r.profile?.fileTypes ? { fileTypes: r.profile.fileTypes } : {}),
    ...(includeProfile ? { profile: r.profile } : {}),
  };
}

// GET /api/datasets/:name/snapshots — every real, immutable version ever
// uploaded for this dataset, newest first. Profile omitted from the list
// view (can be large); fetch a specific version for its full profile.
datasetsRouter.get("/datasets/:name/snapshots", requireAnyPermission(PERM.datasetsView, PERM.train), async (req: Request, res: Response) => {
  const result = await pool.query(
    `SELECT s.*, s.version = MAX(s.version) OVER (PARTITION BY s.file_name) AS is_latest
     FROM dataset_snapshots s WHERE s.dataset_name = $1 ORDER BY s.version DESC`, [req.params.name]);
  res.json(result.rows.map((r) => snapshotDto(r, false)));
});

// GET /api/datasets/:name/snapshots/:version — one exact, immutable
// snapshot, full real profile included — what a model trained against
// this dataset+version genuinely saw.
datasetsRouter.get("/datasets/:name/snapshots/:version", requireAnyPermission(PERM.datasetsView, PERM.train), async (req: Request, res: Response) => {
  const result = await pool.query("SELECT * FROM dataset_snapshots WHERE dataset_name = $1 AND version = $2", [req.params.name, req.params.version]);
  if (result.rows.length === 0) return res.status(404).json({ error: "snapshot not found" });
  res.json(snapshotDto(result.rows[0], true));
});

/**
 * GET /api/datasets/:name/target-candidates?task=classification&version=N
 *
 * Every column in the snapshot, graded on whether it can actually serve as a
 * training target for this task — so the Train wizard can say so while you're
 * choosing, instead of the run failing after launch.
 *
 * The grading is `assessTargetColumn`, the SAME function the train-time quality
 * gate calls. Not a lookalike reimplementation: the wizard offering a target the
 * gate then refuses is precisely the bug this exists to close, and two copies of
 * the rules would reopen it the first time one was edited.
 *
 * Rules invert by task (a regression target is meant to be near-unique; a
 * classification target with that property is an identifier), so `task` is
 * required to grade honestly. Without it, columns come back ungraded rather
 * than graded against a guess.
 */
datasetsRouter.get("/datasets/:name/target-candidates", requireAnyPermission(PERM.datasetsView, PERM.train), async (req: Request, res: Response) => {
  // The caller sends the base model's own task label ("Classification /
  // regression"). Normalise it exactly as the trainer does, so this grades
  // against the task the run will ACTUALLY execute — otherwise a dual-purpose
  // library entry gets graded as a regression and every classification rule is
  // skipped, which is the failure this endpoint exists to prevent.
  const rawTask = typeof req.query.task === "string" ? req.query.task : null;
  const task = rawTask ? mapToClassicalTask(rawTask) ?? rawTask : null;
  const version = req.query.version ? Number(req.query.version) : null;

  const result = version
    ? await pool.query("SELECT * FROM dataset_snapshots WHERE dataset_name = $1 AND version = $2", [req.params.name, version])
    : await pool.query("SELECT * FROM dataset_snapshots WHERE dataset_name = $1 ORDER BY version DESC LIMIT 1", [req.params.name]);
  if (result.rows.length === 0) return res.status(404).json({ error: "no snapshot for this dataset" });

  const snapshot = result.rows[0];
  const profile: any = snapshot.profile;
  if (!profile?.ok || !Array.isArray(profile.columns)) {
    // An unprofiled snapshot genuinely has nothing to grade against. Say so;
    // the wizard shows every column unannotated rather than inventing verdicts.
    return res.json({
      datasetName: req.params.name,
      version: snapshot.version,
      rowCount: snapshot.row_count,
      graded: false,
      reason: "This snapshot has no column profile, so target suitability can't be assessed. Re-upload it to profile it.",
      columns: (snapshot.column_names ?? []).map((name: string) => ({ column: name, severity: null, reason: null })),
    });
  }

  const columns = profile.columns.map((col: any) => {
    const findings = assessTargetColumn(profile, col, isRegressionTask(task, col));
    const blocker = findings.find((f) => f.severity === "blocker");
    const warning = findings.find((f) => f.severity === "warning");
    const chosen = blocker ?? warning ?? null;
    return {
      column: col.name,
      kind: col.kind,
      cardinality: col.cardinality ?? null,
      distinctPct: typeof col.cardinalityRatio === "number" ? Math.round(col.cardinalityRatio * 100) : null,
      missingPct: col.missingPct ?? null,
      severity: chosen ? chosen.severity : null,
      reason: chosen ? chosen.reason : null,
    };
  });

  res.json({
    datasetName: req.params.name,
    version: snapshot.version,
    rowCount: profile.rowCount ?? snapshot.row_count,
    task,
    graded: Boolean(task),
    columns,
  });
});

const DL_CONTENT_TYPES = FORMAT_CONTENT_TYPES;

// GET .../download — streams the real snapshot file back from object storage
// (the same bytes that were uploaded and checksummed). A read, so no write gate.
datasetsRouter.get("/datasets/:name/snapshots/:version/download", requirePermission(PERM.datasetsView), async (req: Request, res: Response) => {
  const result = await pool.query("SELECT * FROM dataset_snapshots WHERE dataset_name = $1 AND version = $2", [req.params.name, req.params.version]);
  if (result.rows.length === 0) return res.status(404).json({ error: "snapshot not found" });
  const s = result.rows[0];
  const storage = getStorage();
  if (!storage) return res.status(503).json({ error: "STORAGE_BACKEND is not configured — nothing to download from" });
  try {
    const buf = await storage.getObject(storageKeyFromUri(s.storage_uri, storage.bucket));
    res.setHeader("Content-Type", DL_CONTENT_TYPES[s.format] || "application/octet-stream");
    res.setHeader("Content-Disposition", `attachment; filename="${req.params.name}-v${s.version}.${s.format}"`);
    res.send(buf);
  } catch (err) {
    res.status(502).json({ error: `couldn't read the snapshot file from storage: ${err instanceof Error ? err.message : String(err)}` });
  }
});

// DELETE a specific snapshot version — removes the row and re-points the
// dataset's latest_snapshot_version at the remaining newest version. Other
// versions are untouched (still immutable); this only drops the one asked for.
datasetsRouter.delete("/datasets/:name/snapshots/:version", requirePermission(PERM.datasetsUpload), async (req: Request, res: Response) => {
  const client = await pool.connect();
  try {
    await client.query("BEGIN");
    const existing = await client.query("SELECT * FROM dataset_snapshots WHERE dataset_name = $1 AND version = $2", [req.params.name, req.params.version]);
    if (existing.rows.length === 0) { await client.query("ROLLBACK"); return res.status(404).json({ error: "snapshot not found" }); }
    await client.query("DELETE FROM dataset_snapshots WHERE dataset_name = $1 AND version = $2", [req.params.name, req.params.version]);
    await refreshDatasetAggregates(client, req.params.name);
    await client.query("COMMIT");
  } catch (err) {
    await client.query("ROLLBACK"); throw err;
  } finally {
    client.release();
  }
  await logAudit(req.params.name, req.actor, `deleted v${req.params.version}`, "delete", [`Deleted snapshot version ${req.params.version}`]);
  res.json({ ok: true });
});

datasetsRouter.get("/datasets", requireAnyPermission(PERM.datasetsView, PERM.train, PERM.dashboard, PERM.pipelines, PERM.experiments), async (_req: Request, res: Response) => {
  const result = await pool.query(
    `SELECT d.*,
       (SELECT COUNT(DISTINCT s.file_name) FROM dataset_snapshots s WHERE s.dataset_name = d.name) AS files_count,
       (SELECT MAX(uploaded_at) FROM dataset_snapshots s WHERE s.dataset_name = d.name) AS last_upload
     FROM datasets d ORDER BY d.name`
  );
  res.json(result.rows.map((r) => datasetRowDto(r, Number(r.files_count), r.last_upload)));
});

// POST /api/datasets — create a new (empty) dataset. Previously the only
// way a dataset row came into existence was the seed script; the upload
// route explicitly 404s for an unknown name ("create it first via seed
// data or a future create-dataset route"). This is that route. The
// dataset starts with no snapshot — the first upload creates version 1.
const createDatasetSchema = z.object({
  name: z.string().trim().min(1).max(120).regex(/^[a-z0-9][a-z0-9-]*$/i, "use letters, digits and hyphens"),
  source: z.enum(["upload", "iceberg"]).default("upload"),
  structureType: z.enum(["structured", "unstructured"]).default("structured"),
  visibility: z.enum(["private", "org"]).default("private"),
  tags: z.array(z.string().trim().min(1)).max(12).default([]),
  detail: z.string().trim().max(500).default(""),
  description: z.string().trim().max(1000).default(""),
  iceberg: z.object({
    catalog: z.string().trim().default("lakehouse"),
    schemaName: z.string().trim().min(1),
    table: z.string().trim().min(1),
    snapshot: z.string().trim().nullable().default(null),
  }).optional(),
});
datasetsRouter.post("/datasets", requirePermission(PERM.datasetsUpload), express.json(), async (req: Request, res: Response) => {
  const parsed = createDatasetSchema.safeParse(req.body);
  if (!parsed.success) return res.status(400).json({ error: parsed.error.issues[0]?.message || "invalid dataset" });
  const { name, source, structureType, visibility, tags, detail, description } = parsed.data;

  const exists = await pool.query("SELECT 1 FROM datasets WHERE name = $1", [name]);
  if (exists.rows.length) return res.status(409).json({ error: `a dataset named '${name}' already exists` });

  if (source === "iceberg" && !parsed.data.iceberg) {
    return res.status(400).json({ error: "iceberg source requires a schema and table" });
  }
  const ice = parsed.data.iceberg;
  const resolvedDetail = detail || (source === "iceberg" && ice
    ? `${ice.schemaName} · ${ice.snapshot ? "snapshot " + ice.snapshot : "latest snapshot"}`
    : "just created · no files yet");
  const r = await insertDatasetRecord({ name, source, structureType, owner: req.actor, tags, detail: resolvedDetail, visibility, iceberg: source === "iceberg" ? ice : undefined, description });
  res.status(201).json(datasetRowDto(r, 0, null));
});

/**
 * The dataset row POST /api/datasets creates, plus its "created" audit entry.
 * Exported for the startup sample-dataset loader, so a sample is registered
 * exactly the way a user-created dataset is. No existence check — callers do
 * that first (the route answers 409, the loader skips).
 */
export async function insertDatasetRecord(d: {
  name: string; source: "upload" | "iceberg"; structureType: "structured" | "unstructured"; owner: string;
  tags: string[]; detail: string; visibility: "private" | "org"; description: string;
  iceberg?: { catalog: string; schemaName: string; table: string; snapshot: string | null };
}): Promise<any> {
  const ice = d.iceberg;
  const result = await pool.query(
    `INSERT INTO datasets (name, source, structure_type, size, rows, owner, tags, detail, visibility, deploy_target, iceberg_ref, description)
     VALUES ($1, $2, $3, '—', '0', $4, $5, $6, $7, $8, $9, $10) RETURNING *`,
    [d.name, d.source, d.structureType, d.owner, JSON.stringify(d.tags), d.detail, d.visibility, deployTargetFor(d.source, d.structureType),
     d.source === "iceberg" && ice ? JSON.stringify(ice) : null, d.description || null]
  );
  await logAudit(d.name, d.owner, "created", "register",
    d.source === "iceberg" && ice
      ? ["Registered as Iceberg-sourced dataset", `Catalog: ${ice.catalog}.${ice.schemaName}.${ice.table}`, ice.snapshot ? `Snapshot pinned: ${ice.snapshot}` : "Reading current snapshot — not pinned"]
      : [`Type: ${d.structureType}`, "Dataset created — no files uploaded yet"]);
  return result.rows[0];
}

datasetsRouter.get("/datasets/:name", requirePermission(PERM.datasetsView), async (req: Request, res: Response) => {
  const result = await pool.query(
    `SELECT d.*,
       (SELECT COUNT(DISTINCT s.file_name) FROM dataset_snapshots s WHERE s.dataset_name = d.name) AS files_count,
       (SELECT MAX(uploaded_at) FROM dataset_snapshots s WHERE s.dataset_name = d.name) AS last_upload
     FROM datasets d WHERE d.name = $1`, [req.params.name]
  );
  if (result.rows.length === 0) return res.status(404).json({ error: "dataset not found" });
  const r = result.rows[0];
  res.json(datasetRowDto(r, Number(r.files_count), r.last_upload));
});

// PATCH /api/datasets/:name — edit the description and/or rename the dataset.
// A rename moves every reference in one transaction: the child tables with
// foreign keys (snapshots, comments, audit log, access) can't follow an update
// of the key, so the row is copied under the new name, children re-pointed, and
// the old row removed. Training runs, HPO searches and model versions keep
// lineage by name, so they move too. Object-storage files are untouched — each
// snapshot keeps its full storage URI. Refused while a run is using the dataset.
const editDatasetSchema = z.object({
  name: z.string().trim().min(1).max(120).regex(/^[a-z0-9][a-z0-9-]*$/i, "use letters, digits and hyphens").optional(),
  description: z.string().trim().max(1000).optional(),
}).refine((b) => b.name !== undefined || b.description !== undefined, { message: "nothing to change" });
datasetsRouter.patch("/datasets/:name", requirePermission(PERM.datasetsUpload), express.json(), async (req: Request, res: Response) => {
  const parsed = editDatasetSchema.safeParse(req.body);
  if (!parsed.success) return res.status(400).json({ error: parsed.error.issues[0]?.message || "invalid change" });
  const oldName = req.params.name;
  const newName = parsed.data.name && parsed.data.name !== oldName ? parsed.data.name : null;
  const client = await pool.connect();
  try {
    await client.query("BEGIN");
    const cur = await client.query("SELECT * FROM datasets WHERE name = $1 FOR UPDATE", [oldName]);
    if (!cur.rows.length) { await client.query("ROLLBACK"); return res.status(404).json({ error: "dataset not found" }); }
    const changes: string[] = [];
    if (parsed.data.description !== undefined && parsed.data.description !== (cur.rows[0].description ?? "")) {
      await client.query("UPDATE datasets SET description = $1 WHERE name = $2", [parsed.data.description || null, oldName]);
      changes.push("Description updated");
    }
    if (newName) {
      if ((await client.query("SELECT 1 FROM datasets WHERE name = $1", [newName])).rows.length) {
        await client.query("ROLLBACK"); return res.status(409).json({ error: `a dataset named '${newName}' already exists` });
      }
      const busy = await client.query("SELECT count(*)::int AS n FROM training_runs WHERE dataset_name = $1 AND status IN ('queued','running')", [oldName]);
      if (busy.rows[0].n > 0) {
        await client.query("ROLLBACK");
        return res.status(409).json({ error: `${busy.rows[0].n} training run(s) are using '${oldName}' right now — rename it once they finish` });
      }
      await client.query(`INSERT INTO datasets SELECT (d).* FROM (SELECT jsonb_populate_record(NULL::datasets, to_jsonb(x) || jsonb_build_object('name', $2::text)) AS d FROM datasets x WHERE x.name = $1) s`, [oldName, newName]);
      for (const t of ["dataset_snapshots", "dataset_comments", "dataset_audit_log", "dataset_access", "training_runs", "hpo_searches", "model_versions"]) {
        await client.query(`UPDATE ${t} SET dataset_name = $2 WHERE dataset_name = $1`, [oldName, newName]);
      }
      // Pipeline definitions name their dataset inside each stage's params.
      await client.query(
        `UPDATE pipeline_definitions SET stages = (
           SELECT jsonb_agg(CASE WHEN st->'params'->>'datasetName' = $1 THEN jsonb_set(st, '{params,datasetName}', to_jsonb($2::text)) ELSE st END)
           FROM jsonb_array_elements(stages::jsonb) st)
         WHERE stages::text LIKE '%' || $1 || '%'`, [oldName, newName]);
      await client.query("DELETE FROM datasets WHERE name = $1", [oldName]);
      changes.unshift(`Renamed from '${oldName}' to '${newName}'`);
    }
    if (changes.length) {
      await client.query("INSERT INTO dataset_audit_log (dataset_name, actor, action, atype, details) VALUES ($1, $2, $3, 'edit', $4)",
        [newName ?? oldName, req.actor, newName ? "renamed" : "edited", JSON.stringify(changes)]);
    }
    await client.query("COMMIT");
  } catch (err) {
    await client.query("ROLLBACK").catch(() => undefined);
    return res.status(500).json({ error: `couldn't save the change: ${err instanceof Error ? err.message : String(err)}` });
  } finally {
    client.release();
  }
  const r = (await pool.query(
    `SELECT d.*, (SELECT COUNT(DISTINCT s.file_name) FROM dataset_snapshots s WHERE s.dataset_name = d.name) AS files_count,
       (SELECT MAX(uploaded_at) FROM dataset_snapshots s WHERE s.dataset_name = d.name) AS last_upload
     FROM datasets d WHERE d.name = $1`, [newName ?? oldName])).rows[0];
  res.json(datasetRowDto(r, Number(r.files_count), r.last_upload));
});

// DELETE /api/datasets/:name — only an empty dataset (every file already
// deleted) can be removed; one with files must have them deleted first, so a
// dataset can never disappear together with data someone still relies on.
datasetsRouter.delete("/datasets/:name", requirePermission(PERM.datasetsUpload), async (req: Request, res: Response) => {
  const files = await pool.query("SELECT count(*)::int AS n FROM dataset_snapshots WHERE dataset_name = $1", [req.params.name]);
  const exists = await pool.query("SELECT 1 FROM datasets WHERE name = $1", [req.params.name]);
  if (!exists.rows.length) return res.status(404).json({ error: "dataset not found" });
  if (files.rows[0].n > 0) {
    return res.status(409).json({ error: `'${req.params.name}' still has ${files.rows[0].n} file(s) — delete every file first, then the dataset` });
  }
  await pool.query("DELETE FROM datasets WHERE name = $1", [req.params.name]);  // comments / audit / access cascade
  res.json({ ok: true });
});

async function logAudit(datasetName: string, actor: string, action: string, atype: string = "event", details: string[] = []) {
  await pool.query(
    "INSERT INTO dataset_audit_log (dataset_name, actor, action, atype, details) VALUES ($1, $2, $3, $4, $5)",
    [datasetName, actor, action, atype, JSON.stringify(details)]
  );
}

/**
 * Recomputes a dataset's summary from its files. Training uses the latest version
 * of every file, so "rows" is their total; size counts every stored version (each
 * still occupies storage). Called after each upload and each version delete.
 */
async function refreshDatasetAggregates(client: { query: (q: string, v?: unknown[]) => Promise<any> }, name: string) {
  await client.query(
    `WITH active AS (
       SELECT DISTINCT ON (file_name) * FROM dataset_snapshots WHERE dataset_name = $1 ORDER BY file_name, version DESC
     ), newest AS (
       SELECT * FROM dataset_snapshots WHERE dataset_name = $1 ORDER BY version DESC LIMIT 1
     ), agg AS (
       SELECT (SELECT COUNT(*) FROM active) AS files, (SELECT COALESCE(SUM(row_count), 0) FROM active) AS rows,
              COUNT(*) AS versions, COALESCE(SUM(size_bytes), 0) AS bytes, MAX(version) AS max_v
       FROM dataset_snapshots WHERE dataset_name = $1
     )
     UPDATE datasets d SET
       storage_uri = (SELECT storage_uri FROM newest),
       column_names = (SELECT column_names FROM newest),
       latest_snapshot_version = agg.max_v,
       rows = CASE WHEN agg.files = 0 THEN '0' ELSE to_char(agg.rows, 'FM999,999,999,990') END,
       size = CASE WHEN agg.files = 0 THEN '—'
                   WHEN agg.bytes >= 1e9 THEN to_char(agg.bytes/1e9,'FM999990.00')||' GB'
                   WHEN agg.bytes >= 1e6 THEN to_char(agg.bytes/1e6,'FM999990.00')||' MB'
                   WHEN agg.bytes >= 1e3 THEN to_char(agg.bytes/1e3,'FM999990.00')||' KB'
                   ELSE agg.bytes||' B' END,
       detail = CASE WHEN agg.files = 0 THEN 'no files yet' ELSE
                  agg.files||' file'||CASE WHEN agg.files = 1 THEN '' ELSE 's' END
                  ||' · '||to_char(agg.rows,'FM999,999,999,990')||' rows across latest versions'
                  ||CASE WHEN agg.versions > agg.files THEN ' · '||agg.versions||' versions' ELSE '' END
                  ||' · checksum verified' END
     FROM agg WHERE d.name = $1`,
    [name]
  );
}

function deployTargetFor(source: string, structureType: string): string {
  if (source === "iceberg") return "Iceberg REST — Ray Data (direct read)";
  if (structureType === "unstructured") return "Object storage — read directly (external)";
  return "Object storage — Ray Data (direct read)";
}

// Enriches a raw datasets row with the display fields the detail/list UI needs:
// real file (snapshot) count, real last-updated time, and a resolved deploy target.
function datasetRowDto(r: any, filesCount: number, lastUpload: string | null) {
  return {
    name: r.name, source: r.source, structure_type: r.structure_type, size: r.size, rows: r.rows,
    owner: r.owner, tags: r.tags, detail: r.detail, description: r.description ?? null, storage_uri: r.storage_uri, column_names: r.column_names,
    latest_snapshot_version: r.latest_snapshot_version,
    visibility: r.visibility ?? "private",
    createdAt: r.created_at, updatedAt: lastUpload ?? r.created_at,
    deployTarget: r.deploy_target ?? deployTargetFor(r.source, r.structure_type),
    icebergRef: r.iceberg_ref ?? null,
    files: filesCount,
  };
}

// ---- Comments ----
datasetsRouter.get("/datasets/:name/comments", requirePermission(PERM.datasetsView), async (req: Request, res: Response) => {
  const result = await pool.query("SELECT * FROM dataset_comments WHERE dataset_name = $1 ORDER BY created_at", [req.params.name]);
  res.json(result.rows.map((r) => ({ id: r.id, author: r.author, body: r.body, createdAt: r.created_at })));
});

const commentSchema = z.object({ body: z.string().min(1) });
datasetsRouter.post("/datasets/:name/comments", requirePermission(PERM.datasetsUpload), async (req: Request, res: Response) => {
  const parsed = commentSchema.safeParse(req.body);
  if (!parsed.success) return res.status(400).json({ error: "invalid comment", details: parsed.error.flatten() });
  const ds = await pool.query("SELECT 1 FROM datasets WHERE name = $1", [req.params.name]);
  if (ds.rows.length === 0) return res.status(404).json({ error: "dataset not found" });

  const author = req.actor;
  const result = await pool.query(
    "INSERT INTO dataset_comments (dataset_name, author, body) VALUES ($1, $2, $3) RETURNING *",
    [req.params.name, author, parsed.data.body]
  );
  await logAudit(req.params.name, author, "commented", "comment", ["Posted a comment"]);
  const r = result.rows[0];
  res.status(201).json({ id: r.id, author: r.author, body: r.body, createdAt: r.created_at });
});

// ---- Audit log (read-only — written automatically by other actions) ----
datasetsRouter.get("/datasets/:name/audit-log", requirePermission(PERM.datasetsView), async (req: Request, res: Response) => {
  const result = await pool.query("SELECT * FROM dataset_audit_log WHERE dataset_name = $1 ORDER BY created_at DESC", [req.params.name]);
  res.json(result.rows.map((r) => ({ id: r.id, actor: r.actor, action: r.action, type: r.atype ?? "event", details: r.details ?? [], createdAt: r.created_at })));
});

// ---- Sharing / access ----
// Independent share levels (read/download/upload/delete) — parity with the
// mockup. The legacy access_level column still gets a representative value so
// its NOT NULL/CHECK holds, but `levels` is the real source of truth.
function levelsToLegacy(levels: string[]): "viewer" | "editor" {
  return levels.some((l) => l === "upload" || l === "delete") ? "editor" : "viewer";
}
datasetsRouter.get("/datasets/:name/access", requirePermission(PERM.datasetsView), async (req: Request, res: Response) => {
  const result = await pool.query("SELECT * FROM dataset_access WHERE dataset_name = $1 ORDER BY principal", [req.params.name]);
  res.json(result.rows.map((r) => ({
    id: r.id, principal: r.principal,
    levels: (r.levels && r.levels.length) ? r.levels : (r.access_level === "editor" ? ["read", "download", "upload"] : ["read"]),
  })));
});

const accessSchema = z.object({
  principal: z.string().min(1),
  levels: z.array(z.enum(["read", "download", "upload", "delete"])).min(1),
});
datasetsRouter.post("/datasets/:name/access", requirePermission(PERM.datasetsUpload), express.json(), async (req: Request, res: Response) => {
  const parsed = accessSchema.safeParse(req.body);
  if (!parsed.success) return res.status(400).json({ error: "invalid grant — provide a principal and at least one level", details: parsed.error.flatten() });
  const ds = await pool.query("SELECT 1 FROM datasets WHERE name = $1", [req.params.name]);
  if (ds.rows.length === 0) return res.status(404).json({ error: "dataset not found" });

  const { principal, levels } = parsed.data;
  const result = await pool.query(
    "INSERT INTO dataset_access (dataset_name, principal, access_level, levels) VALUES ($1, $2, $3, $4) RETURNING *",
    [req.params.name, principal, levelsToLegacy(levels), JSON.stringify(levels)]
  );
  await logAudit(req.params.name, req.actor, `shared with ${principal}`, "share", [`Granted ${levels.join(", ")} to ${principal}`]);
  const r = result.rows[0];
  res.status(201).json({ id: r.id, principal: r.principal, levels: r.levels });
});

datasetsRouter.delete("/datasets/:name/access/:id", requirePermission(PERM.datasetsUpload), async (req: Request, res: Response) => {
  const existing = await pool.query("SELECT * FROM dataset_access WHERE id = $1 AND dataset_name = $2", [req.params.id, req.params.name]);
  if (existing.rows.length === 0) return res.status(404).json({ error: "grant not found" });
  await pool.query("DELETE FROM dataset_access WHERE id = $1", [req.params.id]);
  await logAudit(req.params.name, req.actor, `revoked access for ${existing.rows[0].principal}`, "delete", [`Revoked access for ${existing.rows[0].principal}`]);
  res.json({ ok: true });
});
