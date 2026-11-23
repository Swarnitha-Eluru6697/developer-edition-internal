/**
 * Typed accessors into the generated OpenAPI contract (WS-FE-014).
 *
 * src/api/generated/workspace-schema.ts is produced by `npm run codegen:api`
 * from the committed snapshot openapi/workspace.v<version>.json — never edit
 * it by hand. These helpers are the only way the rest of the app should reach
 * into it, so a backend contract change surfaces as a compile error in the
 * alias that uses it.
 */
import type { components, paths } from "./generated/workspace-schema";

/** Every named schema in the backend's OpenAPI document. */
export type Schemas = components["schemas"];

type Method = "get" | "put" | "post" | "delete" | "patch";

/**
 * The JSON request body of an operation.
 *
 * The dbt/Flink/DQ pass-through routes document their bodies inline (the
 * backend forwards the raw bytes, see app/schemas/proxy.py:json_body), so
 * there is no named schema to alias — this reaches the operation directly.
 */
export type JsonRequestBody<P extends keyof paths, M extends Method & keyof paths[P]> =
  paths[P][M] extends { requestBody?: { content: { "application/json": infer B } } } ? B : never;
