/** The refresh button looks like the browser's reload button and answers a click with one quick blink: no spinning, no waiting. */
import { createRoot } from "react-dom/client";
import { act, createElement, StrictMode } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { ACTION_ICON, IconAction } from "../../src/shared/ui/IconAction";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

function mount(props: Partial<Parameters<typeof IconAction>[0]> = {}, strict = false) {
  const host = document.createElement("div");
  document.body.appendChild(host);
  const element = createElement(IconAction, { icon: ACTION_ICON.refresh, label: "Refresh", onClick: () => {}, ...props });
  act(() => createRoot(host).render(strict ? createElement(StrictMode, null, element) : element));
  const button = () => host.querySelector("button")!;
  const glyph = () => button().querySelector("svg, i")!;
  return { host, button, glyph, blinking: () => !!button().querySelector(".icon-action-blink"), click: () => act(() => button().click()) };
}

beforeEach(() => { document.body.innerHTML = ""; });

describe("refresh button", () => {
  it("draws the browser's reload arrow: one circular arrow, not the two-arrow sync icon", () => {
    expect(ACTION_ICON.refresh).toBe("pi-reload");
    const t = mount();
    expect(t.host.querySelectorAll("svg path")).toHaveLength(2);                 // the circle and the arrowhead
    expect(t.host.querySelector("svg path")!.getAttribute("d")).toContain("a8 8 0 1 1");
  });

  it("is still at rest until clicked, then blinks", () => {
    const t = mount();
    expect(t.blinking()).toBe(false);
    t.click();
    expect(t.blinking()).toBe(true);
  });

  it("blinks again on every click (a new glyph replays the one-shot animation)", () => {
    const t = mount();
    t.click();
    const first = t.glyph();
    t.click();
    expect(t.glyph()).not.toBe(first);
    expect(t.blinking()).toBe(true);
  });

  it("never waits for the refresh: a click that returns a pending promise does not change anything but the blink", () => {
    const t = mount({ onClick: () => new Promise(() => {}) });
    t.click();
    expect(t.button().className).toBe("icon-action");                            // no spinning / busy state
    expect(t.button().disabled).toBe(false);
  });

  it("works in StrictMode and still runs the click handler", () => {
    const onClick = vi.fn();
    const t = mount({ onClick }, true);
    t.click();
    expect(onClick).toHaveBeenCalledTimes(1);
    expect(t.blinking()).toBe(true);
  });

  it("other icons never blink", () => {
    const t = mount({ icon: ACTION_ICON.delete });
    t.click();
    expect(t.blinking()).toBe(false);
  });
});
