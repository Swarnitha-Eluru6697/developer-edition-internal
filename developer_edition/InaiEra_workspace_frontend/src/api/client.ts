/**
 * Backward-compatible barrel for the RTK Query API (WS-FE-014).
 *
 * The API contract used to be one 500+ line `createApi` call covering all
 * nine feature domains at once. It's now split into src/api/base.ts (the
 * shared instance + tag vocabulary) plus one `injectEndpoints` package per
 * domain under src/features/<name>/api.ts — see that file's doc comment for
 * why (RTK Query's own documented code-splitting pattern).
 *
 * This file re-exports everything so existing imports of `workspaceApi` and
 * the `use*Query`/`use*Mutation` hooks from "../api/client" keep working
 * unchanged; new code should prefer importing directly from the owning
 * feature package (e.g. `from "../features/dbt/api"`).
 */
export { workspaceApi } from "./base";

export * from "../features/workspace/api";
export * from "../features/sql/api";
export * from "../features/notebook/api";
export * from "../features/dbt/api";
export * from "../features/flink/api";
export * from "../features/dq/api";
export * from "../features/catalog/api";
export * from "../features/git/api";
export * from "../features/codegen/api";
export * from "../features/settings/api";
export * from "../rbac/api";
