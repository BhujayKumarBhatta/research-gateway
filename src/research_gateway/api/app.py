from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from mcp.server.transport_security import TransportSecuritySettings

from research_gateway.api.schemas import (
    ExportRequest,
    GithubPublishRequest,
    NoteCreate,
    ScreeningUpdate,
    SearchRequest,
    StudyCreate,
    TopicCreate,
    ZoteroSyncRequest,
)
from research_gateway.api.security import RemoteSurfaceMiddleware
from research_gateway.config import Settings
from research_gateway.mcp.server import create_mcp_server
from research_gateway.runtime import GatewayRuntime


def create_app(settings: Settings, runtime: GatewayRuntime | None = None) -> FastAPI:
    gateway = runtime or GatewayRuntime.build(settings)
    mcp_server = create_mcp_server(gateway)
    mcp_http = mcp_server.streamable_http_app(
        streamable_http_path="/mcp",
        stateless_http=True,
        json_response=True,
        host=settings.service.host,
        # RemoteSurfaceMiddleware performs host-independent bearer enforcement. This
        # supports ngrok's generated hostnames while still blocking browser rebinding.
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
    )

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        await gateway.start()
        async with mcp_http.router.lifespan_context(mcp_http):
            yield
        await gateway.aclose()

    app = FastAPI(
        title="Research Gateway internal API",
        version="0.1.0",
        lifespan=lifespan,
        docs_url="/api/docs",
        openapi_url="/api/openapi.json",
    )
    app.state.gateway = gateway
    app.add_middleware(
        RemoteSurfaceMiddleware,
        auth=settings.mcp_remote_auth,
        expose_ui=settings.tunnel.expose_ui,
    )

    @app.get("/health")
    async def health() -> dict[str, Any]:
        return {
            "status": "ok",
            "service": "research-gateway",
            "database_schema": await gateway.database.user_version(),
            "remote_surface": ["/health", "/mcp"],
        }

    @app.get("/api/v1/status")
    async def status() -> dict[str, Any]:
        return {
            "sources": gateway.source_statuses(),
            "summary": await gateway.database.summary(),
        }

    @app.post("/api/v1/studies", status_code=201)
    async def create_study(body: StudyCreate) -> dict[str, Any]:
        return await gateway.database.create_study(
            body.study_id, body.name, body.description, system_test=body.system_test
        )

    @app.get("/api/v1/studies")
    async def list_studies() -> list[dict[str, Any]]:
        return await gateway.database.list_studies()

    @app.get("/api/v1/studies/{study_id}")
    async def study_detail(study_id: str) -> dict[str, Any]:
        study = await gateway.database.get_study(study_id)
        if not study:
            raise HTTPException(404, "Study not found")
        study["topics"] = await gateway.database.list_topics(study_id)
        study["summary"] = await gateway.database.summary(study_id)
        study["search_runs"] = await gateway.database.list_search_runs(study_id=study_id, limit=20)
        page = await gateway.database.list_evidence(study_id=study_id, limit=20)
        study["evidence"] = page.items
        return study

    @app.post("/api/v1/studies/{study_id}/topics", status_code=201)
    async def create_topic(study_id: str, body: TopicCreate) -> dict[str, Any]:
        return await gateway.database.create_topic(
            study_id, body.topic_id, body.name, body.description
        )

    @app.get("/api/v1/studies/{study_id}/topics")
    async def list_topics(study_id: str) -> list[dict[str, Any]]:
        return await gateway.database.list_topics(study_id)

    @app.get("/api/v1/topics/{topic_id}")
    async def topic_detail(topic_id: str) -> dict[str, Any]:
        topic = await gateway.database.get_topic(topic_id)
        if not topic:
            raise HTTPException(404, "Topic not found")
        topic["search_runs"] = await gateway.database.list_search_runs(topic_id=topic_id, limit=100)
        page = await gateway.database.list_evidence(topic_id=topic_id, limit=50)
        topic["evidence"] = page.items
        topic["evidence_count"] = page.total
        return topic

    @app.post("/api/v1/search/explore")
    async def explore(body: SearchRequest) -> dict[str, Any]:
        return await gateway.research.explore(
            study_id=body.study_id,
            topic_id=body.topic_id,
            provider=body.provider,
            search_intent=body.search_intent,
            provider_query=body.provider_query,
            label=body.label,
            filters=body.filters,
            sort=body.sort,
            requested_limit=body.requested_limit,
        )

    @app.post("/api/v1/search/save")
    async def save(body: SearchRequest) -> dict[str, Any]:
        return await gateway.research.save(
            study_id=body.study_id,
            topic_id=body.topic_id,
            provider=body.provider,
            search_intent=body.search_intent,
            provider_query=body.provider_query,
            requested_limit=body.requested_limit,
            label=body.label,
            filters=body.filters,
            sort=body.sort,
        )

    @app.get("/api/v1/search-runs")
    async def search_runs(
        study_id: str | None = None,
        topic_id: str | None = None,
        provider: str | None = None,
        mode: str | None = None,
        status: str | None = None,
        search_code: str | None = None,
        label: str | None = None,
        date_from: str | None = None,
        date_to: str | None = None,
        offset: int = Query(default=0, ge=0),
        limit: int = Query(default=100, ge=1, le=500),
    ) -> list[dict[str, Any]]:
        return await gateway.database.list_search_runs(
            study_id=study_id,
            topic_id=topic_id,
            provider=provider,
            mode=mode,
            status=status,
            search_code=search_code,
            label=label,
            date_from=date_from,
            date_to=date_to,
            offset=offset,
            limit=limit,
        )

    @app.get("/api/v1/search-runs/{search_run_id}")
    async def search_run_detail(search_run_id: str) -> dict[str, Any]:
        run = await gateway.database.get_search_run(search_run_id)
        if not run:
            raise HTTPException(404, "Search run not found")
        run["hits"] = await gateway.database.list_search_hits(search_run_id)
        return run

    @app.get("/api/v1/evidence")
    async def evidence(
        study_id: str | None = None,
        topic_id: str | None = None,
        provider: str | None = None,
        search_code: str | None = None,
        status: str | None = None,
        final: bool | None = None,
        query: str | None = None,
        year: int | None = None,
        document_type: str | None = None,
        publication_type: str | None = None,
        review_status: str | None = None,
        discovered_from: str | None = None,
        discovered_to: str | None = None,
        offset: int = Query(default=0, ge=0),
        limit: int = Query(default=50, ge=1, le=500),
    ) -> dict[str, Any]:
        page = await gateway.database.list_evidence(
            study_id=study_id,
            topic_id=topic_id,
            provider=provider,
            search_code=search_code,
            status=status,
            final=final,
            query=query,
            year=year,
            document_type=document_type,
            publication_type=publication_type,
            review_status=review_status,
            discovered_from=discovered_from,
            discovered_to=discovered_to,
            offset=offset,
            limit=limit,
        )
        return page.model_dump(mode="json")

    @app.get("/api/v1/evidence/{evidence_id}")
    async def evidence_detail(evidence_id: str) -> dict[str, Any]:
        result = await gateway.database.get_evidence(evidence_id)
        if not result:
            raise HTTPException(404, "Evidence record not found")
        result["screening_history"] = await gateway.database.screening_history(evidence_id)
        result["notes"] = await gateway.database.list_notes(evidence_id)
        return result

    @app.post("/api/v1/evidence/{evidence_id}/screening")
    async def screen(evidence_id: str, body: ScreeningUpdate) -> dict[str, Any]:
        await gateway.database.set_screening(
            evidence_id, body.status, reason=body.reason, note=body.note, actor=body.actor
        )
        return await evidence_detail(evidence_id)

    @app.post("/api/v1/evidence/{evidence_id}/notes", status_code=201)
    async def note(evidence_id: str, body: NoteCreate) -> dict[str, Any]:
        return await gateway.database.add_note(evidence_id, body.text, body.actor)

    @app.get("/api/v1/summary")
    async def summary(study_id: str | None = None) -> dict[str, int]:
        return await gateway.database.summary(study_id)

    @app.get("/api/v1/audit")
    async def audit(limit: int = Query(default=100, ge=1, le=500)) -> list[dict[str, Any]]:
        return await gateway.database.list_audit_events(limit=limit)

    @app.post("/api/v1/exports")
    async def export(body: ExportRequest) -> dict[str, Any]:
        return await gateway.exports.export(
            Path(body.path),
            format=body.format,
            study_id=body.study_id,
            topic_id=body.topic_id,
            final_only=body.final_only,
        )

    @app.post("/api/v1/zotero/sync")
    async def zotero_sync(body: ZoteroSyncRequest) -> dict[str, Any]:
        return await gateway.zotero.sync_final_corpus(study_id=body.study_id, dry_run=body.dry_run)

    @app.post("/api/v1/github/publish")
    async def github_publish(body: GithubPublishRequest) -> dict[str, Any]:
        return await gateway.github.publish_files(**body.model_dump())

    ui_dist = Path(__file__).parents[3] / "ui" / "dist"
    if ui_dist.is_dir():
        app.mount("/ui/assets", StaticFiles(directory=ui_dist / "assets"), name="ui-assets")

        @app.get("/ui")
        async def ui_index() -> FileResponse:
            return FileResponse(ui_dist / "index.html")

        @app.get("/ui/{path:path}")
        async def ui(path: str) -> FileResponse:
            candidate = ui_dist / path
            return FileResponse(candidate if candidate.is_file() else ui_dist / "index.html")
    else:

        @app.get("/ui", response_class=HTMLResponse)
        async def ui_missing() -> str:
            return (
                "<h1>Research Gateway</h1><p>The UI has not been built. Run the UI build first.</p>"
            )

    app.mount("/", mcp_http, name="mcp")
    return app
