import { Request, Response, NextFunction } from "express";
import { hasPermission, principalFromRequest, requirePermission } from "@aadhyaanalyticsinc/gateway-auth";
import { checkAuthz, UcsError } from "../ucs/client";
import { modelFqn, ucsConfig, ucsIdentityFromPrincipal, UcsIdentity } from "../ucs/config";

// Authorisation for ML Studio (contract: docs/auth/01-CONTRACT.md).
//
// Identity and permissions come ONLY from the gateway-auth library: Envoy
// verifies the user's ticket and sets x-user-permissions; gatewayAuth() (mounted
// app-wide in app.ts) checks the gateway secret and puts the caller on
// req.principal. This file holds no role names: a route declares the permission
// ID it needs (all IDs are listed in permissions.json at the repo root), and
// which users hold it is decided in the Admin Portal.

export { requirePermission };

/** Every permission ID the code checks. Keep in step with permissions.json. */
export const PERM = {
  access: "mlstudio:access:view",
  dashboard: "mlstudio:dashboard:view",
  modelOverview: "mlstudio:model-overview:view",
  modelCatalog: "mlstudio:model-catalog:view",
  train: "mlstudio:train:run",
  gpuLarge: "mlstudio:gpu:train-large",
  datasetsView: "mlstudio:datasets:view",
  datasetsUpload: "mlstudio:datasets:upload",
  pipelines: "mlstudio:pipelines:view",
  endpoints: "mlstudio:endpoints:view",
  experiments: "mlstudio:experiments:view",
  deploy: "mlstudio:deploy:promote",
  alertsView: "mlstudio:alerts:view",
  alertsEdit: "mlstudio:alerts:edit",
  clusters: "mlstudio:clusters:view",
  baseModelsSync: "mlstudio:base-models:sync",
  monitoringEdit: "mlstudio:monitoring:edit",
} as const;

declare global {
  // eslint-disable-next-line @typescript-eslint/no-namespace
  namespace Express {
    interface Request {
      /** Who acted, for audit and created_by: the gateway-verified user name. */
      actor: string;
    }
  }
}

/** Sets req.actor from the verified principal. Display/audit only — it grants nothing. */
export function actorMiddleware(req: Request, _res: Response, next: NextFunction) {
  req.actor = (req.principal?.username || "").slice(0, 120);
  next();
}

/**
 * For data several pages read (the models list feeds the catalog, the dashboard
 * and the endpoints page): any one of the listed permissions is enough. 401
 * without a gateway principal, 403 when none of them is held.
 */
export function requireAnyPermission(...permissions: string[]) {
  return function requireAnyPermissionMiddleware(req: Request, res: Response, next: NextFunction) {
    if (!req.principal) {
      const gate = principalFromRequest(req);
      if (!gate.ok) return res.status(gate.status).json({ error: gate.error });
      req.principal = gate.principal;
    }
    if (permissions.some((p) => hasPermission(req.principal, p))) return next();
    return res.status(403).json({ error: `missing permission: one of ${permissions.join(", ")}` });
  };
}

// ---- UCS object grants (data-level RBAC, owned by UCS) ----
//
// Actions that write a real model in the catalog are additionally checked
// against UCS's authz/check, as the user: UCS decides from its own grants on
// that model, bound to the user's real groups. This is on top of, never instead
// of, the ML Studio permission on the route.

// Short-lived cache so a burst of requests from one session doesn't hit
// UCS's authz/check once per request — long enough to matter, short
// enough that a real permission change in UCS takes effect quickly.
const AUTHZ_CACHE_TTL_MS = 5_000;
const authzCache = new Map<string, { allow: boolean; expiresAt: number }>();

async function ucsAllows(identity: UcsIdentity, action: string, object: string): Promise<boolean> {
  if (!ucsConfig.enabled) return true;   // no catalog to ask: the route's ML Studio permission decides
  const key = `${identity.username}|${[...identity.groups].sort().join(",")}:${action}:${object}`;
  const cached = authzCache.get(key);
  if (cached && cached.expiresAt > Date.now()) return cached.allow;
  try {
    const decision = await checkAuthz(identity, action, object);
    const allow = decision.decision === "allow";
    authzCache.set(key, { allow, expiresAt: Date.now() + AUTHZ_CACHE_TTL_MS });
    return allow;
  } catch (err) {
    // eslint-disable-next-line no-console
    console.error(`UCS authz/check unreachable for '${identity.username}' -> '${action}' on '${object}' — failing closed:`, err instanceof UcsError ? err.message : err);
    return false; // UCS unreachable fails closed
  }
}

/** Route middleware: UCS must allow `action` on the model the request names. */
export function requireUcsModel(action: "read" | "write" | "create" | "update", resolveObject: (req: Request) => string | null) {
  return async (req: Request, res: Response, next: NextFunction) => {
    const object = resolveObject(req);
    if (!object) {
      return res.status(400).json({ error: `cannot evaluate the catalog permission for '${action}' — the request is missing what it would act on` });
    }
    const allowed = await ucsAllows(ucsIdentityFromPrincipal(req.principal), action, object);
    if (!allowed) {
      return res.status(403).json({
        error: `'${req.actor}' is not permitted to '${action}' on '${object}' — denied by the Unified Catalog Service (or UCS was unreachable, which fails closed)`,
      });
    }
    next();
  };
}

/** UCS write check on the model a new training run will register. */
export const requireUcsTrainingWrite = requireUcsModel("write", (req) => (req.body?.modelName ? modelFqn(req.body.modelName) : null));
/** UCS write check on the model a promotion / rollback moves. */
export const requireUcsDeploymentWrite = requireUcsModel("write", (req) => modelFqn(req.params.modelId));

// Used inline (not as route middleware) where the object being acted on
// only resolves after a DB lookup the middleware itself can't do — e.g.
// completing a training run needs the run's own model_name from the DB
// before there's an FQN to check UCS against at all.
export async function requireUcsModelWrite(req: Request, modelName: string): Promise<{ allowed: boolean; reason?: string }> {
  const object = modelFqn(modelName);
  const allowed = await ucsAllows(ucsIdentityFromPrincipal(req.principal), "write", object);
  return allowed ? { allowed: true } : { allowed: false, reason: `'${req.actor}' is not permitted to write '${object}' — denied by the Unified Catalog Service` };
}
