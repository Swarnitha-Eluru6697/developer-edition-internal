import { defineConfig } from "vitest/config";

export default defineConfig({
  test: {
    environment: "node",
    // Route/service files under test import ../db/connection (a real
    // Pool) at module load time even for pure functions living in the
    // same file — these tests exercise only the pure exports, so a real
    // Postgres connection is never actually needed, but the import
    // itself must not throw. connection.ts only connects lazily on
    // first query, so this is safe without a live database.
  },
});
