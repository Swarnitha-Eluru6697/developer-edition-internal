/*
 * Kafka Connect worker pools in the UI: id -> label, and how a pool's status is styled.
 */
import { test, expect } from "@playwright/test";
import { poolLabel, poolStatusStyle } from "../../src/connectPools";

test("each pool id has a short label", () => {
  expect(poolLabel("connect-cdc-pool")).toBe("CDC");
  expect(poolLabel("connect-jdbc-pool")).toBe("JDBC");
  expect(poolLabel("connect-file-pool")).toBe("File");
  expect(poolLabel("connect-api-pool")).toBe("API");
  expect(poolLabel("connect-stream-pool")).toBe("Stream");
  expect(poolLabel("connect-sink-pool")).toBe("Sink");
});

test("the retired queue pool still reads as the stream pool", () => {
  expect(poolLabel("connect-queue-pool")).toBe("Stream");
});

test("a missing pool is a dash, an unknown one is shown rather than hidden", () => {
  expect(poolLabel(null)).toBe("—");
  expect(poolLabel(undefined)).toBe("—");
  expect(poolLabel("connect-newfangled-pool")).toBe("connect-newfangled-pool");
});

test("only a healthy pool is green; idle is neutral; anything else is a warning", () => {
  expect(poolStatusStyle("healthy")).toEqual({ pill: "bg", dot: "dg" });
  expect(poolStatusStyle("idle")).toEqual({ pill: "bgr", dot: "dgr" });
  expect(poolStatusStyle("degraded")).toEqual({ pill: "ba", dot: "da" });
  expect(poolStatusStyle("unknown")).toEqual({ pill: "ba", dot: "da" });
});
