/** FlinkSQL editor helpers. */

/**
 * Header for a new FlinkSQL file: instructions only, never starter SQL. The
 * file stays empty so the first statement in it is the user's (or codegen's).
 */
export const FLINKSQL_TEMPLATE = `-- FlinkSQL streaming job
--
-- A job is three parts:
--   1. CREATE TABLE for each source  — Kafka topic, CDC stream, …
--   2. CREATE TABLE for each sink    — lakehouse table, Kafka topic, …
--   3. INSERT INTO <sink> SELECT … FROM <source>
--
-- Connector settings go in each table's WITH ( … ) clause.
--
-- ⌘↵          syntax-check this file against the FlinkSQL runner
-- ⌘K          ask the AI chat to generate the pipeline for you
-- Deploy      commit and push, then register the repo under FlinkSQL → Jobs

`;

/** SQL with block and line comments removed (used to tell "only the header"
 *  apart from real statements before calling the runner). */
export function stripSqlComments(sql: string): string {
  return sql.replace(/\/\*[\s\S]*?\*\//g, "").replace(/^\s*--.*$/gm, "").trim();
}

/** Table names a FlinkSQL job writes — whatever it INSERTs INTO. */
export function flinkSinks(sql: string): string[] {
  const out = new Set<string>();
  for (const m of sql.matchAll(/INSERT\s+INTO\s+([\w.`"]+)/gi)) {
    out.add(m[1].replace(/["`]/g, ""));
  }
  return [...out];
}

/** Flink 1.19 routes running and finished jobs under different Web UI segments. */
const LIVE_STATES = ["RUNNING", "RESTARTING", "CREATED", "INITIALIZING", "SUSPENDED"];

export function flinkUiJobUrl(base: string, flinkJobId: string, flinkState: string | null | undefined): string {
  const segment = flinkState && LIVE_STATES.includes(flinkState.toUpperCase()) ? "running" : "completed";
  return `${base}/#/job/${segment}/${flinkJobId}/overview`;
}
