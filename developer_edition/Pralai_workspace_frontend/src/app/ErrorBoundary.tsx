import { Component, type ReactNode } from "react";

// Surface render-time errors on screen instead of a blank page.
export class ErrorBoundary extends Component<{ children: ReactNode }, { error: Error | null }> {
  state = { error: null as Error | null };
  static getDerivedStateFromError(error: Error) {
    return { error };
  }
  componentDidCatch(error: Error, info: unknown) {
    // eslint-disable-next-line no-console
    console.error("App crashed:", error, info);
  }
  render() {
    if (this.state.error) {
      return (
        <div style={{ padding: 24, fontFamily: "monospace", color: "#fff", background: "#0f1117", height: "100vh", boxSizing: "border-box", overflow: "auto" }}>
          <h2 style={{ color: "#f87171" }}>UI crashed while rendering</h2>
          <pre style={{ whiteSpace: "pre-wrap", color: "#fca5a5" }}>{this.state.error.message}</pre>
          <pre style={{ whiteSpace: "pre-wrap", color: "#73827a", fontSize: 12 }}>{this.state.error.stack}</pre>
        </div>
      );
    }
    return this.props.children;
  }
}
