import { readFileSync } from "node:fs";
import { describe, expect, it } from "vitest";
import { STATUS_BG, STATUS_COLOR } from "../../src/features/dbt/styles";

// DBT-005: the runner reports `cancelling` while it is still terminating a run's Kubernetes Job. Every status the
// contract can send must have a colour, or that run's badge silently renders unstyled.
const spec = JSON.parse(readFileSync("openapi/workspace.v0.1.0.json", "utf8"));
const statuses: string[] = spec.components.schemas.DbtRun.properties.status.enum;

describe("dbt run status in the API contract", () => {
  it("includes cancelling", () => {
    expect(statuses).toContain("cancelling");
  });

  it.each(statuses)("has a badge colour and background for %s", (status) => {
    expect(STATUS_COLOR[status]).toBeTruthy();
    expect(STATUS_BG[status]).toBeTruthy();
  });
});
