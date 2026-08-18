from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from mcp.client import Client

from research_gateway.config import ZoteroSettings
from research_gateway.db.database import EvidenceDatabase
from research_gateway.domain.models import SourceRecord
from research_gateway.integrations.zotero import ZoteroAdapter, ZoteroSafetyError
from research_gateway.mcp.server import create_mcp_server


class ZoteroFixture:
    def __init__(self) -> None:
        self.collections: dict[str, dict[str, Any]] = {}
        self.items: dict[str, dict[str, Any]] = {}
        self.children: dict[str, list[dict[str, Any]]] = {}
        self.next_collection = 0
        self.next_item = 0
        self.version = 10
        self.item_post_count = 0

    def _version(self) -> int:
        self.version += 1
        return self.version

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        assert request.headers["Zotero-API-Key"] == "fixture-zotero-key"
        suffix = request.url.path.removeprefix("/users/42")
        if request.method == "GET" and suffix == "/collections/top":
            return self._json(
                [item for item in self.collections.values() if not item["data"]["parentCollection"]]
            )
        if request.method == "GET" and suffix == "/collections":
            return self._json(list(self.collections.values()))
        if request.method == "POST" and suffix == "/collections":
            body = json.loads(request.content)
            self.next_collection += 1
            key = f"COLLECT{self.next_collection}"
            created = {
                "key": key,
                "version": self._version(),
                "data": {"key": key, **body[0]},
            }
            self.collections[key] = created
            return self._json({"successful": {"0": created}, "failed": {}})
        if suffix.startswith("/collections/"):
            return self._collection_request(request, suffix)
        if request.method == "POST" and suffix == "/items":
            body = json.loads(request.content)
            self.next_item += 1
            self.item_post_count += 1
            key = f"ITEMKEY{self.next_item}"
            created = {
                "key": key,
                "version": self._version(),
                "data": {"key": key, **body[0]},
            }
            self.items[key] = created
            return self._json({"successful": {"0": created}, "failed": {}})
        if request.method == "GET" and suffix == "/items":
            return self._item_list(request)
        if suffix.startswith("/items/"):
            return self._item_request(request, suffix)
        raise AssertionError(f"Unhandled Zotero fixture request: {request.method} {suffix}")

    def _collection_request(self, request: httpx.Request, suffix: str) -> httpx.Response:
        parts = suffix.strip("/").split("/")
        key = parts[1]
        if len(parts) == 3 and parts[2] == "collections" and request.method == "GET":
            return self._json(
                [
                    item
                    for item in self.collections.values()
                    if item["data"]["parentCollection"] == key
                ]
            )
        if len(parts) == 3 and parts[2] == "items" and request.method == "GET":
            return self._json(
                [item for item in self.items.values() if key in item["data"].get("collections", [])]
            )
        if len(parts) == 2 and request.method == "GET":
            return self._json(self.collections[key])
        if len(parts) == 2 and request.method == "DELETE":
            self._assert_version(request, self.collections[key])
            del self.collections[key]
            return httpx.Response(204)
        raise AssertionError(f"Unhandled collection request: {request.method} {suffix}")

    def _item_list(self, request: httpx.Request) -> httpx.Response:
        item_keys = request.url.params.get("itemKey")
        if item_keys:
            results = []
            for key in item_keys.split(","):
                item = self.items.get(key)
                if item:
                    results.append(
                        {
                            **item,
                            "citation": f"(Researcher, {item['data'].get('date')})",
                            "bib": f"Researcher. {item['data']['title']}.",
                        }
                    )
            return self._json(results)
        query = str(request.url.params.get("q") or "").casefold()
        if not query:
            return self._json(list(self.items.values()))
        return self._json(
            [item for item in self.items.values() if query in json.dumps(item["data"]).casefold()]
        )

    def _item_request(self, request: httpx.Request, suffix: str) -> httpx.Response:
        parts = suffix.strip("/").split("/")
        key = parts[1]
        if len(parts) == 3 and parts[2] == "children" and request.method == "GET":
            return self._json(self.children.get(key, []))
        if len(parts) == 2 and request.method == "GET":
            return self._json(self.items[key])
        if len(parts) == 2 and request.method == "PATCH":
            self._assert_version(request, self.items[key])
            self.items[key]["data"].update(json.loads(request.content))
            self.items[key]["version"] = self._version()
            return httpx.Response(204)
        if len(parts) == 2 and request.method == "DELETE":
            self._assert_version(request, self.items[key])
            del self.items[key]
            return httpx.Response(204)
        raise AssertionError(f"Unhandled item request: {request.method} {suffix}")

    @staticmethod
    def _assert_version(request: httpx.Request, item: dict[str, Any]) -> None:
        assert request.headers["If-Unmodified-Since-Version"] == str(item["version"])

    @staticmethod
    def _json(value: Any) -> httpx.Response:
        return httpx.Response(200, json=value)


async def _approved_evidence(database: EvidenceDatabase) -> str:
    await database.create_study("zotero-workflow", "Zotero workflow", "Disposable fixture")
    run = await database.create_search_run(
        study_id="zotero-workflow",
        topic_id=None,
        provider="fixture",
        mode="save",
        label="approved",
        search_intent="Test the approved-reference workflow",
        provider_query="RG Zotero disposable paper",
        filters={},
        sort={},
        requested_limit=1,
    )
    result = await database.ingest_search_hit(
        run.search_run_id,
        1,
        SourceRecord(
            provider="fixture",
            provider_record_id="rg-zotero-1",
            title="A Disposable Research Gateway Zotero Paper",
            authors=[{"name": "A. Researcher"}],
            year=2026,
            doi="10.5555/rg-zotero-test",
            publication="Fixture Preprint Repository",
            publication_type="preprint",
            review_status="preprint",
        ),
    )
    await database.set_screening(
        result.evidence_id, "final", reason=None, note="Approved fixture", actor="acceptance"
    )
    return result.evidence_id


@pytest.mark.acceptance
@pytest.mark.asyncio
async def test_disposable_zotero_research_and_bibliography_workflow(tmp_path: Path) -> None:
    database = EvidenceDatabase(tmp_path / "zotero-workflow.db")
    await database.migrate()
    evidence_id = await _approved_evidence(database)
    fixture = ZoteroFixture()
    http_client = httpx.AsyncClient(transport=httpx.MockTransport(fixture))
    adapter = ZoteroAdapter(
        ZoteroSettings(enabled=True, api_key="fixture-zotero-key", library_id="42"),
        database,
        client=http_client,
    )
    server = create_mcp_server(SimpleNamespace(zotero=adapter))

    async with Client(server) as client:
        listed = await client.list_tools()
        names = {tool.name for tool in listed.tools}
        assert {
            "zotero_create_collection",
            "zotero_delete_collection",
            "zotero_create_item",
            "zotero_delete_item",
            "zotero_add_item_to_collection",
            "zotero_remove_item_from_collection",
            "zotero_add_tags",
            "zotero_remove_tags",
            "zotero_get_citation_metadata",
        } <= names

        top = await client.call_tool("zotero_create_collection", {"name": "RG-Zotero-Test"})
        top_key = top.structured_content["collection_key"]
        duplicate = await client.call_tool("zotero_create_collection", {"name": "RG-Zotero-Test"})
        assert duplicate.structured_content["collection_key"] == top_key
        assert duplicate.structured_content["created"] is False

        sub = await client.call_tool(
            "zotero_create_collection",
            {"name": "Sub-Test", "parent_collection_key": top_key},
        )
        sub_key = sub.structured_content["collection_key"]
        assert sub.structured_content["parent_collection_key"] == top_key
        collections = await client.call_tool("zotero_list_collections")
        listed_sub = next(
            item
            for item in collections.structured_content["collections"]
            if item["collection_key"] == sub_key
        )
        assert listed_sub["parent_collection_key"] == top_key
        assert listed_sub["version"]

        created = await client.call_tool(
            "zotero_create_item",
            {
                "evidence_id": evidence_id,
                "collection_keys": [top_key],
                "tags": ["retain-existing"],
                "dry_run": False,
            },
        )
        item_key = created.structured_content["item_key"]
        assert fixture.item_post_count == 1
        repeated = await client.call_tool(
            "zotero_create_item",
            {"evidence_id": evidence_id, "collection_keys": [top_key], "dry_run": False},
        )
        assert repeated.structured_content["item_key"] == item_key
        assert repeated.structured_content["created"] is False
        assert fixture.item_post_count == 1

        read = await client.call_tool("zotero_get_item", {"item_key": item_key})
        assert read.structured_content["evidence_id"] == evidence_id
        tagged = await client.call_tool(
            "zotero_add_tags",
            {"item_key": item_key, "tags": ["preprint-reputed", "R5", "E4", "S2"]},
        )
        assert set(tagged.structured_content["tags"]) >= {
            "retain-existing",
            "preprint-reputed",
            "R5",
            "E4",
            "S2",
        }
        reread = await client.call_tool("zotero_get_item", {"item_key": item_key})
        assert "R5" in reread.structured_content["tags"]

        membership = await client.call_tool(
            "zotero_add_item_to_collection",
            {"item_key": item_key, "collection_key": sub_key},
        )
        assert set(membership.structured_content["collections"]) == {top_key, sub_key}
        assert membership.structured_content["duplicate_item_created"] is False
        assert fixture.item_post_count == 1
        searched = await client.call_tool("zotero_search", {"query": "Disposable"})
        searched_item = searched.structured_content["items"][0]
        assert searched_item["item_key"] == item_key
        assert set(searched_item["collections"]) == {top_key, sub_key}
        assert "preprint-reputed" in searched_item["tags"]

        top_plan = await client.call_tool("zotero_delete_collection", {"collection_key": top_key})
        assert top_plan.structured_content["direct_item_count"] == 1
        assert top_plan.structured_content["child_collection_count"] == 1
        refused = await client.call_tool(
            "zotero_delete_collection",
            {"collection_key": top_key, "dry_run": False},
        )
        assert refused.is_error is True
        assert top_key in fixture.collections

        citation = await client.call_tool(
            "zotero_get_citation_metadata",
            {"item_keys": [item_key], "style": "apa"},
        )
        citation_item = citation.structured_content["items"][0]
        assert citation_item["item_key"] == item_key
        assert citation_item["evidence_id"] == evidence_id
        assert citation_item["DOI"] == "10.5555/rg-zotero-test"
        assert citation_item["formatted_citation"]
        assert citation_item["formatted_bibliography"]
        formatted_citation = await client.call_tool(
            "zotero_format_citation", {"item_keys": [item_key], "style": "apa"}
        )
        formatted_bibliography = await client.call_tool(
            "zotero_format_bibliography", {"item_keys": [item_key], "style": "apa"}
        )
        assert formatted_citation.structured_content["citations"]
        assert formatted_bibliography.structured_content["bibliography"]
        provenance = await client.call_tool(
            "zotero_record_citation_reference",
            {
                "manuscript": "IFT failure modes draft",
                "citation_location": "Methods paragraph 2",
                "item_key": item_key,
                "rationale": "Approved evidence for the methods claim",
            },
        )
        assert provenance.structured_content["evidence_id"] == evidence_id
        assert provenance.structured_content["identifier"] == "doi:10.5555/rg-zotero-test"

        removed_membership = await client.call_tool(
            "zotero_remove_item_from_collection",
            {"item_key": item_key, "collection_key": sub_key},
        )
        assert removed_membership.structured_content["collections"] == [top_key]
        removed_tag = await client.call_tool(
            "zotero_remove_tags", {"item_key": item_key, "tags": ["R5"]}
        )
        assert "R5" not in removed_tag.structured_content["tags"]
        assert "retain-existing" in removed_tag.structured_content["tags"]

        fixture.children[item_key] = [
            {
                "key": "ATTACH01",
                "version": fixture._version(),
                "data": {
                    "key": "ATTACH01",
                    "itemType": "attachment",
                    "title": "Protected fixture PDF",
                },
            }
        ]
        item_plan = await client.call_tool("zotero_delete_item", {"item_key": item_key})
        assert item_plan.structured_content["would_delete"] is True
        assert item_plan.structured_content["version"]
        assert item_plan.structured_content["child_item_count"] == 1
        protected = await client.call_tool(
            "zotero_delete_item", {"item_key": item_key, "dry_run": False}
        )
        assert protected.is_error is True
        assert item_key in fixture.items
        fixture.children.pop(item_key)
        deleted_item = await client.call_tool(
            "zotero_delete_item", {"item_key": item_key, "dry_run": False}
        )
        assert deleted_item.structured_content["deleted"] is True
        assert item_key not in fixture.items

        sub_plan = await client.call_tool("zotero_delete_collection", {"collection_key": sub_key})
        assert sub_plan.structured_content["would_delete"] is True
        await client.call_tool(
            "zotero_delete_collection", {"collection_key": sub_key, "dry_run": False}
        )
        await client.call_tool(
            "zotero_delete_collection", {"collection_key": top_key, "dry_run": False}
        )
        remaining = await client.call_tool("zotero_list_collections")
        assert remaining.structured_content["collections"] == []
        references = await client.call_tool(
            "zotero_list_citation_references", {"manuscript": "IFT failure modes draft"}
        )
        assert references.structured_content["references"][0]["item_key"] == item_key

    assert fixture.collections == {}
    assert fixture.items == {}
    assert await database.get_zotero_link(evidence_id, "user", "42") is None
    events = await database.list_audit_events(limit=100)
    operations = {event["operation"] for event in events}
    assert {
        "zotero.create_collection",
        "zotero.create_item",
        "zotero.add_tags",
        "zotero.add_item_to_collection",
        "zotero.remove_item_from_collection",
        "zotero.remove_tags",
        "zotero.delete_item",
        "zotero.delete_collection",
        "zotero.record_citation_reference",
    } <= operations
    assert any(
        event["operation"] == "zotero.delete_collection" and event["status"] == "failed"
        for event in events
    )
    await http_client.aclose()


@pytest.mark.acceptance
@pytest.mark.asyncio
async def test_zotero_explicit_safety_and_metadata_paths(tmp_path: Path) -> None:
    database = EvidenceDatabase(tmp_path / "zotero-safety.db")
    await database.migrate()
    fixture = ZoteroFixture()
    http_client = httpx.AsyncClient(transport=httpx.MockTransport(fixture))
    adapter = ZoteroAdapter(
        ZoteroSettings(enabled=True, api_key="fixture-zotero-key", library_id="42"),
        database,
        client=http_client,
    )

    planned = await adapter.create_item(
        title="Metadata-only approved paper",
        authors=[{"firstName": "Ada", "lastName": "Lovelace"}],
        year="2026",
        doi="10.5555/metadata-only",
        item_type="journalArticle",
        tags=["peer-reviewed"],
    )
    assert planned["would_create"] is True
    assert planned["planned_item"]["creators"][0]["lastName"] == "Lovelace"
    assert fixture.item_post_count == 0

    top = await adapter.create_collection("Recursive-Test")
    top_key = top["collection_key"]
    sub = await adapter.create_collection("Child", parent_collection_key=top_key)
    sub_key = sub["collection_key"]
    created = await adapter.create_item(
        title="Metadata-only approved paper",
        authors=[{"firstName": "Ada", "lastName": "Lovelace"}],
        year="2026",
        doi="10.5555/metadata-only",
        item_type="journalArticle",
        collection_keys=[top_key],
        tags=["peer-reviewed", "keep-unless-replaced"],
        dry_run=False,
    )
    item_key = created["item_key"]
    replaced = await adapter.set_tags(
        item_key, ["preprint-non-reputed", "R1"], preserve_existing=False
    )
    assert replaced["tags"] == ["preprint-non-reputed", "R1"]
    assert (await adapter.get_link_for_item(item_key))["link"] is None

    deleted_tree = await adapter.delete_collection(top_key, dry_run=False, recursive=True)
    assert deleted_tree["deleted_collection_keys"] == [sub_key, top_key]
    assert deleted_tree["preserved_item_keys"] == [item_key]
    assert item_key in fixture.items
    assert fixture.collections == {}
    assert (await adapter.delete_item(item_key, dry_run=False))["deleted"] is True

    await database.create_study("unapproved", "Unapproved", "Safety refusal")
    run = await database.create_search_run(
        study_id="unapproved",
        topic_id=None,
        provider="fixture",
        mode="save",
        label="",
        search_intent="Do not send unapproved evidence to Zotero",
        provider_query="unapproved",
        filters={},
        sort={},
        requested_limit=1,
    )
    evidence = await database.ingest_search_hit(
        run.search_run_id,
        1,
        SourceRecord(
            provider="fixture",
            provider_record_id="unapproved-1",
            title="Unapproved discovery",
            authors=[{"name": "Careful Researcher"}],
            year=2026,
        ),
    )
    with pytest.raises(ZoteroSafetyError):
        await adapter.create_item(evidence_id=evidence.evidence_id, dry_run=False)
    assert (await adapter.get_link_for_evidence(evidence.evidence_id))["link"] is None
    audit = await database.list_audit_events(limit=100)
    assert any(
        event["operation"] == "zotero.create_item"
        and event["status"] == "failed"
        and event["error_type"] == "zotero_safety_error"
        for event in audit
    )
    await http_client.aclose()
