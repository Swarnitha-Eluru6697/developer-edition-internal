import type { NavigateFunction } from "react-router-dom";
import type { CatalogModel } from "../types/domain";

/**
 * Where clicking a model goes, decided in exactly one place, so the catalog and
 * a base model's "derived models" list can't disagree about it.
 *
 * The rule is the mock's `openModelOrTrainingRun`: a model with a run IN FLIGHT
 * has no versions worth looking at yet — the interesting thing is the run, so go
 * straight to it. Anything else opens the drawer, where the useful answers live:
 * version history, the endpoint serving it, and Test this model.
 *
 * Note this keys off `training` (queued or running right now), NOT `latestRunId`
 * (the newest run, which every model trained here has). Using the latter sent
 * every deployed model to a finished pipeline page instead of to its endpoint
 * and test panel — the run that produced a champion three weeks ago is history,
 * not what you clicked the card to find. That run is still one click away: the
 * drawer links to it.
 */
export function openModelOrTrainingRun(
  navigate: NavigateFunction,
  model: CatalogModel,
  onDrawer?: (m: CatalogModel) => void
) {
  if (model.training && model.trainingRunId) {
    navigate(`/training-runs/${model.trainingRunId}`);
    return;
  }
  // The catalog owns the drawer, so it passes one. Callers elsewhere (the
  // base-model page) hand off to the catalog with the model preselected rather
  // than rendering a second copy of the same drawer.
  if (onDrawer) onDrawer(model);
  else navigate(`/catalog?model=${encodeURIComponent(model.id)}`);
}
