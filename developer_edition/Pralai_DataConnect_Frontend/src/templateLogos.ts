// Which logo (ConnectorLogo.tsx: the Connections page's, plus the product marks simple-icons ships) a catalog template is for (see components/TemplateIcon.tsx).

/** Which Connections-page logo (if any) a template is for. */
const BY_TECH: ReadonlyArray<readonly [RegExp, string]> = [
  [/\bwebhook\b/, "webhook"],
  [/\brest\b/, "rest"],
  [/\bsftp\b/, "sftp"],
  [/\bftp\b/, "ftp"],
  [/\bpostgres(ql)?\b|\bpgvector\b/, "postgresql"],
  [/\bmysql\b/, "mysql"],
  [/\bmongo(db)?\b/, "mongodb"],
  [/\belasticsearch\b/, "elasticsearch"],
  [/\bsql ?server\b|\bsqlserver\b/, "sqlserver"],
  [/\boracle\b/, "oracle"],
  [/\bjdbc\b/, "jdbc"],
  [/\biceberg\b/, "iceberg"],
  [/\badls\b|\bdata lake storage\b|\bdatalake gen2\b/, "adls_gen2"],
  [/\bazure blob\b|\bblob storage\b/, "azure_blob"],
  [/\bgcs\b|\bgoogle cloud storage\b/, "gcs"],
  [/\b(amazon |aws )?s3\b/, "s3"],
  [/^http\b/, "http"],                       // "HTTP Source/Sink" only: not every product that happens to speak HTTP
  [/\blocal file\b|\bfile ?stream\b|\bfilestream\b/, "file"],
  // Products whose own mark ships with simple-icons (see ConnectorLogo.tsx). Specific products before the generic words above would catch them.
  [/\bcassandra\b/, "cassandra"],
  [/\bclickhouse\b/, "clickhouse"],
  [/\bcouchbase\b/, "couchbase"],
  [/\bdatadog\b/, "datadog"],
  [/\bfirebase\b/, "firebase"],
  [/\bbigquery\b/, "bigquery"],
  [/\bdataproc\b/, "dataproc"],
  [/\bgoogle cloud functions\b/, "googlecloud"],
  [/\bhbase\b/, "hbase"],
  [/\bhdfs\b/, "hadoop"],
  [/\binfluxdb\b/, "influxdb"],
  [/\bmilvus\b/, "milvus"],
  [/\bneo4j\b/, "neo4j"],
  [/\bnew relic\b/, "newrelic"],
  [/\bpagerduty\b/, "pagerduty"],
  [/\bprometheus\b/, "prometheus"],
  [/\bqdrant\b/, "qdrant"],
  [/\brabbitmq\b/, "rabbitmq"],
  [/\bredis\b/, "redis"],
  [/^sap\b/, "sap"],
  [/\bscylla\b/, "scylladb"],
  [/\bsinglestore\b/, "singlestore"],
  [/\bsnowflake\b/, "snowflake"],
  [/\bsplunk\b/, "splunk"],
  [/\bteradata\b/, "teradata"],
  [/\bdatabricks\b/, "databricks"],
  [/\bvmware\b/, "vmware"],
];

export function templateLogoType(t: { name: string; connector_class?: string | null }): string | null {
  // By NAME only. The Kafka Connect class is not evidence of the product (YugabyteDB's connector is a PostgreSQL class; an Imply sink is an
  // HTTP class): showing PostgreSQL's logo on YugabyteDB would be a mislabelled logo, and a clear category glyph beats that.
  const name = (t.name || "").toLowerCase();
  for (const [re, type] of BY_TECH) if (re.test(name)) return type;
  return null;
}

/**
 * Templates whose product has no mark in simple-icons' current release but has one in templateBrandLogos.ts. Checked BEFORE templateLogoType,
 * so in the Templates catalog Oracle, SQL Server, S3, Azure Blob and JDBC show their product's mark instead of the Connections page's glyph
 * (the Connections page itself keeps its glyphs). The system a connector talks to decides the mark: "IBM MQ", "Netezza" and "AS400" are IBM;
 * WebLogic is Oracle; JDBC and JMS are Java APIs; Confluent's own Datagen and Data Diode carry Confluent's mark. Protocols with no owner
 * (FTPS, SFTP, Syslog, SNMP, REST, webhook, local files, SpoolDir, File Chunk) keep their glyph.
 */
const BY_BRAND: ReadonlyArray<readonly [RegExp, string]> = [
  [/\bably\b/, "ably"],
  [/\bactivemq\b/, "activemq"],
  [/\badobe\b/, "adobe"],
  [/\baerospike\b/, "aerospike"],
  [/\beventbridge\b/, "aws"],
  [/\bappdynamics\b/, "appdynamics"],
  [/\bcosmos ?db\b/, "cosmos"],
  [/\bdata explorer\b|\bkusto\b/, "adx"],
  [/\bazure\b/, "azure"],
  [/\bcelonis\b/, "celonis"],
  [/\bcrux\b/, "crux"],
  [/\bdata ?diode\b|\bdatagen\b/, "confluent"],
  [/\bweblogic\b|\boracle\b/, "oracle"],
  [/\bsql ?server\b/, "sqlserver"],
  [/\bdiffusion\b/, "diffusion"],
  [/\bfirebolt\b/, "firebolt"],
  [/\bgridgain\b/, "gridgain"],
  [/\bheavy\.?ai\b|\bomnisci\b/, "heavyai"],
  [/\bibm\b|\bas400\b|\bnetezza\b/, "ibm"],
  [/\bimply\b/, "imply"],
  [/\bjdbc\b|\bjms\b/, "java"],
  [/\bkinetica\b/, "kinetica"],
  [/\blightstreamer\b/, "lightstreamer"],
  [/\blittlehorse\b/, "littlehorse"],
  [/\bmapr\b/, "mapr"],
  [/\bprivitar\b/, "privitar"],
  [/\bquestdb\b/, "questdb"],
  [/\brockset\b/, "rockset"],
  [/^s3\b|\b(amazon |aws )s3\b/, "s3"],
  [/\bsalesforce\b/, "salesforce"],
  [/\bstartree\b/, "startree"],
  [/\bsynthesis\b/, "synthesis"],
  [/\btibco\b/, "tibco"],
  [/\btimeplus\b/, "timeplus"],
  [/\bvectara\b/, "vectara"],
  [/\bvertica\b/, "vertica"],
  [/\bweaviate\b/, "weaviate"],
  [/\byugabyte(db)?\b/, "yugabyte"],
];

/** Which templateBrandLogos.ts mark (if any) a template is for — by NAME, for the same reason as templateLogoType. */
export function templateBrandKey(t: { name: string }): string | null {
  const name = (t.name || "").toLowerCase();
  for (const [re, key] of BY_BRAND) if (re.test(name)) return key;
  return null;
}
