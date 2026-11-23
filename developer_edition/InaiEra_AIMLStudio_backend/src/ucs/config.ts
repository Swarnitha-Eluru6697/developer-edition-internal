// Configuration for the external Unified Catalog Service.
//
// UCS authorises on permissions (contract: docs/auth/01-CONTRACT.md, task 25).
// Every call carries UCS's gateway secret (UCS_GATEWAY_SECRET) and a principal:
//   1. On behalf of a signed-in user (authz/check, lineage reads, profiles,
//      explorer tree): the user's own name, id and groups. UCS needs only a
//      gateway principal for these; the user's ML Studio permissions are not
//      forwarded.
//   2. As ML Studio itself (lineage producer, admin registration/RBAC): the
//      service name (UCS_SERVICE_USERNAME) plus the UCS permission the call
//      needs (ucs:lineage-events:create, ucs:catalogs:manage, ucs:rbac:manage).
// No bearer token, realm role or role marker is sent.

export const ucsConfig = {
  // UCS_ENABLED=false runs ML Studio without a Unified Catalog (Developer Edition): catalog
  // permission checks allow (the ML Studio permission on the route still applies), and every
  // other UCS call fails fast as "UCS unreachable", which its callers already treat as best-effort.
  enabled: !["0", "false", "no", "off"].includes((process.env.UCS_ENABLED ?? "true").trim().toLowerCase()),
  baseUrl: process.env.UCS_BASE_URL || "http://localhost:8010",
  // The Gravitino catalog this app's models live under — a real catalog
  // with engine="model" must exist in UCS (see scripts/bootstrapUcs.ts).
  modelCatalog: process.env.UCS_MODEL_CATALOG || "ml_models",
  modelSchema: process.env.UCS_MODEL_SCHEMA || "default",
  // Separate schema for the 85 base-model library entries, distinct from
  // where TRAINED models register (modelSchema, above) — a base model
  // named "random-forest" and a user's trained model also named
  // "random-forest" would otherwise collide in the same namespace.
  baseModelSchema: process.env.UCS_BASE_MODEL_SCHEMA || "base-library",

  // The shared secret UCS requires on every header-authenticated call
  // (X-Inaiera-Gateway-Secret, checked against its own ENVOY_INTERNAL_SECRET).
  // In a deployed data plane Envoy stamps this on proxied traffic; ML Studio
  // calls UCS directly, so it has to present it itself. UCS fails CLOSED when
  // its secret is set and the header is absent — so without this, every
  // authz/check returns 401 and every UCS-gated action is denied, which is
  // exactly what a missing header should do, but is indistinguishable from a
  // genuine permission denial unless this is configured.
  gatewaySecret: process.env.UCS_GATEWAY_SECRET || "",

  // Tenant ML Studio presents to UCS on header-authenticated calls. The user
  // name, groups and permissions come from the gateway-verified caller (see
  // UcsIdentity below), or from the configured service identity for
  // background work.
  tenantId: process.env.UCS_TENANT_ID || "ml_models",
};

/**
 * Who ML Studio says it is calling UCS for. For a user: taken from the
 * gateway-verified principal (name, id, Keycloak groups), never from an app
 * role — UCS's object grants are data bound to real groups. For ML Studio
 * itself: the service name plus the UCS permissions that call needs.
 */
export interface UcsIdentity {
  username: string;
  groups: string[];
  userId?: string;
  /** UCS permission IDs — set only on service identities (see UCS_SERVICE_PERMISSIONS). */
  permissions?: string[];
}

/** The identity of the signed-in caller, from req.principal (gateway-auth).
 *  Their ML Studio permissions are deliberately not forwarded to UCS. */
export function ucsIdentityFromPrincipal(principal: { username: string; groups: string[]; userId?: string } | undefined): UcsIdentity {
  return {
    username: principal?.username ?? "",
    groups: principal?.groups ?? [],
    userId: principal?.userId || undefined,
  };
}

/** The UCS permissions each kind of ML Studio service call needs. */
export const UCS_SERVICE_PERMISSIONS = {
  lineageProducer: ["ucs:lineage-events:create"],
  qualityResultsProducer: ["ucs:quality-results:create"],
  admin: ["ucs:catalogs:manage", "ucs:rbac:manage"],
} as const;

/** ML Studio's own service name towards UCS (UCS_SERVICE_USERNAME). */
export function ucsServiceUsername(): string {
  return (process.env.UCS_SERVICE_USERNAME || "ml-studio-service").trim();
}

/**
 * ML Studio itself calling UCS (lineage producer, admin registration/RBAC,
 * and object-grant checks for background work with no signed-in caller):
 * the service name and exactly the UCS permissions given. UCS_SERVICE_GROUPS
 * (comma-separated) optionally names the groups the service's object grants
 * are bound to, for authz/check from background work.
 */
export function ucsServiceIdentity(permissions: readonly string[] = []): UcsIdentity {
  const groups = (process.env.UCS_SERVICE_GROUPS || "").split(",").map((g) => g.trim()).filter(Boolean);
  return { username: ucsServiceUsername(), groups, permissions: [...permissions] };
}

/** Headers for ML Studio's admin calls to UCS (register catalog/model/version, schemas, RBAC). */
export function buildAdminHeaders(): Record<string, string> {
  return buildIdentityHeaders(ucsServiceIdentity(UCS_SERVICE_PERMISSIONS.admin));
}

export function buildIdentityHeaders(identity: UcsIdentity): Record<string, string> {
  const headers: Record<string, string> = {
    "X-Tenant-Id": ucsConfig.tenantId,
    "X-User-Name": identity.username,
  };
  // Comma lists: every gateway-auth parser accepts them (contract section 4).
  if (identity.groups.length) headers["X-User-Groups"] = identity.groups.join(",");
  if (identity.userId) headers["X-User-Id"] = identity.userId;
  if (identity.permissions && identity.permissions.length) headers["X-User-Permissions"] = identity.permissions.join(",");
  // Omitted rather than sent empty when unconfigured: an empty value is not a
  // valid secret, and sending one would only obscure the real reason for the
  // 401 that follows.
  if (ucsConfig.gatewaySecret) headers["X-Inaiera-Gateway-Secret"] = ucsConfig.gatewaySecret;
  return headers;
}

/** A model's name as UCS stores it. UCS addresses objects as catalog.schema.name, so a
 *  name can't contain a dot: Gravitino rejects "qwen2.5-coder" outright, and an
 *  authorisation check on "ml_models.default.qwen2.5-coder" would read as four parts.
 *  Every UCS call goes through this, so registration and permission checks agree. */
export function ucsName(name: string): string {
  return name.replace(/\./g, "_");
}

export function modelFqn(modelName: string): string {
  return `${ucsConfig.modelCatalog}.${ucsConfig.modelSchema}.${ucsName(modelName)}`;
}

export function baseModelFqn(baseModelId: string): string {
  return `${ucsConfig.modelCatalog}.${ucsConfig.baseModelSchema}.${ucsName(baseModelId)}`;
}
