import { FormEvent, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { api, post } from "../api";
import { Empty, Feedback, PageHeader, Panel, Status } from "../components";
import { useLoad } from "../hooks";

type EvidenceDetail = {
  evidence_id:string; evidence_code:string; title:string; authors:{name?:string}[]; author_names:string;
  year?:number; publication?:string; document_type?:string; abstract?:string; normalized_doi?:string;
  url?:string; screening_status:string; screening_reason?:string; final_corpus:boolean;
  identifiers:{identifier_type:string;identifier_value:string;source_provider:string}[];
  discoveries:{search_run_id:string;search_code:string;provider:string;rank:number;discovered_at:string}[];
  screening_history:{screening_event_id:string;old_status?:string;new_status:string;reason?:string;note?:string;actor:string;timestamp_utc:string}[];
  notes:{note_id:string;text:string;actor:string;created_at:string}[];
};

export function EvidenceDetailPage() {
  const { evidenceId = "" } = useParams();
  const state = useLoad(() => api<EvidenceDetail>(`/evidence/${evidenceId}`), [evidenceId]);
  const [note, setNote] = useState("");
  const [message, setMessage] = useState("");
  async function addNote(event:FormEvent) { event.preventDefault(); setMessage(""); try { await post(`/evidence/${evidenceId}/notes`, {text:note,actor:"ui"}); setNote(""); await state.refresh(); } catch(error) { setMessage(String(error)); } }
  const item = state.data;
  return <><PageHeader eyebrow="Evidence record" title={item?.title || "Evidence detail"}>{item && <Status tone={item.final_corpus?"good":"neutral"}>{item.screening_status}</Status>}</PageHeader>
    <Feedback loading={state.loading} error={state.error}>{item && <><div className="two-column wide-left"><Panel title="Bibliographic record"><dl className="detail-list"><div><dt>Stable code</dt><dd>{item.evidence_code}</dd></div><div><dt>Authors</dt><dd>{item.author_names || item.authors.map(author=>author.name).filter(Boolean).join(", ") || "Unknown"}</dd></div><div><dt>Published</dt><dd>{item.year || "Unknown year"} · {item.publication || "Unknown venue"}</dd></div><div><dt>Type</dt><dd>{item.document_type || "Unknown"}</dd></div><div><dt>DOI</dt><dd>{item.normalized_doi || "None recorded"}</dd></div></dl>{item.abstract && <><h3>Abstract</h3><p>{item.abstract}</p></>}{item.url && <a href={item.url} target="_blank" rel="noreferrer">Open source record</a>}</Panel><Panel title="Identifiers">{item.identifiers.length ? <ul>{item.identifiers.map(identifier=><li key={`${identifier.identifier_type}:${identifier.identifier_value}`}><strong>{identifier.identifier_type}</strong><br/><code>{identifier.identifier_value}</code><br/><small>{identifier.source_provider}</small></li>)}</ul> : <Empty>No external identifiers.</Empty>}</Panel></div><div className="two-column"><Panel title="Discovered by">{item.discoveries.length ? <div className="card-list">{item.discoveries.map(discovery=><article key={`${discovery.search_run_id}:${discovery.rank}`}><Link to={`/search-runs/${discovery.search_run_id}`}><strong>{discovery.search_code} · {discovery.provider}</strong></Link><small>Rank {discovery.rank} · {discovery.discovered_at}</small></article>)}</div> : <Empty>No discovery links.</Empty>}</Panel><Panel title="Screening history">{item.screening_history.length ? <div className="timeline">{item.screening_history.map(event=><article key={event.screening_event_id}><strong>{event.old_status || "new"} → {event.new_status}</strong><p>{event.reason || event.note || "No reason recorded"}</p><small>{event.actor} · {event.timestamp_utc}</small></article>)}</div> : <Empty>No screening decisions yet.</Empty>}</Panel></div><Panel title="Research notes"><form className="inline-form" onSubmit={addNote}><input aria-label="New research note" required value={note} onChange={event=>setNote(event.target.value)} placeholder="Add a concise note"/><button className="primary">Add note</button></form>{message && <p className="error">{message}</p>}{item.notes.length ? <div className="timeline">{item.notes.map(entry=><article key={entry.note_id}><p>{entry.text}</p><small>{entry.actor} · {entry.created_at}</small></article>)}</div> : <Empty>No notes yet.</Empty>}</Panel></>}</Feedback>
  </>;
}
