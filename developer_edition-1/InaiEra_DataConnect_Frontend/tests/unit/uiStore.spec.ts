import { test, expect } from "@playwright/test";
import { createUiStore, readStoredSelected } from "../../src/context/UiContext";
import { connectorStateOf, directionOf, failedTaskCount, flowIndex, isDeployed } from "../../src/connectorRows";
import type { Pipeline, RuntimeFlow } from "../../src/types/api";

test("the UI store starts with nothing selected", () => {
  expect(createUiStore().get()).toEqual({ selected: null });
});

test("readStoredSelected is null without storage and never throws", () => {
  expect(readStoredSelected()).toBeNull();
});

test("each mount gets its own store, so two DataConnect mounts on one page never share state", () => {
  const a = createUiStore();
  const b = createUiStore();
  a.set({ selected: "conn-a" });
  expect(a.get().selected).toBe("conn-a");
  expect(b.get().selected).toBeNull();
});

test("subscribers hear about changes until they unsubscribe", () => {
  const store = createUiStore();
  let calls = 0;
  const off = store.subscribe(() => { calls += 1; });
  store.set({ selected: "x" });
  store.set({ selected: "y" });
  off();
  store.set({ selected: "z" });
  expect(calls).toBe(2);
});

const pipe = (over: Partial<Pipeline>): Pipeline => ({ id: "p", name: "n", connector_name: "c", status: "running", ...over } as Pipeline);

test("drafts, pipelines being deleted and pipelines with no connector are not 'deployed'", () => {
  expect(isDeployed(pipe({}))).toBe(true);
  expect(isDeployed(pipe({ status: "paused" }))).toBe(true);
  expect(isDeployed(pipe({ status: "failed" }))).toBe(true);
  expect(isDeployed(pipe({ status: "draft" }))).toBe(false);
  expect(isDeployed(pipe({ status: "deleting" }))).toBe(false);
  expect(isDeployed(pipe({ connector_name: "" }))).toBe(false);
});

test("direction is a sink only when the pipeline says SINK, in any case", () => {
  expect(directionOf(pipe({ direction: "SINK" }))).toBe("sink");
  expect(directionOf(pipe({ direction: "sink" }))).toBe("sink");
  expect(directionOf(pipe({ direction: "SOURCE" }))).toBe("source");
  expect(directionOf(pipe({ direction: undefined }))).toBe("source");
});

const flow = (connectors: NonNullable<RuntimeFlow["connectors"]>): RuntimeFlow =>
  ({ reachable: true, connect_url: "", bootstrap: "", message: "", connectors, topics: [], sinks: [] });

test("a connector's state comes from Kafka Connect, and is UNKNOWN whenever Connect could not be read", () => {
  const by = flowIndex(flow([
    { name: "ok", pipeline_id: "1", type: "source", state: "RUNNING", tasks: ["RUNNING"], topics: [] },
    { name: "deg", pipeline_id: "2", type: "source", state: "RUNNING", tasks: ["RUNNING", "FAILED"], topics: [] },
  ]));
  expect(connectorStateOf(pipe({ connector_name: "ok" }), by, false)).toBe("running");
  expect(connectorStateOf(pipe({ connector_name: "deg" }), by, false)).toBe("degraded");
  expect(connectorStateOf(pipe({ connector_name: "missing" }), by, false)).toBe("unknown");
  expect(connectorStateOf(pipe({ connector_name: "ok" }), by, true)).toBe("unknown");   // stale or unreachable: never green
});

test("failed tasks are counted from Connect's own task states", () => {
  const by = flowIndex(flow([{ name: "x", pipeline_id: "1", type: "source", state: "RUNNING", tasks: ["RUNNING", "FAILED", "failed", null], topics: [] }]));
  expect(failedTaskCount(by.x)).toBe(2);
  expect(failedTaskCount(undefined)).toBe(0);
});
