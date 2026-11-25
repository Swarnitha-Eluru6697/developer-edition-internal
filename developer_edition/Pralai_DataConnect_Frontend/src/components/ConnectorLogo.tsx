import React from "react";
import {
  siPostgresql, siMysql, siMongodb, siElasticsearch, siGooglecloudstorage,
  siApachecassandra, siClickhouse, siCouchbase, siDatadog, siFirebase, siGooglebigquery, siGoogledataproc, siGooglecloud, siApachehbase,
  siApachehadoop, siInfluxdb, siMilvus, siNeo4j, siNewrelic, siPagerduty, siPrometheus, siQdrant, siRabbitmq, siRedis, siSap, siScylladb,
  siSinglestore, siSnowflake, siSplunk, siTeradata, siDatabricks, siVmware,
} from "simple-icons";

// Brand marks come from simple-icons (bundled, so nothing is fetched at runtime
// inside the federated host). Types with no simple-icons entry get a generic
// line glyph tinted to suit — the brand marks for those aren't licensed for us
// to redistribute, and a clear glyph beats a mislabelled one.
const BRAND: Record<string, { path: string; hex: string }> = {
  postgresql: siPostgresql, mysql: siMysql, mongodb: siMongodb,
  elasticsearch: siElasticsearch, gcs: siGooglecloudstorage,
  // The Templates catalog (components/TemplateIcon.tsx) draws the product's own mark for every template whose product simple-icons ships.
  cassandra: siApachecassandra, clickhouse: siClickhouse, couchbase: siCouchbase, datadog: siDatadog, firebase: siFirebase,
  bigquery: siGooglebigquery, dataproc: siGoogledataproc, googlecloud: siGooglecloud, hbase: siApachehbase, hadoop: siApachehadoop,
  influxdb: siInfluxdb, milvus: siMilvus, neo4j: siNeo4j, newrelic: siNewrelic, pagerduty: siPagerduty, prometheus: siPrometheus,
  qdrant: siQdrant, rabbitmq: siRabbitmq, redis: siRedis, sap: siSap, scylladb: siScylladb, singlestore: siSinglestore,
  snowflake: siSnowflake, splunk: siSplunk, teradata: siTeradata, databricks: siDatabricks, vmware: siVmware,
  // connection types whose product mark is one of the above
  hdfs: siApachehadoop, gemfire: siVmware, gcp_functions: siGooglecloud,
};

const S = { fill: "none", stroke: "currentColor", strokeWidth: 1.7, strokeLinecap: "round", strokeLinejoin: "round" } as const;

const DB = <><ellipse cx="12" cy="6" rx="7" ry="3" /><path d="M5 6v12c0 1.7 3.1 3 7 3s7-1.3 7-3V6M5 12c0 1.7 3.1 3 7 3s7-1.3 7-3" /></>;
const CLOUD = <path d="M7 18a4 4 0 0 1-.6-7.96A5.5 5.5 0 0 1 17 8.5a4.75 4.75 0 0 1 .5 9.5z" />;
const QUEUE = <><rect x="3" y="6" width="18" height="12" rx="2" /><path d="M7 10h2M11 10h2M15 10h2M7 14h10" /></>;
const VECTOR = <><circle cx="6" cy="6" r="2" /><circle cx="18" cy="8" r="2" /><circle cx="9" cy="18" r="2" /><circle cx="17" cy="17" r="2" /><path d="M7.5 7.3l2.4 8.9M8 6.3l8 1.4M11 17.8l4-.6M17.6 10l-.4 5" /></>;
const STREAM = <><path d="M3 8h13l-3-3M21 16H8l3 3" /></>;
const CHIP = <><rect x="6" y="6" width="12" height="12" rx="2" /><path d="M9 3v3M15 3v3M9 18v3M15 18v3M3 9h3M3 15h3M18 9h3M18 15h3" /></>;
const NONE = <><circle cx="12" cy="12" r="8" /><path d="M7 17L17 7" /></>;

const GLYPH: Record<string, { color: string; node: React.ReactNode }> = {
  s3: { color: "#e25444", node: <><path d="M5 6c0-1.1 3.1-2 7-2s7 .9 7 2-3.1 2-7 2-7-.9-7-2z" /><path d="M5 6l1.6 13c.1.9 2.6 1.5 5.4 1.5s5.3-.6 5.4-1.5L19 6" /></> },
  adls_gen2: { color: "#0078d4", node: <>{CLOUD}<path d="M9 14h6M9 17h6" /></> },
  azure_blob: { color: "#0078d4", node: <>{CLOUD}<rect x="9.5" y="12.5" width="5" height="5" rx="1" /></> },
  sqlserver: { color: "#cc2927", node: <>{DB}</> },
  oracle: { color: "#c74634", node: <rect x="2.5" y="7" width="19" height="10" rx="5" /> },
  jdbc: { color: "#5382a1", node: <>{DB}<path d="M18 3v4M21 3v4" /></> },
  iceberg: { color: "#2b7bb9", node: <><path d="M3 11l5-4 3 2 4-5 6 7z" /><path d="M5 11l5 10 4-4 5-6" /></> },
  file: { color: "#5b6d62", node: <><path d="M14 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8z" /><path d="M14 3v5h5M9 13h6M9 17h6" /></> },
  ftp: { color: "#d97706", node: <><path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z" /><path d="M9 14l3-3 3 3M12 11v6" /></> },
  sftp: { color: "#059669", node: <><path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z" /><rect x="9" y="12" width="6" height="5" rx="1" /><path d="M10.5 12v-1.5a1.5 1.5 0 0 1 3 0V12" /></> },
  http: { color: "#1d5fbf", node: <><circle cx="12" cy="12" r="9" /><path d="M3 12h18M12 3c3 3 3 15 0 18M12 3c-3 3-3 15 0 18" /></> },
  rest: { color: "#7c3aed", node: <><path d="M8 4c-2 0-3 1-3 3v2c0 1.5-1 3-2 3 1 0 2 1.5 2 3v2c0 2 1 3 3 3M16 4c2 0 3 1 3 3v2c0 1.5 1 3 2 3-1 0-2 1.5-2 3v2c0 2-1 3-3 3" /></> },
  api: { color: "#0d9488", node: <><path d="M9 7L4 12l5 5M15 7l5 5-5 5M13.5 5l-3 14" /></> },
  activemq: { color: "#b5221d", node: <>{QUEUE}</> },
  aerospike: { color: "#c22127", node: <>{DB}</> },
  vertica: { color: "#1a4c8c", node: <>{DB}</> },
  heavyai: { color: "#1d3b6b", node: <>{DB}</> },
  yugabytedb: { color: "#ff6e42", node: <>{DB}</> },
  questdb: { color: "#a23154", node: <>{DB}</> },
  weaviate: { color: "#00a142", node: <>{VECTOR}</> },
  timeplus: { color: "#d9453b", node: <>{STREAM}</> },
  littlehorse: { color: "#4b5a53", node: <>{STREAM}</> },
  cosmosdb: { color: "#0078d4", node: <>{DB}</> },
  gridgain: { color: "#0e5f95", node: <>{DB}</> },
  diffusion: { color: "#e84a1b", node: <>{STREAM}</> },
  lightstreamer: { color: "#f39200", node: <>{STREAM}</> },
  aws_eventbridge: { color: "#e7157b", node: <>{STREAM}</> },
  data_diode: { color: "#5b6d62", node: <>{STREAM}</> },
  none: { color: "#73827a", node: <>{NONE}</> },
  ably: { color: "#ff5416", node: <>{STREAM}</> },
  azure_data_explorer: { color: "#0078d4", node: <>{CLOUD}</> },
  salesforce: { color: "#00a1e0", node: <>{CLOUD}</> },
  vectara: { color: "#2c58f5", node: <>{VECTOR}</> },
  ibm_mq: { color: "#0f62fe", node: <>{QUEUE}</> },
  tibco_ems: { color: "#1a5ea8", node: <>{QUEUE}</> },
  weblogic_jms: { color: "#c74634", node: <>{QUEUE}</> },
  as400: { color: "#0f62fe", node: <>{CHIP}</> },
  netezza: { color: "#0f62fe", node: <>{DB}</> },
  mapr_db: { color: "#e2231a", node: <>{DB}</> },
  adobe_aep: { color: "#fa0f00", node: <>{CLOUD}</> },
  appdynamics: { color: "#0079c2", node: <>{CLOUD}</> },
  celonis_ems: { color: "#00b28f", node: <>{CLOUD}</> },
  firebolt: { color: "#f72a30", node: <>{DB}</> },
  rockset: { color: "#6c40f4", node: <>{DB}</> },
  kinetica: { color: "#5b2c86", node: <>{DB}</> },
  crux: { color: "#2E3D36", node: <>{DB}</> },
  imply: { color: "#2b3a8f", node: <>{CLOUD}</> },
  onibex: { color: "#0a7cc2", node: <>{DB}</> },
  privitar: { color: "#3e1f73", node: <>{CLOUD}</> },
  fix: { color: "#0f766e", node: <>{STREAM}</> },
  webhook: { color: "#db2777", node: <><circle cx="7" cy="17" r="2.5" /><circle cx="17" cy="17" r="2.5" /><circle cx="12" cy="6" r="2.5" /><path d="M12 8.5L8 14.5M9.5 17h5M13.5 8l3.3 6.5" /></> },
};

/** `mono` draws the logo in one colour — the surrounding text colour — instead of the technology's own brand colour (the Templates page,
 *  where every icon shares one colour). The Connections page keeps the brand colours. */
export function ConnectorLogo({ type, size = 36, mono = false }: { type: string; size?: number; mono?: boolean }) {
  const brand = BRAND[type];
  if (brand) {
    return <svg width={size} height={size} viewBox="0 0 24 24" role="img" aria-hidden="true"><path d={brand.path} fill={mono ? "currentColor" : `#${brand.hex}`} /></svg>;
  }
  const g = GLYPH[type];
  if (g) {
    return <svg width={size} height={size} viewBox="0 0 24 24" aria-hidden="true" style={mono ? undefined : { color: g.color }} {...S}>{g.node}</svg>;
  }
  // Unknown future type: fall back to the old initials so it's still identifiable.
  return <span style={{ fontSize: 11, fontWeight: 500, color: mono ? "inherit" : "var(--color-primary)" }}>{type.slice(0, 4).toUpperCase()}</span>;
}
