import React, { useEffect } from "react";
import { useApp } from "../context/AppContext";
import { useListTemplatesQuery } from "../store/apiSlice";
import { PH, StatCards, Pill, Loading, ErrBox, Drawer, IconExternal } from "../components/ui";
import type { PageFocusProps } from "../searchTarget";
import type { ConnectorTemplate } from "../types/api";
import { parseTransformSpecs } from "../templateAdapters";
import { TemplateIcon } from "../components/TemplateIcon";

function tplInfo(t: ConnectorTemplate) {
  return {
    desc: t.description || `${t.name} is a ${t.category} ${t.connector_type} connector that moves data via Kafka Connect.`,
    docs: t.instructions_url || "",
  };
}

function TemplateView({ t, onClose }: { t: ConnectorTemplate; onClose: () => void }) {
  const transformSpecs = parseTransformSpecs(t.supported_transforms);
  const info = tplInfo(t);
  const footer = (
    <>
      <button className="btn-s" onClick={onClose}>Close</button>
      {info.docs
        ? <a className="btn-p" href={info.docs} target="_blank" rel="noopener noreferrer" style={{ textDecoration: "none", display: "inline-flex", alignItems: "center", gap: 6 }}>View instructions <IconExternal /></a>
        : <span className="tdm">No external instructions</span>}
    </>
  );
  return (
    <Drawer title={t.name} onClose={onClose} footer={footer}>
      <div style={{ display: "flex", alignItems: "center", gap: 10, marginBottom: 14 }}>
        <div className="tpl-logo tpl-logo-lg"><TemplateIcon template={t} size={26} /></div>
        <div>
          <div style={{ fontWeight: 500 }}>{t.name}</div>
          <div style={{ fontSize: 11, color: "var(--color-text-secondary)" }}>
            <Pill cls={t.connector_type === "source" ? "bb" : "bp"}>{t.connector_type}</Pill> · {t.category}
          </div>
        </div>
      </div>
      <div className="fsec">Description</div>
      <div style={{ fontSize: 12, lineHeight: 1.6, marginBottom: 14 }}>{info.desc}</div>
      <div className="fsec">Details</div>
      <div className="kv"><span className="kvk">Connector class</span><span className="kvv" style={{ fontFamily: "var(--font-mono)", fontSize: 10, fontWeight: 400, wordBreak: "break-all", maxWidth: "60%", textAlign: "right" }}>{t.connector_class}</span></div>
      <div className="kv"><span className="kvk">Runtime pool</span><span className="kvv">{t.runtime_pool_type}</span></div>
      <div className="kv"><span className="kvk">Version</span><span className="kvv">{t.connector_version || t.version}</span></div>
      <div className="kv"><span className="kvk">Status</span><span className="kvv"><Pill cls={t.is_active ? "bg" : "bgr"}>{t.is_active ? "active" : "inactive"}</Pill></span></div>
      <div className="fsec" style={{ marginTop: 14 }}>Supported transformations ({transformSpecs.length})</div>
      {transformSpecs.length
        // supported_transforms is free-form JSON on the wire; templateAdapters.ts validates it into typed specs.
        ? transformSpecs.map((x, i) => (
          <div className="smt" key={i} style={{ gridTemplateColumns: "150px 1fr", marginBottom: 6 }}>
            <div className="smt-name">{x.type}</div>
            <span style={{ fontSize: 11, color: "var(--color-text-secondary)" }}>{x.description}</span>
          </div>
        ))
        : <div className="empty">None listed</div>}
    </Drawer>
  );
}

export default function Templates({ focus, clearFocus }: PageFocusProps) {
  const { openModal, closeModal } = useApp();
  const { data: tplsData, isLoading: loading, error } = useListTemplatesQuery();

  // A template picked in the top bar's search: open its details once the catalog is here.
  useEffect(() => {
    if (focus?.kind !== "template" || !tplsData) return;
    const picked = tplsData.find((t) => t.id === focus.id);
    if (picked) openModal(<TemplateView t={picked} onClose={closeModal} />);
    clearFocus?.();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [focus, tplsData, clearFocus]);

  if (loading) return <Loading />;
  if (error) return <ErrBox error={error} />;

  const tpls = [...(tplsData || [])].sort((a, b) => a.name.localeCompare(b.name));
  const sources = tpls.filter((t) => t.connector_type === "source").length;

  return (
    <>
      <PH title="Templates" sub="Prebooted connector templates · read-only catalog" />
      <StatCards cards={[
        { k: "bl", l: "Templates", v: tpls.length },
        { k: "gn", l: "Sources", v: sources },
        { k: "bp", l: "Sinks", v: tpls.length - sources },
        { k: "am", l: "Categories", v: new Set(tpls.map((t) => t.category)).size },
      ]} />
      <div className="tpl-grid">
        {tpls.length ? tpls.map((t) => {
          const short = (tplInfo(t).desc || "").slice(0, 90) + (tplInfo(t).desc.length > 90 ? "…" : "");
          return (
            <button className="tplcard" key={t.id} onClick={() => openModal(<TemplateView t={t} onClose={closeModal} />)} title="Click for details & instructions" aria-label={`${t.name} — view details`}>
              <div className="tpl-top">
                <div className="tpl-logo"><TemplateIcon template={t} /></div>
                <Pill cls={t.connector_type === "source" ? "bb" : "bp"}>{t.connector_type}</Pill>
              </div>
              <div className="tpl-name">{t.name}</div>
              <div className="tpl-desc">{short}</div>
              <div className="tpl-foot">
                <span className="tdm" style={{ padding: 0, fontSize: 10 }}>{t.category}</span>
                <span style={{ fontSize: 10, color: "var(--color-primary)", fontWeight: 500 }}>Details →</span>
              </div>
            </button>
          );
        }) : <div className="empty">No templates</div>}
      </div>
    </>
  );
}
