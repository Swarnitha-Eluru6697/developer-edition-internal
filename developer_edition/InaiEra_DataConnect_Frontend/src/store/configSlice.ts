/*
 * Backend connection config (API base URL) and the caller's tenant. Shared, small, and genuinely global — every RTK
 * Query endpoint's client is rebuilt off of the base URL (see store.ts) — so it lives in Redux rather than local
 * component state. `orgId` is the tenant GET /me reported (Envoy's x-tenant-id): shown in the UI and used to scope
 * local caches, never sent to the backend — the backend takes the tenant from the signed ticket.
 */
import { createSlice, type PayloadAction } from "@reduxjs/toolkit";

export interface ConfigState {
  baseUrl: string;
  orgId: string;
}

const configSlice = createSlice({
  name: "config",
  initialState: { baseUrl: "", orgId: "" } as ConfigState,
  reducers: {
    setConfig(state, action: PayloadAction<{ baseUrl?: string; orgId?: string }>) {
      if (action.payload.baseUrl != null) state.baseUrl = action.payload.baseUrl;
      if (action.payload.orgId != null) state.orgId = action.payload.orgId;
    },
  },
});

export const { setConfig } = configSlice.actions;
export const selectConfig = (state: { config: ConfigState }) => state.config;
export default configSlice.reducer;
