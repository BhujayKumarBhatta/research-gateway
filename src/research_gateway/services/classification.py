from __future__ import annotations

from research_gateway.domain.models import SourceRecord


def classify_record(record: SourceRecord) -> SourceRecord:
    """Add a conservative publication and review classification without guessing."""
    classified = record.model_copy(deep=True)
    if classified.publication_type and classified.review_status != "unknown":
        return classified

    document_type = (record.document_type or "").casefold().replace("-", " ")
    subtype = str(record.raw_metadata.get("subtype") or "").casefold()
    provider = record.provider.casefold()

    if provider == "arxiv" or "preprint" in document_type:
        classified.publication_type = classified.publication_type or "preprint"
        classified.review_status = "preprint"
    elif subtype in {"ar", "re"} or document_type in {"article", "review", "journal article"}:
        classified.publication_type = classified.publication_type or (
            "review_article" if subtype == "re" or document_type == "review" else "journal_article"
        )
        classified.review_status = "peer_reviewed"
    elif (
        subtype in {"cp"}
        or "conference" in document_type
        or "proceedings paper" in document_type
        or provider == "acl_anthology"
    ):
        classified.publication_type = classified.publication_type or "conference_paper"
        classified.review_status = "peer_reviewed"
    elif "book chapter" in document_type:
        classified.publication_type = classified.publication_type or "book_chapter"
    elif "book" in document_type:
        classified.publication_type = classified.publication_type or "book"
    elif "standard" in document_type:
        classified.publication_type = classified.publication_type or "standard"
        classified.review_status = "not_peer_reviewed"
    else:
        classified.publication_type = classified.publication_type or "other"
    return classified
