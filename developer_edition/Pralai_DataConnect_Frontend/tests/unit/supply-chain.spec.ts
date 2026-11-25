/*
 * Hardening: the supply-chain controls stay in place. The scanners run in CI against live advisory data; these fail if someone removes the job
 * or loosens a floor that a real `npm audit` finding forced.
 */
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { test, expect } from "@playwright/test";

const ROOT = join(dirname(fileURLToPath(import.meta.url)), "..", "..");
const read = (...parts: string[]) => readFileSync(join(ROOT, ...parts), "utf8");

const atLeast = (version: string, floor: string) => {
  const a = version.split(".").map(Number), b = floor.split(".").map(Number);
  for (let i = 0; i < 3; i++) { if ((a[i] ?? 0) !== (b[i] ?? 0)) return (a[i] ?? 0) > (b[i] ?? 0); }
  return true;
};

test.describe("supply chain", () => {
  test("the shipped runtime pins Express at the release that fixed the advisories found by npm audit", () => {
    const pkg = JSON.parse(read("runtime", "package.json")) as { dependencies: Record<string, string> };
    expect(pkg.dependencies.express).toMatch(/^\d+\.\d+\.\d+$/);                 // exact, never a range
    expect(atLeast(pkg.dependencies.express ?? "0", "4.22.3")).toBe(true);
  });

  test("the runtime lockfile resolves that same Express version", () => {
    const lock = JSON.parse(read("runtime", "package-lock.json")) as { packages: Record<string, { version: string }> };
    const pkg = JSON.parse(read("runtime", "package.json")) as { dependencies: Record<string, string> };
    expect(lock.packages["node_modules/express"]?.version).toBe(pkg.dependencies.express);
  });

  test("the security workflow audits what ships, scans the image, publishes an SBOM, and runs on a schedule", () => {
    const wf = read(".github", "workflows", "security.yml");
    for (const required of ["npm audit --omit=dev --audit-level=moderate", "working-directory: runtime", "cyclonedx-npm", "aquasecurity/trivy-action", "anchore/sbom-action", "schedule:"]) {
      expect(wf, required).toContain(required);
    }
  });

  test("the Docker image pins its base by version and digest", () => {
    const froms = read("Dockerfile").split("\n").filter((l) => l.startsWith("FROM "));
    expect(froms.length).toBeGreaterThan(0);
    for (const line of froms) expect(line, line).toMatch(/^FROM \S+:\d+\.\d+\.\d+\S*@sha256:[0-9a-f]{64}/);
  });
});
