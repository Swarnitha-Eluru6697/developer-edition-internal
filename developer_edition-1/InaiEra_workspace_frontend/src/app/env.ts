/** True on localhost — there is no control plane there, so a 401 must be
 *  explained on screen rather than redirected to a login page that doesn't exist. */
export const isLocalDev = (): boolean => typeof window !== "undefined"
  && ["localhost", "127.0.0.1"].includes(window.location.hostname);
