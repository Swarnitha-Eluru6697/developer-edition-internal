import type { BaseModel, CatalogModel, Dataset } from "./types/domain";

/** Where the top-bar search sends a term, matched case-insensitively.
 *
 *  An exact name opens the thing itself: a registered model's drawer on the tab
 *  its lifecycle puts it in, a base model's page, a dataset's detail. A partial
 *  name lands on the list it matches — the model tab holding the first match,
 *  else Datasets — filtered to the term. */
export function resolveTopbarSearch(term: string, models: CatalogModel[], base: BaseModel[],
  datasets: Dataset[]): string {
  const t = term.trim().toLowerCase();
  const q = encodeURIComponent(term.trim());
  const tabOf = (m: CatalogModel) => (m.deployed ? "deployed" : m.versions.length > 0 ? "trained" : "training");

  const model = models.find((m) => m.id.toLowerCase() === t);
  if (model) return `/catalog?tab=${tabOf(model)}&model=${encodeURIComponent(model.id)}`;
  const b = base.find((x) => x.id.toLowerCase() === t || x.title.toLowerCase() === t);
  if (b) return `/base-models/${encodeURIComponent(b.id)}`;
  const d = datasets.find((x) => x.name.toLowerCase() === t);
  if (d) return `/datasets?open=${encodeURIComponent(d.name)}`;

  const modelHit = models.find((m) => m.id.toLowerCase().includes(t));
  if (modelHit) return `/catalog?tab=${tabOf(modelHit)}&q=${q}`;
  if (base.some((x) => x.id.toLowerCase().includes(t) || x.title.toLowerCase().includes(t)))
    return `/catalog?tab=base&q=${q}`;
  if (datasets.some((x) => x.name.toLowerCase().includes(t))) return `/datasets?q=${q}`;
  return `/catalog?tab=base&q=${q}`;
}
