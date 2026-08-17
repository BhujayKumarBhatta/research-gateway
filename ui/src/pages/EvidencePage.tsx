import { useState } from "react";
import { Link } from "react-router-dom";
import { api, post } from "../api";
import { Empty, Feedback, PageHeader, Panel, Status } from "../components";
import { useLoad } from "../hooks";

type Evidence = { evidence_id:string;evidence_code:string;title:string;author_names:string;year:number;publication:string;normalized_doi:string;screening_status:string;final_corpus:boolean };
type Page = { items: Evidence[]; total:number };

export function EvidencePage() {
  const [query,setQuery]=useState(""); const [status,setStatus]=useState("");
  const state=useLoad(()=>api<Page>(`/evidence?query=${encodeURIComponent(query)}&status=${encodeURIComponent(status)}`),[query,status]);
  async function screen(id:string,next:string){await post(`/evidence/${id}/screening`,{status:next,actor:"ui"});await state.refresh();}
  return <><PageHeader eyebrow="Review" title="Evidence corpus"><span className="count">{state.data?.total??0} records</span></PageHeader><Panel><div className="filters"><input aria-label="Search evidence" value={query} onChange={e=>setQuery(e.target.value)} placeholder="Title, author, venue, or DOI"/><select aria-label="Screening status" value={status} onChange={e=>setStatus(e.target.value)}><option value="">All decisions</option>{["unreviewed","candidate","included","excluded","final"].map(s=><option key={s}>{s}</option>)}</select></div><Feedback loading={state.loading} error={state.error}>{state.data?.items.length?<div className="evidence-list">{state.data.items.map(item=><article key={item.evidence_id}><div className="evidence-main"><div><span className="code">{item.evidence_code}</span><Status tone={item.final_corpus?"good":"neutral"}>{item.screening_status}</Status></div><h2><Link to={`/evidence/${item.evidence_id}`}>{item.title||"Untitled"}</Link></h2><p>{item.author_names || "Unknown author"} · {item.year||"No year"} · {item.publication||"Unknown venue"}</p><small>{item.normalized_doi||"No DOI"}</small></div><div className="decision"><label>Decision<select value={item.screening_status} onChange={e=>screen(item.evidence_id,e.target.value)}>{["unreviewed","candidate","included","excluded","final"].map(s=><option key={s}>{s}</option>)}</select></label></div></article>)}</div>:<Empty>No evidence matches these filters. Save a search to build the corpus.</Empty>}</Feedback></Panel></>;
}
