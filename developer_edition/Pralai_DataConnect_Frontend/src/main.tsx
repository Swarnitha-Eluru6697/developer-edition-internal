/*
 * Standalone dev entry — renders the same component the host federates in.
 * Run `npm run dev` to work on DataConnect in isolation against a local backend.
 * In dev we point at the backend directly (localStorage-overridable in Settings);
 * in production the host passes `apiBase` pointing through the tunnel.
 */
import React from "react";
import ReactDOM from "react-dom/client";
import DataConnectApp from "./DataConnectApp";

// Developer Edition: the built bundle is served on its own, with the backend
// proxied at the same origin, so production builds use a relative API base.
// (A FEDERATED build with no apiBase would show "DataConnect is not configured".)
const apiBase = import.meta.env.DEV ? undefined : "/api/v1/dataconnect";

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <DataConnectApp apiBase={apiBase} />
  </React.StrictMode>
);
