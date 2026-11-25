import { PYTHON_BIN } from "./pythonBin";
import { spawn } from "child_process"; import path from "path";
// Storage keys a user typed for one pull — only ever passed to the resolver on stdin.
export type IcebergCredentials={accessKeyId:string;secretAccessKey:string;sessionToken?:string};
const SCRIPT=path.join(__dirname,"..","training","iceberg_materialize.py");
export function materializeIcebergSnapshot(table:string, outputPath:string, snapshotId?:string|null, catalog?:string|null, credentials?:IcebergCredentials|null):Promise<any>{return new Promise(resolve=>{const c=spawn(PYTHON_BIN,[SCRIPT],{stdio:["pipe","pipe","pipe"],env:process.env});let o="",e="";c.stdout.on("data",d=>o+=d);c.stderr.on("data",d=>e+=d);c.on("close",code=>{if(code!==0)return resolve({ok:false,error:e||`resolver exited ${code}`});try{resolve(JSON.parse(o.trim()))}catch(x){resolve({ok:false,error:`invalid resolver output: ${String(x)}`})}});c.on("error",x=>resolve({ok:false,error:x.message}));c.stdin.end(JSON.stringify({table,snapshotId,outputPath,catalog,credentials}))})}

// Iceberg catalogs this backend can read: the default ICEBERG_* connection ("lakehouse") plus
// ICEBERG_EXTRA_CATALOGS_JSON entries. Only names and warehouses — never credentials.
// needsCredentials: an S3 warehouse with no keys in .env — the UI asks for them on each pull.
export function configuredIcebergCatalogs(): { name: string; warehouse: string | null; configured: boolean; needsCredentials: boolean }[] {
  const ready = (props: Record<string, unknown>) => Object.keys(props).length > 0 && !Object.values(props).some((v) => String(v).includes("<"));
  let base: Record<string, unknown> = {};
  try { base = JSON.parse(process.env.ICEBERG_CATALOG_PROPERTIES_JSON || "{}"); } catch { /* reported as not configured */ }
  if (process.env.ICEBERG_CATALOG_TYPE) base.type = process.env.ICEBERG_CATALOG_TYPE;
  let extras: Record<string, Record<string, unknown>> = {};
  try { extras = JSON.parse(process.env.ICEBERG_EXTRA_CATALOGS_JSON || "{}"); } catch { /* ignored */ }
  const needs = (warehouse: unknown, props: Record<string, unknown>) => String(warehouse ?? "").startsWith("s3://") && !props["s3.access-key-id"];
  const baseWh = process.env.ICEBERG_WAREHOUSE || null;
  return [
    { name: "lakehouse", warehouse: baseWh, configured: ready(base), needsCredentials: needs(baseWh, base) },
    ...Object.entries(extras).map(([name, p]) => ({ name, warehouse: (p.warehouse as string) ?? null, configured: ready(p), needsCredentials: needs(p.warehouse, p) })),
  ];
}
