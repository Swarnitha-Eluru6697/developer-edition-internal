/**
 * Settings feature package: live status of every engine / service the
 * Workspace depends on (GET /api/v1/settings/engines — app/api/settings.py).
 */
import { workspaceApi } from "../../api/base";
import type {
  EnginesOut,
  CodegenAgentConfig,
  VerifyKeyRequest,
  VerifyKeyResponse,
  UpdateCodegenAgentRequest,
} from "../../types";

export const settingsFeatureApi = workspaceApi.injectEndpoints({
  endpoints: (builder) => ({
    getEngines: builder.query<EnginesOut, void>({
      query: () => "/settings/engines",
      providesTags: ["Settings"],
    }),
    getCodegenAgentConfig: builder.query<CodegenAgentConfig, void>({
      query: () => "/settings/codegen/agent",
      providesTags: ["SettingsCodegen"],
    }),
    verifyCodegenKey: builder.mutation<VerifyKeyResponse, VerifyKeyRequest>({
      query: (body) => ({
        url: "/settings/codegen/verify",
        method: "POST",
        body,
      }),
    }),
    updateCodegenAgentConfig: builder.mutation<CodegenAgentConfig, UpdateCodegenAgentRequest>({
      query: (body) => ({
        url: "/settings/codegen/agent",
        method: "PUT",
        body,
      }),
      invalidatesTags: ["Settings", "SettingsCodegen"],
    }),
  }),
});

export const {
  useGetEnginesQuery,
  useGetCodegenAgentConfigQuery,
  useVerifyCodegenKeyMutation,
  useUpdateCodegenAgentConfigMutation,
} = settingsFeatureApi;
