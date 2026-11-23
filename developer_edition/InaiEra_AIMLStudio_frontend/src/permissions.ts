/*
 * Permission IDs ML Studio's UI checks — the same IDs as the backend's
 * permissions.json (InaiEra_AIMLStudio_backend). No role names: which users
 * hold which permission is decided in the Admin Portal, and the backend
 * returns the signed-in user's permissions from GET /api/me.
 *
 * Hiding something here is convenience only; the API checks every request.
 */
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
} as const;

export type Permission = (typeof PERM)[keyof typeof PERM];

/** The permission that opens each top-level page. */
export const PAGE_PERMISSION: Record<string, Permission> = {
  "/": PERM.dashboard,
  "/model-overview": PERM.modelOverview,
  "/catalog": PERM.modelCatalog,
  "/train": PERM.train,
  "/datasets": PERM.datasetsView,
  "/pipelines": PERM.pipelines,
  "/endpoints": PERM.endpoints,
  "/experiments": PERM.experiments,
  "/deploy": PERM.deploy,
  "/alerts": PERM.alertsView,
};

/**
 * Drill-down pages reached from a row on another page: open whenever one of the
 * pages they are reached from is (a run's detail is as visible as the
 * Pipelines list, the Train wizard that launched it, or Model Overview).
 */
export const DETAIL_PERMISSIONS: { prefix: string; anyOf: Permission[] }[] = [
  { prefix: "/base-models/", anyOf: [PERM.modelCatalog, PERM.train] },
  { prefix: "/training-runs/", anyOf: [PERM.pipelines, PERM.train, PERM.modelOverview] },
  { prefix: "/pipelines/", anyOf: [PERM.pipelines, PERM.train] },
];

/** True when `has` allows opening `pathname`. Unknown paths are not reachable. */
export function canReachPath(has: (p: string) => boolean, pathname: string): boolean {
  const page = PAGE_PERMISSION[pathname];
  if (page) return has(page);
  const detail = DETAIL_PERMISSIONS.find((d) => pathname.startsWith(d.prefix));
  return !!detail && detail.anyOf.some(has);
}
