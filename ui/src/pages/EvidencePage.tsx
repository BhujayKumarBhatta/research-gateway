import { useState } from "react";
import { Link } from "react-router-dom";
import { api, post } from "../api";
import { Empty, Feedback, PageHeader, Panel, Status } from "../components";
import { useLoad } from "../hooks";

type Study = { study_id: string; name: string };
type Topic = { topic_id: string; name: string };
type Evidence = {
  evidence_id: string;
  evidence_code: string;
  title: string;
  author_names: string;
  year: number;
  publication: string;
  normalized_doi: string;
  screening_status: string;
  final_corpus: boolean;
  publication_type?: string;
  review_status: string;
};
type Page = { items: Evidence[]; total: number; offset: number; limit: number };

export function EvidencePage() {
  const studies = useLoad(() => api<Study[]>("/studies"));
  const [study, setStudy] = useState("");
  const topics = useLoad(
    () => (study ? api<Topic[]>(`/studies/${study}/topics`) : Promise.resolve([])),
    [study],
  );
  const [topic, setTopic] = useState("");
  const [provider, setProvider] = useState("");
  const [searchCode, setSearchCode] = useState("");
  const [discoveredFrom, setDiscoveredFrom] = useState("");
  const [discoveredTo, setDiscoveredTo] = useState("");
  const [query, setQuery] = useState("");
  const [status, setStatus] = useState("");
  const [finalCorpus, setFinalCorpus] = useState("");
  const [year, setYear] = useState("");
  const [publicationType, setPublicationType] = useState("");
  const [reviewStatus, setReviewStatus] = useState("");
  const [offset, setOffset] = useState(0);
  const [limit, setLimit] = useState(50);

  const params = new URLSearchParams();
  for (const [key, value] of Object.entries({
    study_id: study,
    topic_id: topic,
    provider,
    search_code: searchCode,
    discovered_from: discoveredFrom,
    discovered_to: discoveredTo,
    query,
    status,
    final: finalCorpus,
    year,
    publication_type: publicationType,
    review_status: reviewStatus,
  })) {
    if (value) params.set(key, value);
  }
  params.set("offset", String(offset));
  params.set("limit", String(limit));
  const state = useLoad(
    () => api<Page>(`/evidence?${params}`),
    [
      study,
      topic,
      provider,
      searchCode,
      discoveredFrom,
      discoveredTo,
      query,
      status,
      finalCorpus,
      year,
      publicationType,
      reviewStatus,
      offset,
      limit,
    ],
  );

  function changeFilter(update: () => void) {
    setOffset(0);
    update();
  }

  async function screen(id: string, next: string) {
    await post(`/evidence/${id}/screening`, { status: next, actor: "ui" });
    await state.refresh();
  }

  const total = state.data?.total ?? 0;
  return (
    <>
      <PageHeader eyebrow="Review" title="Evidence corpus">
        <span className="count">{total} records</span>
      </PageHeader>
      <Panel>
        <div className="filters">
          <input aria-label="Search evidence" value={query} onChange={(event) => changeFilter(() => setQuery(event.target.value))} placeholder="Title, author, venue, or DOI" />
          <select aria-label="Study" value={study} onChange={(event) => changeFilter(() => { setStudy(event.target.value); setTopic(""); })}>
            <option value="">All studies</option>
            {studies.data?.map((item) => <option key={item.study_id} value={item.study_id}>{item.name}</option>)}
          </select>
          <select aria-label="Topic" value={topic} onChange={(event) => changeFilter(() => setTopic(event.target.value))} disabled={!study}>
            <option value="">All topics</option>
            {topics.data?.map((item) => <option key={item.topic_id} value={item.topic_id}>{item.name}</option>)}
          </select>
          <select aria-label="Source" value={provider} onChange={(event) => changeFilter(() => setProvider(event.target.value))}>
            <option value="">All sources</option>
            {["scopus", "arxiv", "acl_anthology", "ieee_xplore", "wos"].map((item) => <option key={item}>{item}</option>)}
          </select>
          <input aria-label="Search ID" value={searchCode} onChange={(event) => changeFilter(() => setSearchCode(event.target.value))} placeholder="Q0001" />
          <label>Discovery from<input aria-label="Discovery from" type="date" value={discoveredFrom} onChange={(event) => changeFilter(() => setDiscoveredFrom(event.target.value))} /></label>
          <label>Discovery to<input aria-label="Discovery to" type="date" value={discoveredTo} onChange={(event) => changeFilter(() => setDiscoveredTo(event.target.value))} /></label>
          <select aria-label="Screening status" value={status} onChange={(event) => changeFilter(() => setStatus(event.target.value))}>
            <option value="">All decisions</option>
            {["unreviewed", "candidate", "included", "excluded", "final"].map((item) => <option key={item}>{item}</option>)}
          </select>
          <select aria-label="Final corpus" value={finalCorpus} onChange={(event) => changeFilter(() => setFinalCorpus(event.target.value))}>
            <option value="">Final and non-final</option><option value="true">Final only</option><option value="false">Non-final only</option>
          </select>
          <input aria-label="Year" type="number" min="1000" max="9999" value={year} onChange={(event) => changeFilter(() => setYear(event.target.value))} placeholder="2026" />
          <select aria-label="Publication type" value={publicationType} onChange={(event) => changeFilter(() => setPublicationType(event.target.value))}>
            <option value="">All publication types</option>
            {["journal_article", "review_article", "conference_paper", "preprint", "book", "book_chapter", "standard", "other"].map((item) => <option key={item}>{item}</option>)}
          </select>
          <select aria-label="Review status" value={reviewStatus} onChange={(event) => changeFilter(() => setReviewStatus(event.target.value))}>
            <option value="">All review statuses</option>
            {["peer_reviewed", "preprint", "not_peer_reviewed", "unknown"].map((item) => <option key={item}>{item}</option>)}
          </select>
          <select aria-label="Page size" value={limit} onChange={(event) => { setOffset(0); setLimit(Number(event.target.value)); }}>
            {[25, 50, 100, 250].map((item) => <option key={item} value={item}>{item}</option>)}
          </select>
        </div>
        <Feedback loading={state.loading} error={state.error}>
          {state.data?.items.length ? (
            <div className="evidence-list">
              {state.data.items.map((item) => (
                <article key={item.evidence_id}>
                  <div className="evidence-main">
                    <div><span className="code">{item.evidence_code}</span><Status tone={item.final_corpus ? "good" : "neutral"}>{item.screening_status}</Status></div>
                    <h2><Link to={`/evidence/${item.evidence_id}`}>{item.title || "Untitled"}</Link></h2>
                    <p>{item.author_names || "Unknown author"} · {item.year || "No year"} · {item.publication || "Unknown venue"}</p>
                    <small>{item.normalized_doi || "No DOI"} · {item.publication_type || "unknown type"} · {item.review_status}</small>
                  </div>
                  <div className="decision"><label>Decision<select value={item.screening_status} onChange={(event) => screen(item.evidence_id, event.target.value)}>{["unreviewed", "candidate", "included", "excluded", "final"].map((value) => <option key={value}>{value}</option>)}</select></label></div>
                </article>
              ))}
            </div>
          ) : <Empty>No evidence matches these filters. Save a search to build the corpus.</Empty>}
        </Feedback>
        <div className="pagination">
          <button type="button" disabled={offset === 0} onClick={() => setOffset(Math.max(0, offset - limit))}>Previous</button>
          <span>{total ? `${offset + 1}–${Math.min(offset + limit, total)} of ${total}` : "0 records"}</span>
          <button type="button" disabled={offset + limit >= total} onClick={() => setOffset(offset + limit)}>Next</button>
        </div>
      </Panel>
    </>
  );
}
