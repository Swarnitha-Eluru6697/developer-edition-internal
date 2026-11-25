/**
 * "View as role": the portal's role strip lets a user who holds several Workspace roles pick one to view the app as. The portal then
 * passes that role's permission IDs as `viewPermissions` (never role names). They live in React context, not in api/apiBase's
 * module-level runtime, so switching re-renders every permission consumer without a remount; ./hooks.ts narrows the caller's
 * permissions with them (the one place that does it).
 *
 * The provider is ./ViewPermissionsProvider.tsx (WorkspaceApp mounts it).
 *
 * UX only: nothing here reaches the backend, which still authorises on the ticket's permissions.
 */
import { createContext, useContext } from "react";

/** null = no narrowing (standalone, an older portal, or "All roles"). */
export const ViewPermissionsContext = createContext<readonly string[] | null>(null);

/** The permission IDs of the role the user views the app as; null = no narrowing. */
export function useViewPermissions(): readonly string[] | null {
  return useContext(ViewPermissionsContext);
}
