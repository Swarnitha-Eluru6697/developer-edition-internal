import { createHash } from "crypto";
import { promises as fsp } from "fs";
import os from "os";
import path from "path";
import { pool } from "./connection";
import { envFlag } from "./referenceData";
import { getStorage } from "../storage";
import { ingestDatasetFile, insertDatasetRecord } from "../routes/datasets";

/**
 * Three small sample datasets a fresh deployment starts with, so the Train
 * wizard has something real to train on before anyone has uploaded data:
 *
 *   sample-fraud-transactions  binary classification, target is_fraudulent
 *   sample-house-prices        regression, target price
 *   sample-daily-sales         time series, date + sales, two years daily
 *
 * Generated here, deterministically (fixed-seed PRNG), rather than checked in
 * as CSV: same bytes — and so the same checksum — on every deployment, nothing
 * extra to copy into the image, and the signal each one carries is readable
 * below instead of hidden in a file. Each has real structure for a model to
 * find (and a little noise so it can't be memorised), and is a few thousand
 * rows at most so it profiles and trains in seconds on the CPU image.
 *
 * Registered through ingestDatasetFile() — the code the upload route runs — so
 * each gets a real profiled, checksummed, versioned snapshot in the configured
 * object store, indistinguishable from a user upload.
 */

/** Owner and uploaded_by recorded on sample rows — how the loader tells its own
 *  datasets from a user's dataset that happens to share the name. */
export const SAMPLE_DATASET_OWNER = "system";

/** mulberry32 — tiny, fast, and identical on every platform. */
export function seededRandom(seed: number): () => number {
  let a = seed >>> 0;
  return () => {
    a = (a + 0x6d2b79f5) >>> 0;
    let t = a;
    t = Math.imul(t ^ (t >>> 15), t | 1);
    t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

function gaussian(rand: () => number): number {
  // Box–Muller; 1 - rand() keeps log() away from 0.
  return Math.sqrt(-2 * Math.log(1 - rand())) * Math.cos(2 * Math.PI * rand());
}
const pick = <T>(rand: () => number, items: readonly T[]): T => items[Math.floor(rand() * items.length)];
const clamp = (v: number, lo: number, hi: number) => Math.min(hi, Math.max(lo, v));
const toCsv = (header: string[], rows: (string | number)[][]) => [header.join(","), ...rows.map((r) => r.join(","))].join("\n") + "\n";

/**
 * Card transactions, ~7% fraudulent. Fraud is more likely for large amounts,
 * card-not-present and international payments, night-time hours, purchases
 * far from home, bursts of activity, new accounts and some merchant types — a
 * logistic model of those, so no single column gives the label away.
 */
export function fraudTransactionsCsv(rows = 4000): string {
  const rand = seededRandom(20260101);
  const categories = ["groceries", "restaurants", "fuel", "electronics", "travel", "online_retail", "entertainment"] as const;
  const categoryRisk: Record<string, number> = { groceries: -0.6, restaurants: -0.4, fuel: -0.2, electronics: 0.9, travel: 0.6, online_retail: 0.7, entertainment: 0 };
  const out: (string | number)[][] = [];
  for (let i = 0; i < rows; i++) {
    const category = pick(rand, categories);
    const amount = Math.exp(3.6 + 1.1 * gaussian(rand) + (category === "travel" || category === "electronics" ? 0.8 : 0));
    const hour = Math.floor(rand() * 24);
    const cardPresent = rand() < (category === "online_retail" ? 0.05 : 0.75) ? 1 : 0;
    const international = rand() < 0.08 ? 1 : 0;
    const distanceKm = Math.exp(1.5 + 1.3 * gaussian(rand));
    const txns24h = Math.floor(clamp(Math.exp(0.8 + 0.7 * gaussian(rand)), 0, 40));
    const accountAgeDays = Math.floor(clamp(Math.exp(6 + 1.2 * gaussian(rand)), 1, 7300));

    const logit = -6.5
      + 0.9 * Math.log(amount / 50)
      + 1.9 * (1 - cardPresent)
      + 1.6 * international
      + (hour <= 5 ? 1.8 : 0)
      + 0.6 * Math.log1p(distanceKm / 10)
      + 0.22 * txns24h
      + (accountAgeDays < 60 ? 1.6 : 0)
      + 1.4 * categoryRisk[category];
    const isFraud = rand() < 1 / (1 + Math.exp(-logit)) ? 1 : 0;
    out.push([amount.toFixed(2), hour, category, cardPresent, international, distanceKm.toFixed(1), txns24h, accountAgeDays, isFraud]);
  }
  return toCsv(["amount_usd", "transaction_hour", "merchant_category", "card_present", "is_international", "distance_from_home_km",
                "txn_count_24h", "account_age_days", "is_fraudulent"], out);
}

/**
 * Home sales. Price is driven mostly by living area times a neighbourhood
 * multiplier, then bathrooms, lot size, age, condition, garage, pool and
 * distance to the centre, with ~8% multiplicative noise.
 */
export function housePricesCsv(rows = 2500): string {
  const rand = seededRandom(20260202);
  const neighborhoods = ["downtown", "riverside", "old_town", "suburb", "hillside"] as const;
  const multiplier: Record<string, number> = { downtown: 1.45, riverside: 1.3, old_town: 1.15, suburb: 1.0, hillside: 0.9 };
  const out: (string | number)[][] = [];
  for (let i = 0; i < rows; i++) {
    const neighborhood = pick(rand, neighborhoods);
    const bedrooms = Math.floor(clamp(3 + 1.1 * gaussian(rand), 1, 7) + 0.5);
    const sqft = Math.round(clamp(450 + bedrooms * 420 + 300 * gaussian(rand), 400, 6500));
    const bathrooms = clamp(Math.round((1 + bedrooms * 0.5 + 0.5 * gaussian(rand)) * 2) / 2, 1, 5);
    const lotSqft = Math.round(clamp((neighborhood === "downtown" ? 1500 : 6000) * Math.exp(0.45 * gaussian(rand)), 500, 40000));
    const yearBuilt = Math.floor(clamp(neighborhood === "old_town" ? 1925 + 30 * rand() : 1955 + 68 * rand(), 1900, 2024));
    const condition = Math.floor(clamp(3 + 1.0 * gaussian(rand), 1, 5) + 0.5);
    const garage = neighborhood === "downtown" ? (rand() < 0.3 ? 1 : 0) : Math.floor(rand() * 3) + (rand() < 0.2 ? 1 : 0);
    const hasPool = rand() < (neighborhood === "hillside" || neighborhood === "suburb" ? 0.18 : 0.05) ? 1 : 0;
    const distanceKm = Math.round(clamp((neighborhood === "downtown" ? 2 : neighborhood === "suburb" ? 18 : neighborhood === "hillside" ? 25 : 8) + 3 * gaussian(rand), 0.5, 45) * 10) / 10;

    const age = 2026 - yearBuilt;
    const price = (40000
      + 165 * sqft * multiplier[neighborhood]
      + 14000 * bathrooms
      + 3.5 * lotSqft
      - 450 * Math.min(age, 80)
      + 12000 * (condition - 3)
      + 9000 * garage
      + 22000 * hasPool
      - 1800 * distanceKm) * Math.exp(0.08 * gaussian(rand));
    out.push([sqft, bedrooms, bathrooms, lotSqft, yearBuilt, neighborhood, condition, garage, hasPool, distanceKm, Math.round(Math.max(price, 60000) / 100) * 100]);
  }
  return toCsv(["sqft_living", "bedrooms", "bathrooms", "lot_size_sqft", "year_built", "neighborhood", "condition", "garage_spaces",
                "has_pool", "distance_to_center_km", "price"], out);
}

/**
 * Daily unit sales for one store, 2024-01-01 through 2025-12-31 (731 days):
 * a gentle upward trend, a weekly cycle (weekends high, Monday low), a yearly
 * cycle peaking in summer, a December holiday lift and Gaussian noise.
 */
export function dailySalesCsv(): string {
  const rand = seededRandom(20260303);
  const weekly = [1.18, 0.86, 0.92, 0.95, 1.0, 1.08, 1.25]; // Sun..Sat (getUTCDay)
  const start = Date.UTC(2024, 0, 1);
  const end = Date.UTC(2025, 11, 31);
  const out: (string | number)[][] = [];
  for (let t = 0, ms = start; ms <= end; t++, ms += 86_400_000) {
    const d = new Date(ms);
    const dayOfYear = Math.floor((ms - Date.UTC(d.getUTCFullYear(), 0, 1)) / 86_400_000);
    const base = 220 + 0.12 * t + 35 * Math.sin((2 * Math.PI * (dayOfYear - 80)) / 365.25);
    const holiday = d.getUTCMonth() === 11 && d.getUTCDate() >= 10 && d.getUTCDate() <= 24 ? 1.3 : 1;
    const sales = Math.max(0, Math.round(base * weekly[d.getUTCDay()] * holiday + 12 * gaussian(rand)));
    out.push([d.toISOString().slice(0, 10), sales]);
  }
  return toCsv(["date", "sales"], out);
}

export interface SampleDataset {
  name: string;
  fileName: string;
  description: string;
  detail: string;
  tags: string[];
  csv: () => string;
}

export const SAMPLE_DATASETS: SampleDataset[] = [
  {
    name: "sample-fraud-transactions",
    fileName: "fraud_transactions.csv",
    description: "Synthetic card transactions (4,000 rows, ~7% fraud) for trying binary classification. Target: is_fraudulent. Generated deterministically by ML Studio — not real customer data.",
    detail: "sample data · binary classification · target is_fraudulent",
    tags: ["sample", "classification", "fraud-detection", "tabular"],
    csv: () => fraudTransactionsCsv(),
  },
  {
    name: "sample-house-prices",
    fileName: "house_prices.csv",
    description: "Synthetic home sales (2,500 rows) for trying regression. Target: price. Generated deterministically by ML Studio — not real listings.",
    detail: "sample data · regression · target price",
    tags: ["sample", "regression", "real-estate", "tabular"],
    csv: () => housePricesCsv(),
  },
  {
    name: "sample-daily-sales",
    fileName: "daily_sales.csv",
    description: "Synthetic daily unit sales for one store, 2024-01-01 to 2025-12-31 (trend + weekly + yearly seasonality). For trying forecasting: timestamp date, value sales. Generated deterministically by ML Studio.",
    detail: "sample data · time series · date → sales (daily)",
    tags: ["sample", "time-series", "forecasting", "sales"],
    csv: () => dailySalesCsv(),
  },
];

export const sampleMarkerKey = (name: string) => `sample-dataset:${name}`;
const SAMPLE_DATASETS_LOCK = "ml-studio:sample-datasets";

export type SampleOutcome = "loaded" | "already-loaded" | "previously-loaded" | "name-taken" | "failed";

/**
 * Loads each sample dataset that has never been loaded before. Never touches a
 * user's data:
 *  - a name recorded in seed_markers was loaded once already — skipped even if
 *    a user has since deleted it, so a deleted sample stays deleted;
 *  - a dataset row with that name owned by anyone other than SAMPLE_DATASET_OWNER
 *    is a user's — skipped;
 *  - a sample row that already has a snapshot is only marked, never re-uploaded.
 * A failed upload (e.g. storage unreachable) leaves no marker, so the next
 * start tries again.
 *
 * Replicas starting together: the first takes a session advisory lock and does
 * the work; the others see the lock held and skip rather than wait (the work
 * spawns the profiler and writes to object storage, so waiting would only
 * delay them to find nothing left to do). Without the lock, two replicas could
 * both see "no snapshot" and upload v1 and v2 of the same sample.
 */
export async function loadSampleDatasets(): Promise<Record<string, SampleOutcome> | null> {
  // Dataset bytes only ever live in object storage — dataset_snapshots holds
  // the URI, never the file — so with no STORAGE_BACKEND there is nowhere to
  // put a sample, exactly as a user upload would get a 503.
  let configured: boolean;
  try {
    configured = !!getStorage();
  } catch (err) {
    // eslint-disable-next-line no-console
    console.warn(`WARNING: sample datasets not loaded — object storage is misconfigured: ${err instanceof Error ? err.message : String(err)}`);
    return null;
  }
  if (!configured) {
    // eslint-disable-next-line no-console
    console.warn("WARNING: sample datasets not loaded — STORAGE_BACKEND is not configured, and dataset files are only ever stored in object storage. Configure storage and restart to load them (or set SEED_SAMPLE_DATASETS=false to silence this).");
    return null;
  }

  const lockClient = await pool.connect();
  try {
    const got = await lockClient.query("SELECT pg_try_advisory_lock(hashtext($1)) AS locked", [SAMPLE_DATASETS_LOCK]);
    if (!got.rows[0]?.locked) {
      // eslint-disable-next-line no-console
      console.log("Sample datasets: another replica is loading them — skipping here");
      return null;
    }
    try {
      const outcomes: Record<string, SampleOutcome> = {};
      for (const sample of SAMPLE_DATASETS) {
        // One sample failing (a profiler error, a DB hiccup) mustn't stop the rest.
        outcomes[sample.name] = await loadOneSample(sample).catch((err): SampleOutcome => {
          // eslint-disable-next-line no-console
          console.warn(`WARNING: sample dataset '${sample.name}' was not loaded: ${err instanceof Error ? err.message : String(err)} — will retry on next start`);
          return "failed";
        });
      }
      return outcomes;
    } finally {
      await lockClient.query("SELECT pg_advisory_unlock(hashtext($1))", [SAMPLE_DATASETS_LOCK]).catch(() => {});
    }
  } finally {
    lockClient.release();
  }
}

async function loadOneSample(sample: SampleDataset): Promise<SampleOutcome> {
  const marker = sampleMarkerKey(sample.name);
  const markSql = "INSERT INTO seed_markers (key) VALUES ($1) ON CONFLICT (key) DO NOTHING";
  if ((await pool.query("SELECT 1 FROM seed_markers WHERE key = $1", [marker])).rows.length) return "previously-loaded";

  const existing = (await pool.query("SELECT owner FROM datasets WHERE name = $1", [sample.name])).rows[0];
  if (existing && existing.owner !== SAMPLE_DATASET_OWNER) {
    // eslint-disable-next-line no-console
    console.warn(`Sample datasets: '${sample.name}' already exists and belongs to '${existing.owner}' — leaving it alone`);
    return "name-taken";
  }
  if (existing) {
    const snaps = await pool.query("SELECT 1 FROM dataset_snapshots WHERE dataset_name = $1 LIMIT 1", [sample.name]);
    if (snaps.rows.length) {
      await pool.query(markSql, [marker]);
      return "already-loaded";
    }
  } else {
    await insertDatasetRecord({
      name: sample.name, source: "upload", structureType: "structured", owner: SAMPLE_DATASET_OWNER,
      tags: sample.tags, detail: sample.detail, visibility: "org", description: sample.description,
    });
  }

  const tmpDir = await fsp.mkdtemp(path.join(os.tmpdir(), "ml-studio-sample-dataset-"));
  try {
    const content = Buffer.from(sample.csv(), "utf8");
    const filePath = path.join(tmpDir, "data.csv");
    await fsp.writeFile(filePath, content);
    const result = await ingestDatasetFile({
      datasetName: sample.name, format: "csv", fileName: sample.fileName, filePath,
      bytes: content.length, checksum: createHash("sha256").update(content).digest("hex"), actor: SAMPLE_DATASET_OWNER,
    });
    if (result.status !== 200) {
      // eslint-disable-next-line no-console
      console.warn(`WARNING: sample dataset '${sample.name}' was not loaded (${result.status}): ${String(result.body.error ?? "unknown error")} — will retry on next start`);
      return "failed";
    }
    await pool.query(markSql, [marker]);
    // eslint-disable-next-line no-console
    console.log(`Sample datasets: loaded '${sample.name}' v${result.body.snapshotVersion} (${result.body.rows} rows) → ${result.body.storageUri}`);
    return "loaded";
  } finally {
    await fsp.rm(tmpDir, { recursive: true, force: true }).catch(() => {});
  }
}

/** Server-start wrapper: honours SEED_SAMPLE_DATASETS and never throws. */
export async function loadSampleDatasetsAtStartup(env: NodeJS.ProcessEnv = process.env): Promise<void> {
  if (!envFlag("SEED_SAMPLE_DATASETS", env)) {
    // eslint-disable-next-line no-console
    console.log("Sample datasets: SEED_SAMPLE_DATASETS=false — not loading");
    return;
  }
  try {
    const outcomes = await loadSampleDatasets();
    // eslint-disable-next-line no-console
    if (outcomes) console.log("Sample datasets:", outcomes);
  } catch (err) {
    // eslint-disable-next-line no-console
    console.warn(`WARNING: sample dataset load failed — the server continues without them: ${err instanceof Error ? err.message : String(err)}`);
  }
}
