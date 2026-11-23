/** Pure helpers for the workspace file tree / file creation. */
import type { FileLanguage, FileTreeNode, UUID } from "../../types";

/** Editor language for a file name. *.flink.sql is checked before .sql, which
 *  would otherwise swallow it. */
export function languageFromFileName(name: string): FileLanguage {
  const lower = name.toLowerCase();
  if (lower.endsWith(".flink.sql")) return "flinksql";
  const ext = lower.includes(".") ? lower.split(".").pop() ?? "" : "";
  switch (ext) {
    case "sql": return "sql";
    case "yml":
    case "yaml": return "yaml";
    case "md": return "md";
    case "py": return "py";
    case "json": return "json";
    case "ipynb": return "ipynb";
    default: return "other";
  }
}

/** Icon (a Tabler icon class) and colour for a file, chosen by its name so the Files panel and the
 *  editor tabs always agree. Falls back to the stored language, then to a plain file. */
export interface FileIcon { icon: string; color: string }

const ICON_BY_EXTENSION: Record<string, FileIcon> = {
  sql: { icon: "ti-file-type-sql", color: "#F97316" },
  md: { icon: "ti-markdown", color: "#F97316" },
  markdown: { icon: "ti-markdown", color: "#F97316" },
  yml: { icon: "ti-file-settings", color: "#F97316" },
  yaml: { icon: "ti-file-settings", color: "#F97316" },
  py: { icon: "ti-brand-python", color: "#F97316" },
  ipynb: { icon: "ti-notebook", color: "#F97316" },
  json: { icon: "ti-braces", color: "#F97316" },
  csv: { icon: "ti-file-type-csv", color: "#F97316" },
  xml: { icon: "ti-file-type-xml", color: "#F97316" },
  txt: { icon: "ti-file-type-txt", color: "#F97316" },
};
const FLINKSQL_ICON: FileIcon = { icon: "ti-wave-sine", color: "#F97316" };
const PLAIN_ICON: FileIcon = { icon: "ti-file", color: "#F97316" };

export function fileIcon(name: string, language?: string | null): FileIcon {
  const lower = name.toLowerCase();
  if (lower.endsWith(".flink.sql") || language === "flinksql") return FLINKSQL_ICON;
  const ext = lower.includes(".") ? lower.split(".").pop() ?? "" : "";
  return ICON_BY_EXTENSION[ext] ?? ICON_BY_EXTENSION[language ?? ""] ?? PLAIN_ICON;
}

/** Depth-first search for a *file* node by its exact path. */
export function findNodeByPath(node: FileTreeNode | undefined, path: string): FileTreeNode | undefined {
  if (!node) return undefined;
  if (node.path === path && node.type === "file") return node;
  for (const c of node.children ?? []) {
    const hit = findNodeByPath(c, path);
    if (hit) return hit;
  }
  return undefined;
}

/** Ids of every file under `node` (a folder exists only because files live
 *  under its path, so deleting one means deleting all of these). */
export function collectFileIds(node: FileTreeNode): UUID[] {
  const ids: UUID[] = [];
  const gather = (n: FileTreeNode) => {
    if (n.type === "file" && n.id) ids.push(n.id);
    for (const c of n.children ?? []) gather(c);
  };
  gather(node);
  return ids;
}

/** id -> path of every file in the tree, so an open tab can be labelled with its file's name. */
export function collectFilePaths(node: FileTreeNode | undefined): Map<UUID, string> {
  const paths = new Map<UUID, string>();
  const gather = (n: FileTreeNode) => {
    if (n.type === "file" && n.id) paths.set(n.id, n.path);
    for (const c of n.children ?? []) gather(c);
  };
  if (node) gather(node);
  return paths;
}

/** "scorecard/README.md" -> "README.md". */
export const baseName = (path: string): string => path.split("/").filter(Boolean).pop() ?? path;

/** What the Files panel shows: a placeholder while the tree loads, the tree, or — only once it has
 *  loaded and there is nothing — the "empty workspace" prompt (it used to flash on every refresh). */
export type FileTreeView = "loading" | "tree" | "empty";

export function fileTreeView(tree: FileTreeNode | undefined, isLoading: boolean): FileTreeView {
  if (tree) return "tree";
  return isLoading ? "loading" : "empty";
}

/** Minimal valid nbformat JSON — the backend validator rejects anything else for .ipynb. */
export function emptyNotebook(withCell: boolean): string {
  return JSON.stringify({
    nbformat: 4, nbformat_minor: 5, metadata: {},
    cells: withCell ? [{ cell_type: "code", metadata: {}, execution_count: null, outputs: [], source: [] }] : [],
  }, null, 1);
}
