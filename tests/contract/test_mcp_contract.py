from __future__ import annotations

from pathlib import Path

import pytest
from mcp.client import Client

from research_gateway.config import Settings
from research_gateway.mcp.server import create_mcp_server
from research_gateway.runtime import GatewayRuntime


@pytest.mark.asyncio
async def test_real_mcp_client_lists_and_calls_tools(tmp_path: Path) -> None:
    settings = Settings.model_validate(
        {
            "database": {"path": tmp_path / "gateway.db"},
            "acl_anthology": {"index_path": tmp_path / "missing.json"},
        }
    )
    runtime = GatewayRuntime.build(settings)
    await runtime.start()
    server = create_mcp_server(runtime)
    async with Client(server) as client:
        listed = await client.list_tools()
        names = {tool.name for tool in listed.tools}
        assert {"create_study", "explore_search", "save_search", "list_evidence"} <= names
        save = next(tool for tool in listed.tools if tool.name == "save_search")
        assert save.annotations is not None
        assert save.annotations.read_only_hint is False
        assert save.annotations.open_world_hint is True
        called = await client.call_tool(
            "create_study",
            {"study_id": "mcp-study", "name": "MCP study", "description": "Contract"},
        )
        assert called.is_error is False
        page = await client.call_tool("list_studies")
        assert page.is_error is False
        assert page.structured_content["studies"][0]["study_id"] == "mcp-study"

        status = await client.call_tool("gateway_status")
        assert status.structured_content["database_schema"] == 1
        sources = await client.call_tool("source_list")
        assert any(source["name"] == "scopus" for source in sources.structured_content["sources"])
        await client.call_tool(
            "topic_create",
            {
                "study_id": "mcp-study",
                "topic_id": "topic-1",
                "name": "Original topic",
            },
        )
        topic = await client.call_tool("topic_get", {"topic_id": "topic-1"})
        assert topic.structured_content["name"] == "Original topic"
        updated = await client.call_tool(
            "topic_update", {"topic_id": "topic-1", "name": "Updated topic"}
        )
        assert updated.structured_content["name"] == "Updated topic"
        study = await client.call_tool("study_get", {"study_id": "mcp-study"})
        assert study.structured_content["topics"][0]["topic_id"] == "topic-1"
        topics = await client.call_tool("topic_list", {"study_id": "mcp-study"})
        assert len(topics.structured_content["topics"]) == 1
        summary = await client.call_tool("topic_summary", {"topic_id": "topic-1"})
        assert summary.structured_content["evidence_count"] == 0
        runs = await client.call_tool("search_runs_list", {"study_id": "mcp-study"})
        assert runs.structured_content["search_runs"] == []
        duplicates = await client.call_tool("evidence_possible_duplicates")
        assert duplicates.structured_content["possible_duplicates"] == []
        exported = await client.call_tool(
            "evidence_export_csv",
            {"path": str(tmp_path / "empty.csv"), "study_id": "mcp-study"},
        )
        assert exported.structured_content["evidence_count"] == 0
        audit = await client.call_tool("audit_recent")
        assert audit.structured_content["events"]
    await runtime.aclose()
