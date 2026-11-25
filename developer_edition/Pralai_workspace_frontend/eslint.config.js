import js from "@eslint/js";
import globals from "globals";
import reactHooks from "eslint-plugin-react-hooks";
import reactRefresh from "eslint-plugin-react-refresh";
import tseslint from "typescript-eslint";

export default tseslint.config(
  // src/api/generated is openapi-typescript output (`npm run codegen:api`).
  { ignores: ["dist", "src/api/generated"] },
  {
    extends: [js.configs.recommended, ...tseslint.configs.recommended],
    files: ["**/*.{ts,tsx}"],
    languageOptions: {
      ecmaVersion: 2020,
      globals: globals.browser,
    },
    // A stale `// eslint-disable-next-line no-console` in src/main.tsx predates
    // this config (no-console isn't enabled here).
    linterOptions: {
      reportUnusedDisableDirectives: "off",
    },
    plugins: {
      "react-hooks": reactHooks,
      "react-refresh": reactRefresh,
    },
    rules: {
      ...reactHooks.configs.recommended.rules,
      // The former "debt" baseline is gone (WS-FE-014): every rule below is a
      // hard error across src/ and tests/.
      "@typescript-eslint/no-explicit-any": "error",
      "@typescript-eslint/no-unused-vars": ["error", { argsIgnorePattern: "^_", varsIgnorePattern: "^_", caughtErrors: "none" }],
      "@typescript-eslint/no-empty-object-type": "error",
      "no-control-regex": "error",
      "react-hooks/exhaustive-deps": "error",
      "react-refresh/only-export-components": "error",
    },
  },
  {
    // The shared InAiEra dropdown is copied verbatim from the other apps
    // (Agents / Catalog / ML Studio) and exports one constant beside the component.
    files: ["src/shared/ui/SearchSelect.tsx"],
    rules: { "react-refresh/only-export-components": ["error", { allowConstantExport: true }] },
  },
);
