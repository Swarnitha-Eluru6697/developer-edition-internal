/*
 * BUG-10: the pure mapping from the backend's recorded schema-introspection outcome to what the pipeline detail
 * drawer shows.
 */
import { test, expect } from "@playwright/test";
import { schemaIntrospectionView } from "../../src/schemaIntrospection";

test("a failure is flagged as one and carries its reason", () => {
  const view = schemaIntrospectionView("failed", "the source did not respond within 8s (connection timed out)");
  expect(view).toMatchObject({ label: "FAILED", cls: "br", isFailure: true });
  expect(view?.message).toContain("did not respond");
});

test("a skip is shown but is not a failure", () => {
  const view = schemaIntrospectionView("skipped", "Connection refused");
  expect(view).toMatchObject({ label: "SKIPPED", isFailure: false, message: "Connection refused" });
});

test("a registered or linked schema has no message", () => {
  expect(schemaIntrospectionView("created", null)).toMatchObject({ label: "AUTO-REGISTERED", message: null, isFailure: false });
  expect(schemaIntrospectionView("linked_existing", "")).toMatchObject({ label: "LINKED TO EXISTING", message: null });
});

test("nothing is shown when introspection was never attempted", () => {
  expect(schemaIntrospectionView(null, null)).toBeNull();
  expect(schemaIntrospectionView(undefined, "stray")).toBeNull();
});

test("an unrecognised status from a newer backend is shown rather than hidden", () => {
  expect(schemaIntrospectionView("partial", "x")).toMatchObject({ label: "PARTIAL", isFailure: false });
});
