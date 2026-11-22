import json
import re
import unicodedata
from datetime import date, datetime

from app.schemas.analysis import Product, Review, SampleStatistics


def select_page_reviews(reviews: list[Review], limit: int) -> list[Review]:
    unique = deduplicate_reviews(reviews, 1000)
    negative = sorted(
        [r for r in unique if r.stars in (1, 2)], key=lambda r: r.date_iso or "", reverse=True
    )
    others = sorted(
        [r for r in unique if r.stars not in (1, 2)], key=lambda r: r.date_iso or "", reverse=True
    )
    reserve = min(2, len(others), max(0, limit - 1))
    selected = negative[: limit - reserve] + others[: limit - len(negative[: limit - reserve])]
    return selected[:limit]


def usable_summary(product: Product) -> bool:
    return bool(
        product.customers_say
        and len(product.customers_say.strip()) >= 40
        and {b.stars for b in product.rating_distribution if b.percentage is not None}
        == {1, 2, 3, 4, 5}
    )


def compact_evidence(product: Product, reviews: list[Review], limit: int) -> dict:
    evidence = {
        "title": product.title[:180],
        "overall_rating": product.overall_rating,
        "rating_count": product.rating_count,
        "rating_distribution": [b.model_dump() for b in product.rating_distribution],
        "amazon_ai_summary": product.customers_say,
        "amazon_feedback_topics": [
            t.model_dump(exclude_none=True) for t in product.feedback_topics
        ],
        "written_reviews": [
            {"id": r.id, "stars": r.stars, "text": r.text[:1000], "date": r.date_iso or r.date}
            for r in select_page_reviews(reviews, 8)
        ],
    }
    if len(json.dumps(evidence, ensure_ascii=False).encode()) > limit:
        from app.services.usage import CostProtectionError

        raise CostProtectionError("Product evidence exceeds the compact input size limit.")
    return evidence


def normalize_text(text: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", text).split())


def parse_review_date(text: str | None) -> str | None:
    if not text:
        return None
    value = normalize_text(text)
    match = re.search(r"(?:on\s+)?(\d{1,2}\s+[A-Za-z]+\s+\d{4})$", value)
    if match:
        try:
            return datetime.strptime(match.group(1), "%d %B %Y").date().isoformat()
        except ValueError:
            return None
    try:
        return date.fromisoformat(value).isoformat()
    except ValueError:
        return None


def deduplicate_reviews(reviews: list[Review], limit: int) -> list[Review]:
    seen_ids: set[str] = set()
    seen_text: set[str] = set()
    result = []
    for review in reviews:
        text = normalize_text(review.text)
        fingerprint = text.casefold()
        if not text or review.id in seen_ids or fingerprint in seen_text:
            continue
        seen_ids.add(review.id)
        seen_text.add(fingerprint)
        result.append(review.model_copy(update={"text": text}))
        if len(result) == limit:
            break
    return result


def sample_statistics(reviews: list[Review]) -> SampleStatistics:
    counts = {str(star): sum(review.stars == star for review in reviews) for star in range(1, 6)}
    dates = sorted(review.date_iso for review in reviews if review.date_iso)
    return SampleStatistics(
        by_star=counts,
        negative_count=counts["1"] + counts["2"],
        positive_count=counts["4"] + counts["5"],
        neutral_count=counts["3"],
        unrated_count=len(reviews) - sum(counts.values()),
        oldest_review_date=dates[0] if dates else None,
        newest_review_date=dates[-1] if dates else None,
    )
