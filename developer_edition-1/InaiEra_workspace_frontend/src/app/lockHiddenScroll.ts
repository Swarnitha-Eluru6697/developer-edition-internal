/**
 * Keep every `overflow: hidden` box where it is.
 *
 * `overflow: hidden` hides the scrollbars but the box can still be scrolled by the browser itself: pressing the mouse button on a page
 * and dragging past an edge (to select text) auto-scrolls the nearest such box, and the whole app slides up, down, left or right (the
 * sidebar cut off on the left, empty space on the right) with no way to get it back. Nothing in this app scrolls a hidden box on purpose,
 * so any scroll that lands on one is put back at once. Boxes that scroll (`auto` / `scroll`) are untouched, and so is the code editor,
 * which manages its own scrolling.
 *
 * The browser window itself is such a box too: `html { overflow: hidden }` (index.html) hides its scrollbars, yet the same drag scrolls
 * the whole page, and a scroll of the window arrives with the document (not an element) as its target, so that is put back as well.
 */
export function lockHiddenScroll(doc: Document = document): () => void {
  const putWindowBack = () => {
    const root = doc.documentElement;
    const view = doc.defaultView;
    if (!view) return;
    const style = view.getComputedStyle(root);
    const page = doc.scrollingElement ?? root;
    if (page.scrollLeft && style.overflowX === "hidden") page.scrollLeft = 0;
    if (page.scrollTop && style.overflowY === "hidden") page.scrollTop = 0;
  };
  const onScroll = (event: Event) => {
    const el = event.target;
    if (el === doc) return putWindowBack();
    if (!(el instanceof HTMLElement) || el.closest(".monaco-editor")) return;
    if (!el.scrollLeft && !el.scrollTop) return;
    const style = getComputedStyle(el);
    if (el.scrollLeft && style.overflowX === "hidden") el.scrollLeft = 0;
    if (el.scrollTop && style.overflowY === "hidden") el.scrollTop = 0;
  };
  // `scroll` does not bubble, so listen in the capture phase to see it on every element.
  doc.addEventListener("scroll", onScroll, true);
  return () => doc.removeEventListener("scroll", onScroll, true);
}
