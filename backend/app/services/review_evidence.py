import re
import unicodedata
from datetime import date, datetime

from app.schemas.analysis import Review, SampleStatistics


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
