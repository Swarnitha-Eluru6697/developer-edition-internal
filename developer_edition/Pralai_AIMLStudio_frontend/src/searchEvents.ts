/** A page cleared the top-bar search's filter; the top bar empties its box to match. */
export const SEARCH_CLEARED = "inaiera:search-cleared";
export const announceSearchCleared = () => window.dispatchEvent(new Event(SEARCH_CLEARED));
