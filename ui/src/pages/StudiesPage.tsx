import { FormEvent, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { api, post } from "../api";
import { Empty, Feedback, PageHeader, Panel } from "../components";
import { useLoad } from "../hooks";

type Study = { study_id: string; name: string; description: string; status: string };
type Run = { search_run_id:string; search_code:string; provider:string; mode:string; provider_query:string };
type Topic = { topic_id:string; name:string; description:string };

export function StudiesPage() {
  const state = useLoad(() => api<Study[]>("/studies"));
  const [form, setForm] = useState({ study_id: "", name: "", description: "" });
  const [message, setMessage] = useState("");
  async function submit(event: FormEvent) {
    event.preventDefault(); setMessage("");
    try { await post("/studies", form); setForm({study_id:"",name:"",description:""}); await state.refresh(); }
    catch (error) { setMessage(String(error)); }
  }
  return <><PageHeader eyebrow="Organize" title="Studies" />
    <div className="two-column wide-left"><Panel title="Current studies"><Feedback loading={state.loading} error={state.error}>{state.data?.length ? <div className="card-list">{state.data.map(study => <article key={study.study_id}><div><Link to={`/studies/${study.study_id}`}><strong>{study.name}</strong></Link><small>{study.study_id}</small></div><p>{study.description || "No description"}</p></article>)}</div> : <Empty>Create a study to group topics, searches, and evidence.</Empty>}</Feedback></Panel>
    <Panel title="New study"><form onSubmit={submit} className="stack"><label>Stable ID<input required value={form.study_id} onChange={event=>setForm({...form,study_id:event.target.value})} placeholder="dissertation-2026" /></label><label>Name<input required value={form.name} onChange={event=>setForm({...form,name:event.target.value})} /></label><label>Plain-language purpose<textarea value={form.description} onChange={event=>setForm({...form,description:event.target.value})} /></label><button className="primary">Create study</button>{message && <p className="error">{message}</p>}</form></Panel></div></>;
}

type StudyDetail = Study & { topics:Topic[]; summary:{total:number;search_runs:number}; search_runs:Run[]; evidence:{evidence_id:string;evidence_code:string;title:string}[] };
export function StudyDetailPage() {
  const {studyId=""} = useParams();
  const state = useLoad(() => api<StudyDetail>(`/studies/${studyId}`), [studyId]);
  return <><PageHeader eyebrow="Study" title={state.data?.name || "Study detail"}/><Feedback loading={state.loading} error={state.error}>{state.data && <><div className="metric-grid"><Panel className="metric"><span>Evidence</span><strong>{state.data.summary.total}</strong></Panel><Panel className="metric"><span>Search runs</span><strong>{state.data.summary.search_runs}</strong></Panel></div><div className="two-column"><Panel title="Topics">{state.data.topics.length ? <div className="card-list">{state.data.topics.map(topic => <article key={topic.topic_id}><Link to={`/topics/${topic.topic_id}`}><strong>{topic.name}</strong></Link><p>{topic.description}</p></article>)}</div> : <Empty>No topics yet.</Empty>}</Panel><Panel title="Recent searches">{state.data.search_runs.map(run => <article key={run.search_run_id}><Link to={`/search-runs/${run.search_run_id}`}>{run.search_code} · {run.provider}</Link><code>{run.provider_query}</code></article>)}</Panel></div></>}</Feedback></>;
}

type TopicDetail = Topic & { search_runs:Run[]; evidence_count:number; evidence:{evidence_id:string;evidence_code:string;title:string}[] };
export function TopicDetailPage() {
  const {topicId=""} = useParams();
  const state = useLoad(() => api<TopicDetail>(`/topics/${topicId}`), [topicId]);
  return <><PageHeader eyebrow="Topic" title={state.data?.name || "Topic detail"}/><Feedback loading={state.loading} error={state.error}>{state.data && <div className="two-column"><Panel title={`${state.data.search_runs.length} search runs`}>{state.data.search_runs.map(run => <article key={run.search_run_id}><Link to={`/search-runs/${run.search_run_id}`}>{run.search_code} · {run.provider} · {run.mode}</Link><code>{run.provider_query}</code></article>)}</Panel><Panel title={`${state.data.evidence_count} deduplicated evidence`}>{state.data.evidence.map(item => <article key={item.evidence_id}><Link to={`/evidence/${item.evidence_id}`}>{item.evidence_code} · {item.title}</Link></article>)}</Panel></div>}</Feedback></>;
}
