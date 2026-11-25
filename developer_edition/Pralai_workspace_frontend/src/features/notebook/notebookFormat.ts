/** nbformat (.ipynb) <-> notebook cell model, plus Jupyter output helpers. */

export type CellLang = "sql" | "python";

export interface NbOutput {
  output_type: string; // 'stream' | 'display_data' | 'execute_result' | 'error'
  payload: Record<string, unknown>;
}

export interface Cell {
  id: string;
  language: CellLang;
  source: string;
  status?: string;
  // SQL result
  columns?: { name: string; type: string }[];
  rows?: unknown[][];
  // Python result
  outputs?: NbOutput[];
  error?: string | null;
  running?: boolean;
  ms?: number;
}

export const newCellId = () => "c" + Math.random().toString(36).slice(2, 9);

interface IpynbCell { source?: string | string[] }

// Notebooks are Python-only (Jupyter-style); SQL lives in the SQL editor.
export function parseCells(content: string): Cell[] {
  try {
    const nb = JSON.parse(content) as { cells?: IpynbCell[] } | null;
    if (nb && Array.isArray(nb.cells) && nb.cells.length) {
      return nb.cells.map((c) => ({
        id: newCellId(),
        language: "python",
        source: Array.isArray(c.source) ? c.source.join("") : (c.source || ""),
      }));
    }
  } catch {
    /* not ipynb JSON — fall through */
  }
  return [{ id: newCellId(), language: "python", source: content && !content.trim().startsWith("{") ? content : "" }];
}

export function toIpynb(cells: Cell[]): string {
  return JSON.stringify(
    {
      nbformat: 4,
      nbformat_minor: 5,
      metadata: {
        kernelspec: { name: "python3", display_name: "Python 3", language: "python" },
        language_info: { name: "python" },
      },
      cells: cells.map((c) => ({
        cell_type: "code",
        metadata: { language: "python" },
        execution_count: null,
        outputs: [],
        source: c.source.split(/(?<=\n)/),
      })),
    },
    null,
    1,
  );
}

// ESC (0x1b) built at runtime so the regex literal carries no control character.
const ANSI_SGR = new RegExp(`${String.fromCharCode(27)}\\[[0-9;]*m`, "g");

/** Strip ANSI colour codes (Jupyter tracebacks are coloured). */
export const stripAnsi = (s: string) => s.replace(ANSI_SGR, "");

/** A mime-bundle entry as text — Jupyter sends multi-line values as string[]. */
export function mimeText(bundle: Record<string, unknown> | undefined, key: string): string {
  const v = bundle?.[key];
  return Array.isArray(v) ? v.join("") : typeof v === "string" ? v : "";
}
