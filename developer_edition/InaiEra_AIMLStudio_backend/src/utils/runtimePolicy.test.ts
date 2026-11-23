import { afterEach, describe, expect, it } from "vitest";
import { getServingMode, requiresDurableArtifacts } from "./runtimePolicy";

const old = { ...process.env };
afterEach(() => { process.env = { ...old }; });

describe("runtime policy", () => {
  it("defaults to development outside NODE_ENV=production", () => {
    delete process.env.SERVING_MODE; process.env.NODE_ENV = "test";
    expect(getServingMode()).toBe("development");
  });
  it("is fail-closed in production", () => {
    process.env.SERVING_MODE = "production"; process.env.NODE_ENV = "test";
    expect(getServingMode()).toBe("production");
    expect(requiresDurableArtifacts()).toBe(true);
  });
  it("supports explicit durable artifact enforcement independently", () => {
    process.env.SERVING_MODE = "development"; process.env.ARTIFACT_PERSISTENCE_MODE = "required";
    expect(requiresDurableArtifacts()).toBe(true);
  });
});
