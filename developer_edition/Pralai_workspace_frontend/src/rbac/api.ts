/**
 * GET /api/v1/me — who the caller is and every permission they hold (platform permission contract, section 5.4). This is THE
 * source of permissions for the UI. Any gateway-verified caller may ask (no Workspace permission needed), so a user without
 * `workspace:access:view` gets an answer that says so instead of a 403.
 *
 * Goes through the shared RTK Query instance, so the runtime API base (the portal's tunnel path when embedded) applies.
 */
import { workspaceApi } from "../api/base";
import type { MyPermissionsResponse } from "../types";

export const permissionsApi = workspaceApi.injectEndpoints({
  endpoints: (builder) => ({
    getMyPermissions: builder.query<MyPermissionsResponse, void>({
      query: () => "/me",
      providesTags: ["Me"],
    }),
  }),
});

export const { useGetMyPermissionsQuery } = permissionsApi;
