/// <reference types="vite/client" />

interface ImportMetaEnv {
  /** Build-time fallback for the control plane's login URL (W-006); /config.json wins at runtime. */
  readonly VITE_CONTROL_PLANE_LOGIN_URL?: string;
}
