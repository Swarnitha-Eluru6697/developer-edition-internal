import type { ReactNode } from 'react';

// What the Control Portal hands this app when it is federated in (see
// AgenticWorkflowApp). Module-level, like BASE in api.ts: set once before the
// first render, read where it is drawn. Unset when the app runs standalone.
let ACCOUNT_MENU: ReactNode = null;
let ROLE_BADGES: ReactNode | undefined = undefined;

export function setHostAccountMenu(menu: ReactNode): void {
  ACCOUNT_MENU = menu;
}

/** The portal's account menu (profile, user management, sign out), drawn in
 *  place of the user-initials avatar at the right of the top bar. Only older
 *  portals pass it; current ones draw the account menu in their own rail. */
export const hostAccountMenu = (): ReactNode => ACCOUNT_MENU;

export function setHostRoleBadges(badges: ReactNode | undefined): void {
  ROLE_BADGES = badges;
}

/** The portal's strip of this app's role names, drawn at the right of the top
 *  bar. Display only: nothing reads it — what the user may do still comes from
 *  the permissions /api/me returns. */
export const hostRoleBadges = (): ReactNode | undefined => ROLE_BADGES;

/** Federated into the Control Portal (it passed roleBadges). The portal's rail
 *  then carries the notification bell and the account menu, so the top bar
 *  drops its own. */
export const isHosted = (): boolean => ROLE_BADGES !== undefined;
