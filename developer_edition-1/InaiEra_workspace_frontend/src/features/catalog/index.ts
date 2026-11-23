/**
 * Catalog feature module: database connections and catalog browsing.
 *
 * ObjectExplorer.tsx and DatabaseConnectionModal.tsx still physically live in
 * src/components/ (another engineer owns them right now); they are re-exported
 * here so callers depend on the feature's public API, not on file locations.
 */
export { ObjectExplorer } from "./components/ObjectExplorer";
export { DatabaseConnectionModal } from "./components/DatabaseConnectionModal";
export * from "./api";
