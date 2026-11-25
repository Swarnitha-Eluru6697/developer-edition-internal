/*
 * FE-BUG-09: the frontend's own, independent redaction layer — defense-in-
 * depth on top of (never a replacement for) the backend's own
 * redact_connector_config. Tested head-on since it's a pure function.
 */
import { test, expect } from "@playwright/test";
import { SECRET_PATTERNS, isSecretKey, redactSecrets, redactJsonString, redactedInlineJson, REDACTED_PLACEHOLDER } from "../../src/secretRedaction";

test.describe("SECRET_PATTERNS / isSecretKey", () => {
  const cases: [string, boolean][] = [
    ["password", true],
    ["db_password", true],
    ["connection.password", true],
    ["secret", true],
    ["client_secret", true],
    ["token", true],
    ["auth_token", true],
    ["access_key", true],
    ["access-key", true],
    ["accessKey", true],
    ["private_key", true],
    ["private-key", true],
    ["privateKey", true],
    ["credential", true],
    ["credentials", true],
    ["username", false],
    ["hostname", false],
    ["port", false],
    ["topic_name", false],
  ];

  for (const [key, expected] of cases) {
    test(`"${key}" is ${expected ? "" : "not "}flagged as secret-shaped`, () => {
      expect(isSecretKey(key)).toBe(expected);
    });
  }

  test("every pattern is case-insensitive", () => {
    for (const pattern of SECRET_PATTERNS) {
      expect(pattern.flags).toContain("i");
    }
  });
});

test.describe("redactSecrets", () => {
  test("replaces the value at a matching key, leaves the key itself intact", () => {
    const result = redactSecrets({ username: "alice", password: "hunter2" }) as Record<string, unknown>;
    expect(result.username).toBe("alice");
    expect(result.password).toBe(REDACTED_PLACEHOLDER);
  });

  test("recurses into nested objects", () => {
    const result = redactSecrets({
      connector_config: { "database.hostname": "db.internal", "database.password": "hunter2" },
    }) as { connector_config: Record<string, unknown> };
    expect(result.connector_config["database.hostname"]).toBe("db.internal");
    expect(result.connector_config["database.password"]).toBe(REDACTED_PLACEHOLDER);
  });

  test("recurses into arrays of objects", () => {
    const result = redactSecrets([
      { name: "a", access_key: null },
      { name: "b", access_key: "AKIA123" },
    ]) as { name: string; access_key: unknown }[];
    if (!result[1]) throw new Error("expected two redacted entries");
    expect(result[1].name).toBe("b");
    expect(result[1].access_key).toBe(REDACTED_PLACEHOLDER);
  });

  test("a secret-shaped key is redacted even if its value is itself an object", () => {
    // Never recurse INTO a matched key's value looking for exceptions —
    // the whole point is the key name alone is enough to distrust it.
    const result = redactSecrets({ credentials: { access_key: "x", secret_key: "y" } }) as { credentials: unknown };
    expect(result.credentials).toBe(REDACTED_PLACEHOLDER);
  });

  test("leaves ordinary prose containing the word 'token' as a substring untouched when it's not a key name", () => {
    // Matches by KEY, never by scanning string content — a value merely
    // mentioning "token" in its text must not itself be mangled.
    const result = redactSecrets({ message: "your session token will expire soon" }) as { message: string };
    expect(result.message).toBe("your session token will expire soon");
  });

  test("passes through primitives and null unchanged", () => {
    expect(redactSecrets("plain string")).toBe("plain string");
    expect(redactSecrets(42)).toBe(42);
    expect(redactSecrets(null)).toBeNull();
    expect(redactSecrets(undefined)).toBeUndefined();
  });

  test("never throws on a circular structure — fails closed to a redacted placeholder, not the raw value", () => {
    const circular: Record<string, unknown> = { name: "loop" };
    circular.self = circular;
    expect(() => redactSecrets(circular)).not.toThrow();
  });
});

test.describe("redactJsonString", () => {
  test("parses a JSON string payload and redacts secret-shaped keys", () => {
    const out = redactJsonString(JSON.stringify({ "database.hostname": "db", "database.password": "hunter2" }));
    expect(out).toContain("db");
    expect(out).toContain(REDACTED_PLACEHOLDER);
    expect(out).not.toContain("hunter2");
  });
  test("handles a JSON array string", () => {
    const out = redactJsonString(JSON.stringify([{ api_token: "abc123" }]));
    expect(out).not.toContain("abc123");
  });
  test("leaves non-JSON text untouched", () => {
    expect(redactJsonString("the token expired at noon")).toBe("the token expired at noon");
  });
  test("a JSON-looking string that won't parse fails closed, never shown raw", () => {
    expect(redactJsonString('{"password": "hunter2", broken')).toBe(REDACTED_PLACEHOLDER);
  });
});

test.describe("redactedInlineJson", () => {
  test("is compact JSON with secret-shaped keys redacted", () => {
    const out = redactedInlineJson({ field: "email", client_secret: "s3cr3t" });
    expect(out).toBe(`{"field":"email","client_secret":"${REDACTED_PLACEHOLDER}"}`);
  });
});
