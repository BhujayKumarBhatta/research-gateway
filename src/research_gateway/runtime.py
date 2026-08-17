from __future__ import annotations

from dataclasses import dataclass

from research_gateway.config import Settings
from research_gateway.db.database import EvidenceDatabase
from research_gateway.integrations.github import GithubAdapter
from research_gateway.integrations.zotero import ZoteroAdapter
from research_gateway.services.exports import ExportService
from research_gateway.services.research import ResearchService
from research_gateway.sources.acl_anthology import AclAnthologyAdapter
from research_gateway.sources.acm_dl import AcmDlAdapter
from research_gateway.sources.arxiv import ArxivAdapter
from research_gateway.sources.ieee_xplore import IeeeXploreAdapter
from research_gateway.sources.registry import SourceRegistry
from research_gateway.sources.scopus import ScopusAdapter
from research_gateway.sources.wos import WosAdapter


@dataclass
class GatewayRuntime:
    settings: Settings
    database: EvidenceDatabase
    sources: SourceRegistry
    research: ResearchService
    exports: ExportService
    zotero: ZoteroAdapter
    github: GithubAdapter

    @classmethod
    def build(cls, settings: Settings) -> GatewayRuntime:
        database = EvidenceDatabase(settings.database.path)
        sources = SourceRegistry(
            [
                ScopusAdapter(settings.scopus),
                ArxivAdapter(settings.arxiv),
                AclAnthologyAdapter(settings.acl_anthology),
                IeeeXploreAdapter(settings.ieee_xplore),
                WosAdapter(settings.wos),
                AcmDlAdapter(settings.acm_dl),
            ]
        )
        return cls(
            settings=settings,
            database=database,
            sources=sources,
            research=ResearchService(database, sources),
            exports=ExportService(database),
            zotero=ZoteroAdapter(settings.zotero, database),
            github=GithubAdapter(settings.github, database),
        )

    async def start(self) -> None:
        await self.database.migrate()

    async def aclose(self) -> None:
        await self.sources.aclose()
        await self.zotero.aclose()
        await self.github.aclose()

    def source_statuses(self) -> list[dict[str, object]]:
        return [
            *self.sources.statuses(),
            self.zotero.status,
            self.github.status,
        ]
