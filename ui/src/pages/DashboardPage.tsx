import { api } from "../api";
import { Feedback, PageHeader, Panel, Status } from "../components";
import { useLoad } from "../hooks";

type Summary = { total: number; unreviewed: number; included: number; excluded: number; final_count: number; search_runs: number };
type Source = { name: string; available: boolean; configured: boolean };
type Dashboard = { summary: Summary; sources: Source[] };

export function DashboardPage() {
  const state = useLoad(() => api<Dashboard>("/status"));
  return <><PageHeader eyebrow="Workspace" title="Research at a glance"><button onClick={() => state.refresh()}>Refresh</button></PageHeader>
    <Feedback loading={state.loading} error={state.error}>{state.data && <>
      <div className="metric-grid">
        {[["Evidence", state.data.summary.total], ["Needs review", state.data.summary.unreviewed], ["Included", state.data.summary.included], ["Final corpus", state.data.summary.final_count], ["Search runs", state.data.summary.search_runs]].map(([label, value]) => <Panel key={label as string} className="metric"><span>{label}</span><strong>{value}</strong></Panel>)}
      </div>
      <div className="two-column"><Panel title="What this workspace does"><p>Search trusted scholarly sources, keep every discovery path, screen evidence, and export a reviewable corpus. The Evidence Store is the local source of truth.</p></Panel>
      <Panel title="Source readiness"><div className="source-strip">{state.data.sources.map(source => <div key={source.name}><span>{source.name.replaceAll("_", " ")}</span><Status tone={source.available ? "good" : source.configured ? "warn" : "neutral"}>{source.available ? "Available" : "Unavailable"}</Status></div>)}</div></Panel></div>
    </>}</Feedback></>;
}
