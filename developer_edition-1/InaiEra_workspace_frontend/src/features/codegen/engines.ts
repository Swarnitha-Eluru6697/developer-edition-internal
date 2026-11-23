/**
 * The SQL engines the assistant can write for, next to "what to generate".
 *
 * Only sql and dbt turns have an engine to choose (python and FlinkSQL have one each). Trino (the default) and ClickHouse can be selected.
 */
import type { CodegenTarget } from "../../types";

/** The engines that can be selected today. pg_lake is not offered any more (the backend still accepts it); more are added here later. */
export const SELECTABLE_ENGINES = ["trino", "clickhouse"] as const;
export type CodegenEngine = (typeof SELECTABLE_ENGINES)[number];
export const DEFAULT_ENGINE: CodegenEngine = "trino";

export const ENGINE_OPTIONS = [
  { value: "trino", label: "Trino" },
  { value: "clickhouse", label: "ClickHouse" },
];

/** Whether a turn for this target takes an engine at all. */
export const targetUsesEngine = (target: CodegenTarget | string): boolean => target === "sql" || target === "dbt";

export const isSelectableEngine = (value: unknown): value is CodegenEngine =>
  typeof value === "string" && (SELECTABLE_ENGINES as readonly string[]).includes(value);
