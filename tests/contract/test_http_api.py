from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from research_gateway.api.app import create_app
from research_gateway.config import Settings


def _settings(tmp_path: Path) -> Settings:
    return Settings.model_validate(
        {
            "database": {"path": tmp_path / "gateway.db"},
            "mcp_remote_auth": {"token": "remote-test-token"},
            "acl_anthology": {"index_path": tmp_path / "missing.json"},
            "zotero": {"enabled": True, "api_key": "fixture", "library_id": "42"},
            "github": {"enabled": True, "token": "fixture"},
        }
    )


def test_local_api_crud_and_health(tmp_path: Path) -> None:
    with TestClient(create_app(_settings(tmp_path))) as client:
        health = client.get("/health")
        assert health.status_code == 200
        assert health.json()["remote_surface"] == ["/health", "/mcp"]
        created = client.post(
            "/api/v1/studies",
            json={"study_id": "s1", "name": "Study", "description": "Question"},
        )
        assert created.status_code == 201
        topic = client.post(
            "/api/v1/studies/s1/topics",
            json={"topic_id": "t1", "name": "Topic", "description": "Area"},
        )
        assert topic.status_code == 201
        assert client.get("/api/v1/studies").json()[0]["study_id"] == "s1"
        assert client.get("/api/v1/studies/s1/topics").json()[0]["topic_id"] == "t1"
        assert client.get("/api/v1/status").status_code == 200
        assert client.get("/api/v1/search-runs").json() == []
        assert client.get("/api/v1/evidence").json()["total"] == 0
        assert client.get("/api/v1/evidence/missing").status_code == 404
        assert client.get("/api/v1/summary").json()["total"] == 0
        assert client.get("/api/v1/audit").json() == []
        export = client.post(
            "/api/v1/exports",
            json={"path": str(tmp_path / "empty.json"), "format": "json", "study_id": "s1"},
        )
        assert export.status_code == 200
        assert export.json()["evidence_count"] == 0
        zotero = client.post("/api/v1/zotero/sync", json={"study_id": "s1"})
        assert zotero.json()["would_create"] == 0
        github = client.post(
            "/api/v1/github/publish",
            json={
                "repository": "owner/repo",
                "branch": "research/change",
                "files": {"evidence.md": "safe"},
                "commit_message": "Evidence",
                "pull_request_title": "Evidence",
                "pull_request_body": "Review",
            },
        )
        assert github.json()["dry_run"] is True
        assert client.get("/ui").status_code in {200, 307}


def test_forwarded_remote_surface_is_narrow_and_authenticated(tmp_path: Path) -> None:
    forwarded = {
        "Host": "127.0.0.1:8765",
        "X-Forwarded-For": "203.0.113.9",
        "X-Forwarded-Host": "demo.ngrok.app",
    }
    with TestClient(create_app(_settings(tmp_path))) as client:
        assert client.get("/health", headers=forwarded).status_code == 200
        assert client.get("/api/v1/status", headers=forwarded).status_code == 404
        assert client.post("/mcp", headers=forwarded, json={}).status_code == 401
        authorized = {
            **forwarded,
            "Authorization": "Bearer remote-test-token",
            "MCP-Protocol-Version": "2026-07-28",
            "MCP-Method": "tools/list",
        }
        response = client.post(
            "/mcp",
            headers=authorized,
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/list",
                "params": {
                    "_meta": {
                        "io.modelcontextprotocol/protocolVersion": "2026-07-28",
                        "io.modelcontextprotocol/clientCapabilities": {},
                    }
                },
            },
        )
        assert response.status_code == 200, response.text
        assert response.json()["result"]["tools"]
