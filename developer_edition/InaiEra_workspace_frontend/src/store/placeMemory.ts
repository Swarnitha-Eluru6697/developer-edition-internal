/**
 * A refresh keeps your place inside a page (the job you had open, its tab: see src/shared/usePersistedState.ts); going to a page from the
 * sidebar, the search or a shortcut does NOT: it opens on its list, as it always did, so every page looks the same when you arrive.
 * Without this the SQL page came back inside the job you last opened, while the dbt and Python pages (where none was open) showed lists.
 *
 * A middleware, so the remembered place is dropped BEFORE the new page mounts and reads it. Opening the page you are already on changes
 * nothing, and a refresh dispatches nothing, so it still finds its place.
 */
import type { Middleware } from "@reduxjs/toolkit";
import { forgetPersisted } from "../shared/usePersistedState";
import { setNavSection, type NavSection } from "./uiSlice";

/** The storage prefix of the inner place of each page that has one (Workspace keeps its open files, as the editor always did). */
export const PLACE_PREFIX: Partial<Record<NavSection, string>> = {
  dbt: "jobs.dbt.", sqljobs: "jobs.sql.", pythonjobs: "jobs.python.", flink: "flink.", dq: "dq.", alerts: "alerts.",
};

// Typed with only the state it reads (see store/index.ts on why RootState cannot be named here).
export const forgetPlaceOnNavigate: Middleware<object, { ui: { navSection: NavSection } }> = (api) => (next) => (action) => {
  if (setNavSection.match(action) && action.payload !== api.getState().ui.navSection) {
    const prefix = PLACE_PREFIX[action.payload];
    if (prefix) forgetPersisted(prefix);
  }
  return next(action);
};
