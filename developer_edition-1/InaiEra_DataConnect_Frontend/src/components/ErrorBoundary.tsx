/*
 * FE-BUG-22: DataConnect is mounted INSIDE the Control Portal's page. An
 * uncaught render error in a React tree unmounts that whole tree, so without a
 * boundary a bad payload on one screen would blank DataConnect inside the
 * host (and the host would get no signal). This catches it, shows a contained
 * message instead, and reports it through the host's optional `onError`.
 *
 * It only catches errors thrown while rendering — not event handlers or async
 * code, which already surface through toasts / FailureDialog.
 *
 * `resetKey`: when it changes (e.g. the user navigates to another page) the
 * boundary clears itself and tries rendering again, so one broken screen
 * doesn't strand the user until they reload.
 */
import React from "react";

interface Props {
  children: React.ReactNode;
  /** Short name of what failed, shown in the message ("This page", "DataConnect"). */
  label?: string;
  /** Host callback; never allowed to throw into the boundary itself. */
  onError?: (error: Error) => void;
  resetKey?: unknown;
}

interface State { error: Error | null }

export default class ErrorBoundary extends React.Component<Props, State> {
  state: State = { error: null };

  static getDerivedStateFromError(error: Error): State {
    return { error };
  }

  componentDidCatch(error: Error): void {
    try { this.props.onError?.(error); } catch { /* a faulty host callback must not take the fallback down too */ }
  }

  componentDidUpdate(prev: Props): void {
    if (this.state.error && prev.resetKey !== this.props.resetKey) this.setState({ error: null });
  }

  render(): React.ReactNode {
    if (!this.state.error) return this.props.children;
    const label = this.props.label ?? "This page";
    return (
      <div className="alert alert-w" role="alert" style={{ margin: 13 }}>
        <div className="at">{label} hit an unexpected error</div>
        <div className="ab">
          Something went wrong while showing this screen. Your data is unaffected.
          Try another page or reload; if it keeps happening, report it with what you were doing.
        </div>
        <button type="button" className="btn-s" style={{ marginTop: 8 }} onClick={() => this.setState({ error: null })}>Try again</button>
      </div>
    );
  }
}
