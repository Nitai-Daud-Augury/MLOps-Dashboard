import { Component, type ErrorInfo, type ReactNode } from 'react';

/**
 * Catches render errors so a crash shows the error (and a way out) instead of a blank screen.
 * `resetKey` (e.g. the pathname) clears the error on navigation.
 */
export class ErrorBoundary extends Component<{ children: ReactNode; resetKey?: string }, { error: Error | null; key?: string }> {
  state: { error: Error | null; key?: string } = { error: null, key: this.props.resetKey };

  static getDerivedStateFromError(error: Error) { return { error }; }

  static getDerivedStateFromProps(props: { resetKey?: string }, state: { error: Error | null; key?: string }) {
    return props.resetKey !== state.key ? { error: null, key: props.resetKey } : null;
  }

  componentDidCatch(error: Error, info: ErrorInfo) { console.error('UI render error', error, info.componentStack); }

  render() {
    const { error } = this.state;
    if (!error) return this.props.children;
    return <div className="card section" role="alert">
      <h2>This page failed to render</h2>
      <p className="helper">The rest of the dashboard still works. Copy the error below when reporting it.</p>
      <pre className="mono wrap" style={{ whiteSpace: 'pre-wrap', margin: 0, padding: 12, background: 'var(--surface-2)', borderRadius: 8 }}>{error.name}: {error.message}</pre>
      <div className="actions">
        <button type="button" className="btn" onClick={() => this.setState({ error: null })}>Try again</button>
        <a className="btn" href="/monitor">Go to Fleet coverage</a>
      </div>
    </div>;
  }
}
