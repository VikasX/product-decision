import json
import re
import unicodedata
from datetime import date, datetime

from app.schemas.analysis import Product, Review, SampleStatistics


# Return a bounded unique sample: prioritize recent negatives, reserve room for comparison.
def select_page_reviews(reviews: list[Review], limit: int) -> list[Review]:
    # Remove repeated IDs/text before choosing reviews so duplicates cannot inflate the sample.
    unique = deduplicate_reviews(reviews, 1000)
    # Keep one- and two-star reviews in a separate list, with recognized newer dates first.
    # lambda is a small callback: use each review ISO date as the sort key; reverse puts newest first.
    negative = sorted(
        [r for r in unique if r.stars in (1, 2)], key=lambda r: r.date_iso or "", reverse=True
    )
    # Keep all other ratings for comparison; these may still contain negative wording.
    others = sorted(
        [r for r in unique if r.stars not in (1, 2)], key=lambda r: r.date_iso or "", reverse=True
    )
    # Leave up to two spaces for comparison while keeping room for a negative review.
    reserve = min(2, len(others), max(0, limit - 1))
    # Fill the remaining space with available reviews rather than inventing missing categories.
    selected = negative[: limit - reserve] + others[: limit - len(negative[: limit - reserve])]
    # Return no more than the requested number, even if the input list is larger.
    return selected[:limit]


# Sparse-review analysis needs substantive summary text and all five rating percentages.
def usable_summary(product: Product) -> bool:
    # Turn the combined summary-length and full-histogram checks into a simple yes/no result.
    return bool(
        product.customers_say
        and len(product.customers_say.strip()) >= 40
        and {b.stars for b in product.rating_distribution if b.percentage is not None}
        == {1, 2, 3, 4, 5}
    )


# Product/reviews in, size-limited JSON-ready evidence out; omit HTML and unrelated metadata.
def compact_evidence(product: Product, reviews: list[Review], limit: int) -> dict:
    # Build a plain dictionary of useful evidence; this is what both adapters serialize for the model.
    evidence = {
        "title": product.title[:180],
        "overall_rating": product.overall_rating,
        "rating_count": product.rating_count,
        "rating_distribution": [b.model_dump() for b in product.rating_distribution],
        "amazon_ai_summary": product.customers_say,
        "amazon_feedback_topics": [
            t.model_dump(exclude_none=True) for t in product.feedback_topics
        ],
        # Each model review keeps its ID, stars, date and up to 1,000 text characters for traceable citations.
        "written_reviews": [
            {"id": r.id, "stars": r.stars, "text": r.text[:1000], "date": r.date_iso or r.date}
            for r in select_page_reviews(reviews, 8)
        ],
    }
    # Measure UTF-8 bytes, including non-English text, before allowing this input to be sent.
    if len(json.dumps(evidence, ensure_ascii=False).encode()) > limit:
        from app.services.usage import CostProtectionError

        raise CostProtectionError("Product evidence exceeds the compact input size limit.")
    # Return a JSON-ready dictionary only after it passes the compact size limit.
    return evidence


# Use consistent Unicode/spacing for extraction, duplicate detection and quote validation.
def normalize_text(text: str) -> str:
    # NFKC standardizes compatible Unicode characters; split/join collapses repeated whitespace.
    return " ".join(unicodedata.normalize("NFKC", text).split())


# Return sortable ISO dates when recognized; leave unknown dates unset rather than guessing.
def parse_review_date(text: str | None) -> str | None:
    # Missing dates stay unknown instead of becoming an invented date.
    if not text:
        return None
    value = normalize_text(text)
    # Recognize an English day-month-year date at the end of the Amazon review-date label.
    match = re.search(r"(?:on\s+)?(\d{1,2}\s+[A-Za-z]+\s+\d{4})$", value)
    if match:
        try:
            return datetime.strptime(match.group(1), "%d %B %Y").date().isoformat()
        # An unrecognized or invalid date should not crash extraction; return unknown instead.
        except ValueError:
            return None
    try:
        return date.fromisoformat(value).isoformat()
    # An unrecognized or invalid date should not crash extraction; return unknown instead.
    except ValueError:
        return None


# Match IDs and normalized text so repeated copies cannot inflate the evidence.
def deduplicate_reviews(reviews: list[Review], limit: int) -> list[Review]:
    # Remember source IDs already included in the output.
    seen_ids: set[str] = set()
    # Also remember normalized text because the same review may appear under another ID.
    seen_text: set[str] = set()
    result = []
    # Examine the incoming reviews in order and retain only the first copy of each.
    for review in reviews:
        text = normalize_text(review.text)
        # Use a case-insensitive comparison so capitalization differences do not create duplicates.
        fingerprint = text.casefold()
        # Skip empty reviews and any duplicate ID or duplicate normalized text.
        if not text or review.id in seen_ids or fingerprint in seen_text:
            continue
        seen_ids.add(review.id)
        seen_text.add(fingerprint)
        # Keep the original review fields, replacing only its text with the normalized version.
        result.append(review.model_copy(update={"text": text}))
        # Stop as soon as the caller has enough unique reviews.
        if len(result) == limit:
            break
    return result


# Compute collected-review counts/dates in Python; these are not overall Amazon ratings.
def sample_statistics(reviews: list[Review]) -> SampleStatistics:
    # Count each exact star value from 1 to 5 directly from the source sample.
    counts = {str(star): sum(review.stars == star for review in reviews) for star in range(1, 6)}
    # Sort only recognized dates so the oldest/newest bounds do not rely on guesses.
    dates = sorted(review.date_iso for review in reviews if review.date_iso)
    return SampleStatistics(
        by_star=counts,
        negative_count=counts["1"] + counts["2"],
        positive_count=counts["4"] + counts["5"],
        neutral_count=counts["3"],
        # Reviews outside the five exact star buckets remain explicitly unrated.
        unrated_count=len(reviews) - sum(counts.values()),
        oldest_review_date=dates[0] if dates else None,
        newest_review_date=dates[-1] if dates else None,
    )
