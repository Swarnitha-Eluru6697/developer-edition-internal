/**
 * WebSocket Redux middleware.
 *
 * Lifecycle:
 *   1. Dispatch `ws/connect` with a session_id.
 *      The middleware first mints a single-use WS ticket via
 *      POST /api/v1/auth/ws-ticket (cookie-authenticated — see
 *      src/api/auth.ts / app/api/auth.py), then opens
 *      wss://<same-host>/ws/sessions/{session_id}?ticket=<ticket> using the
 *      `workspace.v1` subprotocol.
 *
 *   2. Server-sent envelopes (type: execution.queued, execution.schema,
 *      execution.rows, execution.stream, execution.error, execution.succeeded,
 *      etc) are dispatched as Redux actions with type = `ws/<envelope.type>`
 *      and payload = envelope.payload, so any slice can reduce on them.
 *
 *   3. Dispatch `ws/send` with a WSEnvelope to push a message (e.g.
 *      execute.submit, execute.cancel).
 *
 *   4. The middleware pings every 25s and reconnects with backoff on close.
 *
 * Ticket, not bearer token (WS-AUTH-010): browsers can't attach the session
 * cookie's equivalent as a custom header on a WS upgrade, so *something*
 * still has to ride the query string — but a minted ticket is single-use
 * and expires in ~30s (app/api/auth.py:mint_ws_ticket), unlike the raw JWT
 * this used to carry, which was a long-lived bearer credential sitting in
 * plain sight in every proxy/access log line for the connection.
 */
import type { Middleware, MiddlewareAPI } from "@reduxjs/toolkit";
import { createAction } from "@reduxjs/toolkit";
import type { WSEnvelope } from "./types";
import type { RootState } from "../store";
import { apiUrl } from "../api/apiBase";

// Public action creators (you dispatch these)
export const wsConnect = createAction<{ sessionId: string }>("ws/connect");
export const wsDisconnect = createAction("ws/disconnect");
export const wsSend = createAction<WSEnvelope>("ws/send");

// Connection-state actions (the middleware emits these)
export const wsOpened = createAction<{ sessionId: string }>("ws/opened");
export const wsClosed = createAction<{ code: number; reason: string }>("ws/closed");
export const wsError = createAction<{ message: string }>("ws/error");

const BACKOFF_INITIAL_MS = 500;
const BACKOFF_MAX_MS = 30_000;
const PING_INTERVAL_MS = 25_000;

async function mintWsTicket(sessionId: string): Promise<string | null> {
  try {
    const resp = await fetch(apiUrl("/api/v1/auth/ws-ticket"), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      credentials: "same-origin",
      body: JSON.stringify({ session_id: sessionId }),
    });
    if (!resp.ok) return null;
    const data = (await resp.json()) as { ticket: string };
    return data.ticket;
  } catch {
    return null;
  }
}

export const wsMiddleware: Middleware<object, RootState> = (api: MiddlewareAPI) => {
  let socket: WebSocket | null = null;
  let sessionId: string | null = null;
  let backoff = BACKOFF_INITIAL_MS;
  let pingTimer: ReturnType<typeof setInterval> | null = null;
  let reconnectTimer: ReturnType<typeof setTimeout> | null = null;
  // Guards against a slow ticket fetch racing a disconnect/reconnect that
  // happened while it was in flight.
  let connectGeneration = 0;

  const wsUrl = (sid: string, ticket: string): string => {
    const proto = window.location.protocol === "https:" ? "wss:" : "ws:";
    return `${proto}//${window.location.host}${apiUrl("/ws/sessions/")}${sid}?ticket=${encodeURIComponent(ticket)}`;
  };

  const open = async () => {
    if (!sessionId) return;
    const sid = sessionId;
    const generation = ++connectGeneration;

    const ticket = await mintWsTicket(sid);
    // sessionId may have changed (or been cleared) while the ticket request
    // was in flight — a stale ticket for an abandoned connection attempt
    // must not open a socket.
    if (generation !== connectGeneration || sessionId !== sid) return;
    if (!ticket) {
      api.dispatch(wsError({ message: "Could not obtain a WebSocket ticket" }));
      reconnectTimer = setTimeout(open, backoff);
      backoff = Math.min(backoff * 2, BACKOFF_MAX_MS);
      return;
    }

    socket = new WebSocket(wsUrl(sid, ticket), "workspace.v1");

    socket.onopen = () => {
      backoff = BACKOFF_INITIAL_MS;
      api.dispatch(wsOpened({ sessionId: sid }));
      pingTimer = setInterval(() => {
        socket?.send(JSON.stringify({ v: 1, type: "ping", ts: new Date().toISOString(), payload: {} }));
      }, PING_INTERVAL_MS);
    };

    socket.onmessage = (event) => {
      try {
        const envelope = JSON.parse(event.data) as WSEnvelope;
        if (envelope.v !== 1) return;
        // Re-dispatch as a Redux action: any slice can listen for ws/<type>.
        // Pass the whole envelope so reducers can read execution_id (top-level)
        // as well as the nested chunk payload.
        api.dispatch({ type: `ws/${envelope.type}`, payload: envelope });
      } catch (e) {
        api.dispatch(wsError({ message: `Malformed WS message: ${(e as Error).message}` }));
      }
    };

    socket.onclose = (event) => {
      if (pingTimer) { clearInterval(pingTimer); pingTimer = null; }
      api.dispatch(wsClosed({ code: event.code, reason: event.reason }));
      // Reconnect with backoff unless the close was intentional (1000)
      if (event.code !== 1000 && sessionId) {
        reconnectTimer = setTimeout(open, backoff);
        backoff = Math.min(backoff * 2, BACKOFF_MAX_MS);
      }
    };

    socket.onerror = () => {
      api.dispatch(wsError({ message: "WebSocket error" }));
    };
  };

  const close = () => {
    connectGeneration++; // invalidate any in-flight ticket fetch
    if (reconnectTimer) { clearTimeout(reconnectTimer); reconnectTimer = null; }
    if (pingTimer) { clearInterval(pingTimer); pingTimer = null; }
    if (socket && socket.readyState <= WebSocket.OPEN) {
      socket.close(1000, "client disconnect");
    }
    socket = null;
    sessionId = null;
  };

  return (next) => (action) => {
    if (wsConnect.match(action)) {
      close();
      sessionId = action.payload.sessionId;
      void open();
    } else if (wsDisconnect.match(action)) {
      close();
    } else if (wsSend.match(action)) {
      if (socket && socket.readyState === WebSocket.OPEN) {
        socket.send(JSON.stringify(action.payload));
      } else {
        api.dispatch(wsError({ message: "WebSocket not open" }));
      }
    }
    return next(action);
  };
};
