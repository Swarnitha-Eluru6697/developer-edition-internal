/** Types for scripts/generate-rbac.mjs (imported by tests/permissions.test.ts). */
export interface PermissionsDoc {
  app: string;
  version: number;
  permissions: { id: string; label: string; description?: string }[];
}
export function validate(doc: PermissionsDoc): void;
export function render(doc: PermissionsDoc): string;
