/**
 * A Git provider's logo on an orange rounded square — the same badge as the
 * Agents app's GitProviderBadge (Settings → Git integrations), so a provider
 * looks the same in both apps.
 */
const ICON: Record<string, string> = {
  github: "ti-brand-github",
  gitlab: "ti-brand-gitlab",
  bitbucket: "ti-brand-bitbucket",
  azure_devops: "ti-brand-azure",
};

export function GitProviderBadge({ provider, size = 34 }: { provider: string; size?: number }) {
  return (
    <span
      aria-hidden
      style={{
        width: size, height: size, borderRadius: 8, background: "#0A7D5A", color: "#fff", flexShrink: 0,
        display: "inline-grid", placeItems: "center", verticalAlign: "middle",
      }}
    >
      <i className={`ti ${ICON[provider] ?? "ti-git-branch"}`} style={{ fontSize: Math.round(size * 0.6), lineHeight: 1 }} />
    </span>
  );
}
