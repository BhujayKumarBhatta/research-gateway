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
| Zotero | Final-corpus item sync, dry-run by default | Library-scoped key | Durable local item links; no deletes or PDF uploads |
| GitHub | Reads and branch-to-commit-to-pull-request writes | Fine-grained token | Safe operation summaries; no force/delete/merge/settings writes |

Scopus is the mandatory live scholarly source for release. arXiv and ACL are the open-source release checks. IEEE and both Web of Science contracts always run against deterministic fixtures in CI. Their live checks run only when the corresponding external approval is active; otherwise the command reports that the live test is deferred instead of misreporting an implementation failure.

Publication type and review status are separate fields. “Preprint” means the work was
shared before a journal or conference review decision, such as an arXiv record.
“Peer reviewed” is assigned only when provider metadata identifies a journal article,
review, or conference paper. Ambiguous records remain `unknown`.
