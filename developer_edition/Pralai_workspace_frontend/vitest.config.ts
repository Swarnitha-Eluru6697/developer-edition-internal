// Separate from vite.config.ts / vite.config.docker.ts on purpose: those two
// are the dev-server and Docker-dev configs (proxying to the backend, dev
// token redirect) and neither should gain test-runner concerns. `vite build`
// never reads this file, and `vitest` never reads the other two.
import { defineConfig } from "vitest/config";

export default defineConfig({
  test: {
    environment: "jsdom",
    // Per-feature specs live in tests/features/<feature>.test.ts; shared test
    // helpers in tests/helpers/. tests/ is type-checked (tsconfig.test.json)
    // and linted (`npm run lint`) like src/.
    include: ["tests/**/*.test.ts", "tests/**/*.test.tsx"],
    // Clears sessionStorage after every test: screens remember their place there.
    setupFiles: ["tests/setup.ts"],
  },
});
