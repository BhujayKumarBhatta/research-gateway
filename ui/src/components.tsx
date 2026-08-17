import type { PropsWithChildren, ReactNode } from "react";

export function PageHeader({ eyebrow, title, children }: PropsWithChildren<{ eyebrow: string; title: string }>) {
  return (
    <header className="page-header">
      <div><p className="eyebrow">{eyebrow}</p><h1>{title}</h1></div>
      {children && <div className="actions">{children}</div>}
    </header>
  );
}

export function Panel({ title, children, className = "" }: PropsWithChildren<{ title?: string; className?: string }>) {
  return <section className={`panel ${className}`}>{title && <h2>{title}</h2>}{children}</section>;
}

export function Status({ children, tone = "neutral" }: PropsWithChildren<{ tone?: "good" | "warn" | "bad" | "neutral" }>) {
  return <span className={`status status-${tone}`}>{children}</span>;
}

export function Empty({ children }: PropsWithChildren) {
  return <div className="empty">{children}</div>;
}

export function Feedback({ loading, error, children }: { loading: boolean; error: string; children: ReactNode }) {
  if (loading) return <div className="loading">Loading…</div>;
  if (error) return <div className="error" role="alert">{error}</div>;
  return <>{children}</>;
}
