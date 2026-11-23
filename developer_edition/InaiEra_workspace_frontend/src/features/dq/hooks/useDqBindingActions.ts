import { useDispatch } from "react-redux";
import {
  useApplyDqBindingMutation,
  useDeleteDqBindingMutation,
  useRefreshDqRenderMutation,
  useResolveDqBindingMutation,
} from "../api";
import { apiErrorMessage, asApiError } from "../../../api/errors";
import { notify } from "../../../store/uiSlice";
import { askConfirm } from "../../../shared/ui/dialogStore";
import type { AppDispatch } from "../../../store";
import type { DqBinding } from "../../../types";
import { dqErrorMessage } from "../dqUtils";

/**
 * Per-binding actions for the Data Quality panel. `selected` /
 * `onSelectedChange` let resolve/delete keep the panel's open binding in sync.
 */
export function useDqBindingActions(selected: DqBinding | null, onSelectedChange: (b: DqBinding | null) => void) {
  const dispatch = useDispatch<AppDispatch>();
  const [deleteBinding] = useDeleteDqBindingMutation();
  const [refreshRender, { isLoading: refreshing }] = useRefreshDqRenderMutation();
  const [applyBinding, { isLoading: applying }] = useApplyDqBindingMutation();
  const [resolveBinding, { isLoading: resolving }] = useResolveDqBindingMutation();

  // Re-resolve the binding's job (via its runner) and dataset — DQ
  // refuses to apply an unresolved binding (DQ-BIND-003).
  const resolve = async (b: DqBinding) => {
    try {
      const r = await resolveBinding(b.id).unwrap();
      if (selected?.id === b.id) onSelectedChange({ ...selected, ...r });
      dispatch(notify({ kind: "success", message: `Resolved binding on "${b.target_table}"` }));
    } catch (e) {
      dispatch(notify({ kind: "error", message: dqErrorMessage(e, "Resolve failed") }));
    }
  };

  /** Returns true when the binding was removed. */
  const remove = async (b: DqBinding): Promise<boolean> => {
    if (!(await askConfirm({ title: "Remove quality binding", message: `Remove the quality binding on "${b.target_table}"? The job stops running these checks.`, confirmLabel: "Remove", danger: true }))) return false;
    try {
      await deleteBinding(b.id).unwrap();
      dispatch(notify({ kind: "success", message: `Removed binding on "${b.target_table}"` }));
      return true;
    } catch (e) {
      dispatch(notify({ kind: "error", message: apiErrorMessage(e, "Remove failed", { prefer: "detail" }) }));
      return false;
    }
  };

  const refresh = async () => {
    if (!selected) return;
    try {
      await refreshRender(selected.id).unwrap();
      dispatch(notify({ kind: "success", message: "Artifact re-rendered from the current ruleset" }));
    } catch (e) {
      dispatch(notify({ kind: "error", message: apiErrorMessage(e, "Re-render failed", { prefer: "detail" }) }));
    }
  };

  // Rendering only produces the artifact. Until it is pushed to the runner the
  // job behaves exactly as before — this is the step that actually arms it.
  // (Not wired to a button in the panel today; kept with its error handling.)
  const apply = async () => {
    if (!selected) return;
    try {
      const r = await applyBinding(selected.id).unwrap();
      if (!r.applied) {
        dispatch(notify({ kind: "error", message: r.error ?? r.message ?? "Nothing was applied" }));
        return;
      }
      dispatch(notify({ kind: "success", message: r.message ?? "Applied to the job" }));
    } catch (e) {
      // 422 = DQ refused (unresolved binding, unsupported blocking rule,
      // incompatible engine, contract mismatch…); 502 = the runner failed.
      if (asApiError(e).data?.reason === "binding_unresolved") {
        if (await askConfirm({
          title: "Binding needs resolving",
          message: "This binding hasn't been resolved against its job and dataset yet, so DQ won't apply it. Resolve it now, then apply again?",
          confirmLabel: "Resolve",
        })) await resolve(selected);
        return;
      }
      dispatch(notify({ kind: "error", message: dqErrorMessage(e, "Apply failed") }));
    }
  };

  return { resolve, remove, refresh, apply, refreshing, applying, resolving };
}
