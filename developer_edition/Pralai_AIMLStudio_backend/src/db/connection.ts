import { Pool } from "pg";
import fs from "fs";
import path from "path";
import dotenv from "dotenv";

dotenv.config();

const connectionString =
  process.env.DATABASE_URL || "postgres://postgres:postgres@localhost:5432/mlstudio";

export const pool = new Pool({ connectionString });

export async function initSchema(): Promise<void> {
  const schema = fs.readFileSync(path.join(__dirname, "schema.sql"), "utf8");
  // Serialised across processes: CREATE TABLE IF NOT EXISTS is not race-safe
  // in Postgres — two replicas starting together against an empty database
  // both pass the "not exists" check and the loser fails with a duplicate
  // pg_type key, crashing that pod before it listens. One transaction behind
  // an advisory lock makes the second replica wait, then find every table
  // already there.
  const client = await pool.connect();
  try {
    await client.query("BEGIN");
    await client.query("SELECT pg_advisory_xact_lock(hashtext('ml-studio:init-schema'))");
    await client.query(schema);
    await client.query("COMMIT");
  } catch (err) {
    await client.query("ROLLBACK").catch(() => {});
    throw err;
  } finally {
    client.release();
  }
}
