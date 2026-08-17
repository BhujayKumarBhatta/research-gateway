# Providers

Each source reports whether it is enabled, configured, and actually available. “Unavailable” is an honest state, not a reason to scrape a website or silently substitute another database.

| Source | V0.1 behavior | Credential | Saved metadata |
|---|---|---|---|
| Scopus | Official Search API, full count/search/save | Elsevier API key | Minimal licensed raw metadata; normalized fields retained |
| arXiv | Official Atom API, paced requests | None | Open metadata and abstract |
| ACL Anthology | Local index built from official data | None | Open metadata and abstract |
| IEEE Xplore | Official Metadata API adapter | IEEE API key | Minimal licensed raw metadata |
| Web of Science | Official Starter adapter; Expanded stays unavailable without its contract | Clarivate API key | Minimal licensed raw metadata |
| ACM Digital Library | Unavailable until a supported official search mechanism is verified | Unknown | None |
| Zotero | Final-corpus item sync, dry-run by default | Library-scoped key | Durable local item links; no deletes or PDF uploads |
| GitHub | Reads and branch-to-commit-to-pull-request writes | Fine-grained token | Safe operation summaries; no force/delete/merge/settings writes |

Scopus is the mandatory live scholarly source for release. arXiv and ACL are the open-source release checks. IEEE and Web of Science live calls are optional when their credentials are not configured.
