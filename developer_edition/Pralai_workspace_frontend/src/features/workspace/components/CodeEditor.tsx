/**
 * Monaco editor wrapper for SQL and notebook cells.
 *
 * The diagram's "one editor for all" promise rests on this component. Phase 1
 * exposes SQL (.sql) and notebook (.ipynb) - notebook cells render as one
 * embedded Monaco per cell in the parent notebook view; the wrapper itself
 * is language-agnostic.
 *
 * Lakehouse catalog autocomplete is wired up by registering a custom
 * CompletionItemProvider once on mount; the provider calls the workspace
 * API to fetch dataset/table/column suggestions. (Stubbed here - the real
 * implementation lives behind a `useCatalogQuery` hook.)
 */
import Editor, { OnChange, OnMount } from "@monaco-editor/react";
import { useCallback, useRef } from "react";
import type { FileLanguage } from "../../../types";

interface Props {
  language: FileLanguage;
  value: string;
  onChange: (next: string) => void;
  readOnly?: boolean;
  // Triggered by Cmd/Ctrl+Enter — preview SQL or run notebook cell.
  onRun?: () => void;
  // Triggered by Cmd/Ctrl+S — save the file to the backend.
  onSave?: () => void;
  // Reports the currently highlighted text (empty string when nothing selected)
  // so the parent can "run selection" like DBeaver/pgAdmin.
  onSelectionChange?: (selectedText: string) => void;
}

// FlinkSQL reuses Monaco's SQL grammar — the dialect differs (streaming DDL,
// WITH connector options) but the tokenizer is close enough to be useful.
const MONACO_LANGUAGE: Record<string, string> = {
  sql: "sql", flinksql: "sql", py: "python", md: "markdown", yaml: "yaml",
};

const monacoLanguage = (lang: FileLanguage): string =>
  MONACO_LANGUAGE[lang] ?? "json";

export function CodeEditor({ language, value, onChange, readOnly, onRun, onSave, onSelectionChange }: Props) {
  const onChangeRef = useRef(onChange);
  onChangeRef.current = onChange;
  const onRunRef = useRef(onRun);
  onRunRef.current = onRun;
  const onSaveRef = useRef(onSave);
  onSaveRef.current = onSave;
  const onSelRef = useRef(onSelectionChange);
  onSelRef.current = onSelectionChange;

  const handleChange: OnChange = useCallback((next) => {
    onChangeRef.current(next ?? "");
  }, []);

  const handleMount: OnMount = useCallback((editor, monaco) => {
    // Use refs so the latest handlers/state are always used (not stale closures).
    editor.addCommand(monaco.KeyMod.CtrlCmd | monaco.KeyCode.Enter, () => onRunRef.current?.());
    editor.addCommand(monaco.KeyMod.CtrlCmd | monaco.KeyCode.KeyS, () => onSaveRef.current?.());
    editor.onDidChangeCursorSelection(() => {
      const sel = editor.getSelection();
      const text = sel ? (editor.getModel()?.getValueInRange(sel) ?? "") : "";
      onSelRef.current?.(text);
    });
  }, []);

  return (
    <Editor
      height="100%"
      language={monacoLanguage(language)}
      value={value}
      onChange={handleChange}
      onMount={handleMount}
      theme="vs"
      options={{
        readOnly,
        fontSize: 13,
        fontFamily: '"JetBrains Mono", "Menlo", monospace',
        minimap: { enabled: false },
        renderWhitespace: "selection",
        scrollBeyondLastLine: false,
        smoothScrolling: true,
        tabSize: 2,
        automaticLayout: true,
      }}
    />
  );
}
