/** Settings → codegen agent card: a holder of workspace:settings:manage (can_edit) picks the agent and enters a key; everyone else only sees which agent is active. */
import { createRoot } from "react-dom/client";
import { act, createElement } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { CodegenAgentConfig, EngineStatus } from "../../src/types";
import { pickSearchSelect, searchSelectOptions } from "../helpers/searchSelect";

const state = vi.hoisted(() => ({
  config: undefined as unknown,
  update: vi.fn(),
  verify: vi.fn(),
  refetchConfig: vi.fn(),
}));
vi.mock("../../src/features/settings/api", () => ({
  useGetCodegenAgentConfigQuery: () => ({ data: state.config, refetch: state.refetchConfig }),
  useUpdateCodegenAgentConfigMutation: () => [(b: unknown) => ({ unwrap: () => state.update(b) }), { isLoading: false }],
  useVerifyCodegenKeyMutation: () => [(b: unknown) => ({ unwrap: () => state.verify(b) }), { isLoading: false }],
}));

import { CodegenAgentCard } from "../../src/features/settings/components/CodegenAgentCard";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const engine = { key: "codegen", name: "Codegen agent (model gateway)", category: "engine", description: "d", status: "connected",
  detail: null, latency_ms: 5, endpoint: null, missing: [], impact: null } as EngineStatus;

const config = (over: Partial<CodegenAgentConfig> = {}): CodegenAgentConfig => ({
  provider: "codegen", label: "Codegen Agent (default)", model: "codegen-generation", masked_key: null, has_key: false, can_edit: true,
  status: "connected", message: "Successfully connected", ...over });

function mount() {
  const host = document.createElement("div");
  document.body.appendChild(host);
  const onRefetchEngines = vi.fn();
  act(() => createRoot(host).render(createElement(CodegenAgentCard, { engine, onRefetchEngines })));
  return { host, onRefetchEngines };
}
const select = (host: HTMLElement) => host.querySelector<HTMLButtonElement>("#codegen-agent")!;
const LABEL: Record<string, string> = { codegen: "Codegen Agent (default)", anthropic: "Anthropic key", openai: "OpenAI key" };
const keyBox = (host: HTMLElement) => host.querySelector<HTMLInputElement>("input[aria-label$='API key']");
const ok = (host: HTMLElement) => [...host.querySelectorAll("button")].find((b) => b.textContent === "OK" || b.textContent === "Verifying…")!;

async function pick(host: HTMLElement, value: string) {
  await pickSearchSelect(select(host), LABEL[value]);
}
async function type(el: HTMLInputElement, value: string) {
  await act(async () => {
    Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")!.set!.call(el, value);
    el.dispatchEvent(new Event("input", { bubbles: true }));
  });
}

beforeEach(() => {
  document.body.innerHTML = "";
  state.config = config();
  state.update = vi.fn(async () => config({ provider: "openai", has_key: true, masked_key: "sk-l••••••••WXYZ", message: "Successfully connected" }));
  state.verify = vi.fn(async () => ({ success: true, message: "Successfully connected" }));
  state.refetchConfig = vi.fn();
});

describe("codegen agent card", () => {
  it("offers the default, Anthropic and OpenAI, and keeps the key masked until the eye is clicked", async () => {
    const { host } = mount();
    expect(await searchSelectOptions(select(host))).toEqual(["Codegen Agent (default)", "Anthropic key", "OpenAI key"]);
    await act(async () => { select(host).click(); });                       // close it again
    expect(keyBox(host)).toBeNull();                                          // nothing to type for the default agent
    await pick(host, "openai");
    expect(keyBox(host)!.type).toBe("password");
    await act(async () => { host.querySelector<HTMLButtonElement>("button[title='Show key']")!.click(); });
    expect(keyBox(host)!.type).toBe("text");
  });

  it("OK stays disabled until a key is typed, then saves it and clears the box", async () => {
    const { host, onRefetchEngines } = mount();
    await pick(host, "openai");
    expect(ok(host).disabled).toBe(true);
    await type(keyBox(host)!, "  sk-live-0123456789  ");
    expect(ok(host).disabled).toBe(false);
    await act(async () => { ok(host).click(); });
    expect(state.update).toHaveBeenCalledWith({ provider: "openai", api_key: "sk-live-0123456789" });
    expect(host.querySelector('[role="status"]')!.textContent).toBe("Successfully connected");
    expect(keyBox(host)!.value).toBe("");
    expect(onRefetchEngines).toHaveBeenCalled();
    expect(state.refetchConfig).toHaveBeenCalled();
  });

  it("a provider's rejection and a permission error are shown as text, never as an object", async () => {
    const { host } = mount();
    await pick(host, "anthropic");
    await type(keyBox(host)!, "bad");
    state.update = vi.fn(async () => { throw { status: 400, data: { detail: { code: "VALIDATION_FAILED", message: "Connection failed: Anthropic rejected the API key" } } }; });
    await act(async () => { ok(host).click(); });
    expect(host.querySelector('[role="status"]')!.textContent).toBe("Connection failed: Anthropic rejected the API key");
    state.update = vi.fn(async () => { throw { status: 403, data: { detail: { code: "FORBIDDEN", message: "missing permission workspace:settings:manage" } } }; });
    await act(async () => { ok(host).click(); });
    expect(host.querySelector('[role="status"]')!.textContent).toBe("missing permission workspace:settings:manage");
  });

  it("with a key already saved, OK re-checks it instead of asking for it again, and never shows it", async () => {
    state.config = config({ provider: "openai", has_key: true, masked_key: "sk-l••••••••WXYZ" });
    const { host } = mount();
    expect(select(host).textContent).toBe("OpenAI key");
    expect(keyBox(host)!.placeholder).toContain("sk-l••••••••WXYZ");
    expect(ok(host).disabled).toBe(false);
    await act(async () => { ok(host).click(); });
    expect(state.verify).toHaveBeenCalledWith({ provider: "openai" });
    expect(state.update).not.toHaveBeenCalled();
  });

  it("another provider's saved key is not offered for reuse", async () => {
    state.config = config({ provider: "openai", has_key: true, masked_key: "sk-l••••••••WXYZ" });
    const { host } = mount();
    await pick(host, "anthropic");
    expect(keyBox(host)!.placeholder).toContain("sk-ant");
    expect(ok(host).disabled).toBe(true);
  });

  it("switching back to the default agent sends no key", async () => {
    state.config = config({ provider: "openai", has_key: true, masked_key: "sk-l••••••••WXYZ" });
    const { host } = mount();
    await pick(host, "codegen");
    await act(async () => { ok(host).click(); });
    expect(state.update).toHaveBeenCalledWith({ provider: "codegen", api_key: undefined });
  });

  it("warns that prompts leave the perimeter when an external provider is chosen", async () => {
    const { host } = mount();
    await pick(host, "openai");
    expect(host.textContent).toContain("are sent to OpenAI");
    expect(host.textContent).toContain("Switching back to the default removes the saved key");
  });

  it("someone without can_edit sees the active agent but cannot change it or see a key box", async () => {
    state.config = config({ provider: "openai", has_key: true, masked_key: null, can_edit: false });
    const { host } = mount();
    expect(select(host).disabled).toBe(true);
    expect(host.textContent).toContain("Active: OpenAI");
    expect(host.textContent).toContain("You need permission to edit Workspace settings to change the agent");
    expect(keyBox(host)).toBeNull();
    expect([...host.querySelectorAll("button")].find((b) => b.textContent === "OK")).toBeUndefined();
  });
});
