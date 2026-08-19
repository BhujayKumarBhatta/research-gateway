from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions, RevocationOptions
from mcp.server.mcpserver import MCPServer
from mcp_types import ToolAnnotations

from research_gateway.domain.models import ScreeningStatus
from research_gateway.oauth.provider import SingleUserOAuthProvider
from research_gateway.oauth.routes import install_approval_routes
from research_gateway.runtime import GatewayRuntime

READ_ONLY = ToolAnnotations(
    read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False
)
REMOTE_READ = ToolAnnotations(
    read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=True
)
LOCAL_WRITE = ToolAnnotations(
    read_only_hint=False, destructive_hint=False, idempotent_hint=False, open_world_hint=False
)
REMOTE_WRITE = ToolAnnotations(
    read_only_hint=False, destructive_hint=False, idempotent_hint=False, open_world_hint=True
)
REMOTE_DESTRUCTIVE = ToolAnnotations(
    read_only_hint=False, destructive_hint=True, idempotent_hint=False, open_world_hint=True
)


def create_mcp_server(
    runtime: GatewayRuntime,
    *,
    oauth_provider: SingleUserOAuthProvider | None = None,
) -> MCPServer[None]:
    oauth = oauth_provider.settings if oauth_provider else None
    auth = (
        AuthSettings(
            issuer_url=oauth.issuer_url,
            resource_server_url=oauth.resource_url,
            client_registration_options=ClientRegistrationOptions(
                enabled=True,
                valid_scopes=[oauth.scope],
                default_scopes=[oauth.scope],
            ),
            revocation_options=RevocationOptions(enabled=True),
            required_scopes=[oauth.scope],
        )
        if oauth
        else None
    )
    server: MCPServer[None] = MCPServer(
        "research-gateway",
        title="Research Gateway",
        description="A local-first evidence search, screening, provenance, and export gateway.",
        instructions=(
            "Explore counts a query without saving hits. Save reruns the exact provider query "
            "and records permitted metadata. External writes default to dry-run."
        ),
        version="0.1.0",
        auth_server_provider=oauth_provider,
        auth=auth,
    )
    if oauth_provider:
        install_approval_routes(server, oauth_provider)

    @server.tool(
        description="Show each source's real availability, contract, and retention policy.",
        annotations=READ_ONLY,
    )
    async def list_source_status() -> dict[str, Any]:
        return {"sources": runtime.source_statuses()}

    @server.tool(
        description="Create a research study container in the local Evidence Store.",
        annotations=LOCAL_WRITE,
    )
    async def create_study(
        study_id: str, name: str, description: str = "", system_test: bool = False
    ) -> dict[str, Any]:
        result = await runtime.database.create_study(
            study_id, name, description, system_test=system_test
        )
        await runtime.database.audit(
            "study.create",
            status="completed",
            study_id=study_id,
            entity_type="study",
            entity_id=study_id,
            safe_summary=f"Created study {study_id}.",
        )
        return result

    @server.tool(description="List local research studies.", annotations=READ_ONLY)
    async def list_studies() -> dict[str, Any]:
        return {"studies": await runtime.database.list_studies()}

    @server.tool(description="Create a topic inside an existing study.", annotations=LOCAL_WRITE)
    async def create_topic(
        study_id: str, topic_id: str, name: str, description: str = ""
    ) -> dict[str, Any]:
        return await runtime.database.create_topic(study_id, topic_id, name, description)

    @server.tool(
        description=(
            "Count an exact provider query and record the run without saving hits or evidence."
        ),
        annotations=REMOTE_READ.model_copy(
            update={"read_only_hint": False, "idempotent_hint": False}
        ),
    )
    async def explore_search(
        study_id: str,
        provider: str,
        search_intent: str,
        provider_query: str,
        topic_id: str | None = None,
        label: str = "",
        filters: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return await runtime.research.explore(
            study_id=study_id,
            topic_id=topic_id,
            provider=provider,
            search_intent=search_intent,
            provider_query=provider_query,
            label=label,
            filters=filters,
        )

    @server.tool(
        description=(
            "Rerun an exact provider query and save its permitted discoveries with provenance."
        ),
        annotations=REMOTE_WRITE,
    )
    async def save_search(
        study_id: str,
        provider: str,
        search_intent: str,
        provider_query: str,
        requested_limit: int,
        topic_id: str | None = None,
        label: str = "",
        filters: dict[str, Any] | None = None,
        sort: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return await runtime.research.save(
            study_id=study_id,
            topic_id=topic_id,
            provider=provider,
            search_intent=search_intent,
            provider_query=provider_query,
            requested_limit=requested_limit,
            label=label,
            filters=filters,
            sort=sort,
        )

    @server.tool(
        description=(
            "List evidence with study, topic, provider, screening, final, and text filters."
        ),
        annotations=READ_ONLY,
    )
    async def list_evidence(
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
        offset: int = 0,
        limit: int = 50,
    ) -> dict[str, Any]:
        page = await runtime.database.list_evidence(
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
            limit=min(max(limit, 1), 500),
        )
        return page.model_dump(mode="json")

    @server.tool(
        description="Read one evidence record, its identifiers, and every discovery path.",
        annotations=READ_ONLY,
    )
    async def get_evidence(evidence_id: str) -> dict[str, Any]:
        result = await runtime.database.get_evidence(evidence_id)
        if result is None:
            raise ValueError("Evidence record was not found.")
        return result

    @server.tool(
        description="Record a reversible screening decision and its reason in local history.",
        annotations=LOCAL_WRITE,
    )
    async def set_screening_status(
        evidence_id: str,
        status: ScreeningStatus,
        reason: str | None = None,
        note: str | None = None,
        actor: str = "mcp",
    ) -> dict[str, Any]:
        await runtime.database.set_screening(
            evidence_id, status, reason=reason, note=note, actor=actor
        )
        return {
            "evidence_id": evidence_id,
            "screening_status": status.value,
            "history": await runtime.database.screening_history(evidence_id),
        }

    @server.tool(
        description="Add a durable local note to an evidence record.", annotations=LOCAL_WRITE
    )
    async def add_evidence_note(evidence_id: str, text: str, actor: str = "mcp") -> dict[str, Any]:
        return await runtime.database.add_note(evidence_id, text, actor)

    @server.tool(
        description="Show a compact local corpus and search-run summary.", annotations=READ_ONLY
    )
    async def get_research_summary(study_id: str | None = None) -> dict[str, int]:
        return await runtime.database.summary(study_id)

    @server.tool(
        description="List recent safe audit events without credentials or raw provider errors.",
        annotations=READ_ONLY,
    )
    async def list_audit_events(limit: int = 100) -> dict[str, Any]:
        return {"events": await runtime.database.list_audit_events(limit=min(max(limit, 1), 500))}

    @server.tool(
        description="Export evidence and provenance as JSON, CSV, XLSX, or Markdown.",
        annotations=LOCAL_WRITE,
    )
    async def export_evidence(
        path: str,
        format: Literal["json", "csv", "xlsx", "markdown"],
        study_id: str | None = None,
        topic_id: str | None = None,
        final_only: bool = False,
    ) -> dict[str, Any]:
        return await runtime.exports.export(
            Path(path),
            format=format,
            study_id=study_id,
            topic_id=topic_id,
            final_only=final_only,
        )

    @server.tool(
        description=(
            "Plan or perform idempotent final-corpus item creation in Zotero. "
            "Dry-run is the default; no deletes or file uploads."
        ),
        annotations=REMOTE_WRITE,
    )
    async def sync_final_corpus_to_zotero(
        study_id: str | None = None, dry_run: bool = True
    ) -> dict[str, Any]:
        return await runtime.zotero.sync_final_corpus(study_id=study_id, dry_run=dry_run)

    @server.tool(description="Read safe repository metadata from GitHub.", annotations=REMOTE_READ)
    async def get_github_repository(repository: str) -> dict[str, Any]:
        return await runtime.github.get_repository(repository)

    @server.tool(
        description="List GitHub issues, excluding pull requests.", annotations=REMOTE_READ
    )
    async def list_github_issues(repository: str, state: str = "open") -> dict[str, Any]:
        return {"issues": await runtime.github.list_issues(repository, state=state)}

    @server.tool(
        description=(
            "Plan or publish text files through a new branch, commit, and pull request. "
            "Dry-run is the default; never force, delete, merge, or write the default branch."
        ),
        annotations=REMOTE_WRITE,
    )
    async def publish_files_to_github(
        repository: str,
        branch: str,
        files: dict[str, str],
        commit_message: str,
        pull_request_title: str,
        pull_request_body: str,
        dry_run: bool = True,
    ) -> dict[str, Any]:
        return await runtime.github.publish_files(
            repository=repository,
            branch=branch,
            files=files,
            commit_message=commit_message,
            pull_request_title=pull_request_title,
            pull_request_body=pull_request_body,
            dry_run=dry_run,
        )

    # Stable public names. The earlier compact names remain as compatibility aliases.
    @server.tool(
        name="gateway_status",
        description="Read gateway health, schema, corpus summary, and source readiness.",
        annotations=READ_ONLY,
    )
    async def gateway_status_tool() -> dict[str, Any]:
        return {
            "status": "ok",
            "database_schema": await runtime.database.user_version(),
            "summary": await runtime.database.summary(),
            "sources": runtime.source_statuses(),
            "remote_surface": ["/health", "/mcp"],
        }

    @server.tool(
        name="source_list",
        description=(
            "List source availability, capabilities, and retention contracts without secrets."
        ),
        annotations=READ_ONLY,
    )
    async def source_list_tool() -> dict[str, Any]:
        return await list_source_status()

    @server.tool(
        name="study_list",
        description="List studies in the local Evidence Store.",
        annotations=READ_ONLY,
    )
    async def study_list_tool() -> dict[str, Any]:
        return await list_studies()

    @server.tool(
        name="study_get", description="Read one study and its topics.", annotations=READ_ONLY
    )
    async def study_get_tool(study_id: str) -> dict[str, Any]:
        study = await runtime.database.get_study(study_id)
        if not study:
            raise ValueError("Study was not found.")
        study["topics"] = await runtime.database.list_topics(study_id)
        return study

    @server.tool(
        name="study_create", description="Create a local research study.", annotations=LOCAL_WRITE
    )
    async def study_create_tool(
        study_id: str, name: str, description: str = "", system_test: bool = False
    ) -> dict[str, Any]:
        return await create_study(study_id, name, description, system_test)

    @server.tool(name="topic_list", description="List topics for a study.", annotations=READ_ONLY)
    async def topic_list_tool(study_id: str) -> dict[str, Any]:
        return {"topics": await runtime.database.list_topics(study_id)}

    @server.tool(name="topic_get", description="Read one topic.", annotations=READ_ONLY)
    async def topic_get_tool(topic_id: str) -> dict[str, Any]:
        topic = await runtime.database.get_topic(topic_id)
        if not topic:
            raise ValueError("Topic was not found.")
        return topic

    @server.tool(
        name="topic_create", description="Create a topic inside a study.", annotations=LOCAL_WRITE
    )
    async def topic_create_tool(
        study_id: str, topic_id: str, name: str, description: str = ""
    ) -> dict[str, Any]:
        return await create_topic(study_id, topic_id, name, description)

    @server.tool(
        name="topic_update",
        description="Update a local topic's plain-language fields or status.",
        annotations=LOCAL_WRITE,
    )
    async def topic_update_tool(
        topic_id: str,
        name: str | None = None,
        description: str | None = None,
        status: str | None = None,
    ) -> dict[str, Any]:
        return await runtime.database.update_topic(
            topic_id, name=name, description=description, status=status
        )

    @server.tool(
        name="research_explore_search",
        description="Count an exact provider query, recording provenance but no hits or evidence.",
        annotations=REMOTE_WRITE,
    )
    async def research_explore_search_tool(
        study_id: str,
        provider: str,
        search_intent: str,
        provider_query: str,
        topic_id: str | None = None,
        label: str = "",
        filters: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return await explore_search(
            study_id, provider, search_intent, provider_query, topic_id, label, filters
        )

    @server.tool(
        name="research_save_search",
        description=(
            "Save an exact provider query's permitted discoveries with deduplication "
            "and provenance."
        ),
        annotations=REMOTE_WRITE,
    )
    async def research_save_search_tool(
        study_id: str,
        provider: str,
        search_intent: str,
        provider_query: str,
        max_records: int,
        topic_id: str | None = None,
        label: str = "",
        filters: dict[str, Any] | None = None,
        sort: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return await save_search(
            study_id,
            provider,
            search_intent,
            provider_query,
            max_records,
            topic_id,
            label,
            filters,
            sort,
        )

    @server.tool(
        name="search_runs_list",
        description="List compact stored search runs with optional filters.",
        annotations=READ_ONLY,
    )
    async def search_runs_list_tool(
        study_id: str | None = None,
        topic_id: str | None = None,
        provider: str | None = None,
        mode: str | None = None,
        status: str | None = None,
        search_code: str | None = None,
        label: str | None = None,
        date_from: str | None = None,
        date_to: str | None = None,
        offset: int = 0,
        limit: int = 100,
    ) -> dict[str, Any]:
        return {
            "search_runs": await runtime.database.list_search_runs(
                study_id=study_id,
                topic_id=topic_id,
                provider=provider,
                mode=mode,
                status=status,
                search_code=search_code,
                label=label,
                date_from=date_from,
                date_to=date_to,
                offset=max(offset, 0),
                limit=min(max(limit, 1), 500),
            )
        }

    @server.tool(
        name="search_run_get",
        description="Read an exact stored query, run outcome, and its saved hits.",
        annotations=READ_ONLY,
    )
    async def search_run_get_tool(search_run_id: str) -> dict[str, Any]:
        run = await runtime.database.get_search_run(search_run_id)
        if not run:
            raise ValueError("Search run was not found.")
        run["hits"] = await runtime.database.list_search_hits(search_run_id)
        return run

    async def _direct_count(
        provider: str, provider_query: str, filters: dict[str, Any] | None
    ) -> dict[str, Any]:
        adapter = runtime.sources.get(provider)
        if not adapter.status.available:
            raise ValueError(f"Source is unavailable: {adapter.status.unavailable_reason}")
        return {
            "provider": provider,
            "provider_query": provider_query,
            "total": await adapter.count(provider_query, filters=filters),
        }

    async def _direct_search(
        provider: str,
        provider_query: str,
        limit: int,
        offset: int,
        filters: dict[str, Any] | None,
        sort: dict[str, Any] | None,
    ) -> dict[str, Any]:
        adapter = runtime.sources.get(provider)
        if not adapter.status.available:
            raise ValueError(f"Source is unavailable: {adapter.status.unavailable_reason}")
        page = await adapter.search(
            provider_query,
            limit=min(limit, adapter.retention_policy.max_page_size),
            offset=offset,
            filters=filters,
            sort=sort,
        )
        return page.model_dump(mode="json", exclude={"records": {"__all__": {"raw_metadata"}}})

    @server.tool(
        name="scopus_count",
        description="Count an exact Scopus query without saving records.",
        annotations=REMOTE_READ,
    )
    async def scopus_count_tool(
        provider_query: str, filters: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        return await _direct_count("scopus", provider_query, filters)

    @server.tool(
        name="scopus_search",
        description="Read a compact page from the official Scopus Search API without saving it.",
        annotations=REMOTE_READ,
    )
    async def scopus_search_tool(
        provider_query: str,
        limit: int = 10,
        offset: int = 0,
        filters: dict[str, Any] | None = None,
        sort: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return await _direct_search("scopus", provider_query, limit, offset, filters, sort)

    @server.tool(
        name="arxiv_search",
        description="Read a compact page from the official arXiv Atom API without saving it.",
        annotations=REMOTE_READ,
    )
    async def arxiv_search_tool(
        provider_query: str, limit: int = 10, offset: int = 0, sort: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        return await _direct_search("arxiv", provider_query, limit, offset, None, sort)

    @server.tool(
        name="acl_search",
        description=(
            "Read a compact page from the local official ACL Anthology index without saving it."
        ),
        annotations=READ_ONLY,
    )
    async def acl_search_tool(
        provider_query: str, limit: int = 10, offset: int = 0
    ) -> dict[str, Any]:
        return await _direct_search("acl_anthology", provider_query, limit, offset, None, None)

    @server.tool(
        name="ieee_search",
        description="Read a compact page from the official IEEE Metadata API without saving it.",
        annotations=REMOTE_READ,
    )
    async def ieee_search_tool(
        provider_query: str, limit: int = 10, offset: int = 0
    ) -> dict[str, Any]:
        return await _direct_search("ieee_xplore", provider_query, limit, offset, None, None)

    @server.tool(
        name="wos_search",
        description=(
            "Read a compact page from the configured official Web of Science API without saving it."
        ),
        annotations=REMOTE_READ,
    )
    async def wos_search_tool(
        provider_query: str, limit: int = 10, offset: int = 0
    ) -> dict[str, Any]:
        return await _direct_search("wos", provider_query, limit, offset, None, None)

    @server.tool(
        name="evidence_search",
        description="Search and filter canonical local evidence with pagination.",
        annotations=READ_ONLY,
    )
    async def evidence_search_tool(
        study_id: str | None = None,
        topic_id: str | None = None,
        provider: str | None = None,
        search_code: str | None = None,
        screening_status: str | None = None,
        final: bool | None = None,
        query: str | None = None,
        year: int | None = None,
        document_type: str | None = None,
        publication_type: str | None = None,
        review_status: str | None = None,
        discovered_from: str | None = None,
        discovered_to: str | None = None,
        offset: int = 0,
        limit: int = 50,
    ) -> dict[str, Any]:
        return await list_evidence(
            study_id=study_id,
            topic_id=topic_id,
            provider=provider,
            search_code=search_code,
            status=screening_status,
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

    @server.tool(
        name="evidence_get",
        description="Read canonical evidence, identifiers, and all discovery paths.",
        annotations=READ_ONLY,
    )
    async def evidence_get_tool(evidence_id: str) -> dict[str, Any]:
        return await get_evidence(evidence_id)

    @server.tool(
        name="evidence_list_discoveries",
        description="List every search run that discovered one evidence record.",
        annotations=READ_ONLY,
    )
    async def evidence_list_discoveries_tool(evidence_id: str) -> dict[str, Any]:
        return {"discoveries": await runtime.database.list_discoveries(evidence_id)}

    @server.tool(
        name="evidence_possible_duplicates",
        description="List uncertain duplicate candidates that require human review.",
        annotations=READ_ONLY,
    )
    async def evidence_possible_duplicates_tool(limit: int = 100) -> dict[str, Any]:
        return {
            "possible_duplicates": await runtime.database.list_possible_duplicates(
                limit=min(max(limit, 1), 500)
            )
        }

    @server.tool(
        name="evidence_set_screening",
        description="Record a reversible screening decision with history.",
        annotations=LOCAL_WRITE,
    )
    async def evidence_set_screening_tool(
        evidence_id: str,
        status: ScreeningStatus,
        reason: str | None = None,
        note: str | None = None,
        actor: str = "mcp",
    ) -> dict[str, Any]:
        return await set_screening_status(evidence_id, status, reason, note, actor)

    @server.tool(
        name="evidence_add_note",
        description="Add a durable note to local evidence.",
        annotations=LOCAL_WRITE,
    )
    async def evidence_add_note_tool(
        evidence_id: str, text: str, actor: str = "mcp"
    ) -> dict[str, Any]:
        return await add_evidence_note(evidence_id, text, actor)

    @server.tool(
        name="evidence_set_final",
        description="Add or remove an evidence record from the final corpus with history.",
        annotations=LOCAL_WRITE,
    )
    async def evidence_set_final_tool(
        evidence_id: str, final: bool, actor: str = "mcp", note: str | None = None
    ) -> dict[str, Any]:
        status = ScreeningStatus.FINAL if final else ScreeningStatus.INCLUDED
        return await set_screening_status(evidence_id, status, None, note, actor)

    @server.tool(
        name="topic_summary",
        description="Read evidence and search counts scoped to a topic.",
        annotations=READ_ONLY,
    )
    async def topic_summary_tool(topic_id: str) -> dict[str, Any]:
        topic = await runtime.database.get_topic(topic_id)
        if not topic:
            raise ValueError("Topic was not found.")
        evidence_page = await runtime.database.list_evidence(topic_id=topic_id, limit=1)
        runs = await runtime.database.list_search_runs(topic_id=topic_id, limit=500)
        return {
            "topic": topic,
            "evidence_count": evidence_page.total,
            "search_run_count": len(runs),
        }

    @server.tool(
        name="evidence_export_excel",
        description="Export study evidence and provenance to a reviewable Excel workbook.",
        annotations=LOCAL_WRITE,
    )
    async def evidence_export_excel_tool(
        path: str, study_id: str | None = None, final_only: bool = False
    ) -> dict[str, Any]:
        return await export_evidence(path, "xlsx", study_id, None, final_only)

    @server.tool(
        name="evidence_export_csv",
        description="Export canonical evidence rows to CSV.",
        annotations=LOCAL_WRITE,
    )
    async def evidence_export_csv_tool(
        path: str, study_id: str | None = None, final_only: bool = False
    ) -> dict[str, Any]:
        return await export_evidence(path, "csv", study_id, None, final_only)

    @server.tool(
        name="zotero_sync_corpus",
        description=(
            "Dry-run or idempotently create final-corpus Zotero items; "
            "never delete or upload files."
        ),
        annotations=REMOTE_WRITE,
    )
    async def zotero_sync_corpus_tool(
        study_id: str | None = None, dry_run: bool = True
    ) -> dict[str, Any]:
        return await sync_final_corpus_to_zotero(study_id, dry_run)

    @server.tool(
        name="zotero_search",
        description="Search bibliographic items in the configured Zotero library.",
        annotations=REMOTE_READ,
    )
    async def zotero_search_tool(query: str, limit: int = 25) -> dict[str, Any]:
        return await runtime.zotero.search_items(query, limit=limit)

    @server.tool(
        name="zotero_get_item",
        description="Read one Zotero bibliographic item by key.",
        annotations=REMOTE_READ,
    )
    async def zotero_get_item_tool(item_key: str) -> dict[str, Any]:
        return await runtime.zotero.get_item(item_key)

    @server.tool(
        name="zotero_list_collections",
        description="List collections in the configured Zotero library.",
        annotations=REMOTE_READ,
    )
    async def zotero_list_collections_tool(limit: int = 100) -> dict[str, Any]:
        return await runtime.zotero.list_collections(limit=limit)

    @server.tool(
        name="zotero_credential_status",
        description=(
            "Read the configured Zotero key's effective library permissions without "
            "returning the key."
        ),
        annotations=REMOTE_READ,
    )
    async def zotero_credential_status_tool() -> dict[str, Any]:
        return await runtime.zotero.credential_status()

    @server.tool(
        name="zotero_create_collection",
        description=(
            "Idempotently create a top-level Zotero collection or a subcollection under "
            "parent_collection_key."
        ),
        annotations=REMOTE_WRITE,
    )
    async def zotero_create_collection_tool(
        name: str, parent_collection_key: str | None = None
    ) -> dict[str, Any]:
        return await runtime.zotero.create_collection(
            name, parent_collection_key=parent_collection_key
        )

    @server.tool(
        name="zotero_delete_collection",
        description=(
            "Inspect or delete one Zotero collection. Dry-run is the default; a non-empty "
            "collection requires recursive=true, which preserves its bibliography items."
        ),
        annotations=REMOTE_DESTRUCTIVE,
    )
    async def zotero_delete_collection_tool(
        collection_key: str, dry_run: bool = True, recursive: bool = False
    ) -> dict[str, Any]:
        return await runtime.zotero.delete_collection(
            collection_key, dry_run=dry_run, recursive=recursive
        )

    @server.tool(
        name="zotero_create_item",
        description=(
            "Plan or idempotently create one approved Zotero item from canonical evidence "
            "or supplied metadata. Dry-run is the default."
        ),
        annotations=REMOTE_WRITE,
    )
    async def zotero_create_item_tool(
        evidence_id: str | None = None,
        title: str | None = None,
        authors: list[dict[str, Any]] | None = None,
        year: str | None = None,
        doi: str | None = None,
        url: str | None = None,
        item_type: str | None = None,
        collection_keys: list[str] | None = None,
        tags: list[str] | None = None,
        arxiv_id: str | None = None,
        dry_run: bool = True,
    ) -> dict[str, Any]:
        return await runtime.zotero.create_item(
            evidence_id=evidence_id,
            title=title,
            authors=authors,
            year=year,
            doi=doi,
            url=url,
            item_type=item_type,
            collection_keys=collection_keys,
            tags=tags,
            arxiv_id=arxiv_id,
            dry_run=dry_run,
        )

    @server.tool(
        name="zotero_delete_item",
        description=(
            "Inspect or delete one Zotero bibliographic item with version protection. "
            "Dry-run is the default; items with child notes or attachments are refused."
        ),
        annotations=REMOTE_DESTRUCTIVE,
    )
    async def zotero_delete_item_tool(item_key: str, dry_run: bool = True) -> dict[str, Any]:
        return await runtime.zotero.delete_item(item_key, dry_run=dry_run)

    @server.tool(
        name="zotero_add_item_to_collection",
        description="Add an existing Zotero item to a collection without duplicating the item.",
        annotations=REMOTE_WRITE,
    )
    async def zotero_add_item_to_collection_tool(
        item_key: str, collection_key: str
    ) -> dict[str, Any]:
        return await runtime.zotero.add_item_to_collection(item_key, collection_key)

    @server.tool(
        name="zotero_remove_item_from_collection",
        description="Remove one collection membership without deleting the Zotero item.",
        annotations=REMOTE_WRITE,
    )
    async def zotero_remove_item_from_collection_tool(
        item_key: str, collection_key: str
    ) -> dict[str, Any]:
        return await runtime.zotero.remove_item_from_collection(item_key, collection_key)

    @server.tool(
        name="zotero_add_tags",
        description="Add extensible research tags while preserving every existing Zotero tag.",
        annotations=REMOTE_WRITE,
    )
    async def zotero_add_tags_tool(item_key: str, tags: list[str]) -> dict[str, Any]:
        return await runtime.zotero.add_tags(item_key, tags)

    @server.tool(
        name="zotero_remove_tags",
        description="Remove only selected Zotero tags while preserving unrelated tags.",
        annotations=REMOTE_WRITE,
    )
    async def zotero_remove_tags_tool(item_key: str, tags: list[str]) -> dict[str, Any]:
        return await runtime.zotero.remove_tags(item_key, tags)

    @server.tool(
        name="zotero_set_tags",
        description=(
            "Set Zotero research tags; preserve_existing defaults to true to retain unrelated tags."
        ),
        annotations=REMOTE_WRITE,
    )
    async def zotero_set_tags_tool(
        item_key: str, tags: list[str], preserve_existing: bool = True
    ) -> dict[str, Any]:
        return await runtime.zotero.set_tags(item_key, tags, preserve_existing=preserve_existing)

    @server.tool(
        name="zotero_get_citation_metadata",
        description=(
            "Retrieve structured Zotero bibliographic metadata plus Zotero-rendered citation "
            "and bibliography text for up to 50 selected item keys."
        ),
        annotations=REMOTE_READ,
    )
    async def zotero_get_citation_metadata_tool(
        item_keys: list[str], style: str = "apa", locale: str = "en-US"
    ) -> dict[str, Any]:
        return await runtime.zotero.get_citation_metadata(item_keys, style=style, locale=locale)

    @server.tool(
        name="zotero_format_citation",
        description=(
            "Render in-text citations from selected Zotero items with an official CSL style."
        ),
        annotations=REMOTE_READ,
    )
    async def zotero_format_citation_tool(
        item_keys: list[str], style: str = "apa", locale: str = "en-US"
    ) -> dict[str, Any]:
        return await runtime.zotero.format_citation(item_keys, style=style, locale=locale)

    @server.tool(
        name="zotero_format_bibliography",
        description="Render bibliography entries from the same selected Zotero records.",
        annotations=REMOTE_READ,
    )
    async def zotero_format_bibliography_tool(
        item_keys: list[str], style: str = "apa", locale: str = "en-US"
    ) -> dict[str, Any]:
        return await runtime.zotero.format_bibliography(item_keys, style=style, locale=locale)

    @server.tool(
        name="zotero_get_link_for_evidence",
        description="Read the durable Zotero item mapping for one Research Gateway evidence ID.",
        annotations=READ_ONLY,
    )
    async def zotero_get_link_for_evidence_tool(evidence_id: str) -> dict[str, Any]:
        return await runtime.zotero.get_link_for_evidence(evidence_id)

    @server.tool(
        name="zotero_get_link_for_item",
        description="Read the Research Gateway evidence mapping for one Zotero item key.",
        annotations=READ_ONLY,
    )
    async def zotero_get_link_for_item_tool(item_key: str) -> dict[str, Any]:
        return await runtime.zotero.get_link_for_item(item_key)

    @server.tool(
        name="zotero_record_citation_reference",
        description=(
            "Record minimum durable provenance from a manuscript location to a Zotero item "
            "and its linked reviewed evidence."
        ),
        annotations=LOCAL_WRITE,
    )
    async def zotero_record_citation_reference_tool(
        manuscript: str,
        item_key: str,
        citation_location: str | None = None,
        rationale: str = "",
    ) -> dict[str, Any]:
        return await runtime.zotero.record_citation_reference(
            manuscript=manuscript,
            item_key=item_key,
            citation_location=citation_location,
            rationale=rationale,
        )

    @server.tool(
        name="zotero_list_citation_references",
        description="List durable manuscript-to-Zotero citation provenance records.",
        annotations=READ_ONLY,
    )
    async def zotero_list_citation_references_tool(
        manuscript: str | None = None, limit: int = 100
    ) -> dict[str, Any]:
        return await runtime.zotero.list_citation_references(manuscript=manuscript, limit=limit)

    @server.tool(
        name="github_propose_change",
        description=(
            "Dry-run or publish text changes through branch, commit, and pull request only."
        ),
        annotations=REMOTE_WRITE,
    )
    async def github_propose_change_tool(
        repository: str,
        branch: str,
        files: dict[str, str],
        commit_message: str,
        pull_request_title: str,
        pull_request_body: str,
        dry_run: bool = True,
    ) -> dict[str, Any]:
        return await publish_files_to_github(
            repository,
            branch,
            files,
            commit_message,
            pull_request_title,
            pull_request_body,
            dry_run,
        )

    @server.tool(
        name="github_search_repositories",
        description="Search repositories through the official GitHub API.",
        annotations=REMOTE_READ,
    )
    async def github_search_repositories_tool(query: str, limit: int = 20) -> dict[str, Any]:
        return await runtime.github.search_repositories(query, limit=limit)

    @server.tool(
        name="github_search_code",
        description=(
            "Search code through the official GitHub API, optionally within one repository."
        ),
        annotations=REMOTE_READ,
    )
    async def github_search_code_tool(
        query: str, repository: str | None = None, limit: int = 20
    ) -> dict[str, Any]:
        return await runtime.github.search_code(query, repository=repository, limit=limit)

    @server.tool(
        name="github_read_file",
        description="Read one UTF-8 text file from a GitHub repository and ref.",
        annotations=REMOTE_READ,
    )
    async def github_read_file_tool(
        repository: str, path: str, ref: str | None = None
    ) -> dict[str, Any]:
        return await runtime.github.read_file(repository, path, ref=ref)

    @server.tool(
        name="github_list_tree",
        description="List paths in a GitHub Git tree.",
        annotations=REMOTE_READ,
    )
    async def github_list_tree_tool(
        repository: str, tree_sha: str, recursive: bool = True
    ) -> dict[str, Any]:
        return await runtime.github.list_tree(repository, tree_sha, recursive=recursive)

    @server.tool(
        name="github_get_issue", description="Read one GitHub issue.", annotations=REMOTE_READ
    )
    async def github_get_issue_tool(repository: str, number: int) -> dict[str, Any]:
        return await runtime.github.get_issue(repository, number)

    @server.tool(
        name="github_get_pull_request",
        description="Read one GitHub pull request.",
        annotations=REMOTE_READ,
    )
    async def github_get_pull_request_tool(repository: str, number: int) -> dict[str, Any]:
        return await runtime.github.get_pull_request(repository, number)

    @server.tool(
        name="github_create_issue",
        description="Plan or create a GitHub issue; dry-run is the default.",
        annotations=REMOTE_WRITE,
    )
    async def github_create_issue_tool(
        repository: str, title: str, body: str, dry_run: bool = True
    ) -> dict[str, Any]:
        return await runtime.github.create_issue(repository, title, body, dry_run=dry_run)

    @server.tool(
        name="github_comment_issue",
        description="Plan or add a GitHub issue comment; dry-run is the default.",
        annotations=REMOTE_WRITE,
    )
    async def github_comment_issue_tool(
        repository: str, number: int, body: str, dry_run: bool = True
    ) -> dict[str, Any]:
        return await runtime.github.comment_issue(repository, number, body, dry_run=dry_run)

    @server.tool(
        name="audit_recent",
        description="List recent safe operational audit events.",
        annotations=READ_ONLY,
    )
    async def audit_recent_tool(limit: int = 100) -> dict[str, Any]:
        return await list_audit_events(limit)

    return server
