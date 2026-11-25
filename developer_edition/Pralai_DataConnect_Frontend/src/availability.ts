/*
 * NEW-09: what the backend says about whether a choice can actually be executed — a connector template, a quarantine destination, a failure action.
 *
 * Every such choice carries an `availability` with one of three states (app/core/capability.py in the backend):
 *
 *   supported               it works as offered, right now
 *   requires_configuration  implemented, but this deployment is not set up for it (or that could not be checked): selectable, and flagged
 *   unavailable             it cannot be run here, and the API refuses it: shown DISABLED, with the reason, never silently hidden
 *
 * The backend decides; the screens only render it. Nothing here guesses from a connector's name. The block is dynamic JSON, so it is validated field
 * by field (never cast): a missing or malformed one means "no information", and a choice with no information is left selectable and unflagged — the
 * backend still refuses a choice it cannot run, so not knowing never blocks a user who could proceed.
 */
import { booleanOf, isRecord, stringOf } from "./json";

export type AvailabilityState = "supported" | "requires_configuration" | "unavailable";

export interface Availability {
  state: AvailabilityState;
  /** A stable identifier to switch on (PLUGIN_NOT_INSTALLED, NOT_IMPLEMENTED, ...); null when supported. */
  code: string | null;
  message: string | null;
  /** What to do about it, when anything can be done. */
  remediation: string | null;
  /** false when the backend could not check live (Kafka Connect unreachable). */
  verified: boolean;
}

const STATES: readonly AvailabilityState[] = ["supported", "requires_configuration", "unavailable"];

function stateOf(value: unknown): AvailabilityState | undefined {
  return STATES.find((state) => state === value);
}

/** The availability block, or undefined when it is absent or not one the backend could have sent. */
export function availabilityOf(value: unknown): Availability | undefined {
  if (!isRecord(value)) return undefined;
  const state = stateOf(value.state);
  if (state === undefined) return undefined;
  return {
    state,
    code: stringOf(value.code) ?? null,
    message: stringOf(value.message) ?? null,
    remediation: stringOf(value.remediation) ?? null,
    verified: booleanOf(value.verified) ?? true,
  };
}

/** Can the user pick it? Everything except `unavailable`. No information (undefined) is selectable: the backend refuses what it cannot run. */
export function isSelectable(availability: Availability | undefined): boolean {
  return availability?.state !== "unavailable";
}

/** The option text: the name, plus what is wrong with it when something is. */
export function optionLabel(name: string, availability: Availability | undefined): string {
  if (availability?.state === "unavailable") return `${name} — unavailable`;
  if (availability?.state === "requires_configuration") return `${name} — needs configuration`;
  return name;
}

export interface AvailabilityNote { tone: "error" | "warning"; title: string; text: string; code: string | null }

/** What to say under a chosen option whose availability is not "supported"; null when there is nothing to say. */
export function availabilityNote(availability: Availability | undefined): AvailabilityNote | null {
  if (!availability || availability.state === "supported") return null;
  const unavailable = availability.state === "unavailable";
  const fallback = unavailable ? "This option is not available." : "This option needs configuration before it will run.";
  const text = [availability.message ?? fallback, availability.remediation].filter(Boolean).join(" ");
  const title = unavailable ? "Unavailable" : availability.verified ? "Needs configuration" : "Could not be confirmed";
  return { tone: unavailable ? "error" : "warning", title, text, code: availability.code };
}

/** Selectable first, then unavailable — each group in the order given — so what can be used is not buried under what cannot. */
export function selectableFirst<T>(items: readonly T[], availabilityOfItem: (item: T) => Availability | undefined): T[] {
  const usable = items.filter((item) => isSelectable(availabilityOfItem(item)));
  return [...usable, ...items.filter((item) => !isSelectable(availabilityOfItem(item)))];
}

/** The id to start on: the first template that is `supported`, else the first that is at least selectable, else the first. */
export function defaultChoice<T extends { id: string }>(items: readonly T[], availabilityOfItem: (item: T) => Availability | undefined): string | undefined {
  const supported = items.find((item) => availabilityOfItem(item)?.state === "supported");
  return (supported ?? items.find((item) => isSelectable(availabilityOfItem(item))) ?? items[0])?.id;
}

// ── Quality policy options ─────────────────────────────────────────────────────────────────────────────────────────────

export interface CapabilityOption { id: string; label: string; description: string; availability: Availability }
export interface QualityCapabilitiesView { quarantine_sinks: CapabilityOption[]; failure_actions: CapabilityOption[] }

const SUPPORTED: Availability = { state: "supported", code: null, message: null, remediation: null, verified: true };

/** What to offer until the backend has answered, or if it cannot: only what has always worked. Never an option the backend might refuse. */
export const SAFE_QUALITY_CAPABILITIES: QualityCapabilitiesView = {
  quarantine_sinks: [{ id: "kafka", label: "Kafka topic", description: "Failing records are written to the pipeline's quarantine topic.", availability: SUPPORTED }],
  failure_actions: [
    { id: "route_to_quarantine", label: "Route to quarantine", description: "Failing records are copied to the quarantine destination.", availability: SUPPORTED },
    { id: "log", label: "Log and count only", description: "Failing records are counted and logged.", availability: SUPPORTED },
  ],
};

function optionsOf(value: unknown): CapabilityOption[] {
  if (!Array.isArray(value)) return [];
  return value.flatMap((entry): CapabilityOption[] => {
    if (!isRecord(entry)) return [];
    const id = stringOf(entry.id);
    const availability = availabilityOf(entry.availability);
    if (id === undefined || availability === undefined) return [];       // an option with no readable availability is not offered at all
    return [{ id, label: stringOf(entry.label) ?? id, description: stringOf(entry.description) ?? "", availability }];
  });
}

/** The backend's quality capabilities, validated. Anything unreadable, or an empty list, falls back to the safe set for THAT list. */
export function qualityCapabilitiesOf(value: unknown): QualityCapabilitiesView {
  const record = isRecord(value) ? value : {};
  const sinks = optionsOf(record.quarantine_sinks);
  const actions = optionsOf(record.failure_actions);
  return {
    quarantine_sinks: sinks.length ? sinks : SAFE_QUALITY_CAPABILITIES.quarantine_sinks,
    failure_actions: actions.length ? actions : SAFE_QUALITY_CAPABILITIES.failure_actions,
  };
}
