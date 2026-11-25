/*
 * NEW-09: the availability adapter. The block from the backend is dynamic JSON, so it is validated, never cast; a missing or malformed one means "no
 * information", which leaves a choice selectable and unflagged (the backend still refuses what it cannot run).
 */
import { test, expect } from "@playwright/test";
import {
  SAFE_QUALITY_CAPABILITIES, availabilityNote, availabilityOf, defaultChoice, isSelectable, optionLabel, qualityCapabilitiesOf, selectableFirst,
  type Availability,
} from "../../src/availability";

const block = (over: Record<string, unknown> = {}) => ({ state: "supported", code: null, message: null, remediation: null, verified: true, ...over });
const A = (state: Availability["state"], over: Partial<Availability> = {}): Availability => ({ state, code: null, message: null, remediation: null, verified: true, ...over });

test.describe("availabilityOf", () => {
  test("reads each of the three states", () => {
    for (const state of ["supported", "requires_configuration", "unavailable"] as const) expect(availabilityOf(block({ state }))?.state).toBe(state);
  });
  test("keeps the code, message, remediation and verified flag, and defaults verified to true", () => {
    expect(availabilityOf({ state: "unavailable", code: "X", message: "m", remediation: "r", verified: false }))
      .toEqual({ state: "unavailable", code: "X", message: "m", remediation: "r", verified: false });
    expect(availabilityOf({ state: "supported" })).toEqual({ state: "supported", code: null, message: null, remediation: null, verified: true });
  });
  test("anything that is not a known state, or not an object, is no information", () => {
    for (const bad of [undefined, null, "supported", 3, [], {}, { state: "maybe" }, { state: "SUPPORTED" }, { state: null }, { state: 1 }]) {
      expect(availabilityOf(bad), JSON.stringify(bad)).toBeUndefined();
    }
  });
  test("a field of the wrong type is dropped, not trusted", () => {
    expect(availabilityOf({ state: "unavailable", code: 5, message: {}, remediation: [], verified: "no" }))
      .toEqual({ state: "unavailable", code: null, message: null, remediation: null, verified: true });
  });
});

test.describe("isSelectable / optionLabel", () => {
  test("only `unavailable` is not selectable; no information is selectable", () => {
    expect(isSelectable(A("supported"))).toBe(true);
    expect(isSelectable(A("requires_configuration"))).toBe(true);
    expect(isSelectable(A("unavailable"))).toBe(false);
    expect(isSelectable(undefined)).toBe(true);
  });
  test("the label names the problem, and only when there is one", () => {
    expect(optionLabel("Kafka", A("supported"))).toBe("Kafka");
    expect(optionLabel("Kafka", undefined)).toBe("Kafka");
    expect(optionLabel("Kafka", A("requires_configuration"))).toBe("Kafka — needs configuration");
    expect(optionLabel("Kafka", A("unavailable"))).toBe("Kafka — unavailable");
  });
});

test.describe("availabilityNote", () => {
  test("nothing to say when supported or unknown", () => {
    expect(availabilityNote(A("supported"))).toBeNull();
    expect(availabilityNote(undefined)).toBeNull();
  });
  test("unavailable is an error carrying the message, the remediation and the code", () => {
    expect(availabilityNote(A("unavailable", { code: "PLUGIN_NOT_INSTALLED", message: "Not installed.", remediation: "Install it." })))
      .toEqual({ tone: "error", title: "Unavailable", text: "Not installed. Install it.", code: "PLUGIN_NOT_INSTALLED" });
  });
  test("needs configuration is a warning; one that could not be CHECKED says so instead", () => {
    expect(availabilityNote(A("requires_configuration", { message: "Set it." }))).toMatchObject({ tone: "warning", title: "Needs configuration", text: "Set it." });
    expect(availabilityNote(A("requires_configuration", { verified: false, message: "Unreachable." }))).toMatchObject({ tone: "warning", title: "Could not be confirmed" });
  });
  test("a note with no message from the backend still says something sensible", () => {
    expect(availabilityNote(A("unavailable"))?.text).toBe("This option is not available.");
    expect(availabilityNote(A("requires_configuration"))?.text).toBe("This option needs configuration before it will run.");
  });
});

test.describe("ordering and defaults", () => {
  const items = [
    { id: "a", a: A("unavailable") }, { id: "b", a: A("supported") }, { id: "c", a: A("unavailable") },
    { id: "d", a: A("requires_configuration") }, { id: "e", a: undefined },
  ];
  const of = (item: { a: Availability | undefined }) => item.a;

  test("selectable first, unavailable after, each group in its own order", () => {
    expect(selectableFirst(items, of).map((i) => i.id)).toEqual(["b", "d", "e", "a", "c"]);
  });
  test("nothing is lost or duplicated", () => {
    expect(selectableFirst(items, of)).toHaveLength(items.length);
    expect(selectableFirst([], of)).toEqual([]);
  });
  test("the default is the first SUPPORTED, else the first selectable, else the first", () => {
    expect(defaultChoice(items, of)).toBe("b");
    expect(defaultChoice(items.filter((i) => i.id !== "b"), of)).toBe("d");
    expect(defaultChoice(items.filter((i) => i.a?.state === "unavailable"), of)).toBe("a");
    expect(defaultChoice([], of)).toBeUndefined();
  });
});

test.describe("qualityCapabilitiesOf", () => {
  const option = (id: string, availability: unknown) => ({ id, label: id.toUpperCase(), description: `about ${id}`, availability });

  test("reads the lists the backend sent", () => {
    const view = qualityCapabilitiesOf({
      quarantine_sinks: [option("kafka", block()), option("object_storage", block({ state: "unavailable", code: "NOT_IMPLEMENTED" }))],
      failure_actions: [option("log", block())],
    });

    expect(view.quarantine_sinks.map((o) => [o.id, o.label, o.availability.state])).toEqual([["kafka", "KAFKA", "supported"], ["object_storage", "OBJECT_STORAGE", "unavailable"]]);
    expect(view.failure_actions.map((o) => o.id)).toEqual(["log"]);
  });
  test("an option with no readable availability is not offered at all", () => {
    const view = qualityCapabilitiesOf({
      quarantine_sinks: [option("kafka", block()), option("odd", { state: "maybe" }), option("none", undefined), { label: "no id", availability: block() }, null, 7],
      failure_actions: [],
    });

    expect(view.quarantine_sinks.map((o) => o.id)).toEqual(["kafka"]);
  });
  test("a list that is missing, empty or unusable falls back to the safe set FOR THAT LIST only", () => {
    const view = qualityCapabilitiesOf({ quarantine_sinks: "nonsense", failure_actions: [option("log", block())] });

    expect(view.quarantine_sinks).toEqual(SAFE_QUALITY_CAPABILITIES.quarantine_sinks);
    expect(view.failure_actions.map((o) => o.id)).toEqual(["log"]);                           // the good list is kept
  });
  test("no answer at all gives the safe set", () => {
    for (const nothing of [undefined, null, "x", 5, [], {}]) expect(qualityCapabilitiesOf(nothing)).toEqual(SAFE_QUALITY_CAPABILITIES);
  });
  test("the safe set offers only what has always worked, all supported", () => {
    expect(SAFE_QUALITY_CAPABILITIES.quarantine_sinks.map((o) => o.id)).toEqual(["kafka"]);
    expect(SAFE_QUALITY_CAPABILITIES.failure_actions.map((o) => o.id)).toEqual(["route_to_quarantine", "log"]);
    for (const o of [...SAFE_QUALITY_CAPABILITIES.quarantine_sinks, ...SAFE_QUALITY_CAPABILITIES.failure_actions]) expect(o.availability.state).toBe("supported");
  });
  test("a label and description default sensibly when the backend omits them", () => {
    const view = qualityCapabilitiesOf({ quarantine_sinks: [{ id: "kafka", availability: block() }] });

    expect(view.quarantine_sinks[0]).toMatchObject({ id: "kafka", label: "kafka", description: "" });
  });
});
