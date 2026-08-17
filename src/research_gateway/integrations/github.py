from __future__ import annotations

import base64
from typing import Any

import httpx

from research_gateway.config import GithubSettings
from research_gateway.db.database import EvidenceDatabase
from research_gateway.sources.base import ProviderConfigurationError, safe_http_error


class GithubAdapter:
    """A bounded GitHub client: reads plus branch/commit/pull-request writes only."""

    def __init__(
        self,
        settings: GithubSettings,
        database: EvidenceDatabase,
        *,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.settings = settings
        self.database = database
        self._owns_client = client is None
        self.client = client or httpx.AsyncClient(timeout=30)

    @property
    def status(self) -> dict[str, Any]:
        return {
            "name": "github",
            "enabled": self.settings.enabled,
            "configured": self.settings.configured,
            "available": self.settings.enabled and self.settings.configured,
            "read_capabilities": [
                "repository",
                "contents",
                "tree",
                "issues",
                "pull_requests",
                "repository_search",
                "code_search",
            ],
            "write_capabilities": [
                "issue",
                "issue_comment",
                "branch",
                "commit",
                "pull_request",
            ],
            "safety": ["dry_run_default", "no_force", "no_delete", "no_merge", "no_settings"],
        }

    async def get_repository(self, repository: str) -> dict[str, Any]:
        payload = await self._request("GET", f"/repos/{repository}")
        return {
            "full_name": payload.get("full_name"),
            "private": payload.get("private"),
            "default_branch": payload.get("default_branch"),
            "html_url": payload.get("html_url"),
        }

    async def list_issues(self, repository: str, *, state: str = "open") -> list[dict[str, Any]]:
        payload = await self._request("GET", f"/repos/{repository}/issues", params={"state": state})
        return [
            {key: item.get(key) for key in ("number", "title", "state", "html_url")}
            for item in payload
            if "pull_request" not in item
        ]

    async def search_repositories(self, query: str, *, limit: int = 20) -> dict[str, Any]:
        payload = await self._request(
            "GET", "/search/repositories", params={"q": query, "per_page": min(limit, 100)}
        )
        return {
            "total_count": payload.get("total_count", 0),
            "items": [
                {
                    key: item.get(key)
                    for key in (
                        "full_name",
                        "private",
                        "description",
                        "default_branch",
                        "html_url",
                    )
                }
                for item in payload.get("items") or []
            ],
        }

    async def search_code(
        self, query: str, *, repository: str | None = None, limit: int = 20
    ) -> dict[str, Any]:
        qualified = f"{query} repo:{repository}" if repository else query
        payload = await self._request(
            "GET", "/search/code", params={"q": qualified, "per_page": min(limit, 100)}
        )
        return {
            "total_count": payload.get("total_count", 0),
            "items": [
                {
                    "name": item.get("name"),
                    "path": item.get("path"),
                    "repository": (item.get("repository") or {}).get("full_name"),
                    "html_url": item.get("html_url"),
                }
                for item in payload.get("items") or []
            ],
        }

    async def read_file(
        self, repository: str, path: str, *, ref: str | None = None
    ) -> dict[str, Any]:
        payload = await self._request(
            "GET",
            f"/repos/{repository}/contents/{path}",
            params={"ref": ref} if ref else None,
        )
        if payload.get("type") != "file":
            raise ValueError("GitHub path is not a file.")
        content = payload.get("content") or ""
        decoded = (
            base64.b64decode(content).decode("utf-8")
            if payload.get("encoding") == "base64"
            else str(content)
        )
        return {
            "path": payload.get("path"),
            "sha": payload.get("sha"),
            "size": payload.get("size"),
            "content": decoded,
            "html_url": payload.get("html_url"),
        }

    async def list_tree(
        self, repository: str, tree_sha: str, *, recursive: bool = True
    ) -> dict[str, Any]:
        payload = await self._request(
            "GET",
            f"/repos/{repository}/git/trees/{tree_sha}",
            params={"recursive": "1"} if recursive else None,
        )
        return {
            "sha": payload.get("sha"),
            "truncated": bool(payload.get("truncated")),
            "items": [
                {key: item.get(key) for key in ("path", "mode", "type", "sha", "size")}
                for item in payload.get("tree") or []
            ],
        }

    async def get_issue(self, repository: str, number: int) -> dict[str, Any]:
        payload = await self._request("GET", f"/repos/{repository}/issues/{number}")
        return {
            key: payload.get(key)
            for key in (
                "number",
                "title",
                "body",
                "state",
                "html_url",
                "created_at",
                "updated_at",
            )
        }

    async def get_pull_request(self, repository: str, number: int) -> dict[str, Any]:
        payload = await self._request("GET", f"/repos/{repository}/pulls/{number}")
        return {
            key: payload.get(key)
            for key in ("number", "title", "body", "state", "draft", "merged", "html_url")
        }

    async def create_issue(
        self,
        repository: str,
        title: str,
        body: str,
        *,
        dry_run: bool = True,
    ) -> dict[str, Any]:
        if dry_run:
            await self.database.record_github_operation(
                "create_issue",
                repository,
                dry_run=True,
                status="planned",
                safe_summary=f"Would create issue: {title[:100]}",
            )
            return {"dry_run": True, "repository": repository, "title": title, "body": body}
        payload = await self._request(
            "POST", f"/repos/{repository}/issues", json={"title": title, "body": body}
        )
        await self.database.record_github_operation(
            "create_issue",
            repository,
            dry_run=False,
            status="completed",
            safe_summary=f"Created issue {payload.get('number')}.",
        )
        return {
            "dry_run": False,
            "number": payload.get("number"),
            "html_url": payload.get("html_url"),
        }

    async def comment_issue(
        self,
        repository: str,
        number: int,
        body: str,
        *,
        dry_run: bool = True,
    ) -> dict[str, Any]:
        if dry_run:
            return {"dry_run": True, "repository": repository, "issue": number, "body": body}
        payload = await self._request(
            "POST", f"/repos/{repository}/issues/{number}/comments", json={"body": body}
        )
        await self.database.record_github_operation(
            "comment_issue",
            repository,
            dry_run=False,
            status="completed",
            safe_summary=f"Commented on issue {number}.",
        )
        return {
            "dry_run": False,
            "id": payload.get("id"),
            "html_url": payload.get("html_url"),
        }

    async def publish_files(
        self,
        *,
        repository: str,
        branch: str,
        files: dict[str, str],
        commit_message: str,
        pull_request_title: str,
        pull_request_body: str,
        dry_run: bool = True,
    ) -> dict[str, Any]:
        if not self.settings.configured:
            raise ProviderConfigurationError("GitHub token is not configured.")
        if not files:
            raise ValueError("At least one file is required.")
        if branch in {"main", "master"}:
            raise ValueError("Direct writes to a conventional default branch are not allowed.")
        plan = {
            "repository": repository,
            "branch": branch,
            "files": sorted(files),
            "steps": ["create_branch", "create_commit", "open_pull_request"],
            "force": False,
            "merge": False,
        }
        if dry_run:
            await self.database.record_github_operation(
                "publish_files",
                repository,
                dry_run=True,
                status="planned",
                safe_summary=f"Would publish {len(files)} files through branch {branch} and a PR.",
            )
            return {"dry_run": True, **plan}

        repo = await self._request("GET", f"/repos/{repository}")
        default_branch = str(repo["default_branch"])
        if branch == default_branch:
            raise ValueError("Direct writes to the repository default branch are not allowed.")
        base_ref = await self._request("GET", f"/repos/{repository}/git/ref/heads/{default_branch}")
        base_sha = base_ref["object"]["sha"]
        await self._request(
            "POST",
            f"/repos/{repository}/git/refs",
            json={"ref": f"refs/heads/{branch}", "sha": base_sha},
        )
        commit = await self._request("GET", f"/repos/{repository}/git/commits/{base_sha}")
        tree_items = []
        for path, content in sorted(files.items()):
            blob = await self._request(
                "POST",
                f"/repos/{repository}/git/blobs",
                json={
                    "content": base64.b64encode(content.encode()).decode(),
                    "encoding": "base64",
                },
            )
            tree_items.append({"path": path, "mode": "100644", "type": "blob", "sha": blob["sha"]})
        tree = await self._request(
            "POST",
            f"/repos/{repository}/git/trees",
            json={"base_tree": commit["tree"]["sha"], "tree": tree_items},
        )
        new_commit = await self._request(
            "POST",
            f"/repos/{repository}/git/commits",
            json={"message": commit_message, "tree": tree["sha"], "parents": [base_sha]},
        )
        await self._request(
            "PATCH",
            f"/repos/{repository}/git/refs/heads/{branch}",
            json={"sha": new_commit["sha"], "force": False},
        )
        pull = await self._request(
            "POST",
            f"/repos/{repository}/pulls",
            json={
                "title": pull_request_title,
                "head": branch,
                "base": default_branch,
                "body": pull_request_body,
                "draft": False,
            },
        )
        await self.database.record_github_operation(
            "publish_files",
            repository,
            dry_run=False,
            status="completed",
            safe_summary=(
                f"Published {len(files)} files on branch {branch}; opened PR {pull.get('number')}."
            ),
        )
        return {
            "dry_run": False,
            "repository": repository,
            "branch": branch,
            "commit_sha": new_commit["sha"],
            "pull_request_number": pull.get("number"),
            "pull_request_url": pull.get("html_url"),
        }

    async def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        response = await self.client.request(
            method,
            self.settings.api_url.rstrip("/") + path,
            headers={
                "Authorization": f"Bearer {self.settings.token.get_secret_value()}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2026-03-10",
            },
            **kwargs,
        )
        if not 200 <= response.status_code < 300:
            raise safe_http_error("github", response.status_code)
        return response.json() if response.content else {}

    async def aclose(self) -> None:
        if self._owns_client:
            await self.client.aclose()
