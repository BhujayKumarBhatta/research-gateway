from research_gateway.domain.models import SourceRecord
from research_gateway.services.classification import classify_record


def test_arxiv_is_classified_as_preprint_without_claiming_peer_review() -> None:
    classified = classify_record(
        SourceRecord(
            provider="arxiv",
            provider_record_id="2501.1",
            title="A preprint",
            document_type="preprint",
        )
    )
    assert classified.publication_type == "preprint"
    assert classified.review_status == "preprint"


def test_journal_article_is_classified_as_peer_reviewed_when_source_contract_supports_it() -> None:
    classified = classify_record(
        SourceRecord(
            provider="scopus",
            provider_record_id="2-s2.0-1",
            title="A journal paper",
            document_type="Article",
            raw_metadata={"subtype": "ar"},
        )
    )
    assert classified.publication_type == "journal_article"
    assert classified.review_status == "peer_reviewed"


def test_unknown_document_type_remains_unknown() -> None:
    classified = classify_record(
        SourceRecord(provider="fixture", provider_record_id="x", document_type="Other")
    )
    assert classified.publication_type == "other"
    assert classified.review_status == "unknown"
