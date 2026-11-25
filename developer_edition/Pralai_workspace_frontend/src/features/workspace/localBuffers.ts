/** New files that exist only in the editor until they are saved ("Untitled-1.sql"), like a desktop
 *  editor: nothing is created on the server when you click "SQL file", "Notebook", … — only when you save.
 *
 *  They live in the editor's tabs under an id with a reserved prefix. Every kind the Add New menu offers
 *  works this way, notebooks included: a SQL, Python or notebook-cell run does not need a saved file. */
import type { FileWithContent, UUID } from "../../types";
import type { NewFileLanguage } from "./newFiles";

export const LOCAL_BUFFER_PREFIX = "local:";

export type LocalLanguage = NewFileLanguage;

export const isLocalBufferId = (id: string | null | undefined): boolean => !!id && id.startsWith(LOCAL_BUFFER_PREFIX);

const EXTENSION: Record<LocalLanguage, string> = { sql: ".sql", flinksql: ".flink.sql", ipynb: ".ipynb", py: ".py", yaml: ".yml" };

const SAVE_STEM: Record<LocalLanguage, string> = { sql: "query", flinksql: "query", ipynb: "notebook", py: "script", yaml: "config" };

/** "Untitled-3.sql" — the number is the lowest one not used by a buffer that is open right now, so
 *  closing "Untitled-1" and opening another gives "Untitled-1" again (the id never repeats). */
export const localBufferName = (n: number, language: LocalLanguage): string => `Untitled-${n}${EXTENSION[language]}`;

/** Default name offered by "Save": "query.sql", "notebook.ipynb", "script.py", … */
export const localBufferSaveName = (language: LocalLanguage): string => `${SAVE_STEM[language]}${EXTENSION[language]}`;

export interface LocalBuffer {
  id: UUID;
  /** The N of "Untitled-N", unique among the open buffers. */
  number: number;
  name: string;
  language: LocalLanguage;
}

/** Lowest N >= 1 that no open buffer is using. */
export function nextUntitledNumber(open: Iterable<Pick<LocalBuffer, "number">>): number {
  const used = new Set([...open].map((b) => b.number));
  let n = 1;
  while (used.has(n)) n += 1;
  return n;
}

/** The editor's view of a buffer as a file, so the rest of the editor treats it like any other. */
export function localBufferFile(buffer: LocalBuffer, workspaceId: UUID, content: string): FileWithContent {
  const now = new Date(0).toISOString();
  return {
    id: buffer.id, workspace_id: workspaceId, path: buffer.name, language: buffer.language, content,
    sha256: null, branch: "main", is_dirty: false, last_committed_sha: null,
    size_bytes: content.length, created_at: now, updated_at: now,
  };
}
