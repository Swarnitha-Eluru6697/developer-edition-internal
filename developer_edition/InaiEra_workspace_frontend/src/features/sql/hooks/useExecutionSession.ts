import { useEffect, useState } from "react";
import { useDispatch } from "react-redux";
import { useCreateExecutionSessionMutation } from "../api";
import { wsConnect, wsDisconnect } from "../../../ws/middleware";
import type { AppDispatch } from "../../../store";
import type { CurrentUser, UUID } from "../../../types";

/**
 * One execution session per active workspace, with the WebSocket connected to
 * it, so execution events (published to ws:session:{id}) reach the client.
 * Connecting up-front avoids missing early events on Run.
 */
export function useExecutionSession(me: CurrentUser | undefined, workspaceId: UUID | null): UUID | null {
  const dispatch = useDispatch<AppDispatch>();
  const [createExecutionSession] = useCreateExecutionSessionMutation();
  const [sessionId, setSessionId] = useState<UUID | null>(null);

  useEffect(() => {
    if (!me || !workspaceId) return;
    let cancelled = false;
    (async () => {
      try {
        const session = await createExecutionSession({ workspace_id: workspaceId }).unwrap();
        if (cancelled) return;
        setSessionId(session.id);
        dispatch(wsConnect({ sessionId: session.id }));
      } catch {
        /* ignore — Run will surface the error */
      }
    })();
    return () => { cancelled = true; dispatch(wsDisconnect()); };
  }, [me, workspaceId, createExecutionSession, dispatch]);

  return sessionId;
}
