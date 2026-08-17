# Operations

## Prepare the project

```bash
uv sync --dev
cd ui && npm ci && cd ..
uv run research-gateway ui-build
uv run research-gateway init-config
```

The last command creates `~/.research-gateway/config.toml`. On Windows, the equivalent location is `%USERPROFILE%\.research-gateway\config.toml`. Keep it outside Git. Add credentials directly to that file; never paste them into logs, issues, or chat.

Validate the installation:

```bash
uv run research-gateway config-check
uv run research-gateway doctor
uv run research-gateway db-info
```

Start the local workspace:

```bash
uv run research-gateway serve
```

Open `http://127.0.0.1:8765/ui`. The service also exposes local MCP at `http://127.0.0.1:8765/mcp`.

## Explore and Save

Explore asks a provider for the current number of matches. It records the exact query and purpose but creates no search hits or evidence. Use it to refine syntax safely.

Save reruns that exact provider query, captures the requested page range, and records every permitted discovery. A paper found in several searches remains one evidence record with several discovery paths.

## Private remote MCP with ngrok

Set `[tunnel] authtoken` and `[mcp_remote_auth] token` in the global file, then run:

```bash
uv run research-gateway serve --tunnel
uv run research-gateway tunnel-status
```

The process prints a public `/mcp` URL and a `/health` URL. By default the public hostname cannot reach `/ui` or `/api/v1`. Remote MCP requests must send the static token in an `Authorization: Bearer ...` header. The token never belongs in the URL.

Stopping the Python process closes the listener. ChatGPT connection is a separate user action whose availability and authentication choices depend on the current product plan; a working ngrok URL alone is not a claim that ChatGPT registration is complete.

## Acceptance commands

```bash
uv run research-gateway acceptance fixture
uv run research-gateway acceptance live-scopus
uv run research-gateway acceptance live-open
uv run research-gateway acceptance remote-ngrok
uv run research-gateway acceptance live-scopus-ngrok
```

Fixture acceptance requires no real credential. The live gates use a temporary database and do not pollute normal research data.
