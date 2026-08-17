import { NavLink, Route, Routes } from "react-router-dom";
import { AuditPage } from "./pages/AuditPage";
import { DashboardPage } from "./pages/DashboardPage";
import { EvidenceDetailPage } from "./pages/EvidenceDetailPage";
import { EvidencePage } from "./pages/EvidencePage";
import { ExportsPage } from "./pages/ExportsPage";
import { IntegrationsPage } from "./pages/IntegrationsPage";
import { SearchPage } from "./pages/SearchPage";
import { SearchRunDetailPage, SearchRunsPage } from "./pages/SearchRunsPage";
import { SourcesPage } from "./pages/SourcesPage";
import { StudiesPage, StudyDetailPage, TopicDetailPage } from "./pages/StudiesPage";

const links = [
  ["/", "Overview"], ["/studies", "Studies"], ["/search", "Search"], ["/search-runs", "Search runs"],
  ["/evidence", "Evidence"], ["/sources", "Sources"], ["/exports", "Exports"],
  ["/integrations", "Integrations"], ["/audit", "Audit"],
];

export default function App() {
  return (
    <div className="shell">
      <aside className="sidebar">
        <div className="brand"><span className="brand-mark">RG</span><div><strong>Research Gateway</strong><small>Evidence workspace</small></div></div>
        <nav aria-label="Primary">
          {links.map(([to, label]) => <NavLink key={to} to={to} end={to === "/"}>{label}</NavLink>)}
        </nav>
        <div className="local-note"><span className="pulse" />Local workspace<small>Remote UI is disabled</small></div>
      </aside>
      <main>
        <Routes>
          <Route path="/" element={<DashboardPage />} />
          <Route path="/studies" element={<StudiesPage />} />
          <Route path="/studies/:studyId" element={<StudyDetailPage />} />
          <Route path="/topics/:topicId" element={<TopicDetailPage />} />
          <Route path="/search" element={<SearchPage />} />
          <Route path="/search-runs" element={<SearchRunsPage />} />
          <Route path="/search-runs/:runId" element={<SearchRunDetailPage />} />
          <Route path="/evidence" element={<EvidencePage />} />
          <Route path="/evidence/:evidenceId" element={<EvidenceDetailPage />} />
          <Route path="/sources" element={<SourcesPage />} />
          <Route path="/exports" element={<ExportsPage />} />
          <Route path="/integrations" element={<IntegrationsPage />} />
          <Route path="/audit" element={<AuditPage />} />
        </Routes>
      </main>
    </div>
  );
}
