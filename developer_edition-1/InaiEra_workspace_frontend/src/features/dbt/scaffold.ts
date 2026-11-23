/**
 * Scaffold for an EMPTY dbt project (standard dbt layout): folders plus a
 * minimal dbt_project.yml, no model code. The workspace's "Add New → dbt
 * project" creates these files; the user then adds their own .sql / .yml,
 * pushes to Git and registers the repo under Jobs → dbt.
 */
import type { FileCreate } from "../../types";

/** dbt project names must be valid identifiers (lowercase, digits, underscores). */
export function normalizeDbtProjectName(raw: string): string {
  return (raw.trim() || "my_project").toLowerCase().replace(/[^a-z0-9_]+/g, "_").replace(/^_+|_+$/g, "") || "my_project";
}

const SCAFFOLD_FOLDERS = [
  "models/staging", "models/intermediate", "models/marts",
  "macros", "snapshots", "seeds", "tests", "target", "logs",
];

export function dbtProjectYml(name: string): string {
  return `name: '${name}'
version: '1.0.0'
config-version: 2

# The dbt Runner rewrites this to its generated profile at run time.
profile: '${name}'

model-paths: ["models"]
seed-paths: ["seeds"]
macro-paths: ["macros"]
snapshot-paths: ["snapshots"]
test-paths: ["tests"]

# Add your model configs here, e.g.:
# models:
#   ${name}:
#     staging:
#       +materialized: view
#     marts:
#       +materialized: table
`;
}

/** The files to create, dbt_project.yml first. Empty folders are a .gitkeep
 *  placeholder — an object store has no real directories. */
export function buildDbtScaffold(name: string): FileCreate[] {
  return [
    { path: `${name}/dbt_project.yml`, language: "yaml", content: dbtProjectYml(name) },
    ...SCAFFOLD_FOLDERS.map((dir): FileCreate => ({ path: `${name}/${dir}/.gitkeep`, language: "other", content: "" })),
  ];
}
