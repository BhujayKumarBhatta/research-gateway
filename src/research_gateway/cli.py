from __future__ import annotations

import asyncio
import json
import logging
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
from research_gateway.db.database import EvidenceDatabase
from research_gateway.mcp.server import create_mcp_server
from research_gateway.oauth.setup import initialize_oauth, with_oauth_urls
from research_gateway.operations.backups import ExcelBackupService
from research_gateway.operations.logging import configure_logging
from research_gateway.operations.service import ServiceManager, ServiceStartError
from research_gateway.operations.storage import relocate_storage
from research_gateway.operations.supervisor import SupervisorError, SystemdUserSupervisor
from research_gateway.runtime import GatewayRuntime
from research_gateway.tunnel import NgrokTunnel

app = typer.Typer(
    name="research-gateway",
    help="Build a traceable research corpus from scholarly sources and review it locally.",
    no_args_is_help=True,
)
acceptance_app = typer.Typer(help="Run deterministic and live release gates.")
service_app = typer.Typer(help="Install, operate, and inspect the local background service.")
app.add_typer(acceptance_app, name="acceptance")
app.add_typer(service_app, name="service")


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


@app.command("oauth-init")
def oauth_init(
    config: Annotated[Path | None, typer.Option("--config")] = None,
    generate_password: Annotated[
        bool,
        typer.Option(
            "--generate-password",
            help="Generate and display a one-time OAuth authorization password.",
        ),
    ] = False,
) -> None:
    """Initialize secure single-user OAuth values in the external global config."""
    selected = (config or resolve_config_path()).expanduser().absolute()
    password = None
    if not generate_password:
        password = typer.prompt(
            "OAuth authorization password", hide_input=True, confirmation_prompt=True
        )
    try:
        result = initialize_oauth(selected, password=password, generate_password=generate_password)
    except (ConfigError, ValueError) as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(2) from exc
    typer.echo(f"OAuth configuration initialized: {result.config_path}")
    typer.echo(f"OAuth state store: {result.store_path}")
    if result.generated_password:
        typer.echo("One-time generated OAuth authorization password (store it securely):")
        typer.echo(result.generated_password)
        typer.echo("This password will not be shown again.")


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
    typer.echo(f"Remote auth mode: {settings.mcp_remote_auth.mode}")
    typer.echo(f"Remote auth configured: {settings.remote_auth_configured}")
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
    typer.echo(f"Remote authentication configured: {settings.remote_auth_configured}")


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
            typer.echo(f"Remote auth mode: {settings.mcp_remote_auth.mode}")
            typer.echo(f"Remote auth configured: {settings.remote_auth_configured}")
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
def tunnel_status(
    config: Annotated[Path | None, typer.Option("--config")] = None,
) -> None:
    """Show non-secret state recorded by the currently running tunnel process."""
    settings = _load(config)
    runtime_directory = settings.runtime.directory or settings.database.path.parent / "runtime"
    state_path = runtime_directory / "tunnel.json"
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


@app.command("backup-excel")
def backup_excel(
    config: Annotated[Path | None, typer.Option("--config")] = None,
) -> None:
    """Create a timestamped Excel snapshot and refresh backups/latest.xlsx."""
    settings = _load(config)

    async def create() -> None:
        database = EvidenceDatabase(settings.database.path)
        await database.migrate()
        directory = settings.backup.directory or settings.database.path.parent / "backups"
        result = await ExcelBackupService(
            database, directory, retention_count=settings.backup.retention_count
        ).create()
        typer.echo(f"Excel backup: {result.path}")
        typer.echo(f"Latest Excel backup: {result.latest_path}")

    asyncio.run(create())


@app.command("relocate-storage")
def relocate_storage_command(
    root: Annotated[
        Path,
        typer.Option("--root", help="New root for DB, logs, backups, and runtime state."),
    ],
    config: Annotated[Path | None, typer.Option("--config")] = None,
) -> None:
    """Copy the Evidence Store and repoint non-secret paths without removing the source."""
    selected = (config or resolve_config_path()).expanduser().absolute()
    result = relocate_storage(selected, root)
    asyncio.run(EvidenceDatabase(result.database_path).migrate())
    typer.echo(f"Database: {result.database_path}")
    typer.echo(f"Log: {result.log_path}")
    typer.echo(f"Excel backups: {result.backup_directory}")
    typer.echo(f"Runtime state: {result.runtime_directory}")
    typer.echo(f"Previous config backup: {result.config_backup}")


@service_app.command("start")
def service_start(
    config: Annotated[Path | None, typer.Option("--config")] = None,
    tunnel: Annotated[bool | None, typer.Option("--tunnel/--no-tunnel")] = None,
) -> None:
    """Start Research Gateway and use automatic recovery when it is installed."""
    selected = (config or resolve_config_path()).expanduser().absolute()
    settings = _load(selected)
    manager = ServiceManager(settings, selected)
    supervisor = SystemdUserSupervisor(settings, selected)
    try:
        if supervisor.manages_config:
            result = _start_with_supervisor(supervisor, manager, tunnel=tunnel)
        else:
            result = manager.start(tunnel=bool(tunnel))
    except (ServiceStartError, SupervisorError) as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(2) from None
    if result.get("running") and not result.get("started"):
        typer.echo("Research Gateway is already running.")
    _print_service_status(result)
    if result.get("running") and not result.get("started"):
        typer.echo("Service start: no action required.")


@service_app.command("stop")
def service_stop(
    config: Annotated[Path | None, typer.Option("--config")] = None,
) -> None:
    """Stop the supervised service or a validated detached process."""
    selected = (config or resolve_config_path()).expanduser().absolute()
    settings = _load(selected)
    manager = ServiceManager(settings, selected)
    supervisor = SystemdUserSupervisor(settings, selected)
    try:
        result = supervisor.stop(manager) if supervisor.manages_config else manager.stop()
    except SupervisorError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(2) from None
    typer.echo("Service: stopped" if result.get("stopped") else "Service: already stopped")


@service_app.command("restart")
def service_restart(
    config: Annotated[Path | None, typer.Option("--config")] = None,
    tunnel: Annotated[bool | None, typer.Option("--tunnel/--no-tunnel")] = None,
) -> None:
    """Restart Research Gateway and repair a failed supervised process."""
    selected = (config or resolve_config_path()).expanduser().absolute()
    settings = _load(selected)
    manager = ServiceManager(settings, selected)
    supervisor = SystemdUserSupervisor(settings, selected)
    try:
        if supervisor.manages_config:
            current = supervisor.status(manager)
            _reject_unowned_port(current)
            if tunnel is not None:
                supervisor.configure_tunnel(tunnel)
            result = supervisor.restart(manager)
        else:
            result = manager.restart(tunnel=bool(tunnel))
    except (ServiceStartError, SupervisorError) as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(2) from None
    _print_service_status(result)


@service_app.command("status")
def service_status(
    config: Annotated[Path | None, typer.Option("--config")] = None,
) -> None:
    """Show safe process, local URL, public URL, database, and log state."""
    selected = (config or resolve_config_path()).expanduser().absolute()
    settings = _load(selected)
    manager = ServiceManager(settings, selected)
    supervisor = SystemdUserSupervisor(settings, selected)
    result = supervisor.status(manager) if supervisor.manages_config else manager.status()
    _print_service_status(result)


@service_app.command("install")
def service_install(
    config: Annotated[Path | None, typer.Option("--config")] = None,
    tunnel: Annotated[bool, typer.Option("--tunnel/--no-tunnel")] = True,
    working_directory: Annotated[
        Path | None,
        typer.Option(
            "--working-directory",
            help="Durable Research Gateway repository checkout used by systemd.",
        ),
    ] = None,
    python_executable: Annotated[
        Path | None,
        typer.Option(
            "--python-executable",
            help="Python executable from the durable repository virtual environment.",
        ),
    ] = None,
) -> None:
    """Install automatic crash recovery, enable it at login, and start it."""
    selected = (config or resolve_config_path()).expanduser().absolute()
    settings = _load(selected)
    manager = ServiceManager(settings, selected)
    supervisor = SystemdUserSupervisor(
        settings,
        selected,
        working_directory=working_directory,
        python_executable=python_executable,
    )
    try:
        supervisor.validate_durable_location()
        if supervisor.installed and not supervisor.owned:
            raise SupervisorError(
                f"Existing unit is not managed by Research Gateway: {supervisor.unit_path}"
            )
        current = supervisor.status(manager) if supervisor.manages_config else manager.status()
        already_supervised = current.get("classification") == "supervised"
        if not already_supervised:
            if current.get("classification") == "managed":
                manager.stop()
            elif current.get("running") or current.get("classification") == "port_conflict":
                _reject_unowned_port(current)
        supervisor.install(tunnel=tunnel)
        result = supervisor.restart(manager) if already_supervised else supervisor.start(manager)
    except (ServiceStartError, SupervisorError) as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(2) from None
    typer.echo("Supervision installed and service started.")
    _print_service_status(result)


@service_app.command("uninstall")
def service_uninstall(
    config: Annotated[Path | None, typer.Option("--config")] = None,
) -> None:
    """Stop and remove Research Gateway automatic recovery without deleting data."""
    selected = (config or resolve_config_path()).expanduser().absolute()
    settings = _load(selected)
    supervisor = SystemdUserSupervisor(settings, selected)
    try:
        result = supervisor.uninstall()
    except SupervisorError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(2) from None
    typer.echo("Supervision removed." if result.get("removed") else "Supervision not installed.")


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
    log_path = configure_logging(settings)
    logging.getLogger(__name__).info("Preparing Research Gateway service.")
    if settings.backup.enabled and settings.backup.on_service_start:
        logging.getLogger(__name__).info("Creating startup Excel backup.")

        async def prepare_backup() -> int:
            database = EvidenceDatabase(settings.database.path)
            await database.migrate()
            directory = settings.backup.directory or settings.database.path.parent / "backups"
            result = await ExcelBackupService(
                database, directory, retention_count=settings.backup.retention_count
            ).create()
            return result.evidence_count

        evidence_count = asyncio.run(prepare_backup())
        logging.getLogger(__name__).info(
            "Startup Excel backup completed for %d evidence records.", evidence_count
        )
    should_tunnel = settings.tunnel.start_on_serve if tunnel is None else tunnel
    active_tunnel = NgrokTunnel(settings) if should_tunnel else None
    try:
        base_url = f"http://{settings.service.host}:{settings.service.port}"
        if active_tunnel:
            public = active_tunnel.start()
            base_url = public.public_url or base_url
            typer.echo(f"Public health: {public.public_health_url}")
            typer.echo(f"Public MCP: {public.public_mcp_url}")
        runtime_settings = with_oauth_urls(settings, base_url)
        gateway = GatewayRuntime.build(runtime_settings)
        http_app = create_app(runtime_settings, gateway)
        typer.echo(
            f"Local UI: http://{runtime_settings.service.host}:{runtime_settings.service.port}/ui"
        )
        typer.echo(f"Log: {log_path}")
        uvicorn.run(
            http_app,
            host=runtime_settings.service.host,
            port=runtime_settings.service.port,
            log_config=None,
        )
    finally:
        if active_tunnel:
            active_tunnel.stop()
        logging.getLogger(__name__).info("Research Gateway service stopped.")


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


@acceptance_app.command("oauth-fixture")
def acceptance_oauth_fixture() -> None:
    """Run the deterministic OAuth discovery, PKCE, refresh, and MCP contract."""
    root = Path(__file__).parents[2]
    completed = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "tests/contract/test_oauth_http.py"],
        cwd=root,
        check=False,
    )
    if completed.returncode:
        raise typer.Exit(completed.returncode)
    typer.echo("OAUTH FIXTURE ACCEPTANCE: PASS")


@acceptance_app.command("oauth-ngrok")
def acceptance_oauth_ngrok(
    config: Annotated[Path | None, typer.Option("--config")] = None,
) -> None:
    """Verify OAuth discovery and authenticated MCP through real ngrok."""
    from research_gateway.acceptance import run_oauth_ngrok

    asyncio.run(run_oauth_ngrok(_load(config), include_scopus=False))


@acceptance_app.command("oauth-browser-ngrok")
def acceptance_oauth_browser_ngrok(
    config: Annotated[Path | None, typer.Option("--config")] = None,
) -> None:
    """Verify one real browser OAuth approval and authenticated MCP through ngrok."""
    from research_gateway.acceptance import run_oauth_browser_ngrok

    asyncio.run(run_oauth_browser_ngrok(_load(config)))


@acceptance_app.command("oauth-scopus-ngrok")
def acceptance_oauth_scopus_ngrok(
    config: Annotated[Path | None, typer.Option("--config")] = None,
) -> None:
    """Verify the ChatGPT-compatible OAuth MCP path through live Scopus."""
    from research_gateway.acceptance import run_oauth_ngrok

    asyncio.run(run_oauth_ngrok(_load(config), include_scopus=True))


@acceptance_app.command("live-scopus-ngrok")
def acceptance_live_scopus_ngrok(
    config: Annotated[Path | None, typer.Option("--config")] = None,
) -> None:
    """Verify public ngrok MCP through real Scopus and a temporary Evidence Store."""
    from research_gateway.acceptance import run_remote_ngrok

    asyncio.run(run_remote_ngrok(_load(config), include_scopus=True))


@acceptance_app.command("live-wos")
def acceptance_live_wos(
    config: Annotated[Path | None, typer.Option("--config")] = None,
) -> None:
    """Run WoS live acceptance only when the configured subscription is active."""
    from research_gateway.acceptance import run_live_wos

    asyncio.run(run_live_wos(_load(config)))


@acceptance_app.command("live-ieee")
def acceptance_live_ieee(
    config: Annotated[Path | None, typer.Option("--config")] = None,
) -> None:
    """Run IEEE live acceptance only when its external API approval is active."""
    from research_gateway.acceptance import run_live_ieee

    asyncio.run(run_live_ieee(_load(config)))


@acceptance_app.command("live-licensed")
def acceptance_live_licensed(
    config: Annotated[Path | None, typer.Option("--config")] = None,
) -> None:
    """Run or explicitly defer WoS and IEEE live gates according to approval state."""
    from research_gateway.acceptance import run_live_ieee, run_live_wos

    settings = _load(config)
    asyncio.run(run_live_wos(settings))
    asyncio.run(run_live_ieee(settings))


def _load(config: Path | None):
    try:
        return load_settings(config)
    except ConfigError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(2) from exc


def _start_with_supervisor(
    supervisor: SystemdUserSupervisor,
    manager: ServiceManager,
    *,
    tunnel: bool | None,
) -> dict[str, object]:
    current = supervisor.status(manager)
    _reject_unowned_port(current)
    tunnel_changed = tunnel is not None and supervisor.configure_tunnel(tunnel)
    if current.get("classification") == "supervised" and current.get("running"):
        if tunnel_changed:
            return supervisor.restart(manager)
        return {**current, "started": False}
    return supervisor.start(manager)


def _reject_unowned_port(result: dict[str, object]) -> None:
    classification = result.get("classification")
    if classification == "port_conflict":
        raise SupervisorError(
            "The configured port is occupied by another service. "
            "Research Gateway did not stop or replace it."
        )
    if result.get("running") and classification != "supervised":
        raise SupervisorError(
            "A Research Gateway process not owned by the supervisor is already running. "
            "It was left intact; stop that process before enabling automatic recovery."
        )


def _print_service_status(result: dict[str, object]) -> None:
    classification = result.get("classification")
    labels = {
        "managed": "running (managed)",
        "supervised": "running (supervised with automatic recovery)",
        "supervisor_starting": "starting (supervisor is retrying)",
        "supervisor_failed": "stopped (supervisor reports a failure)",
        "unmanaged": "running (existing/unmanaged instance)",
        "port_conflict": "stopped (configured port occupied by another service)",
        "stopped": "stopped",
    }
    fallback = "running" if result.get("running") else "stopped"
    typer.echo(f"Service: {labels.get(classification, fallback)}")
    if result.get("pid"):
        typer.echo(f"PID: {result['pid']}")
    if result.get("observed_config_path"):
        typer.echo(f"Observed config: {result['observed_config_path']}")
    if result.get("supervisor_installed"):
        supervisor_state = "enabled" if result.get("supervisor_enabled") else "installed"
        typer.echo(f"Supervisor: {supervisor_state}")
        typer.echo(f"Automatic restarts: {result.get('supervisor_restarts', 0)}")
    for label, key in (
        ("Local UI", "local_ui_url"),
        ("Local MCP", "local_mcp_url"),
        ("Public MCP", "public_mcp_url"),
        ("Database", "database_path"),
        ("Log", "log_path"),
        ("Supervisor log", "supervisor_log_path"),
    ):
        if result.get(key):
            typer.echo(f"{label}: {result[key]}")
    if result.get("running") and result.get("tunnel_state") == "unknown":
        typer.echo("Tunnel: local gateway healthy; tunnel state unknown")


_CONFIG_TEMPLATE = """# Research Gateway global configuration. Keep this file outside Git.
[service]
host = "127.0.0.1"
port = 8765

[database]
path = "~/.research-gateway/data/research_gateway.db"

[logging]
# path = "~/.research-gateway/data/logs/research-gateway.log"
level = "INFO"

[backup]
enabled = true
on_service_start = true
retention_count = 20

[runtime]
# directory = "~/.research-gateway/data/runtime"

[mcp_remote_auth]
mode = "static_bearer"
token = ""
allow_unauthenticated = false

[mcp_oauth]
enabled = false
issuer_url = ""
resource_url = ""
scope = "research-gateway"
admin_password_hash = ""
signing_secret = ""
sealing_secret = ""
# store_path = "~/.research-gateway/data/runtime/oauth.sqlite3"
access_token_minutes = 60
refresh_token_days = 30
approval_completion_seconds = 90

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
approval_status = "pending"
api_key = ""
query_field = "querytext"

[wos]
enabled = false
mode = "starter"
approval_status = "pending"
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
