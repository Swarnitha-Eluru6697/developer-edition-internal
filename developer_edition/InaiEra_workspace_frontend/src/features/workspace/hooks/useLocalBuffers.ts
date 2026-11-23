import { useCallback, useRef, useState } from "react";
import type { UUID } from "../../../types";
import {
  LOCAL_BUFFER_PREFIX, localBufferName, nextUntitledNumber, type LocalBuffer, type LocalLanguage,
} from "../localBuffers";

/** The editor's unsaved new files (see localBuffers.ts). Their text lives in the drafts, not here. */
export function useLocalBuffers() {
  const [buffers, setBuffers] = useState<Record<UUID, LocalBuffer>>({});
  // The latest buffers, readable synchronously: two files opened back to back must not pick the same number.
  const live = useRef<Record<UUID, LocalBuffer>>({});
  const ids = useRef(0);   // ids only ever grow, so a closed buffer's id is never reused (its text can't resurface)

  const create = useCallback((language: LocalLanguage): LocalBuffer => {
    const number = nextUntitledNumber(Object.values(live.current));
    ids.current += 1;
    const id: UUID = `${LOCAL_BUFFER_PREFIX}${ids.current}`;
    const buffer: LocalBuffer = { id, number, name: localBufferName(number, language), language };
    live.current = { ...live.current, [id]: buffer };
    setBuffers(live.current);
    return buffer;
  }, []);

  const remove = useCallback((id: UUID) => {
    if (!(id in live.current)) return;
    const { [id]: _gone, ...rest } = live.current;
    live.current = rest;
    setBuffers(rest);
  }, []);

  const clear = useCallback(() => { live.current = {}; setBuffers({}); }, []);

  return { buffers, create, remove, clear };
}
