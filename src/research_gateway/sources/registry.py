from __future__ import annotations

from collections.abc import Iterable

from research_gateway.sources.base import SourceAdapter


class SourceRegistry:
    """A small catalog that keeps provider selection explicit and testable."""

    def __init__(self, adapters: Iterable[SourceAdapter] = ()) -> None:
        self._adapters = {adapter.name: adapter for adapter in adapters}

    def add(self, adapter: SourceAdapter) -> None:
        self._adapters[adapter.name] = adapter

    def get(self, name: str) -> SourceAdapter:
        try:
            return self._adapters[name]
        except KeyError as exc:
            raise KeyError(f"Unknown research source: {name}") from exc

    def statuses(self) -> list[dict[str, object]]:
        return [
            adapter.status.model_dump(mode="json") for _, adapter in sorted(self._adapters.items())
        ]

    async def aclose(self) -> None:
        for adapter in self._adapters.values():
            await adapter.aclose()
