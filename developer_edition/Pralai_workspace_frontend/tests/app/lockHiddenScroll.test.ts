/**
 * Dragging the mouse past an edge scrolls an `overflow: hidden` box and slides the whole app out of place. The guard puts any such scroll
 * back, and leaves boxes that really scroll (and the code editor) alone.
 */
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { lockHiddenScroll } from "../../src/app/lockHiddenScroll";

/** jsdom never scrolls: give the element a scroll position the test can set, like a browser dragging a selection would. */
function scrollable(el: HTMLElement) {
  let left = 0;
  let top = 0;
  Object.defineProperty(el, "scrollLeft", { get: () => left, set: (v: number) => { left = v; }, configurable: true });
  Object.defineProperty(el, "scrollTop", { get: () => top, set: (v: number) => { top = v; }, configurable: true });
  return el;
}
function box(overflow: string, parent: HTMLElement = document.body) {
  const el = scrollable(document.createElement("div"));
  el.style.overflowX = overflow;
  el.style.overflowY = overflow;
  parent.appendChild(el);
  return el;
}
const dragScroll = (el: HTMLElement, left: number, top: number) => {
  el.scrollLeft = left;
  el.scrollTop = top;
  el.dispatchEvent(new Event("scroll"));                       // the event does not bubble: only the capture listener sees it
};

let stop: () => void;
beforeEach(() => { document.body.innerHTML = ""; stop = lockHiddenScroll(); });
afterEach(() => stop());

describe("lockHiddenScroll", () => {
  it("puts a hidden box back where it was, sideways and up/down", () => {
    const el = box("hidden");
    dragScroll(el, 120, 80);
    expect([el.scrollLeft, el.scrollTop]).toEqual([0, 0]);
  });

  it("only undoes the direction that is hidden", () => {
    const el = box("hidden");
    el.style.overflowY = "auto";
    dragScroll(el, 50, 70);
    expect([el.scrollLeft, el.scrollTop]).toEqual([0, 70]);
  });

  it("leaves a box that scrolls alone", () => {
    for (const overflow of ["auto", "scroll"]) {
      const el = box(overflow);
      dragScroll(el, 30, 40);
      expect([el.scrollLeft, el.scrollTop]).toEqual([30, 40]);
    }
  });

  it("leaves the code editor to scroll itself", () => {
    const editor = document.createElement("div");
    editor.className = "monaco-editor";
    document.body.appendChild(editor);
    const inner = box("hidden", editor);
    dragScroll(inner, 60, 90);
    expect([inner.scrollLeft, inner.scrollTop]).toEqual([60, 90]);
  });

  it("stops guarding once it is switched off", () => {
    const el = box("hidden");
    stop();
    dragScroll(el, 10, 10);
    expect([el.scrollLeft, el.scrollTop]).toEqual([10, 10]);
    stop = lockHiddenScroll();
  });
});

describe("lockHiddenScroll: the browser window", () => {
  const setRootOverflow = (value: string) => {
    document.documentElement.style.overflowX = value;
    document.documentElement.style.overflowY = value;
  };
  const scrollPage = (left: number, top: number) => {
    const page = scrollable(document.documentElement);        // jsdom has no scrollingElement: a browser's is the <html> element
    Object.defineProperty(document, "scrollingElement", { value: page, configurable: true });
    page.scrollLeft = left;
    page.scrollTop = top;
    document.dispatchEvent(new Event("scroll"));               // a window scroll has the document itself as its target
    return page;
  };
  afterEach(() => {
    setRootOverflow("");
    Reflect.deleteProperty(document, "scrollingElement");
  });

  it("puts the window back when the page is hidden", () => {
    setRootOverflow("hidden");
    const page = scrollPage(40, 90);
    expect([page.scrollLeft, page.scrollTop]).toEqual([0, 0]);
  });

  it("lets a page that scrolls (the error screens) scroll", () => {
    setRootOverflow("auto");
    const page = scrollPage(40, 90);
    expect([page.scrollLeft, page.scrollTop]).toEqual([40, 90]);
  });
});
