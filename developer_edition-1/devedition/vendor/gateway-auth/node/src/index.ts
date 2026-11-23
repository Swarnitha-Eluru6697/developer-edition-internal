// v1.1 permission API (contract 01-CONTRACT.md)
export {
  gatewayAuth, requirePermission, principalFromRequest, isPublicPath, meBody, GatewayAuthOptions,
} from "./middleware";
export { hasPermission, extractIdentity, GatewayIdentity } from "./identity";
export { verifyGatewaySecret } from "./gatewaySecret";
export { splitMulti } from "./parse";
// Deprecated (role names); removed in the next release
export { createGatewayMiddleware, GatewayMiddlewareOptions } from "./middleware";
export { resolveRole, RoleConfig } from "./resolveRole";
