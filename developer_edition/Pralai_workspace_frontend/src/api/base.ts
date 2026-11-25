/**
 * Base RTK Query API instance (WS-FE-014).
 *
 * Declares no endpoints itself — every feature package under src/features/
 * (workspace, sql, notebook, dbt, flink, dq, catalog, git, codegen) adds its
 * own slice of the contract via `.injectEndpoints({...})`, RTK Query's own
 * documented pattern for splitting one API across feature boundaries
 * (https://redux-toolkit.js.org/rtk-query/usage/code-splitting). Each
 * feature package now owns and can be tested against only the endpoints it
 * actually uses, instead of every consumer depending on one 500+ line file
 * covering all nine domains at once.
 *
 * `tagTypes` still has to be declared once, here, up front — RTK Query
 * requires the full tag vocabulary at the base `createApi` call; a feature
 * package can only use tags already listed in this array.
 */
import { createApi, fetchBaseQuery } from "@reduxjs/toolkit/query/react";
import { apiUrl } from "./apiBase";

export const workspaceApi = createApi({
  reducerPath: "workspaceApi",
  // No Authorization header here (WS-AUTH-010): the HttpOnly session cookie
  // set by POST /api/v1/auth/exchange (see src/api/auth.ts) authenticates
  // every request automatically — `credentials: "same-origin"` is fetch's
  // default for a same-origin baseUrl like this one, kept explicit so that
  // stays true even if baseUrl ever changes.
  // baseUrl is resolved per request, not once at import: when the app is
  // federated, the host sets the tunnel base (api/apiBase.ts) after this
  // module has already loaded.
  baseQuery: (args, api, extraOptions) =>
    fetchBaseQuery({
      baseUrl: apiUrl("/api/v1"),
      credentials: "same-origin",
    })(args, api, extraOptions),
  tagTypes: [
    "Workspace", "File", "GitBinding", "Execution", "Me",
    "CodegenSession", "CodegenMessage", "DatabaseConnection", "FlinkJob",
    "DqRuleset", "DqBinding", "DqRender", "DqRule",
    // Previously referenced via `as any` casts in the monolithic
    // api/client.ts because it wasn't declared here — an untyped cache-
    // invalidation tag for the whole dbt feature (WS-FE-014).
    "DbtProject",
    "Settings", "SettingsCodegen",
    "Alert", "AlertRule", "AlertChannel",
  ],
  endpoints: () => ({}),
});
