from __future__ import annotations

from pathlib import Path
from typing import ClassVar

import pytest

from research_gateway.config import AclSettings
from research_gateway.sources.acl_anthology import AclAnthologyAdapter, build_official_index
from research_gateway.sources.base import ProviderUnavailableError

FIXTURES = Path(__file__).parents[2] / "fixtures"


@pytest.mark.asyncio
async def test_acl_search_uses_deterministic_local_official_index() -> None:
    adapter = AclAnthologyAdapter(AclSettings(index_path=FIXTURES / "acl_index.json"))

    page = await adapter.search('title:"fine tuning" year:2025', limit=10, offset=0)

    assert page.total_results == 1
    record = page.records[0]
    assert record.provider_record_id == "2025.acl-long.1"
    assert record.identifiers["acl_id"] == "2025.acl-long.1"
    assert record.doi == "10.1000/test"
    assert record.raw_metadata["source"] == "official-acl-anthology"


@pytest.mark.asyncio
async def test_acl_missing_index_is_honestly_unavailable(tmp_path: Path) -> None:
    adapter = AclAnthologyAdapter(AclSettings(index_path=tmp_path / "missing.json"))

    with pytest.raises(ProviderUnavailableError):
        await adapter.search("title:test", limit=10, offset=0)


def test_acl_refresh_builds_atomic_index_from_official_library(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class Text:
        def __init__(self, value: str) -> None:
            self.value = value

        def as_text(self) -> str:
            return self.value

    class Name:
        def as_full(self) -> str:
            return "Example, Ada"

    class Author:
        name = Name()

    class Volume:
        year = "2026"
        title = Text("Proceedings of ACL")

    class Paper:
        is_deleted = False
        is_frontmatter = False
        full_id = "2026.acl-long.7"
        title = Text("A Reproducible Paper")
        authors: ClassVar[list[Author]] = [Author()]
        parent = Volume()
        doi = "10.1000/acl.7"
        web_url = "https://aclanthology.org/2026.acl-long.7/"
        abstract = Text("A compact abstract.")

    class Collection:
        def papers(self) -> list[Paper]:
            return [Paper()]

    class FakeAnthology:
        collections: ClassVar[dict[str, Collection]] = {"2026.acl": Collection()}

    def from_repo(*, path: Path, verbose: bool) -> FakeAnthology:
        assert path == tmp_path / "official"
        assert verbose is False
        return FakeAnthology()

    monkeypatch.setattr("research_gateway.sources.acl_anthology.Anthology.from_repo", from_repo)
    index_path = tmp_path / "acl" / "index.json"

    result = build_official_index(index_path, tmp_path / "official")

    assert result["record_count"] == 1
    assert not index_path.with_suffix(".json.tmp").exists()
    adapter = AclAnthologyAdapter(AclSettings(index_path=index_path))
    record = adapter._load()["records"][0]
    assert record["id"] == "2026.acl-long.7"
    assert record["authors"] == [{"name": "Example, Ada"}]
