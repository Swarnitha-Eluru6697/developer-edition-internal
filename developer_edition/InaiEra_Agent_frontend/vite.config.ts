import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import federation from '@originjs/vite-plugin-federation'

const BACKEND = 'http://127.0.0.1:8001'

// ML Studio's API, for Integrations > Model Providers > ML Studio models.
// Its own routes live under /api too, so it gets a distinct prefix here,
// rewritten back to /api on the way through.
const MLSTUDIO_BACKEND = process.env.MLSTUDIO_BACKEND || 'http://127.0.0.1:4001'

// Every agent:* permission (backend permissions.json) — the dev default, so
// a developer sees every page. Narrow it with DEV_GATEWAY_PERMISSIONS to try
// what a user with fewer permissions sees.
const ALL_AGENT_PERMISSIONS = [
  'agent:access:view', 'agent:overview:view', 'agent:studio:edit', 'agent:templates:view',
  'agent:templates:manage-platform', 'agent:rag:manage', 'agent:integrations:manage', 'agent:runs:view',
  'agent:traces:view', 'agent:monitoring:view', 'agent:guardrails:manage', 'agent:alerts:manage',
  'agent:webapps:use', 'agent:settings:edit', 'agent:agents:run', 'agent:agents:publish',
  'agent:maintenance:run',
].join(',')

// Stand in for Envoy while developing, so the gateway-authorization path
// is reachable from the browser at all: Envoy sets these headers from the
// verified ticket's claims, and nothing else may. The UI reads the result
// back from GET /api/me.
//
// OFF unless DEV_GATEWAY_SECRET is exported, so this changes nothing by
// default and no secret is ever committed here. It is used only by
// `server.proxy` below — the dev server — and has no effect on a build.
//
//   cd InaiEra_agent_backend
//   export DEV_GATEWAY_SECRET=$(grep '^ENVOY_INTERNAL_SECRET' .env | cut -d= -f2)
//   cd ../InaiEra_Agent_frontend && npm run dev
//
// Override who you arrive as with DEV_GATEWAY_USER / _USER_ID / _TENANT /
// _GROUPS / _PERMISSIONS (comma-separated permission IDs). List headers are
// sent comma-separated, which the gateway-auth library accepts.
function withGatewayHeaders(target: string) {
  const secret = process.env.DEV_GATEWAY_SECRET;
  if (!secret) return target;
  const user = process.env.DEV_GATEWAY_USER || 'dev-user';
  const userId = process.env.DEV_GATEWAY_USER_ID || user;
  const permissions = process.env.DEV_GATEWAY_PERMISSIONS || ALL_AGENT_PERMISSIONS;
  const tenant = process.env.DEV_GATEWAY_TENANT || 'prod_metalake';
  // eslint-disable-next-line no-console
  console.log(
    `[dev-gateway] injecting identity headers on /api and /mcp as ` +
    `${user} (tenant: ${tenant}, permissions: ${permissions}). ` +
    `The backend must also have TRUST_ENVOY_HEADERS=true and the same secret.`,
  );
  return {
    target,
    // Set on the request forwarded to the backend, exactly as Envoy's
    // claim_to_headers would. The browser sends nothing.
    headers: {
      'x-inaiera-gateway-secret': secret,
      'x-user-name': user,
      'x-user-id': userId,
      'x-user-permissions': permissions,
      'x-user-groups': process.env.DEV_GATEWAY_GROUPS || 'data-engineers',
      'x-tenant-id': tenant,
    },
  };
}

function withMlStudioHeaders(target: string) {
  const rewrite = (p: string) => p.replace(/^\/mlstudio-api/, '/api')
  const base = withGatewayHeaders(target)
  if (typeof base === 'string') return { target, changeOrigin: true, rewrite }
  return {
    ...base, changeOrigin: true, rewrite,
    headers: {
      ...base.headers,
      'x-user-permissions': process.env.DEV_GATEWAY_MLSTUDIO_PERMISSIONS || 'mlstudio:endpoints:view',
    },
  }
}

export default defineConfig({
  plugins: [
    react(),
    // Agentic Workflow ships as a Module-Federation REMOTE: the Control Portal
    // loads remoteEntry.js through the tunnel and mounts `AgenticWorkflowApp`.
    // The standalone index.html build still works alongside it.
    federation({
      name: 'agenticworkflow_mfe',
      filename: 'remoteEntry.js',
      exposes: {
        './AgenticWorkflowApp': './src/AgenticWorkflowApp.tsx',
      },
      // Must match the host's shared list and React major (18).
      shared: ['react', 'react-dom'],
    }),
  ],
  // Federation output uses top-level await, so esnext is required. One CSS file
  // so the remote's styles load with it.
  build: { target: 'esnext', cssCodeSplit: false },
  server: {
    port: 5175,
    // Local-dev proxy — forwards these paths to the backend (running
    // separately on :8001) so the frontend can use window.location.origin
    // as its API base (see api.ts). In the data plane this app is a
    // federated remote: the host passes apiBase and Envoy routes the API,
    // so the production nginx serves static files only.
    proxy: {
      '/api': withGatewayHeaders(BACKEND),
      '/mcp': withGatewayHeaders(BACKEND),
      '/embed.js': BACKEND,
      // Same dev identity as /api, but with ML Studio's read permission for
      // its deployments list (ML Studio's backend checks the same gateway secret).
      '/mlstudio-api': withMlStudioHeaders(MLSTUDIO_BACKEND),
    },
  },
})
