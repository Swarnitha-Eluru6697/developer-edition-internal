/*
 * FE-BUG-04: DataConnect has infrastructure-specific failures (Kafka Connect
 * rejected a config, a source system timed out, a topic policy can't be
 * applied to the live cluster, ...) that shouldn't all collapse into a
 * generic "Request failed" toast. The backend already carries everything
 * needed to do better — code, message, retryable, request/correlation id,
 * sanitized details (see app/core/errors.py's DataConnectError hierarchy and
 * app/services/safe_error.py's SafeError, which is what a background
 * Operation/QualityExecutionJob's error_code/error_message/retryable/
 * correlation_id come from) — this module is where the frontend maps that
 * into copy, and FailureInfo/toFailureInfo* below normalize the two shapes
 * (a thrown ApiError vs. a terminal Operation) into one the FailureDialog
 * component (see components/FailureDialog.tsx) can render uniformly.
 *
 * Every code below is either a DataConnectError subclass's own `code` or
 * one SafeError._classify produces — kept in sync by hand with those two
 * backend modules, the same "no shared package between the two repos"
 * convention the rest of the UI follows for backend vocabularies.
 * A code with no entry here still renders fine (FailureDialog falls back to
 * the backend's own message alone) — this map only makes the well-known
 * infrastructure codes read as a clearer explanation than their raw message.
 */
import { errorMessage } from "./errors";
import { isRecord, stringOf } from "./json";
import type { Operation } from "./types/api";

export const ERROR_EXPLANATIONS: Record<string, string> = {
  SOURCE_CONNECTION_FAILED: "The source system could not be reached.",
  SOURCE_CONNECTION_REFUSED: "The source system refused the connection.",
  SOURCE_CONNECTION_TIMEOUT: "The source system did not respond in time.",
  SOURCE_HTTP_ERROR: "The source system returned an HTTP error.",
  DESTINATION_BLOCKED: "This destination isn't permitted by network policy.",
  DRIVER_NOT_INSTALLED: "A required driver isn't installed on this worker.",
  KAFKA_UNAVAILABLE: "Kafka could not be reached.",
  KAFKA_CONNECT_UNAVAILABLE: "Kafka Connect could not be reached.",
  KAFKA_CONNECT_CONFIG_INVALID: "Kafka Connect rejected the connector configuration.",
  KAFKA_CONNECT_PLUGIN_MISSING: "This connector template isn't compatible with the Kafka Connect worker.",
  TOPIC_CREATE_FAILED: "The Kafka broker could not create or verify the topic.",
  TOPIC_POLICY_INVALID: "This pipeline's topic policy can't be applied to the live Kafka cluster.",
  SCHEMA_INCOMPATIBLE: "This schema version is incompatible with the configured compatibility mode.",
  SCHEMA_DRIFT_PENDING: "A schema drift approval is still pending for this pipeline.",
  QUALITY_POLICY_INVALID: "This quality policy's rule definitions are invalid.",
  OPERATION_TIMEOUT: "This took longer than expected to complete.",
  OPERATION_FAILED: "This operation failed unexpectedly.",
  DATABASE_UNAVAILABLE: "DataConnect's own database is temporarily unavailable.",
};

/** The normalized shape FailureDialog renders — whether it came from a
 * thrown ApiError or a terminal (status: "failed") Operation/
 * QualityExecutionJob. */
export interface FailureInfo {
  code?: string;
  message: string;
  retryable: boolean;
  /** request_id (a direct API failure) or correlation_id (a background
   * Operation/QualityExecutionJob failure) — same idea, traceable back to
   * the backend's own structured logs, whichever name applies. */
  requestId?: string | null;
  details?: Record<string, unknown> | null;
}

export function explainCode(code: string | null | undefined): string | undefined {
  return code ? ERROR_EXPLANATIONS[code] : undefined;
}

/** Normalizes whatever a catch block actually caught — an ApiError (has its
 * own code/retryable/requestId/details) or anything else — into FailureInfo.
 * FE-BUG-05: this is deliberately never reached for "the browser gave up
 * polling" any more — pollOperation/pollDiscoveryRun report that as a
 * PollOutcome (see pollUntilTerminal.ts), not a thrown exception, precisely
 * so it can never be mistaken for a real failure here. Never throws: this
 * is itself error-handling code, so it degrades to a bare message rather
 * than compound the original failure. */
export function toFailureInfo(e: unknown): FailureInfo {
  if (isRecord(e) && typeof e.message === "string" && "retryable" in e) {
    return {
      code: stringOf(e.code),
      message: e.message,
      retryable: Boolean(e.retryable),
      requestId: stringOf(e.requestId),
      details: isRecord(e.details) ? e.details : undefined,
    };
  }
  return { message: errorMessage(e), retryable: false };
}

/** Same idea, for a terminal Operation this app polled to `status: "failed"`
 * (see store/pollOperation.ts) — its error_code/error_message/retryable/
 * correlation_id already came from the backend's SafeError classification. */
export function toFailureInfoFromOperation(op: Operation, fallbackMessage: string): FailureInfo {
  return {
    code: op.error_code ?? undefined,
    message: op.error_message || fallbackMessage,
    retryable: Boolean(op.retryable),
    requestId: op.correlation_id,
  };
}
