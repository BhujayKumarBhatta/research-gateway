# Safety and data handling

Real secrets live only in the global TOML file. The repository example contains empty values. Central redaction recognizes provider keys, bearer tokens, ngrok credentials, Zotero keys, and GitHub authorization headers.

The audit table stores what operation happened, where, when, and whether it succeeded. It stores a short safe summary rather than request headers or raw provider exceptions.

Remote access is narrow:

- the application binds to loopback (`127.0.0.1`) by default;
- the ngrok hostname exposes `/health` and `/mcp` only unless a user deliberately opts into more;
- remote MCP requires a static bearer token;
- the UI and internal API remain local by default;
- tunnel runtime state contains only the public URL, process ID, start time, and exposed paths;
- listener shutdown runs even after an error.

External writes remain bounded. Zotero sync creates or reuses bibliography items but never deletes or uploads files. GitHub proposals use a new branch, non-force commit, and pull request. They never write the default branch directly, merge, delete, or change repository settings.
