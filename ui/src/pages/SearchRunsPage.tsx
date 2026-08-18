import { useState } from "react";
import { Link, useParams } from "react-router-dom";
import { api } from "../api";
import { Empty, Feedback, PageHeader, Panel, Status } from "../components";
import { useLoad } from "../hooks";

type SearchRun = {
  search_run_id: string;
  search_code: string;
  study_id: string;
  topic_id?: string;
  provider: string;
  mode: string;
  label: string;
  search_intent: string;
  provider_query: string;
  status: string;
  executed_at_utc: string;
  retrieved_count: number;
  new_evidence_count: number;
  existing_evidence_count: number;
  error_summary?: string;
};

type SearchRunDetail = SearchRun & {
  filters: Record<string, unknown>;
  sort: Record<string, unknown>;
  pagination: Record<string, unknown>;
  provider_metadata: Record<string, unknown>;
  hits: { search_hit_id:string; evidence_id:string; evidence_code:string; title:string; rank:number; provider_record_id:string }[];
};

type Study = { study_id: string; name: string };
type Topic = { topic_id: string; name: string };

export function SearchRunsPage() {
  const studies = useLoad(() => api<Study[]>("/studies"));
  const [study, setStudy] = useState("");
  const topics = useLoad(
    () => (study ? api<Topic[]>(`/studies/${study}/topics`) : Promise.resolve([])),
    [study],
  );
  const [topic, setTopic] = useState("");
  const [provider, setProvider] = useState("");
  const [mode, setMode] = useState("");
  const [status, setStatus] = useState("");
  const [searchCode, setSearchCode] = useState("");
  const [label, setLabel] = useState("");
  const [dateFrom, setDateFrom] = useState("");
  const [dateTo, setDateTo] = useState("");
  const params = new URLSearchParams();
  for (const [key, value] of Object.entries({study_id:study, topic_id:topic, provider, mode, status, search_code:searchCode, label, date_from:dateFrom, date_to:dateTo})) {
    if (value) params.set(key, value);
  }
  const state = useLoad(
    () => api<SearchRun[]>(`/search-runs?${params}`),
    [study, topic, provider, mode, status, searchCode, label, dateFrom, dateTo],
  );
  return <><PageHeader eyebrow="Trace" title="Search runs"><span className="count">{state.data?.length ?? 0} runs</span></PageHeader>
    <Panel><div className="filters run-filters">
      <select aria-label="Study" value={study} onChange={event=>{setStudy(event.target.value);setTopic("");}}><option value="">All studies</option>{studies.data?.map(value=><option key={value.study_id} value={value.study_id}>{value.name}</option>)}</select>
      <select aria-label="Topic" value={topic} onChange={event=>setTopic(event.target.value)} disabled={!study}><option value="">All topics</option>{topics.data?.map(value=><option key={value.topic_id} value={value.topic_id}>{value.name}</option>)}</select>
      <select aria-label="Provider" value={provider} onChange={event=>setProvider(event.target.value)}><option value="">All providers</option>{["scopus","arxiv","acl_anthology","ieee_xplore","wos"].map(value=><option key={value}>{value}</option>)}</select>
      <select aria-label="Mode" value={mode} onChange={event=>setMode(event.target.value)}><option value="">Explore and Save</option><option value="explore">explore</option><option value="save">save</option></select>
      <select aria-label="Run status" value={status} onChange={event=>setStatus(event.target.value)}><option value="">All statuses</option>{["completed","partial","failed"].map(value=><option key={value}>{value}</option>)}</select>
      <input aria-label="Search ID" value={searchCode} onChange={event=>setSearchCode(event.target.value)} placeholder="Q0001" />
      <input aria-label="Label" value={label} onChange={event=>setLabel(event.target.value)} placeholder="baseline" />
      <label>From date<input aria-label="From date" type="date" value={dateFrom} onChange={event=>setDateFrom(event.target.value)} /></label>
      <label>To date<input aria-label="To date" type="date" value={dateTo} onChange={event=>setDateTo(event.target.value)} /></label>
    </div>
      <Feedback loading={state.loading} error={state.error}>{state.data?.length ? <div className="run-table" role="table">{state.data.map(run=><article key={run.search_run_id}><div><Link to={`/search-runs/${run.search_run_id}`}><strong>{run.search_code}</strong></Link><small>{run.executed_at_utc}</small></div><div><span>{run.provider}</span><small>{run.mode} · {run.retrieved_count} retrieved</small></div><Status tone={run.status==="completed"?"good":run.status==="partial"?"warn":"bad"}>{run.status}</Status></article>)}</div> : <Empty>No search runs match these filters.</Empty>}</Feedback>
    </Panel></>;
}

export function SearchRunDetailPage() {
  const { runId = "" } = useParams();
  const state = useLoad(() => api<SearchRunDetail>(`/search-runs/${runId}`), [runId]);
  const run = state.data;
  return <><PageHeader eyebrow="Search run" title={run?.search_code || "Search run detail"}>{run && <Status tone={run.status==="completed"?"good":run.status==="partial"?"warn":"bad"}>{run.status}</Status>}</PageHeader>
    <Feedback loading={state.loading} error={state.error}>{run && <><div className="metric-grid"><Panel className="metric"><span>Retrieved</span><strong>{run.retrieved_count}</strong></Panel><Panel className="metric"><span>New evidence</span><strong>{run.new_evidence_count}</strong></Panel><Panel className="metric"><span>Already known</span><strong>{run.existing_evidence_count}</strong></Panel></div><div className="two-column wide-left"><Panel title="Exact request"><dl className="detail-list"><div><dt>Provider</dt><dd>{run.provider}</dd></div><div><dt>Mode</dt><dd>{run.mode}</dd></div><div><dt>Purpose</dt><dd>{run.search_intent}</dd></div><div><dt>Provider query</dt><dd><code>{run.provider_query}</code></dd></div><div><dt>Executed</dt><dd>{run.executed_at_utc}</dd></div></dl>{run.error_summary && <p className="error">{run.error_summary}</p>}</Panel><Panel title="Request settings"><pre className="query-panel">{JSON.stringify({filters:run.filters,sort:run.sort,pagination:run.pagination}, null, 2)}</pre></Panel></div><Panel title={`${run.hits.length} saved discoveries`}>{run.hits.length ? <div className="card-list">{run.hits.map(hit=><article key={hit.search_hit_id}><div><Link to={`/evidence/${hit.evidence_id}`}><strong>{hit.evidence_code} · {hit.title}</strong></Link><small>Rank {hit.rank} · {hit.provider_record_id}</small></div></article>)}</div> : <Empty>Explore runs record counts only; Save runs retain discoveries here.</Empty>}</Panel></>}</Feedback>
  </>;
}
