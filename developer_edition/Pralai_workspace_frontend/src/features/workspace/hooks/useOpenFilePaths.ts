import { useMemo } from "react";
import { useGetFileTreeQuery } from "../api";
import { useGetGitBindingQuery } from "../../git";
import type { UUID } from "../../../types";
import { collectFilePaths } from "../fileTree";

/** id -> path for the workspace's files, to label editor tabs with real file names.
 *  Reads the same two cached queries as the file panel (same branch argument), so it adds no requests. */
export function useOpenFilePaths(workspaceId: UUID | null): Map<UUID, string> {
  const { data: binding } = useGetGitBindingQuery(workspaceId ?? "", { skip: !workspaceId });
  const branch = binding?.current_branch ?? undefined;
  const { data: tree } = useGetFileTreeQuery({ workspaceId: workspaceId ?? "", branch }, { skip: !workspaceId });
  return useMemo(() => collectFilePaths(tree), [tree]);
}
