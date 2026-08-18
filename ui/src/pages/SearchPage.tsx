import { FormEvent, useState } from "react";
import { api, post } from "../api";
import { Feedback, PageHeader, Panel, Status } from "../components";
import { useLoad } from "../hooks";

type Study = { study_id: string; name: string };
type Topic = { topic_id: string; name: string };
type Run = { search_run_id: string; search_code: string; provider: string; mode: string; provider_query: string; status: string; retrieved_count: number };

export function SearchPage() {
  const studies = useLoad(() => api<Study[]>("/studies"));
  const runs = useLoad(() => api<Run[]>("/search-runs"));
  const [form, setForm] = useState({study_id:"",topic_id:"",provider:"scopus",search_intent:"",provider_query:"",requested_limit:25,label:""});
  const topics = useLoad(
    () => form.study_id ? api<Topic[]>(`/studies/${form.study_id}/topics`) : Promise.resolve([]),
    [form.study_id],
  );
  const [mode, setMode] = useState<"explore"|"save">("explore"); const [message,setMessage]=useState("");
  async function submit(event: FormEvent) { event.preventDefault(); setMessage("Running…"); try { const result=await post<Run>(`/search/${mode}`, {...form, topic_id: form.topic_id || null}); setMessage(`${result.search_code} completed${mode === "save" ? `; ${result.retrieved_count} discoveries saved` : " without saving hits"}.`); await runs.refresh(); } catch(e){setMessage(String(e));} }
  return <><PageHeader eyebrow="Discover" title="Scholarly search" />
    <div className="two-column wide-left"><Panel title="Exact provider query"><form onSubmit={submit} className="stack"><div className="segmented"><button type="button" className={mode==="explore"?"active":""} onClick={()=>setMode("explore")}>Explore count</button><button type="button" className={mode==="save"?"active":""} onClick={()=>setMode("save")}>Save results</button></div><label>Study<select required value={form.study_id} onChange={e=>setForm({...form,study_id:e.target.value,topic_id:""})}><option value="">Choose…</option>{studies.data?.map(s=><option value={s.study_id} key={s.study_id}>{s.name}</option>)}</select></label><label>Topic<select value={form.topic_id} onChange={e=>setForm({...form,topic_id:e.target.value})} disabled={!form.study_id}><option value="">No topic</option>{topics.data?.map(t=><option value={t.topic_id} key={t.topic_id}>{t.name}</option>)}</select></label><label>Source<select value={form.provider} onChange={e=>setForm({...form,provider:e.target.value})}>{["scopus","arxiv","acl_anthology","ieee_xplore","wos"].map(p=><option key={p}>{p}</option>)}</select></label><label>Search label<input value={form.label} onChange={e=>setForm({...form,label:e.target.value})} placeholder="baseline" /></label><label>Why run this search?<input required value={form.search_intent} onChange={e=>setForm({...form,search_intent:e.target.value})} placeholder="Find evaluation studies" /></label><label>Exact provider query<textarea className="query" required value={form.provider_query} onChange={e=>setForm({...form,provider_query:e.target.value})} placeholder='TITLE-ABS-KEY("large language model")' /></label>{mode==="save"&&<label>Maximum records<input type="number" min="1" max="10000" value={form.requested_limit} onChange={e=>setForm({...form,requested_limit:Number(e.target.value)})}/></label>}<button className="primary">{mode==="explore"?"Explore without saving":"Save exact search"}</button>{message&&<p className="notice">{message}</p>}</form></Panel>
    <Panel title="Recent runs"><Feedback loading={runs.loading} error={runs.error}>{<div className="run-list">{runs.data?.slice(0,10).map(run=><article key={run.search_run_id}><div><strong>{run.search_code}</strong><Status tone={run.status==="completed"?"good":"bad"}>{run.status}</Status></div><small>{run.provider} · {run.mode}</small><code>{run.provider_query}</code></article>)}</div>}</Feedback></Panel></div></>;
}
