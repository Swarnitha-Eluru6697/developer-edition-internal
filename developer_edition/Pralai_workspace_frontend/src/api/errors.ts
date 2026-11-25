/**
 * Human-readable message from an RTK Query / fetch error.
 *
 * The workspace backend answers errors as `{error: {code, message}}`; the
 * proxied dbt/Flink/DQ services (and FastAPI's own validation) as
 * `{detail: {message}}`. Call sites historically checked those in different
 * orders, so `prefer` keeps each one's original precedence.
 */
export interface ApiErrorLike {
  status?: number | string;
  error?: string;
  message?: string;
  data?: {
    error?: { message?: string } | string;
    detail?: { message?: string } | string;
    message?: string;
    [key: string]: unknown;
  };
}

export function asApiError(e: unknown): ApiErrorLike {
  return (e && typeof e === "object" ? e : {}) as ApiErrorLike;
}

function nested(v: { message?: string } | string | undefined): string | undefined {
  return v && typeof v === "object" ? v.message : undefined;
}

export function apiErrorMessage(
  e: unknown,
  fallback: string,
  { prefer = "error" }: { prefer?: "error" | "detail" } = {},
): string {
  const data = asApiError(e).data;
  const fromError = nested(data?.error);
  const fromDetail = nested(data?.detail);
  return (prefer === "error" ? fromError ?? fromDetail : fromDetail ?? fromError) ?? fallback;
}

/** HTTP status of an RTK Query error, or null (FETCH_ERROR etc. are strings). */
export function apiErrorStatus(e: unknown): number | string | null {
  return asApiError(e).status ?? null;
}
