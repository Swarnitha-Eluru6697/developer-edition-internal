/** The jobs a rule or a channel can be limited to: every dbt / SQL / Python project and Flink job the caller may see. */
import { useMemo } from "react";
import { canOpenSection } from "../../app/navigation";
import { useListDbtProjectsQuery } from "../dbt/api";
import { kindOf } from "../dbt/jobKinds";
import { useListFlinkJobsQuery } from "../flink/api";
import { usePermissions } from "../../rbac/hooks";

export interface JobChoice { key: string; name: string; kind: "dbt" | "sql" | "python" | "flink" }

export function useJobChoices(): JobChoice[] {
  const permissions = usePermissions();
  const { data: projects } = useListDbtProjectsQuery({}, { skip: !canOpenSection("dbt", permissions) });
  const { data: flink } = useListFlinkJobsQuery(undefined, { skip: !canOpenSection("flink", permissions) });
  return useMemo(() => [
    ...(projects ?? []).map((p) => ({ key: p.id, name: p.name, kind: kindOf(p) })),
    ...(flink ?? []).map((j) => ({ key: j.id, name: j.name, kind: "flink" as const })),
  ].sort((a, b) => a.name.localeCompare(b.name)), [projects, flink]);
}
