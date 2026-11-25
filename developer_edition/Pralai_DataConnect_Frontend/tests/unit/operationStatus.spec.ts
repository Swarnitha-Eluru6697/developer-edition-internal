/*
 * FE-BUG-05: polling must stop on every terminal SERVER status (including
 * `stale` and `cancelled`) and keep going on queued/running.
 */
import { test, expect } from "@playwright/test";
import { isTerminalOperationStatus } from "../../src/store/operationStatus";

test.describe("isTerminalOperationStatus", () => {
  for (const s of ["succeeded", "failed", "cancelled", "stale"]) {
    test(`${s} is terminal`, () => expect(isTerminalOperationStatus(s)).toBe(true));
  }
  for (const s of ["queued", "running"]) {
    test(`${s} is not terminal`, () => expect(isTerminalOperationStatus(s)).toBe(false));
  }
  test("unknown statuses keep polling", () => expect(isTerminalOperationStatus("???")).toBe(false));
});
