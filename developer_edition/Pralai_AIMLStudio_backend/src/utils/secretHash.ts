import { randomBytes, scryptSync, timingSafeEqual } from "crypto";

// Node's own documentation recommends scrypt for exactly this use case
// (password/secret hashing) — a real KDF, not a new dependency. Format:
// "salt:hash", both hex, so the salt travels with the hash in one column
// rather than needing a second one.
export function hashSecret(plaintext: string): string {
  const salt = randomBytes(16).toString("hex");
  const hash = scryptSync(plaintext, salt, 64).toString("hex");
  return `${salt}:${hash}`;
}

export function verifySecret(plaintext: string, stored: string): boolean {
  const [salt, hash] = stored.split(":");
  if (!salt || !hash) return false;
  const candidate = scryptSync(plaintext, salt, 64);
  const expected = Buffer.from(hash, "hex");
  // Real constant-time comparison — a plain === here would leak timing
  // information about how many leading bytes matched, a genuine (if
  // narrow) side channel for a secret comparison.
  if (candidate.length !== expected.length) return false;
  return timingSafeEqual(candidate, expected);
}
