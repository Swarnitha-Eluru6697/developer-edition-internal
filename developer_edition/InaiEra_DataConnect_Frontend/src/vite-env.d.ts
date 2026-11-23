/// <reference types="vite/client" />

// The UI sets CSS Grid track counts via a custom property (e.g. style={{ "--cols": "1fr 2fr" }}).
// React's CSSProperties type rejects unknown keys, so widen it to allow any --* custom property.
import "react";
declare module "react" {
  interface CSSProperties {
    [key: `--${string}`]: string | number;
  }
}
