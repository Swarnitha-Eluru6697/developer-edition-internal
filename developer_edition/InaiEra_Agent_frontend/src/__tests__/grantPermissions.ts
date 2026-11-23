// Test helper: make GET /api/me (api.me, auto-mocked by vi.mock('../api'))
// answer with the given permissions. The UI shows nothing until it does.
import { vi } from 'vitest';
import { api } from '../api';
import { ALL_PERMISSIONS } from '../permissions';
import type { Me } from '../types';

export function meWith(permissions: string[], user = 'tester'): Me {
  return { user, userId: `id-${user}`, tenant: 'test-tenant', permissions };
}

/** api.me resolves with these permissions (default: every agent:* permission). */
export function grantPermissions(permissions: string[] = ALL_PERMISSIONS, user?: string): void {
  vi.mocked(api.me).mockResolvedValue(meWith(permissions, user));
}
