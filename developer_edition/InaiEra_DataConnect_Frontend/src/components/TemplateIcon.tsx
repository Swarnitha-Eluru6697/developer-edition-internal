import React from "react";
import { ConnectorLogo } from "./ConnectorLogo";
import { templateBrandKey, templateLogoType } from "../templateLogos";
import { TEMPLATE_BRAND_LOGOS } from "../templateBrandLogos";

// The Templates catalog used to mark every template with its initials on a red tile ("AS" for Ably Sink). Now a template wears the same
// round icon as the Connections page: the very logo the Connections page uses when the template is for one of those technologies
// (PostgreSQL, MySQL, MongoDB, Elasticsearch, GCS, S3, Oracle, SQL Server, JDBC, Iceberg, ADLS, Azure Blob, FTP, SFTP, HTTP, REST, webhook,
// file), then the product's own mark from templateBrandLogos.ts (Ably, ActiveMQ, Oracle, Salesforce, ...), and only otherwise a line glyph for its CATEGORY (messaging, warehouse, cache, ...). No third-party brand marks beyond the ones
// ConnectorLogo already ships: a clear glyph beats a mislabelled logo (see ConnectorLogo.tsx).
// Every icon is drawn in ONE colour (white, on the platform's orange tile — see .tpl-logo in styles.css, the same treatment as the Catalog's
// catalog cards), whatever the technology's own brand colour is: the shapes are unchanged, only the colour is shared.

const S = { fill: "none", stroke: "currentColor", strokeWidth: 1.7, strokeLinecap: "round", strokeLinejoin: "round" } as const;
const DB = <><ellipse cx="12" cy="6" rx="7" ry="3" /><path d="M5 6v12c0 1.7 3.1 3 7 3s7-1.3 7-3V6M5 12c0 1.7 3.1 3 7 3s7-1.3 7-3" /></>;

const CATEGORY: Record<string, { node: React.ReactNode }> = {
  messaging: { node: <><path d="M4 5h16v11H9l-5 4z" /><path d="M8 9h8M8 12.5h5" /></> },
  warehouse: { node: <><path d="M3 10l9-6 9 6" /><path d="M5 10v9h14v-9M9 19v-5h6v5" /></> },
  api: { node: <path d="M9 7L4 12l5 5M15 7l5 5-5 5M13.5 5l-3 14" /> },
  file: { node: <><path d="M14 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8z" /><path d="M14 3v5h5M9 13h6M9 17h6" /></> },
  observability: { node: <path d="M3 12h4l2.5-6 4 12 2.5-6H21" /> },
  document: { node: <><rect x="4" y="3" width="16" height="18" rx="2" /><path d="M8 8h8M8 12h8M8 16h5" /></> },
  database_cdc: { node: <>{DB}<path d="M20 4l2 2-2 2M4 20l-2-2 2-2" /></> },
  database_jdbc: { node: <>{DB}<path d="M18 3v4M21 3v4" /></> },
  database: { node: <>{DB}</> },
  datalake: { node: <><path d="M3 9c3 2 6-2 9 0s6 2 9 0M3 14c3 2 6-2 9 0s6 2 9 0M3 19c3 2 6-2 9 0s6 2 9 0" /></> },
  cache: { node: <path d="M13 2L4 14h7l-1 8 9-12h-7z" /> },
  widecolumn: { node: <><rect x="3" y="4" width="18" height="16" rx="2" /><path d="M9 4v16M15 4v16M3 10h18" /></> },
  vector: { node: <><circle cx="6" cy="18" r="2" /><circle cx="18" cy="6" r="2" /><circle cx="18" cy="18" r="2" /><path d="M7.5 16.5l9-9M8 18h8" /></> },
  timeseries: { node: <><path d="M3 3v18h18" /><path d="M7 15l4-5 3 3 5-7" /></> },
  graph: { node: <><circle cx="12" cy="5" r="2.5" /><circle cx="5" cy="18" r="2.5" /><circle cx="19" cy="18" r="2.5" /><path d="M11 7.3L6 15.7M13 7.3l5 8.4M7.5 18h9" /></> },
  search: { node: <><circle cx="11" cy="11" r="6.5" /><path d="M16 16l5 5" /></> },
  testing: { node: <path d="M9 3h6M10 3v6l-5.5 9.5A2 2 0 0 0 6.2 21h11.6a2 2 0 0 0 1.7-2.5L14 9V3" /> },
  workflow: { node: <><rect x="3" y="4" width="6" height="5" rx="1" /><rect x="15" y="15" width="6" height="5" rx="1" /><path d="M9 6.5h4a2 2 0 0 1 2 2V15" /></> },
  object_store: { node: <><path d="M5 6c0-1.1 3.1-2 7-2s7 .9 7 2-3.1 2-7 2-7-.9-7-2z" /><path d="M5 6l1.6 13c.1.9 2.6 1.5 5.4 1.5s5.3-.6 5.4-1.5L19 6" /></> },
};
// A category the catalog gains later still gets a clear, neutral glyph (a plug), never initials.
const GENERIC = { node: <><path d="M9 3v5M15 3v5M6 8h12v4a6 6 0 0 1-12 0z" /><path d="M12 18v3" /></> };

export function TemplateIcon({ template, size = 24 }: { template: { name: string; category?: string | null; connector_class?: string | null }; size?: number }) {
  const brand = TEMPLATE_BRAND_LOGOS[templateBrandKey(template) ?? ""];
  if (brand?.path) return <svg width={size} height={size} viewBox="0 0 24 24" role="img" aria-hidden="true"><path d={brand.path} fill="currentColor" /></svg>;
  if (brand?.mask) {
    const mask = `url("${brand.mask}") center / contain no-repeat`;
    return <span className="tpl-mark" aria-hidden="true" style={{ width: size, height: size, WebkitMask: mask, mask }} />;
  }
  const logo = templateLogoType(template);
  if (logo) return <ConnectorLogo type={logo} size={size} mono />;
  const g = CATEGORY[template.category ?? ""] ?? GENERIC;
  return <svg width={size} height={size} viewBox="0 0 24 24" aria-hidden="true" {...S}>{g.node}</svg>;
}
