/*
 * Store factory. One store per mounted <DataConnectApp> (it's a federated
 * MFE, not a page app, so a module-level singleton would leak state across
 * remounts / multiple hosts on one page).
 *
 * The tricky part: `baseUrl` isn't a build-time constant — the host
 * passes it as a prop and it can change at runtime (the standalone Settings
 * page repointing the backend). RTK Query endpoints are
 * defined once at module scope, so they can't close over per-store config
 * directly. Instead each store gets its own `extra.client` (the plain fetch
 * client from api.ts), injected as the redux-thunk extra argument and read by
 * every `queryFn` in apiSlice.ts. When the base URL changes, this factory
 * rebuilds `extra.client` and calls `dataConnectApi.util.resetApiState()` so
 * every active query refetches against the new backend. (The tenant is not
 * client config: the backend takes it from the signed ticket.)
 */
import { configureStore } from "@reduxjs/toolkit";
import { createApi as createHttpClient, type ApiOperation } from "../api";
import { dataConnectApi } from "./apiSlice";
import configReducer, { selectConfig, type ConfigState } from "./configSlice";
import notificationsReducer from "./notificationsSlice";

export interface StoreExtra {
  client: ReturnType<typeof createHttpClient>;
}

export interface CreateStoreOptions extends ConfigState {
  onOperation?: (op: ApiOperation) => void;
}

export function createDataConnectStore({ baseUrl, orgId, onOperation }: CreateStoreOptions) {
  const extra: StoreExtra = { client: createHttpClient({ baseUrl, onOperation }) };

  const store = configureStore({
    reducer: {
      config: configReducer,
      notifications: notificationsReducer,
      [dataConnectApi.reducerPath]: dataConnectApi.reducer,
    },
    preloadedState: { config: { baseUrl, orgId } },
    middleware: (getDefaultMiddleware) =>
      getDefaultMiddleware({ thunk: { extraArgument: extra } }).concat(dataConnectApi.middleware),
  });

  let prev = selectConfig(store.getState());
  store.subscribe(() => {
    const cfg = selectConfig(store.getState());
    if (cfg.baseUrl !== prev.baseUrl) {
      prev = cfg;
      extra.client = createHttpClient({ baseUrl: cfg.baseUrl, onOperation });
      store.dispatch(dataConnectApi.util.resetApiState());
    }
  });

  return store;
}

export type DataConnectStore = ReturnType<typeof createDataConnectStore>;
export type RootState = ReturnType<DataConnectStore["getState"]>;
export type AppDispatch = DataConnectStore["dispatch"];
