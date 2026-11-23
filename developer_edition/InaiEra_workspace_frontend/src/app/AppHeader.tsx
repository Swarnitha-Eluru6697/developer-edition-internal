/**
 * White header bar above the content, laid out like the other InAiEra apps:
 * search on the left, then notifications and the avatar.
 * Hosted by a current Control Portal (it passed `roleBadges`): the portal's role-badge strip sits at the right instead, on one line,
 * and the bell and avatar are left out — the portal rail carries both. An older portal's `accountMenu` is still drawn if given.
 * (Git lives in the file panel: Files → Changes, and Add New → From Git repository.)
 * (The AI assistant toggle sits in the editor tab bar, next to Run.)
 */
import type { CSSProperties } from "react";
import { hasPortalChrome, hostAccountMenu, hostRoleBadges, isEmbedded } from "../api/apiBase";
import type { UUID, Workspace } from "../types";
import { HeaderSearch } from "./HeaderSearch";
import { NotificationBell } from "./NotificationBell";
import { DeveloperUserMenu } from "./DeveloperUserMenu";
import { useGetMeQuery } from "../features/workspace/api";

interface Props {
  workspaces: Workspace[] | undefined;
  activeWs: UUID | null;
  onSelectWorkspace: (id: UUID) => void;
}

const iconBtn: CSSProperties = {
  width: 34, height: 34, borderRadius: 9, display: "grid", placeItems: "center",
  border: "none", background: "none", color: "#6B7280", cursor: "pointer", flexShrink: 0,
};
// One line that never wraps the header: it may shrink (down to nothing) and scrolls sideways when the role names don't fit.
const roleBadgesSlot: CSSProperties = {
  flex: "0 1 auto", minWidth: 0, display: "flex", alignItems: "center", justifyContent: "flex-end",
  whiteSpace: "nowrap", overflowX: "auto", overflowY: "hidden", scrollbarWidth: "none",
};

export function AppHeader({ workspaces, activeWs, onSelectWorkspace }: Props) {
  const portalChrome = hasPortalChrome();
  return (
    <div style={{ height: 56, flexShrink: 0, display: "flex", alignItems: "center", gap: 16, padding: "0 20px", background: "#fff", borderBottom: "1px solid #E5E7EB", position: "relative", zIndex: 20 }}>
      <HeaderSearch workspaces={workspaces} activeWs={activeWs} onSelectWorkspace={onSelectWorkspace} />
      <div style={{ flex: 1 }} />
      {portalChrome ? (
        <div data-testid="portal-role-badges" style={roleBadgesSlot}>{hostRoleBadges()}</div>
      ) : (
        <div style={{ display: "flex", alignItems: "center", gap: 2 }}>
          <NotificationBell buttonStyle={iconBtn} />
        </div>
      )}
      {/* Inside the portal its account menu (profile, sign out) takes this place, if it still passes one (older portals). */}
      {isEmbedded() ? hostAccountMenu() : <WorkspaceUser />}
    </div>
  );
}

/** Developer Edition account menu (sign out ends the Developer Edition session in the shell, for every product). */
function WorkspaceUser() {
  const { data: me } = useGetMeQuery();
  return <DeveloperUserMenu name={me?.display_name || me?.email || ""} email={me?.email || ""} />;
}
