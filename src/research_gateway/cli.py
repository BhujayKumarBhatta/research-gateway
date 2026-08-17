from __future__ import annotations

import asyncio
import json
import subprocess
import sys
from contextlib import suppress
from pathlib import Path
from typing import Annotated

import typer
import uvicorn

from research_gateway.api.app import create_app
from research_gateway.config import (
    ConfigError,
    generate_remote_token,
    load_settings,
    resolve_config_path,
)
from research_gateway.mcp.server import create_mcp_server
from research_gateway.runtime import GatewayRuntime
from research_gateway.tunnel import NgrokTunnel

app = typer.Typer(
    name="research-gateway",
    help="Build a traceable research corpus from scholarly sources and review it locally.",
    no_args_is_help=True,
)
acceptance_app = typer.Typer(help="Run deterministic and live release gates.")
app.add_typer(acceptance_app, name="acceptance")


@app.command("init-config")
def init_config(
    path: Annotated[
        Path | None, typer.Option("--path", help="Global TOML configuration path.")
    ] = None,
    force: Annotated[bool, typer.Option(help="Replace an existing configuration file.")] = False,
) -> None:
    """Create a secret-free global configuration template outside the repository."""
    selected = (path or resolve_config_path()).expanduser().absolute()
    if selected.exists() and not force:
        raise typer.BadParameter(f"Configuration already exists: {selected}")
    selected.parent.mkdir(parents=True, exist_ok=True)
    selected.write_text(_CONFIG_TEMPLATE, encoding="utf-8")
    with suppress(OSError):
        selected.chmod(0o600)
    typer.echo(f"Created configuration template: {selected}")


@app.command("generate-token")
def generate_token() -> None:
    """Generate a strong bearer token to copy into the global configuration."""
    typer.echo(generate_remote_token())


@app.command()
def status(
    config: Annotated[Path | None, typer.Option("--config")] = None,
) -> None:
    """Show safe configuration and source readiness without displaying credentials."""
    settings = _load(config)
    runtime = GatewayRuntime.build(settings)
    typer.echo(f"Config: {(config or resolve_config_path()).expanduser().absolute()}")
    typer.echo(f"Database: {settings.database.path}")
    typer.echo(f"Local URL: http://{settings.service.host}:{settings.service.port}")
    typer.echo(f"Remote auth configured: {settings.mcp_remote_auth.configured}")
    typer.echo(f"ngrok configured: {settings.tunnel.configured}")
    for source in runtime.source_statuses():
        typer.echo(
            f"{source['name']}: enabled={source['enabled']} "
            f"configured={source['configured']} available={source['available']}"
        )
    asyncio.run(runtime.aclose())


@app.command("config-check")
def config_check(
    config: Annotated[Path | None, typer.Option("--config")] = None,
) -> None:
    """Validate the global TOML file without printing any values or secrets."""
    selected = config or resolve_config_path()
    try:
        settings = load_settings(selected, require_file=True)
    except ConfigError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(2) from exc
    typer.echo(f"CONFIG CHECK: PASS ({selected.expanduser().absolute()})")
    typer.echo(f"Remote authentication configured: {settings.mcp_remote_auth.configured}")


@app.command("db-info")
def db_info(
    config: Annotated[Path | None, typer.Option("--config")] = None,
) -> None:
    """Migrate the Evidence Store and print safe schema and row counts."""
    settings = _load(config)

    async def inspect() -> None:
        runtime = GatewayRuntime.build(settings)
        try:
            await runtime.start()
            typer.echo(f"Database: {settings.database.path}")
            typer.echo(f"Schema version: {await runtime.database.user_version()}")
            for table in ("studies", "topics", "search_runs", "search_hits", "evidence"):
                typer.echo(f"{table}: {await runtime.database.count_rows(table)}")
        finally:
            await runtime.aclose()

    asyncio.run(inspect())


@app.command()
def doctor(
    config: Annotated[Path | None, typer.Option("--config")] = None,
) -> None:
    """Check local database, UI assets, providers, and remote-access readiness."""
    settings = _load(config)

    async def inspect() -> None:
        runtime = GatewayRuntime.build(settings)
        try:
            await runtime.start()
            await runtime.database.user_version()
            typer.echo("Database migration/write check: PASS")
            ui_dist = Path(__file__).parents[2] / "ui" / "dist" / "index.html"
            typer.echo(f"Built UI: {'PASS' if ui_dist.is_file() else 'MISSING (run ui-build)'}")
            for source in runtime.source_statuses():
                readiness = (
                    "available"
                    if source["available"]
                    else source.get("unavailable_reason", "unavailable")
                )
                typer.echo(f"Source {source['name']}: {readiness}")
            typer.echo(f"Remote bearer configured: {settings.mcp_remote_auth.configured}")
            typer.echo(f"ngrok configured: {settings.tunnel.configured}")
        finally:
            await runtime.aclose()

    asyncio.run(inspect())


@app.command("ui-build")
def ui_build() -> None:
    """Build the React application into ui/dist using the locked npm dependencies."""
    ui_dir = Path(__file__).parents[2] / "ui"
    completed = subprocess.run(["npm", "run", "build"], cwd=ui_dir, check=False)
    if completed.returncode:
        raise typer.Exit(completed.returncode)
    typer.echo("UI BUILD: PASS")


@app.command("tunnel-status")
def tunnel_status() -> None:
    """Show non-secret state recorded by the currently running tunnel process."""
    state_path = Path.home() / ".research-gateway" / "runtime" / "tunnel.json"
    if not state_path.is_file():
        typer.echo("Tunnel: stopped")
        return
    try:
        payload = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        typer.echo("Tunnel state is unreadable.", err=True)
        raise typer.Exit(2) from None
    typer.echo("Tunnel: running")
    typer.echo(f"Public URL: {payload.get('public_url')}")
    typer.echo(f"Started: {payload.get('started_at')}")
    typer.echo(f"Exposed paths: {', '.join(payload.get('exposed_paths') or [])}")


@app.command("acl-refresh")
def acl_refresh(
    config: Annotated[Path | None, typer.Option("--config")] = None,
    repository_path: Annotated[Path | None, typer.Option("--repository-path")] = None,
) -> None:
    """Clone/update official ACL Anthology data and rebuild the local search index."""
    from research_gateway.sources.acl_anthology import AclAnthologyAdapter

    settings = _load(config)
    result = asyncio.run(AclAnthologyAdapter(settings.acl_anthology).refresh_index(repository_path))
    typer.echo(f"ACL index: {result['index_path']}")
    typer.echo(f"ACL records: {result['record_count']}")


@app.command()
def serve(
    config: Annotated[Path | None, typer.Option("--config")] = None,
    tunnel: Annotated[bool | None, typer.Option("--tunnel/--no-tunnel")] = None,
) -> None:
    """Serve the local API, browser UI, and Streamable HTTP MCP endpoint."""
    settings = _load(config)
    should_tunnel = settings.tunnel.start_on_serve if tunnel is None else tunnel
    gateway = GatewayRuntime.build(settings)
    http_app = create_app(settings, gateway)
    active_tunnel = NgrokTunnel(settings) if should_tunnel else None
    try:
        if active_tunnel:
            public = active_tunnel.start()
            typer.echo(f"Public health: {public.public_health_url}")
            typer.echo(f"Public MCP: {public.public_mcp_url}")
        typer.echo(f"Local UI: http://{settings.service.host}:{settings.service.port}/ui")
        uvicorn.run(http_app, host=settings.service.host, port=settings.service.port)
    finally:
        if active_tunnel:
            active_tunnel.stop()


@app.command("stdio")
def stdio_server(
    config: Annotated[Path | None, typer.Option("--config")] = None,
) -> None:
    """Run the same MCP tool contract over standard input/output."""
    settings = _load(config)
    runtime = GatewayRuntime.build(settings)
    asyncio.run(runtime.start())
    server = create_mcp_server(runtime)
    try:
        server.run("stdio")
    finally:
        asyncio.run(runtime.aclose())


@acceptance_app.command("fixture")
def acceptance_fixture() -> None:
    """Run deterministic multi-source acceptance with fake credentials and a temporary DB."""
    root = Path(__file__).parents[2]
    completed = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "tests/acceptance/test_fixture_acceptance.py"],
        cwd=root,
        check=False,
    )
    if completed.returncode:
        raise typer.Exit(completed.returncode)
    typer.echo("MULTI-SOURCE FIXTURE ACCEPTANCE: PASS")


@acceptance_app.command("live-scopus")
def acceptance_live_scopus(
    config: Annotated[Path | None, typer.Option("--config")] = None,
) -> None:
    """Run the real Scopus-to-MCP-to-temporary-Evidence-Store release gate."""
    from research_gateway.acceptance import run_live_scopus

    asyncio.run(run_live_scopus(_load(config)))


@acceptance_app.command("live-open")
def acceptance_live_open(
    config: Annotated[Path | None, typer.Option("--config")] = None,
) -> None:
    """Verify live arXiv and the local official ACL Anthology index."""
    from research_gateway.acceptance import run_live_open

    asyncio.run(run_live_open(_load(config)))


@acceptance_app.command("remote-ngrok")
def acceptance_remote_ngrok(
    config: Annotated[Path | None, typer.Option("--config")] = None,
) -> None:
    """Verify authenticated MCP through a real Python-managed ngrok endpoint."""
    from research_gateway.acceptance import run_remote_ngrok

    asyncio.run(run_remote_ngrok(_load(config), include_scopus=False))


@acceptance_app.command("live-scopus-ngrok")
def acceptance_live_scopus_ngrok(
    config: Annotated[Path | None, typer.Option("--config")] = None,
) -> None:
    """Verify public ngrok MCP through real Scopus and a temporary Evidence Store."""
    from research_gateway.acceptance import run_remote_ngrok

    asyncio.run(run_remote_ngrok(_load(config), include_scopus=True))


def _load(config: Path | None):
    try:
        return load_settings(config)
    except ConfigError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(2) from exc


_CONFIG_TEMPLATE = """# Research Gateway global configuration. Keep this file outside Git.
[service]
host = "127.0.0.1"
port = 8765

[database]
path = "~/.research-gateway/data/research_gateway.db"

[mcp_remote_auth]
mode = "static_bearer"
token = ""
allow_unauthenticated = false

[tunnel]
enabled = true
authtoken = ""
domain = ""
expose_ui = false
start_on_serve = false

[scopus]
enabled = true
api_key = ""
institutional_token = ""

[arxiv]
enabled = true
polite_delay_seconds = 3.0

[acl_anthology]
enabled = true
index_path = "~/.research-gateway/indexes/acl/index.json"

[ieee_xplore]
enabled = false
api_key = ""

[wos]
enabled = false
mode = "starter"
api_key = ""

[acm_dl]
enabled = false

[zotero]
enabled = false
api_key = ""
library_type = "user"
library_id = ""
collection_key = ""
collection_name = ""

[github]
enabled = false
token = ""
default_owner = ""
default_repository = ""
"""


if __name__ == "__main__":
    app()
