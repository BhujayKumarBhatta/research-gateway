# Research Gateway development rules

Research Gateway is a source-neutral personal research tool. Keep these rules true in every change.

- The SQLite Evidence Store is the trusted master record. Excel, CSV, JSON, and Markdown are exports.
- Preserve the exact provider query, the user's search purpose, and every discovery path. This history is provenance (the record of where a result came from).
- Keep provider behavior in adapters. MCP, the local API, and the UI call shared services rather than copying business rules.
- Deduplicate conservatively. Merge exact DOI or stable-work matches; leave uncertain cases for a person to review.
- Respect each provider's retention and license policy. Never add scraping as a substitute for an official API.
- Keep real secrets only in the global configuration. Never commit, log, export, audit, or return credentials.
- External writes default to dry-run. GitHub writes use a new branch and pull request; never force, delete, merge, change settings, or write the default branch directly.
- Never delete Zotero items or upload PDFs in V0.1.
- Add a failing test before meaningful new behavior, then implement the smallest complete change.
- Keep the deterministic multi-source acceptance green.
- ngrok is an explicit V0.1 feature. Bind locally to loopback, expose only `/health` and authenticated `/mcp` by default, never log tunnel or MCP tokens, and close listeners during shutdown.

Do not add Kubernetes, Docker, Redis, Celery, PostgreSQL, vector databases, embeddings, RAG, LLM calls, agent frameworks, multi-tenancy, cloud deployment, browser scraping, or bulk PDF downloading unless a later approved scope explicitly changes the product boundary.
