# Research Gateway

Research Gateway is a private personal workspace for finding scholarly papers, keeping a trustworthy history of how they were found, screening them, and preparing a final research corpus. It is an independent project and is not part of Cognityx.

## Where it fits

```text
MCP client / local browser / command line
                    |
            Research Gateway services
             /                    \
official scholarly sources    Evidence Store (SQLite)
                                      |
                review -> final corpus -> Excel/CSV/JSON/Markdown
                                      -> Zotero bibliography
                                      -> GitHub branch + pull request
```

The local SQLite Evidence Store is the trusted master record. It keeps each exact provider query and every search that discovered a paper. This trace is provenance (the record of where a result came from). Excel and CSV are generated views, never the database of record.

## The research flow

```text
Study -> Topic -> Explore -> Save -> Evidence -> Screening -> Final corpus
```

- **Explore** asks a source for a count and records the query, purpose, and time. It creates no hits or evidence.
- **Save** reruns the exact query and stores permitted results, search hits, and discovery links.
- **Evidence** is the canonical paper record. Exact normalized DOI, trusted stable identifier, then a strong title/year/first-author fingerprint are used conservatively to avoid duplicates.
- **Screening** records every decision and reason. Final records can be synced to Zotero or exported for review.

## Interfaces

- MCP (Model Context Protocol, a standard way for AI clients to call tools) is the primary automation interface. Streamable HTTP is at `/mcp`; standard input/output is available with `research-gateway stdio`.
- The React + TypeScript review workspace is served at `/ui`.
- The browser uses a small internal JSON API under `/api/v1`; it is not a GPT Action API.
- All interfaces call the same Python services and Evidence Store.

## Quick start

Requirements: Python 3.12+, `uv`, Node.js 22+, and npm.

```bash
uv sync --dev
cd ui
npm ci
cd ..
uv run research-gateway ui-build
uv run research-gateway init-config
```

Edit `~/.research-gateway/config.toml` directly. On Windows use `%USERPROFILE%\.research-gateway\config.toml`. The repository includes [a secret-free example](config/local.example.toml), but real keys must never be copied into this checkout.

Check the installation and start the local workspace:

```bash
uv run research-gateway config-check
uv run research-gateway doctor
uv run research-gateway serve
```

Open `http://127.0.0.1:8765/ui`.

For a WSL installation whose durable data belongs on Windows drive D, copy the
database safely and update only the non-secret path entries in the global config:

```bash
uv run research-gateway relocate-storage \
  --config /mnt/c/Users/Bhujay_ROG/.research-gateway/config.toml \
  --root /mnt/d/AI/research-gateway
```

The source database and a timestamped config backup are retained. The destination
contains `data/research_gateway.db`, rotating logs, Excel backups, and process state.

## Source behavior

- Scopus uses the official Search API and is the mandatory live release source.
- arXiv uses the official Atom API and waits between consecutive requests.
- ACL Anthology searches a local index made from official Anthology data; normal search never scrapes the site.
- IEEE Xplore has a complete official Metadata API adapter and deterministic contract tests.
- Web of Science Starter and Expanded each have complete official API adapters and deterministic contract tests.
- IEEE and both Web of Science modes remain unavailable for live use until their
  external approval is active. This does not disable their fixture/contract tests.
- ACM Digital Library remains honestly unavailable because an official supported search API has not been verified. Crossref or OpenAlex is not presented as ACM search.
- Zotero checks the remote library by DOI, then by normalized title and year, before
  creating anything. It links a matching item back to Evidence, defaults to dry-run,
  skips durable existing links, and never deletes or uploads PDFs.
- GitHub supports bounded reads and branch-to-commit-to-pull-request proposals. It defaults to dry-run and never force-pushes, deletes, merges, changes settings, or writes directly to the default branch.

The Sources page and `source_list` MCP tool show actual availability, capabilities, paging notes, and retention policy without displaying credentials.

Each saved paper also receives a conservative publication classification. For
example, arXiv records are marked as preprints, while an unknown source type stays
unknown rather than being called peer reviewed without evidence.

## Background service, logs, and Excel safety copy

Use the lifecycle commands for normal day-to-day operation:

```bash
uv run research-gateway service start
uv run research-gateway service status
uv run research-gateway service restart
uv run research-gateway service stop
```

Add `--tunnel` to `start` or `restart` to keep the authenticated public MCP endpoint
running. A timestamped Excel workbook is created on service start and
`backups/latest.xlsx` always points to the newest completed snapshot. Logs rotate at
the configured size and configured credentials are removed by the central redaction
filter.

## Private remote MCP through ngrok

The official ngrok Python SDK is integrated; a separately managed ngrok command is not required. Put an ngrok authtoken under `[tunnel]` and a strong static token under `[mcp_remote_auth]`, then run:

```bash
uv run research-gateway serve --tunnel
uv run research-gateway tunnel-status
```

The server still binds to loopback. The public hostname reaches only `/health` and bearer-protected `/mcp` by default; `/ui` and `/api/v1` remain local. Tokens are sent in an authorization header, never a URL. Ctrl+C closes both the local server and listener.

ChatGPT custom-app availability and accepted authentication profiles can depend on the current plan. Passing the remote MCP acceptance proves the public authenticated protocol path; registering that endpoint in ChatGPT remains a separate user action.

## Tests and release gates

```bash
uv run ruff check .
uv run ruff format --check .
uv run pytest
uv run mkdocs build --strict
uv build
cd ui && npm test && npm run build && npm run test:e2e
```

The deterministic gate uses only fake fixture credentials:

```bash
uv run research-gateway acceptance fixture
```

Run the real local gates after fixture and UI checks are green:

```bash
uv run research-gateway acceptance live-scopus
uv run research-gateway acceptance live-open
uv run research-gateway acceptance live-licensed
uv run research-gateway acceptance remote-ngrok
uv run research-gateway acceptance live-scopus-ngrok
```

Live gates use temporary databases. They do not print configured secrets or alter normal research data. `live-licensed` runs an approved Web of Science or IEEE gate and otherwise reports `LIVE TEST DEFERRED — EXTERNAL APPROVAL PENDING` separately for Starter, Expanded, and IEEE. GitHub Actions uses fixtures and mocks only; it requires no external credentials.

## Configuration and limits

Set `RESEARCH_GATEWAY_CONFIG` to override the normal global config path for tests or a special launch. Database and export paths may be anywhere local. Generated databases, personal exports, `ui/dist`, `.env` files, and `config/local.toml` are ignored by Git.

V0.1 does not include PDF bulk download, scraping, embeddings, RAG, LLM calls, agent frameworks, multi-user accounts, cloud deployment, automatic GitHub merging, repository deletion, or Zotero deletion. It is deliberately focused research plumbing.

Detailed guides are in [the documentation](docs/index.md).
