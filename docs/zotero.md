# Zotero research and bibliography workflow

Zotero is the long-term bibliography for papers that a researcher and ChatGPT have decided are worth keeping. Research Gateway remains the trusted record of how a paper was discovered and reviewed. The link between them is the Research Gateway evidence ID and Zotero item key.

## Where Zotero fits

```text
official source -> Research Gateway evidence -> human/ChatGPT approval
                                              |
                                              v
                         Zotero item -> collections and research tags
                                     -> citation and bibliography
                                     -> manuscript reference provenance
```

Research Gateway does not send every search result to Zotero. Evidence must be included or final before `zotero_create_item` accepts an `evidence_id`. A caller may also deliberately create one item from supplied metadata.

## MCP tools

The following tools are available to ChatGPT:

| Purpose | Tool |
|---|---|
| Search and read | `zotero_search`, `zotero_get_item`, `zotero_list_collections` |
| Check permissions | `zotero_credential_status` |
| Synchronize a final corpus | `zotero_sync_corpus` |
| Create/delete folders | `zotero_create_collection`, `zotero_delete_collection` |
| Create/delete one record | `zotero_create_item`, `zotero_delete_item` |
| Change folder membership | `zotero_add_item_to_collection`, `zotero_remove_item_from_collection` |
| Change tags | `zotero_add_tags`, `zotero_remove_tags`, `zotero_set_tags` |
| Read citation data | `zotero_get_citation_metadata` |
| Render references | `zotero_format_citation`, `zotero_format_bibliography` |
| Follow evidence links | `zotero_get_link_for_evidence`, `zotero_get_link_for_item` |
| Record manuscript use | `zotero_record_citation_reference`, `zotero_list_citation_references` |

Search results contain `item_key`, `version`, `tags`, and `collections`. Collection results contain `collection_key`, `name`, `parent_collection_key`, and `version`, so a later tool call does not require a manual lookup.

Tags are free text. Current classifications such as `peer-reviewed`, `preprint-reputed`, `preprint-non-reputed`, `R1` through `R5`, `E1` through `E5`, and `S1` through `S6` need no schema or code change. Adding or removing selected tags preserves unrelated Zotero tags.

## Duplicate prevention and links

One Zotero item may appear in several collections, like one song appearing in several playlists. Adding a paper to another collection updates the same item's collection list; it never creates another bibliographic record.

Before creating an item, Research Gateway checks, in order:

1. the locally stored Zotero item key for the evidence;
2. a normalized DOI;
3. an arXiv identifier;
4. normalized title, year, and first-author identity.

The `zotero_links` table answers in both directions: which Zotero item belongs to an evidence record, and which evidence record belongs to a Zotero item. Deleting a disposable Zotero item removes its active link. A manuscript reference already recorded in `citation_references` retains its item key, evidence ID, identifier, location, and rationale for audit history.

## Safety behavior

- Item and collection deletion default to `dry_run=true`.
- Every new Zotero write and every deletion plan records a safe audit event.
- Real `PATCH` and `DELETE` calls use the current Zotero object version. A concurrent change is rejected instead of overwritten.
- A non-empty collection is refused unless `recursive=true` is explicit. Recursive collection deletion removes collection folders and preserves bibliography items.
- An item with child notes or attachments is refused. Research Gateway never deletes attachments or PDFs implicitly and has no PDF-upload operation.
- API keys remain only in the external global configuration.

## Citation data and rendering

`zotero_get_citation_metadata` returns only fields present in Zotero, including authors, date/year, journal or conference, volume, issue, pages, publisher, DOI, and URL. It also asks Zotero to render a citation and bibliography entry using the requested Citation Style Language style (CSL, a standard formatting rule such as `apa`, `ieee`, or `elsevier-harvard`). The rendering comes from the same Zotero item as the structured metadata.

The implementation uses Zotero Web API v3 JSON reads with `include=data,citation,bib`, `style`, and `locale`. No separate citation library or large dependency is added. See Zotero's [read API and bibliography parameters](https://www.zotero.org/support/dev/web_api/v3/basics) and [version-protected write requests](https://www.zotero.org/support/dev/web_api/v3/write_requests).

## Controlled ChatGPT test sequence

Use only the disposable names below. Do not use a real collection such as `IFTFailureModes`.

1. `zotero_create_collection(name="RG-Zotero-Test")`
2. `zotero_create_collection(name="Sub-Test", parent_collection_key="<top key>")`
3. `zotero_create_item(evidence_id="<approved evidence ID>", collection_keys=["<top key>"], dry_run=false)`
4. `zotero_get_item(item_key="<item key>")`
5. `zotero_add_tags(item_key="<item key>", tags=["preprint-reputed", "R5", "E4", "S2"])`
6. `zotero_add_item_to_collection(item_key="<item key>", collection_key="<sub key>")`
7. `zotero_search(query="<distinctive title or DOI>")` and confirm the same item key, tags, and both collection keys.
8. `zotero_get_citation_metadata(item_keys=["<item key>"], style="apa")`
9. `zotero_record_citation_reference(manuscript="<draft name>", citation_location="<location>", item_key="<item key>", rationale="<why this approved evidence supports the claim>")`
10. `zotero_remove_item_from_collection(item_key="<item key>", collection_key="<sub key>")`
11. `zotero_delete_item(item_key="<item key>")` and inspect the dry run.
12. `zotero_delete_item(item_key="<item key>", dry_run=false)`
13. `zotero_delete_collection(collection_key="<sub key>")`, inspect, then repeat with `dry_run=false`.
14. `zotero_delete_collection(collection_key="<top key>")`, inspect, then repeat with `dry_run=false`.
15. `zotero_list_collections()` and confirm both disposable collections are gone.

The deterministic acceptance test performs this lifecycle without external credentials. Live writes must still use a deliberately disposable collection and an approved Zotero key with write permission.

## Deliberately deferred

This change does not build a Word editor, modify DOCX files, upload PDFs, or delete child notes and attachments. The durable `citation_references` records and stable Zotero item keys are the extension point for a later manuscript layer.
