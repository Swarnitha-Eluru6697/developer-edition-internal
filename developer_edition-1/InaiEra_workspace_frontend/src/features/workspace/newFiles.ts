/** What "Add New" can create, and how a new file is named and seeded. */
import type { Permission } from "../../rbac/permissions";
import { FLINKSQL_TEMPLATE } from "../flink";
import { emptyNotebook } from "./fileTree";

// File kinds the "Add New" menu can create. dbt is not here — it scaffolds a
// folder tree, not a single file (see useFileActions.newDbtProject).
export type NewFileLanguage = "sql" | "ipynb" | "yaml" | "flinksql" | "py";

export interface NewFileOption {
  label: string;
  hint: string;
  icon: string;
  color: string;
  kind: NewFileLanguage | "dbt";
  /** The permission the option needs ON TOP of workspace:files:edit (which every option needs: it creates a file). SQL and
   *  YAML need nothing more — writing SQL is not running it. */
  permission?: Permission;
}

export const NEW_FILE_OPTIONS: NewFileOption[] = [
  { label: "SQL file", hint: ".sql · runs against the lakehouse", icon: "ti-file-type-sql", color: "#F97316", kind: "sql" },
  { label: "Python file", hint: ".py · runs on a notebook kernel", icon: "ti-brand-python", color: "#F97316", kind: "py", permission: "workspace:notebooks:run" },
  { label: "FlinkSQL file", hint: ".flink.sql · streaming job definition", icon: "ti-wave-sine", color: "#F97316", kind: "flinksql", permission: "workspace:flink:edit" },
  { label: "Notebook", hint: ".ipynb · cells with rich output", icon: "ti-notebook", color: "#F97316", kind: "ipynb", permission: "workspace:notebooks:run" },
  { label: "dbt project", hint: "scaffolds the standard dbt folders", icon: "ti-brand-databricks", color: "#F97316", kind: "dbt", permission: "workspace:dbt:edit" },
];

const EXTENSIONS: Record<NewFileLanguage, string> = {
  sql: ".sql", ipynb: ".ipynb", yaml: ".yml", flinksql: ".flink.sql", py: ".py",
};

/** Scratch name for a new file. FlinkSQL uses *.flink.sql so a Git round-trip
 *  keeps it distinguishable from lakehouse SQL (_language_for in app/api/git.py). */
export function untitledFileName(language: NewFileLanguage, now: number = Date.now()): string {
  return `untitled${now}${EXTENSIONS[language]}`;
}

/** Starting content: valid nbformat for notebooks (the backend validates it),
 *  the instruction header for FlinkSQL, empty otherwise. */
export function seedContent(language: NewFileLanguage): string {
  if (language === "ipynb") return emptyNotebook(true);
  if (language === "flinksql") return FLINKSQL_TEMPLATE;
  return "";
}

/** Untitled scratch files are "saved as" a real name instead of updated in place. */
/** The "Add New" options a holder of `permissions` may use: none at all without workspace:files:edit. */
export function allowedNewFileOptions(permissions: readonly string[]): NewFileOption[] {
  if (!permissions.includes("workspace:files:edit")) return [];
  return NEW_FILE_OPTIONS.filter(({ permission }) => !permission || permissions.includes(permission));
}

export const isUntitledPath = (path: string) => /^untitled\d+/.test(path);

/** Suggested "Save as" name for an untitled scratch file. */
export const suggestedSaveName = (path: string) => path.replace(/^untitled\d+/, "query");
