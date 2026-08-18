from __future__ import annotations

import base64
from pathlib import Path

import httpx
import pytest

from research_gateway.config import GithubSettings, ZoteroSettings
from research_gateway.db.database import EvidenceDatabase
from research_gateway.domain.models import SourceRecord
from research_gateway.integrations.github import GithubAdapter
from research_gateway.integrations.zotero import ZoteroAdapter


async def _database_with_final_evidence(tmp_path: Path) -> tuple[EvidenceDatabase, str]:
    database = EvidenceDatabase(tmp_path / "evidence.db")
    await database.migrate()
    await database.create_study("s1", "Study", "Purpose")
    run = await database.create_search_run(
        study_id="s1",
        topic_id=None,
        provider="fixture",
        mode="save",
        label="",
        search_intent="intent",
        provider_query="query",
        filters={},
        sort={},
        requested_limit=1,
    )
    result = await database.ingest_search_hit(
        run.search_run_id,
        1,
        SourceRecord(
            provider="fixture",
            provider_record_id="r1",
            title="A useful final paper",
            authors=[{"name": "Grace Hopper"}],
            year=2024,
            doi="10.1000/final",
        ),
    )
    await database.set_screening(result.evidence_id, "final", reason=None, note=None, actor="test")
    return database, result.evidence_id


@pytest.mark.asyncio
async def test_zotero_dry_run_does_not_call_remote(tmp_path: Path) -> None:
    database, _ = await _database_with_final_evidence(tmp_path)
    requests: list[str] = []

    async def read_only(request: httpx.Request) -> httpx.Response:
        requests.append(request.method)
        assert request.method == "GET"
        return httpx.Response(200, json=[])

    client = httpx.AsyncClient(transport=httpx.MockTransport(read_only))
    adapter = ZoteroAdapter(
        ZoteroSettings(enabled=True, api_key="zotero-secret", library_id="42"),
        database,
        client=client,
    )
    result = await adapter.sync_final_corpus(study_id="s1")
    assert result == {
        "dry_run": True,
        "final_evidence_count": 1,
        "would_create": 1,
        "would_link_existing": 0,
        "already_linked": 0,
        "would_ensure_collection": False,
        "deleted": 0,
        "files_uploaded": 0,
    }
    assert requests == ["GET"]
    await client.aclose()


@pytest.mark.asyncio
async def test_zotero_write_is_idempotent_from_durable_link(tmp_path: Path) -> None:
    database, evidence_id = await _database_with_final_evidence(tmp_path)
    calls = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        assert request.headers["Zotero-API-Key"] == "zotero-secret"
        if request.method == "GET":
            return httpx.Response(200, json=[])
        return httpx.Response(200, json={"successful": {"0": {"key": "ITEM1"}}})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    adapter = ZoteroAdapter(
        ZoteroSettings(enabled=True, api_key="zotero-secret", library_id="42"),
        database,
        client=client,
    )
    first = await adapter.sync_final_corpus(study_id="s1", dry_run=False)
    second = await adapter.sync_final_corpus(study_id="s1", dry_run=False)
    assert first["created"] == 1
    assert second["created"] == 0
    assert second["already_linked"] == 1
    assert calls == 2
    assert await database.get_zotero_link(evidence_id, "user", "42")
    await client.aclose()


@pytest.mark.asyncio
async def test_zotero_links_remote_doi_match_instead_of_creating_duplicate(tmp_path: Path) -> None:
    database, evidence_id = await _database_with_final_evidence(tmp_path)
    requests: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request.method)
        assert request.method == "GET"
        assert request.url.params["q"] == "10.1000/final"
        return httpx.Response(
            200,
            json=[
                {
                    "key": "REMOTE1",
                    "version": 4,
                    "data": {
                        "itemType": "journalArticle",
                        "title": "A useful final paper",
                        "DOI": "https://doi.org/10.1000/FINAL",
                        "collections": [],
                    },
                }
            ],
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    adapter = ZoteroAdapter(
        ZoteroSettings(enabled=True, api_key="zotero-secret", library_id="42"),
        database,
        client=client,
    )
    result = await adapter.sync_final_corpus(study_id="s1", dry_run=False)
    assert result["created"] == 0
    assert result["matched_existing"] == 1
    assert requests == ["GET"]
    link = await database.get_zotero_link(evidence_id, "user", "42")
    assert link and link["item_key"] == "REMOTE1"
    await client.aclose()


@pytest.mark.asyncio
async def test_github_publish_defaults_to_a_no_network_plan(tmp_path: Path) -> None:
    database = EvidenceDatabase(tmp_path / "evidence.db")
    await database.migrate()

    async def unexpected(_: httpx.Request) -> httpx.Response:
        raise AssertionError("dry-run made a remote request")

    client = httpx.AsyncClient(transport=httpx.MockTransport(unexpected))
    adapter = GithubAdapter(
        GithubSettings(enabled=True, token="github-secret"), database, client=client
    )
    result = await adapter.publish_files(
        repository="owner/repo",
        branch="research/update",
        files={"evidence.md": "safe"},
        commit_message="Add evidence",
        pull_request_title="Add evidence",
        pull_request_body="Automated research export",
    )
    assert result["dry_run"] is True
    assert result["force"] is False
    assert result["merge"] is False
    assert await database.count_rows("github_operations") == 1
    await client.aclose()


@pytest.mark.asyncio
async def test_github_refuses_direct_default_branch_even_in_dry_run(tmp_path: Path) -> None:
    database = EvidenceDatabase(tmp_path / "evidence.db")
    await database.migrate()
    client = httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(500)))
    adapter = GithubAdapter(GithubSettings(enabled=True, token="token"), database, client=client)
    with pytest.raises(ValueError, match="Direct writes"):
        await adapter.publish_files(
            repository="owner/repo",
            branch="main",
            files={"x": "y"},
            commit_message="m",
            pull_request_title="t",
            pull_request_body="b",
        )
    await client.aclose()


@pytest.mark.asyncio
async def test_github_full_write_uses_non_force_branch_commit_and_pr(tmp_path: Path) -> None:
    database = EvidenceDatabase(tmp_path / "evidence.db")
    await database.migrate()
    seen: list[tuple[str, str, object]] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        body = __import__("json").loads(request.content) if request.content else None
        seen.append((request.method, request.url.path, body))
        path = request.url.path
        if path == "/repos/owner/repo":
            return httpx.Response(200, json={"default_branch": "trunk"})
        if path.endswith("/git/ref/heads/trunk"):
            return httpx.Response(200, json={"object": {"sha": "base-sha"}})
        if path.endswith("/git/refs"):
            return httpx.Response(201, json={"ref": "refs/heads/research/change"})
        if path.endswith("/git/commits/base-sha"):
            return httpx.Response(200, json={"tree": {"sha": "tree-base"}})
        if path.endswith("/git/blobs"):
            return httpx.Response(201, json={"sha": "blob-sha"})
        if path.endswith("/git/trees"):
            return httpx.Response(201, json={"sha": "tree-new"})
        if path.endswith("/git/commits"):
            return httpx.Response(201, json={"sha": "commit-new"})
        if path.endswith("/git/refs/heads/research/change"):
            return httpx.Response(200, json={"ref": "updated"})
        if path.endswith("/pulls"):
            return httpx.Response(201, json={"number": 7, "html_url": "https://github.test/pr/7"})
        raise AssertionError(path)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    adapter = GithubAdapter(
        GithubSettings(enabled=True, token="github-secret"), database, client=client
    )
    result = await adapter.publish_files(
        repository="owner/repo",
        branch="research/change",
        files={"reports/evidence.md": "safe evidence"},
        commit_message="Add evidence",
        pull_request_title="Add evidence",
        pull_request_body="Review this export",
        dry_run=False,
    )
    assert result["commit_sha"] == "commit-new"
    assert result["pull_request_number"] == 7
    ref_update = next(
        item for item in seen if item[1].endswith("/research/change") and item[0] == "PATCH"
    )
    assert ref_update[2] == {"sha": "commit-new", "force": False}
    pr = next(item for item in seen if item[1].endswith("/pulls"))
    assert pr[2]["base"] == "trunk"
    assert pr[2]["draft"] is False
    await client.aclose()


@pytest.mark.asyncio
async def test_zotero_reads_and_creates_named_collection(tmp_path: Path) -> None:
    database, _ = await _database_with_final_evidence(tmp_path)
    requests: list[tuple[str, str]] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append((request.method, request.url.path))
        if request.method == "GET" and request.url.path.endswith("/items"):
            return httpx.Response(
                200,
                json=[{"key": "I1", "version": 2, "data": {"title": "Paper", "DOI": "x"}}],
            )
        if request.method == "GET" and request.url.path.endswith("/items/I1"):
            return httpx.Response(200, json={"key": "I1", "version": 2, "data": {"title": "Paper"}})
        if request.method == "GET" and request.url.path.endswith("/collections"):
            return httpx.Response(200, json=[])
        if request.method == "POST" and request.url.path.endswith("/collections"):
            return httpx.Response(200, json={"successful": {"0": {"key": "C1"}}})
        if request.method == "POST" and request.url.path.endswith("/items"):
            body = __import__("json").loads(request.content)
            assert body[0]["collections"] == ["C1"]
            return httpx.Response(200, json={"successful": {"0": {"key": "I2"}}})
        raise AssertionError(request.url.path)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    adapter = ZoteroAdapter(
        ZoteroSettings(
            enabled=True,
            api_key="secret",
            library_id="42",
            collection_name="Final corpus",
        ),
        database,
        client=client,
    )
    assert (await adapter.search_items("Paper"))["items"][0]["key"] == "I1"
    assert (await adapter.get_item("I1"))["title"] == "Paper"
    assert (await adapter.list_collections())["collections"] == []
    synced = await adapter.sync_final_corpus(study_id="s1", dry_run=False)
    assert synced["created"] == 1
    assert ("POST", "/users/42/collections") in requests
    await client.aclose()


@pytest.mark.asyncio
async def test_github_read_and_issue_capabilities(tmp_path: Path) -> None:
    database = EvidenceDatabase(tmp_path / "evidence.db")
    await database.migrate()

    async def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/search/repositories":
            return httpx.Response(200, json={"total_count": 1, "items": [{"full_name": "o/r"}]})
        if path == "/search/code":
            return httpx.Response(
                200,
                json={
                    "total_count": 1,
                    "items": [
                        {"name": "a.py", "path": "src/a.py", "repository": {"full_name": "o/r"}}
                    ],
                },
            )
        if path.endswith("/contents/README.md"):
            return httpx.Response(
                200,
                json={
                    "type": "file",
                    "path": "README.md",
                    "sha": "s",
                    "size": 4,
                    "encoding": "base64",
                    "content": base64.b64encode(b"text").decode(),
                },
            )
        if "/git/trees/" in path:
            return httpx.Response(
                200, json={"sha": "tree", "tree": [{"path": "a.py", "type": "blob"}]}
            )
        if path.endswith("/issues/3"):
            return httpx.Response(200, json={"number": 3, "title": "Issue", "state": "open"})
        if path.endswith("/pulls/4"):
            return httpx.Response(200, json={"number": 4, "title": "PR", "state": "open"})
        if path.endswith("/issues") and request.method == "POST":
            return httpx.Response(201, json={"number": 5, "html_url": "https://github.test/5"})
        if path.endswith("/issues/3/comments"):
            return httpx.Response(201, json={"id": 8, "html_url": "https://github.test/c/8"})
        raise AssertionError(path)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    adapter = GithubAdapter(GithubSettings(enabled=True, token="secret"), database, client=client)
    assert (await adapter.search_repositories("research"))["total_count"] == 1
    assert (await adapter.search_code("term", repository="o/r"))["items"][0]["path"] == "src/a.py"
    assert (await adapter.read_file("o/r", "README.md"))["content"] == "text"
    assert (await adapter.list_tree("o/r", "tree"))["items"][0]["type"] == "blob"
    assert (await adapter.get_issue("o/r", 3))["title"] == "Issue"
    assert (await adapter.get_pull_request("o/r", 4))["title"] == "PR"
    assert (await adapter.create_issue("o/r", "Title", "Body"))["dry_run"] is True
    assert (await adapter.create_issue("o/r", "Title", "Body", dry_run=False))["number"] == 5
    assert (await adapter.comment_issue("o/r", 3, "Body"))["dry_run"] is True
    assert (await adapter.comment_issue("o/r", 3, "Body", dry_run=False))["id"] == 8
    await client.aclose()
