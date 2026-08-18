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

## Put durable data on drive D from WSL

The following command uses the SQLite backup mechanism, keeps the old database, and
preserves every credential line in the external config. It also configures the log,
Excel backup, and process-state directories under the same root.

```bash
uv run research-gateway relocate-storage \
  --config /mnt/c/Users/Bhujay_ROG/.research-gateway/config.toml \
  --root /mnt/d/AI/research-gateway
```

The database then lives at
`D:\AI\research-gateway\data\research_gateway.db` in Windows and
`/mnt/d/AI/research-gateway/data/research_gateway.db` in WSL.

Start the local workspace:

```bash
uv run research-gateway serve
```

Open `http://127.0.0.1:8765/ui`. The service also exposes local MCP at `http://127.0.0.1:8765/mcp`.

For a background service, use:

```bash
uv run research-gateway service start
uv run research-gateway service status
uv run research-gateway service restart
uv run research-gateway service stop
```

Use `service start --tunnel` or `service restart --tunnel` for authenticated public
MCP. The status command prints only safe locations and URLs. It never prints OAuth
tokens, the authorization password, a bearer token, or provider keys. A repeated
start reports the existing healthy gateway and does not create another process.
Status says whether that process is managed by the current runtime-state file,
unmanaged, stopped, or whether another program occupies the configured port. Stop
and restart never adopt or kill an unmanaged process automatically.

## Logs and Excel backups

The service creates an Excel safety copy on every start when `[backup]` has
`on_service_start = true`. Each file has a UTC timestamp and `latest.xlsx` is refreshed
only after the workbook is complete. Create one manually with:

```bash
uv run research-gateway backup-excel
```

The application log rotates rather than growing forever. The central log filter
replaces configured secrets before a message is written.

Follow the active log in WSL with:

```bash
tail -f /mnt/d/AI/research-gateway/logs/research-gateway.log
```

Press `Ctrl+C` to stop only `tail`; the gateway keeps running. HTTP access entries
retain the method, path, and status but omit query strings. OAuth lifecycle entries
use a short one-way correlation label so one authorization can be traced without
recording its request identifier, browser state, password, code, or token.

## Explore and Save

Explore asks a provider for the current number of matches. It records the exact query and purpose but creates no search hits or evidence. Use it to refine syntax safely.

Save reruns that exact provider query, captures the requested page range, and records every permitted discovery. A paper found in several searches remains one evidence record with several discovery paths.

## Private remote MCP with ngrok

OAuth is the recommended ChatGPT path. Set `[tunnel] authtoken` in the external global file, then initialize the single-user authorization password and secrets:

```bash
uv run research-gateway oauth-init \
  --config /mnt/c/Users/Bhujay_ROG/.research-gateway/config.toml
uv run research-gateway service start --tunnel \
  --config /mnt/c/Users/Bhujay_ROG/.research-gateway/config.toml
uv run research-gateway service status \
  --config /mnt/c/Users/Bhujay_ROG/.research-gateway/config.toml
```

`oauth-init` asks for the authorization password without echoing it and stores only a strong salted hash. `--generate-password` is also available; that explicit command displays the generated password once. Store it in a password manager.

The public `/mcp` endpoint first returns an authorization challenge. A client then discovers the OAuth server, registers, opens the small Research Gateway approval page, and exchanges a one-use code protected by PKCE (a verifier that prevents a stolen code from being redeemed). Access tokens expire after 60 minutes by default, while rotated refresh tokens can renew the connection for 30 days. A browser or proxy can repeat one successful approval submission during a short 90-second completion window and receive the same redirect. This is an idempotent retry (repeating the request has the same result); it does not make the code reusable, and a second token exchange still fails.

ChatGPT configuration:

- Name: `Research Gateway`
- Server URL: the `Public MCP` URL printed by service status
- Authentication: `OAuth`
- Advanced OAuth Settings: leave automatic

The gateway publishes `/.well-known/oauth-protected-resource/mcp` and `/.well-known/oauth-authorization-server`, so manual endpoint values are normally unnecessary. The public hostname can reach only health, MCP, and required OAuth routes. `/ui` and `/api/v1` remain local.

For an older client that already supports a fixed bearer header, retain the separate legacy mode:

```toml
[mcp_remote_auth]
mode = "static_bearer"
token = "set-only-in-the-external-config"
```

OAuth mode never accepts the legacy static token, and static mode never accepts OAuth tokens. Stopping the Python process closes the listener.

## Acceptance commands

```bash
uv run research-gateway acceptance fixture
uv run research-gateway acceptance live-scopus
uv run research-gateway acceptance live-open
uv run research-gateway acceptance live-licensed
uv run research-gateway acceptance remote-ngrok
uv run research-gateway acceptance live-scopus-ngrok
uv run research-gateway acceptance oauth-fixture
uv run research-gateway acceptance oauth-ngrok
uv run research-gateway acceptance oauth-browser-ngrok
uv run research-gateway acceptance oauth-scopus-ngrok
```

Fixture acceptance requires no real credential. The live gates use a temporary database and do not pollute normal research data. `live-licensed` reports separate results for Web of Science Starter, Web of Science Expanded, and IEEE Xplore. Pending external approval is reported as a deferred live test; fixture and contract coverage still has to pass.
