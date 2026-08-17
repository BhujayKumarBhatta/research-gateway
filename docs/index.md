# Research Gateway

Research Gateway helps one researcher search scholarly services, keep a durable record of what was found, review the results, and prepare a final corpus. It is a private personal project and has no dependency on Cognityx.

The program sits between research clients and external scholarly services:

```text
MCP client / local browser / command line
                    |
            shared Python services
              /             \
scholarly providers       Evidence Store (SQLite)
                                |
                 screening -> final corpus -> exports/Zotero/GitHub PR
```

The Evidence Store is the trusted master record. It keeps the exact source query and every later discovery. That trace is called provenance (the record of where a result came from).

Start with the [operations guide](operations.md).
