/**
 * Root Redux store.
 *
 * Combines:
 *   - workspaceApi   (RTK Query reducer & middleware for HTTP API calls)
 *   - auth slice     (JWT capture / clear)
 *   - execution slice (streaming results from WS)
 *   - codegen slice  (streaming chat-driven SQL/dbt/FlinkSQL generation)
 *   - wsMiddleware   (WebSocket lifecycle + envelope -> action bridging)
 */
import { combineReducers, configureStore } from "@reduxjs/toolkit";
import { setupListeners } from "@reduxjs/toolkit/query";
// The barrel (not ../api/base) so every feature package has injected its
// endpoints before the store is created.
import { workspaceApi } from "../api/client";
import authReducer from "../api/auth";
import executionReducer from "../features/sql/executionSlice";
import codegenReducer from "../features/codegen/codegenSlice";
import uiReducer from "./uiSlice";
import { wsMiddleware } from "../ws/middleware";
import { forgetPlaceOnNavigate } from "./placeMemory";

/** Derived from the reducer map, NOT from `typeof store.getState`.
 *
 *  The store lists wsMiddleware, wsMiddleware is typed Middleware<{}, RootState>, and
 *  RootState came from the store — a cycle TypeScript reports as "Type alias 'RootState'
 *  circularly references itself", after which `store` degrades to `any` and every
 *  useSelector in the app silently loses its types. Taking RootState from the reducers
 *  breaks the loop: reducers know nothing about middleware. */
const rootReducer = combineReducers({
  [workspaceApi.reducerPath]: workspaceApi.reducer,
  auth: authReducer,
  execution: executionReducer,
  codegen: codegenReducer,
  ui: uiReducer,
});
export type RootState = ReturnType<typeof rootReducer>;

export const store = configureStore({
  reducer: rootReducer,
  middleware: (getDefault) =>
    getDefault({
      // WS messages can contain binary-ish payloads (Arrow batches in future).
      // RTK Query already serialises its own actions safely.
      serializableCheck: false,
    })
      .concat(workspaceApi.middleware)
      .concat(wsMiddleware)
      .concat(forgetPlaceOnNavigate),
});

setupListeners(store.dispatch);

export type AppDispatch = typeof store.dispatch;
