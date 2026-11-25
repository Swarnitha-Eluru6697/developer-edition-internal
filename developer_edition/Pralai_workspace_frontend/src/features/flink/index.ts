/** Flink feature module: FlinkSQL jobs (proxied FlinkSQL Runner) + editor validation. */
export { FlinkPanel } from "./components/FlinkPanel";
export { FlinkValidationStrip } from "./components/FlinkValidationStrip";
export { useFlinkValidation } from "./hooks/useFlinkValidation";
export { FLINKSQL_TEMPLATE, flinkSinks, flinkUiJobUrl, stripSqlComments } from "./flinkSql";
export { parseProvenance, provenanceOf, userDescription, type Provenance } from "./provenance";
export * from "./api";
