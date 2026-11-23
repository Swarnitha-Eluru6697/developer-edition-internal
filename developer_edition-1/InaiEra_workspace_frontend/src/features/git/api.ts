/**
 * Git feature package (WS-FE-014): repo binding, OAuth connect, branches,
 * commit/push/pull, and pull requests.
 */
import { workspaceApi } from "../../api/base";
import type {
  BranchInfo,
  CommitOut,
  CommitRequest,
  GitActionResult,
  GitBinding,
  GitStatus,
  OAuthInitiate,
  PullRequestCreate,
  PullRequestOut,
  RepoListItem,
  UUID,
  WorkspaceFromGitCreate,
  WorkspaceFromGitResult,
} from "../../types";

export interface GitConnection {
  provider: string;
  /** An OAuth app is configured for this provider on this deployment. */
  oauth_available: boolean;
  /** The user has a stored credential (OAuth token or PAT) for it. */
  connected: boolean;
  username: string | null;
  instance_url: string | null;
  expires_at: string | null;
  expired: boolean;
}

export const gitFeatureApi = workspaceApi.injectEndpoints({
  endpoints: (builder) => ({
    getGitBinding: builder.query<GitBinding | null, UUID>({
      query: (workspaceId) => `/git/workspaces/${workspaceId}/binding`,
      providesTags: (_r, _e, workspaceId) => [
        { type: "GitBinding", id: workspaceId },
      ],
    }),
    commitAndPush: builder.mutation<
      CommitOut,
      { workspaceId: UUID; body: CommitRequest }
    >({
      query: ({ workspaceId, body }) => ({
        url: `/git/workspaces/${workspaceId}/commit`,
        method: "POST",
        body,
      }),
      invalidatesTags: (_r, _e, { workspaceId }) => [{ type: "GitBinding", id: workspaceId }],
    }),
    pushChanges: builder.mutation<GitActionResult, UUID>({
      query: (workspaceId) => ({ url: `/git/workspaces/${workspaceId}/push`, method: "POST" }),
    }),
    pullChanges: builder.mutation<GitActionResult, UUID>({
      query: (workspaceId) => ({ url: `/git/workspaces/${workspaceId}/pull`, method: "POST" }),
      invalidatesTags: (_r, _e, workspaceId) => [{ type: "File", id: `TREE:${workspaceId}` }],
    }),

    // -------- workspace-from-git + branch management --------
    createWorkspaceFromGit: builder.mutation<WorkspaceFromGitResult, WorkspaceFromGitCreate>({
      query: (body) => ({ url: "/git/workspaces/from-git", method: "POST", body }),
      invalidatesTags: ["Workspace"],
    }),
    storePAT: builder.mutation<{ status: string }, { workspaceId: UUID; token: string }>({
      query: ({ workspaceId, token }) => ({ url: `/git/workspaces/${workspaceId}/token`, method: "POST", body: { token } }),
    }),
    createBranch: builder.mutation<BranchInfo, { workspaceId: UUID; name: string }>({
      query: ({ workspaceId, name }) => ({ url: `/git/workspaces/${workspaceId}/branches`, method: "POST", body: { name } }),
      invalidatesTags: (_r, _e, { workspaceId }) => [{ type: "GitBinding", id: workspaceId }],
    }),
    switchBranch: builder.mutation<BranchInfo, { workspaceId: UUID; branch: string }>({
      query: ({ workspaceId, branch }) => ({ url: `/git/workspaces/${workspaceId}/branch`, method: "PATCH", body: { branch } }),
      invalidatesTags: (_r, _e, { workspaceId }) => [{ type: "GitBinding", id: workspaceId }],
    }),
    initiateOAuth: builder.query<OAuthInitiate, string>({
      query: (provider) => `/git/${provider}/connect`,
    }),
    // The caller's real Git sign-in status per provider (never a token).
    listGitConnections: builder.query<GitConnection[], void>({
      query: () => "/git/connections",
    }),
    disconnectGit: builder.mutation<void, string>({
      query: (provider) => ({ url: `/git/connections/${provider}`, method: "DELETE" }),
    }),
    listRepos: builder.query<RepoListItem[], string>({
      query: (provider) => `/git/${provider}/repos`,
    }),
    getGitStatus: builder.query<GitStatus, UUID>({
      query: (workspaceId) => `/git/workspaces/${workspaceId}/status`,
      providesTags: (_r, _e, workspaceId) => [{ type: "GitBinding", id: workspaceId }],
    }),
    createPR: builder.mutation<PullRequestOut, { workspaceId: UUID; body: PullRequestCreate }>({
      query: ({ workspaceId, body }) => ({ url: `/git/workspaces/${workspaceId}/pull-requests`, method: "POST", body }),
    }),
  }),
});

export const {
  useGetGitBindingQuery,
  useCommitAndPushMutation,
  usePushChangesMutation,
  usePullChangesMutation,
  useCreateWorkspaceFromGitMutation,
  useStorePATMutation,
  useCreateBranchMutation,
  useSwitchBranchMutation,
  useInitiateOAuthQuery,
  useLazyInitiateOAuthQuery,
  useListGitConnectionsQuery,
  useDisconnectGitMutation,
  useListReposQuery,
  useGetGitStatusQuery,
  useCreatePRMutation,
} = gitFeatureApi;
