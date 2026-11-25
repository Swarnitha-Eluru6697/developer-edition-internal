import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { Provider } from "react-redux";
import { establishSessionFromUrl } from "./api/auth";
import { loadRuntimeConfig } from "./api/runtimeConfig";
import { store } from "./store";
import { App } from "./app/App";
import { ErrorBoundary } from "./app/ErrorBoundary";
import { lockHiddenScroll } from "./app/lockHiddenScroll";

lockHiddenScroll();

const container = document.getElementById("root");
if (!container) throw new Error("#root not found");

// Must resolve before the first render: App.tsx's /auth/me call needs the
// session cookie to already be set if this load carried a one-time
// #token= fragment (WS-AUTH-010). Resolves immediately when there's no token to
// exchange, so a normal reload isn't delayed.
// loadRuntimeConfig never rejects and is bounded (3 s): a missing /config.json cannot block start-up.
Promise.all([loadRuntimeConfig(), establishSessionFromUrl()]).finally(() => {
  createRoot(container).render(
    <StrictMode>
      <ErrorBoundary>
        <Provider store={store}>
          <App />
        </Provider>
      </ErrorBoundary>
    </StrictMode>,
  );
});
