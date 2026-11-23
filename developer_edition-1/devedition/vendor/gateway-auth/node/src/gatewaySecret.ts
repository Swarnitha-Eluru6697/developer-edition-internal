import * as crypto from "crypto";

/**
 * Constant-time check that a request actually came through Envoy, not just
 * something on the same network path as it. This is identical across every
 * service by design — there is no per-service variation here, which is
 * exactly why it belongs in this shared package rather than being
 * reimplemented per service (that reimplementation is how ML Studio's
 * original gap happened).
 *
 * Fails closed: an unconfigured secret means every request is rejected,
 * never silently trusted. These are identity claims, not an unauthenticated
 * GET — the failure mode has to be strict, not permissive-by-default.
 */
export function verifyGatewaySecret(expected: string | undefined, provided: string | null | undefined): boolean {
  if (!expected || !provided) return false;
  const a = Buffer.from(provided);
  const b = Buffer.from(expected);
  if (a.length !== b.length) return false;
  return crypto.timingSafeEqual(a, b);
}
