import js from "@eslint/js";
import globals from "globals";
import reactHooks from "eslint-plugin-react-hooks";
import reactRefresh from "eslint-plugin-react-refresh";
import tseslint from "typescript-eslint";

/*
 * Flat config (ESLint 9+). Type-aware correctness is already covered by
 * `npm run typecheck` (tsc --noEmit, strict) — this config's job is the
 * things tsc doesn't check: unused vars/imports, React Hooks rules, and
 * fast-refresh-breaking exports, not a second type checker.
 */
export default tseslint.config(
  { ignores: ["dist", "node_modules", "test-results", "playwright-report"] },
  {
    files: ["**/*.{ts,tsx}"],
    extends: [js.configs.recommended, ...tseslint.configs.recommended],
    languageOptions: {
      ecmaVersion: 2020,
      // Both browser (app code, tests/e2e's in-page assertions) and Node
      // (tests/unit, playwright.config.ts, vite.config.ts) globals apply
      // across this one project — the overlap is harmless.
      globals: { ...globals.browser, ...globals.node },
    },
    plugins: {
      "react-hooks": reactHooks,
      "react-refresh": reactRefresh,
    },
    rules: {
      // Only the two long-stable hook-correctness rules, not
      // eslint-plugin-react-hooks@7's full "recommended" — that config now
      // bundles the newer React Compiler rule family (set-state-in-effect,
      // refs, immutability, purity, ...), which assumes code written against
      // React Compiler's stricter rules and flags several idiomatic,
      // correct patterns already in this codebase (e.g. AppContext.tsx's
      // lazy `if (!ref.current) ref.current = ...` singleton-store
      // initialization). Adopting that family is a real, separate migration
      // decision, not something a "make CI's lint step exist" pass should
      // force through.
      "react-hooks/rules-of-hooks": "error",
      "react-hooks/exhaustive-deps": "warn",
      "react-refresh/only-export-components": ["warn", { allowConstantExport: true }],
      // FE-GAP-02: no explicit `any`. A value the backend leaves as free-form JSON is `unknown` (or `Record<string, unknown>`) at the
      // boundary and is validated into a typed view model before the UI uses it (src/json.ts, templateAdapters.ts, qualityRules.ts,
      // schemaFields.ts, transformSteps.ts, connectionConfig.ts, pipelineAdapters.ts). Genuine contracts come from the generated OpenAPI
      // types (src/types/api.ts). If something truly cannot be typed, say why in a `// eslint-disable-next-line` comment at that line.
      "@typescript-eslint/no-explicit-any": "error",
      "@typescript-eslint/no-unused-vars": ["warn", { argsIgnorePattern: "^_", varsIgnorePattern: "^_" }],
      // `cond && cond()` as a statement is this codebase's established idiom
      // for "best-effort, never let this throw" side effects (api.ts's
      // report(), AppContext.tsx's stableOnOperation) — allow it rather than
      // forcing every call site into an `if` statement. typescript-eslint's
      // `recommended` config turns off the base no-unused-expressions in
      // favor of its own TS-aware version, so that's the one to configure.
      "@typescript-eslint/no-unused-expressions": ["error", { allowShortCircuit: true, allowTernary: true }],
      // FE-01: no raw-HTML injection. Icons and copy are React elements; an HTML string would have to be trusted to be safe.
      "no-restricted-syntax": ["error", { selector: "JSXAttribute[name.name='dangerouslySetInnerHTML']", message: "Render React elements instead of raw HTML (FE-01)." }],
    },
  },
  {
    // AppContext.tsx colocates its `useApp()` hook with the `AppProvider`
    // component it reads from — the conventional React context shape, and
    // not something splitting into a second file would meaningfully
    // improve. `allowExportNames` tells the fast-refresh rule about that
    // one non-component export instead of it warning on every edit to this
    // file.
    files: ["src/context/AppContext.tsx"],
    rules: {
      "react-refresh/only-export-components": ["warn", { allowConstantExport: true, allowExportNames: ["useApp"] }],
    },
  },
);
