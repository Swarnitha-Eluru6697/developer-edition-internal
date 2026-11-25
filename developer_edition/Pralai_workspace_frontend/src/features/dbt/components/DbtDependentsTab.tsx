/** The job's links tab: its upstream and downstream jobs. */
import { useState } from "react";
import type { DbtProject, UUID } from "../../../types";
import { kindOf } from "../jobKinds";
import { DbtJobLinks, type JobLinks } from "./DbtJobLinks";

interface Props {
  project: DbtProject;
  projects: DbtProject[];
  saving: boolean;
  onSave: (links: JobLinks) => void;
}

const same = (a: UUID[], b: UUID[]) => a.length === b.length && a.every((id) => b.includes(id));

export function DbtDependentsTab({ project, projects, saving, onSave }: Props) {
  const saved: JobLinks = { upstream: project.upstream_project_ids ?? [], downstream: project.dependent_project_ids ?? [] };
  const [links, setLinks] = useState<JobLinks>(saved);
  const changed = !same(links.upstream, saved.upstream) || !same(links.downstream, saved.downstream);
  return (
    <div style={{ maxWidth: 520 }}>
      <div style={{ fontSize: 13, fontWeight: 600, marginBottom: 12 }}>{project.name} — Upstream and downstream jobs</div>

      <DbtJobLinks selfId={project.id} kind={kindOf(project)} projects={projects} value={links} onChange={setLinks} />
      <button onClick={() => onSave(links)} disabled={!changed || saving}
        style={{ marginTop: 12, height: 32, padding: "0 18px", fontSize: 13, background: "#0A7D5A", color: "#fff", border: "none", borderRadius: 6,
          cursor: !changed || saving ? "default" : "pointer", opacity: !changed || saving ? 0.55 : 1 }}>
        {saving ? "Saving…" : "Save"}
      </button>
    </div>
  );
}
