# Providers

Each source reports whether it is enabled, configured, and actually available. “Unavailable” is an honest state, not a reason to scrape a website or silently substitute another database.

| Source | V0.1 behavior | Credential | Saved metadata |
|---|---|---|---|
| Scopus | Official Search API, full count/search/save | Elsevier API key | Minimal licensed raw metadata; normalized fields retained |
| arXiv | Official Atom API, paced requests | None | Open metadata and abstract |
| ACL Anthology | Local index built from official data | None | Open metadata and abstract |
| IEEE Xplore | [Official Metadata API](https://developer.ieee.org/docs/read/Metadata_API_details), including one-based paging, filters, and sorting | IEEE API key plus active approval | Minimal licensed raw metadata |
| Web of Science Starter | [Official Starter API](https://developer.clarivate.com/apis/wos-starter/swagger), including `/documents`, `q`, `db`, `limit`, and `page` | Clarivate key plus active Starter approval | Minimal licensed raw metadata |
| Web of Science Expanded | [Official Expanded API](https://developer.clarivate.com/apis/wos/swagger), including `databaseId`, `usrQuery`, `count`, and `firstRecord` | Clarivate key plus active Expanded approval | Minimal licensed raw metadata |
| ACM Digital Library | Unavailable until a supported official search mechanism is verified | Unknown | None |
| Zotero | Approved-item and final-corpus workflow with guarded collection, tag, citation, and deletion tools | Library-scoped key | Durable bidirectional item links and manuscript citation provenance; no PDF uploads |
| GitHub | Reads and branch-to-commit-to-pull-request writes | Fine-grained token | Safe operation summaries; no force/delete/merge/settings writes |

Scopus is the mandatory live scholarly source for release. arXiv and ACL are the open-source release checks. IEEE and both Web of Science contracts always run against deterministic fixtures in CI. Their live checks run only when the corresponding external approval is active; otherwise the command reports that the live test is deferred instead of misreporting an implementation failure.

Publication type and review status are separate fields. “Preprint” means the work was
shared before a journal or conference review decision, such as an arXiv record.
“Peer reviewed” is assigned only when provider metadata identifies a journal article,
review, or conference paper. Ambiguous records remain `unknown`.

## Zotero read and write checks

`zotero_search` returns the Zotero `item_key` with each bibliographic result. Pass that
same key to `zotero_get_item` to read the exact item. `zotero_credential_status` reads
Zotero's current-key metadata and reports effective library read, write, and note
permissions without returning the API key.

Final-corpus synchronization does not require a collection. When both collection
settings are empty, Research Gateway writes the bibliographic item to the library
root and stores the returned item key beside the local evidence record. A repeated
sync sees that durable link and does not create a duplicate. Zotero can return HTTP
200 for a batch in which an individual item failed; Research Gateway checks every
item result and records such a write as failed with a safe status, category,
operation, and stage.

The complete approved-reference workflow is described in [Zotero research and bibliography workflow](zotero.md). Collection and item deletion are available only behind a dry run, version protection, non-empty checks, and attachment refusal.
