// Class-based error boundary around the WebGL map. A single bad shader
// line must cost one panel, not the whole app: while crashed, the HUD and
// star field (rendered outside this boundary) stay alive, and "Повторить"
// remounts only the sigma subtree via a nonce key.
import { Component, Fragment, type ErrorInfo, type ReactNode } from "react";

interface GraphErrorBoundaryProps {
  children: ReactNode;
}

interface GraphErrorBoundaryState {
  error: Error | null;
  /** bumped on retry — remounts children so a poisoned canvas is discarded */
  nonce: number;
}

export class GraphErrorBoundary extends Component<GraphErrorBoundaryProps, GraphErrorBoundaryState> {
  state: GraphErrorBoundaryState = { error: null, nonce: 0 };

  static getDerivedStateFromError(error: Error): Partial<GraphErrorBoundaryState> {
    return { error };
  }

  componentDidCatch(error: Error, info: ErrorInfo): void {
    // console-only for now: the map is decorative, alerts would be noise
    console.error("[graph] map crashed:", error, info.componentStack);
  }

  retry = (): void => {
    this.setState((state) => ({ error: null, nonce: state.nonce + 1 }));
  };

  render(): ReactNode {
    const { error, nonce } = this.state;
    if (error) {
      return (
        <div className="state-block error graph-empty graph-crash">
          <i className="bi bi-hdd-stack" aria-hidden="true" />
          <h3>Карта временно недоступна</h3>
          <p>Созвездие не собралось: {(error.message || "неизвестная ошибка рендера").slice(0, 200)}</p>
          <button className="btn" onClick={this.retry}>
            <i className="bi bi-arrow-clockwise" aria-hidden="true" /> Повторить
          </button>
        </div>
      );
    }
    return <Fragment key={nonce}>{this.props.children}</Fragment>;
  }
}
