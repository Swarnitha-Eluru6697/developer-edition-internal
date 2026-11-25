/**
 * The editor's right-hand codegen column: a drag-to-resize divider plus the
 * chat panel. Width lives in uiSlice so it survives a reload (saveChatWidth).
 */
import { useCallback } from "react";
import { useDispatch, useSelector } from "react-redux";
import { CodegenChatPanel } from "./CodegenChatPanel";
import { useDragResize } from "../../../shared/hooks/useDragResize";
import { CHAT_WIDTH_DEFAULT, clampChatWidth, saveChatWidth, setChatWidth } from "../../../store/uiSlice";
import type { AppDispatch, RootState } from "../../../store";
import type { CodegenTarget, UUID } from "../../../types";

interface Props {
  workspaceId: UUID;
  activeFileId: UUID | null;
  activeFileContent: string;
  activeFileLanguage: string | null;
  execSessionId: UUID | null;
  onInsertCode: (code: string, target: CodegenTarget) => void;
  /** Reports drag state so the shell can suppress its grid transition mid-drag. */
  onResizingChange: (resizing: boolean) => void;
}

const noop = () => {};

export function CodegenChatColumn({ onResizingChange, ...panel }: Props) {
  const dispatch = useDispatch<AppDispatch>();
  const chatWidth = useSelector((s: RootState) => s.ui.chatWidth);
  // Drag the divider left of the chat to resize it (left = wider).
  const onResize = useCallback((w: number) => dispatch(setChatWidth(w)), [dispatch]);
  const compute = useCallback((start: number, delta: number) => clampChatWidth(start + delta), []);
  const onEnd = useCallback((w: number) => { onResizingChange(false); saveChatWidth(w); }, [onResizingChange]);
  const { start, resizing } = useDragResize({ axis: "x", current: chatWidth, compute, onResize, onEnd });

  return (
    <div style={{ display: "flex", minWidth: 0, minHeight: 0, overflow: "hidden" }}>
      {/* Drag to resize · double-click to reset — mirrors the results divider */}
      <div
        onMouseDown={(e) => { onResizingChange(true); start(e); }}
        onDoubleClick={() => { dispatch(setChatWidth(CHAT_WIDTH_DEFAULT)); saveChatWidth(CHAT_WIDTH_DEFAULT); }}
        title="Drag to resize · double-click to reset"
        style={{ width: 6, flexShrink: 0, cursor: "col-resize", background: resizing ? "#B7E0CB" : "#E3E8E2" }}
        onMouseEnter={(e) => (e.currentTarget.style.background = "#B7E0CB")}
        onMouseLeave={(e) => !resizing && (e.currentTarget.style.background = "#E3E8E2")}
      />
      {/* minmax(0,1fr) + minHeight:0 are load-bearing: an auto-sized row
          would let the panel grow to its content height, so its transcript
          would never scroll and older turns would be clipped out of reach. */}
      <div style={{ flex: 1, minWidth: 0, minHeight: 0, display: "grid", gridTemplateRows: "minmax(0, 1fr)", gridTemplateColumns: "minmax(0, 1fr)" }}>
        <CodegenChatPanel {...panel} onArtifactApplied={noop} />
      </div>
    </div>
  );
}
