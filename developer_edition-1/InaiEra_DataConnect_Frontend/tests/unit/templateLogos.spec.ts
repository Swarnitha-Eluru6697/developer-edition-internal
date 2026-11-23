/*
 * Templates wear the Connections page's round icon: the product's own logo when one exists (the Connections ones plus the brand marks
 * simple-icons ships), a category glyph otherwise — never initials, and never another product's logo.
 */
import { test, expect } from "@playwright/test";
import { templateBrandKey, templateLogoType } from "../../src/templateLogos";
import { TEMPLATE_BRAND_LOGOS } from "../../src/templateBrandLogos";

const logo = (name: string) => templateLogoType({ name });

test("a template for a technology the Connections page has a logo for gets that logo", () => {
  expect(logo("PostgreSQL Sink")).toBe("postgresql");
  expect(logo("Debezium PostgreSQL CDC")).toBe("postgresql");
  expect(logo("Debezium MySQL CDC")).toBe("mysql");
  expect(logo("MongoDB Source")).toBe("mongodb");
  expect(logo("Debezium MongoDB CDC")).toBe("mongodb");
  expect(logo("Elasticsearch Sink")).toBe("elasticsearch");
  expect(logo("GCS Source")).toBe("gcs");
  expect(logo("S3 Sink")).toBe("s3");
  expect(logo("Debezium Oracle CDC")).toBe("oracle");
  expect(logo("Debezium SQL Server CDC")).toBe("sqlserver");
  expect(logo("JDBC Source")).toBe("jdbc");
  expect(logo("Azure Blob Source")).toBe("azure_blob");
  expect(logo("SFTP Source")).toBe("sftp");
  expect(logo("FTP Sink")).toBe("ftp");
  expect(logo("REST API Source")).toBe("rest");
  expect(logo("Webhook Sink")).toBe("webhook");
  expect(logo("HTTP Sink")).toBe("http");
  expect(logo("Local File Source")).toBe("file");
  expect(logo("Apache Iceberg Sink")).toBe("iceberg");
});

test("sftp is not mistaken for ftp, nor a name that merely contains a letter pair for a technology", () => {
  expect(logo("SFTP Sink")).toBe("sftp");
  expect(logo("Ably Sink")).toBeNull();
  expect(logo("AS400 Gateway for Kafka Sink")).toBeNull();
  expect(logo("Diffusion Source")).toBeNull();                   // no brand mark exists: the category glyph, not a guess
  expect(logo("Firebolt Sink")).toBeNull();                      // "Fire…" is not Firebase
  expect(logo("Debezium Connector")).toBeNull();
});

test("a product with its own brand mark gets that mark", () => {
  const expected: Record<string, string> = {
    "Cassandra Sink": "cassandra", "DataStax Cassandra Sink": "cassandra", "ClickHouse Sink": "clickhouse", "Onibex ClickHouse Sink": "clickhouse",
    "Couchbase Source": "couchbase", "Datadog Logs Sink": "datadog", "Firebase Realtime DB Source": "firebase", "Google BigQuery Sink": "bigquery",
    "Google Dataproc Sink": "dataproc", "Google Cloud Functions Sink": "googlecloud", "HBase Sink": "hbase", "HDFS 3 Sink": "hadoop",
    "InfluxDB Sink": "influxdb", "Milvus Sink": "milvus", "Neo4j Source": "neo4j", "New Relic Logs Sink": "newrelic", "PagerDuty Sink": "pagerduty",
    "Prometheus Metrics Sink": "prometheus", "Qdrant Sink": "qdrant", "RabbitMQ Source": "rabbitmq", "Redis Sink": "redis", "SAP ODP Source": "sap",
    "Scylla CDC Source": "scylladb", "SingleStore Sink": "singlestore", "Snowflake Sink": "snowflake", "Splunk Sink": "splunk",
    "Teradata Source": "teradata", "Onibex Databricks Sink": "databricks", "VMware Tanzu GemFire Sink": "vmware",
  };
  for (const [name, type] of Object.entries(expected)) expect(logo(name), name).toBe(type);
});

test("a product that merely speaks a protocol does not borrow that protocol's logo", () => {
  expect(logo("Splunk HTTP Source")).toBe("splunk");             // HTTP in the name, but it is Splunk: its own mark, not the globe
  expect(logo("Imply Native Integration Sink")).toBeNull();
  expect(logo("YugabyteDB Source")).toBeNull();                  // its connector class is a PostgreSQL one; the product is not PostgreSQL
});

test("the Kafka Connect class alone never decides the logo", () => {
  expect(templateLogoType({ name: "YugabyteDB Source", connector_class: "io.debezium.connector.postgresql.PostgresConnector" })).toBeNull();
});

test("matching ignores case", () => {
  expect(logo("postgresql sink")).toBe("postgresql");
  expect(logo("MYSQL SINK")).toBe("mysql");
});

test("every logo type it can return is one the Connections page can draw", async () => {
  const { readFileSync } = await import("node:fs");
  const src = readFileSync(new URL("../../src/components/ConnectorLogo.tsx", import.meta.url), "utf8");
  const mapping = readFileSync(new URL("../../src/templateLogos.ts", import.meta.url), "utf8");
  const tech = mapping.slice(mapping.indexOf("BY_TECH"), mapping.indexOf("export function templateLogoType"));
  const types = [...tech.matchAll(/\/,\s*"([a-z0-9_]+)"\]/g)].map((m) => m[1]);
  expect(types.length).toBeGreaterThan(10);
  for (const type of types) expect(src, `${type} has no logo in ConnectorLogo`).toMatch(new RegExp(`\\b${type}\\b`));
});

// ── Product marks simple-icons' current release lacks (templateBrandLogos.ts) ──────────────────────────────────────────────────────

const brand = (name: string) => templateBrandKey({ name });

test("connectors for products with a mark of their own get it, whoever wrote the connector", () => {
  expect(brand("Ably Sink")).toBe("ably");
  expect(brand("ActiveMQ Sink")).toBe("activemq");
  expect(brand("Salesforce CDC Source")).toBe("salesforce");
  expect(brand("Azure Cosmos DB Sink")).toBe("cosmos");
  expect(brand("Azure Data Explorer Sink")).toBe("adx");
  expect(brand("Azure Blob Source")).toBe("azure");
  expect(brand("Amazon EventBridge Sink")).toBe("aws");
  expect(brand("S3 Sink")).toBe("s3");
  expect(brand("HEAVY.AI (OmniSci) Sink")).toBe("heavyai");
  expect(brand("YugabyteDB Source")).toBe("yugabyte");
});

test("the system the connector talks to decides the mark", () => {
  expect(brand("IBM MQ for z/OS Source")).toBe("ibm");
  expect(brand("AS400 Gateway for Kafka Sink")).toBe("ibm");
  expect(brand("Netezza Sink")).toBe("ibm");
  expect(brand("WebLogic JMS (Preview) Source")).toBe("oracle");          // Oracle's server, not "JMS"
  expect(brand("Oracle CDC by A2 Solutions")).toBe("oracle");
  expect(brand("JMS Sink")).toBe("java");
  expect(brand("JDBC Source")).toBe("java");
  expect(brand("Datagen Source")).toBe("confluent");
});

test("protocols nobody owns keep their glyph", () => {
  for (const name of ["REST API Source", "SFTP Source", "FTPS Sink (Preview)", "Syslog Source", "SNMP Trap Source", "Webhook Sink", "Local File Source", "SpoolDir Source", "File Chunk Source"]) {
    expect(brand(name), name).toBeNull();
  }
  expect(brand("PostgreSQL Sink")).toBeNull();                               // simple-icons' current release has it: unchanged
});

test("every mark a template can get exists, each with exactly one drawing and where it came from", async () => {
  const { readFileSync } = await import("node:fs");
  const mapping = readFileSync(new URL("../../src/templateLogos.ts", import.meta.url), "utf8");
  const keys = new Set([...mapping.slice(mapping.indexOf("const BY_BRAND")).matchAll(/\/,\s*"([a-z0-9_]+)"\]/g)].map((m) => m[1]!));
  expect(keys.size).toBeGreaterThan(30);
  for (const key of keys) {
    const logo = TEMPLATE_BRAND_LOGOS[key];
    expect(logo, `${key} has no mark`).toBeDefined();
    expect(Boolean(logo!.path) !== Boolean(logo!.mask), `${key}: a path or a mask, not both`).toBe(true);
    expect(logo!.source.length, `${key} says where it came from`).toBeGreaterThan(5);
    if (logo!.mask) expect(logo!.mask).toMatch(/^data:image\/png;base64,/);   // bundled: nothing is fetched at runtime inside a host
  }
  for (const key of Object.keys(TEMPLATE_BRAND_LOGOS)) expect(keys.has(key), `${key} is never used`).toBe(true);
});
