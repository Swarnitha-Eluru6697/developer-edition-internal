import { announceSearchCleared } from "../searchEvents";
import IconAction from "./IconAction";

/** What the top-bar search is filtering this page to, with a way out. Pages carry no search
 *  box of their own — the one in the top bar is the only one — so this is where a person
 *  sees the filter is on and clears it. */
export function SearchNote({ term, onClear }: { term: string; onClear: () => void }) {
  if (!term.trim()) return null;
  return (
    <span className="search-note" role="status">
      Results for “<b>{term.trim()}</b>”
      <IconAction icon="x" label="Clear search" size="sm" onClick={() => { onClear(); announceSearchCleared(); }} />
    </span>
  );
}
