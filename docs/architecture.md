# Architecture

One Python process serves three local interfaces: MCP for automation, a small JSON API for the browser, and the built React application. All three call the same research and export services. They do not call each other over HTTP.

Provider adapters translate official service responses into a common candidate record. The research service decides whether to count only (Explore) or save discoveries (Save). The Evidence Store then connects each source hit to one canonical paper where a conservative match is safe.

```text
Study -> Topic -> Search Run -> Search Hit -> Evidence -> Screening -> Final corpus
                                      \------ discovery history ------/
```

SQLite is deliberately sufficient for this local single-user tool. A schema version in `PRAGMA user_version` makes database changes explicit. Full-text search and indexed filters support local browsing without contacting a provider.

The React application is a review workspace, not a second backend. Python serves `ui/dist` at `/ui`; the browser uses `/api/v1`. MCP is available over standard input/output and Streamable HTTP at `/mcp`.
