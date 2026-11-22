import asyncio
import hashlib
import os
import re
import time
from collections import deque
from urllib.parse import urlsplit

import httpx
from bs4 import BeautifulSoup
from playwright.async_api import Error as BrowserError
from playwright.async_api import async_playwright

from app.core.config import Settings
from app.schemas.analysis import Product, RatingBucket, Review
from app.services.review_evidence import (
    deduplicate_reviews,
    normalize_text,
    parse_review_date,
)

MAX_REVIEWS = 100
STAR_FILTERS = (
    ("1-star reviews", "one_star"),
    ("2-star reviews", "two_star"),
    ("4-star reviews", "four_star"),
    ("5-star reviews", "five_star"),
)


class AccessRestrictedError(ValueError):
    pass


def parse_metadata(soup: BeautifulSoup, product: Product) -> None:
    title = soup.select_one("#productTitle")
    if title:
        product.title = normalize_text(title.get_text(" ", strip=True))[:500]
    rating = soup.select_one(
        "#acrPopover[title], #averageCustomerReviews .a-icon-alt, [data-hook='rating-out-of-text']"
    )
    if rating:
        value = rating.get("title") or rating.get_text(" ", strip=True)
        match = re.search(r"\b([1-5](?:[.,]\d+)?)\s*(?:out of|/|von|sur)\s*5", str(value), re.I)
        if match:
            parsed_rating = float(match.group(1).replace(",", "."))
            if 1 <= parsed_rating <= 5:
                product.overall_rating = parsed_rating
    histogram = soup.select_one("#histogramTable, #cm_cr_dp_d_rating_histogram")
    if histogram:
        count = re.search(
            r"([\d,]+)\s+(?:global\s+)?ratings?\b", histogram.get_text(" ", strip=True), re.I
        )
        if count:
            product.rating_count = int(count.group(1).replace(",", ""))
        buckets: dict[int, float] = {}
        for element in histogram.select("a, tr"):
            label = str(element.get("aria-label") or element.get_text(" ", strip=True))
            match = re.search(r"(\d+(?:\.\d+)?)\s*(?:%|percent).*?([1-5])\s*stars?", label, re.I)
            if match:
                percentage, star = float(match.group(1)), int(match.group(2))
            else:
                match = re.search(r"([1-5])\s*stars?.*?(\d+(?:\.\d+)?)\s*%", label, re.I)
                if not match:
                    continue
                star, percentage = int(match.group(1)), float(match.group(2))
            if 0 <= percentage <= 100:
                buckets[star] = percentage
        if buckets:
            product.rating_distribution = [
                RatingBucket(stars=star, percentage=percentage)
                for star, percentage in sorted(buckets.items(), reverse=True)
            ]
    if product.rating_count is None:
        count_element = soup.select_one("#acrCustomerReviewText, [data-hook='total-review-count']")
        if count_element:
            count = re.search(
                r"([\d,]+)\s+(?:global\s+)?ratings?\b",
                count_element.get_text(" ", strip=True),
                re.I,
            )
            if count:
                product.rating_count = int(count.group(1).replace(",", ""))


def parse_page(html: str, product: Product) -> tuple[str | None, list[Review]]:
    soup = BeautifulSoup(html, "html.parser")
    previous_title = product.title
    parse_metadata(soup, product)
    reviews = []
    for element in soup.select('[data-hook="review"]')[:MAX_REVIEWS]:
        body = element.select_one(
            '[data-hook="review-body"], [data-hook="reviewRichContentContainer"]'
        )
        if body is None:
            continue
        text = normalize_text(body.get_text(" ", strip=True))[:4000]
        if not text:
            continue
        review_id = str(element.get("id", "")).removeprefix("customer_review-")
        if not re.fullmatch(r"R[A-Z0-9]+", review_id):
            review_id = hashlib.sha256(text.encode()).hexdigest()[:16]
        rating = element.select_one(
            '[data-hook="review-star-rating"], [data-hook="cmps-review-star-rating"]'
        )
        stars_match = re.match(r"\s*([1-5](?:[.,]\d+)?)\b", rating.get_text()) if rating else None
        stars = float(stars_match.group(1).replace(",", ".")) if stars_match else None
        if stars is not None and not 1 <= stars <= 5:
            stars = None
        date_element = element.select_one('[data-hook="review-date"]')
        date = normalize_text(date_element.get_text(" ", strip=True)) if date_element else None
        variant = element.select_one('[data-hook="format-strip"]')
        title = element.select_one('[data-hook="review-title"]')
        title_text = normalize_text(title.get_text(" ", strip=True)) if title else None
        if title_text:
            title_text = (
                re.sub(r"^[1-5](?:\.\d)?\s+out of\s+5\s+stars?\s*", "", title_text, flags=re.I)
                or None
            )
        reviews.append(
            Review(
                id=review_id,
                text=text,
                stars=stars,
                date=date,
                date_iso=parse_review_date(date),
                title=title_text,
                variant=normalize_text(variant.get_text(" ", strip=True)) if variant else None,
                source_url=f"https://www.amazon.in/gp/customer-reviews/{review_id}"
                if review_id.startswith("R")
                else product.url,
            )
        )
    return product.title if product.title != previous_title else None, reviews


def review_page_urls(product: Product) -> list[tuple[str, str]]:
    base = f"https://www.amazon.in/product-reviews/{product.asin}/"
    return (
        [("listing", product.url)]
        + [(label, f"{base}?filterByStar={star}&sortBy=recent") for label, star in STAR_FILTERS]
        + [("recent reviews", f"{base}?sortBy=recent")]
    )


def next_review_page(html: str, current_url: str, asin: str) -> str | None:
    soup = BeautifulSoup(html, "html.parser")
    link = soup.select_one("#cm_cr-pagination_bar li.a-last a, ul.a-pagination li.a-last a")
    if not link or not isinstance(link.get("href"), str):
        return None
    target = httpx.URL(current_url).join(link["href"])
    parsed = urlsplit(str(target))
    if (
        parsed.scheme == "https"
        and parsed.hostname in {"amazon.in", "www.amazon.in"}
        and parsed.username is None
        and parsed.password is None
        and parsed.port in {None, 443}
        and re.match(
            rf"^/(?:product-reviews|portal/customer-reviews)/{re.escape(asin)}(?:/|$)",
            parsed.path,
            re.I,
        )
    ):
        return str(target)
    return None


def check_access(html: str, url: str) -> None:
    if "/ap/signin" in url or 'id="ap_password"' in html:
        raise AccessRestrictedError("Amazon requires sign-in")
    if "validateCaptcha" in html or 'id="captchacharacters"' in html:
        raise AccessRestrictedError("Amazon returned a CAPTCHA")


async def fetch_page(client: httpx.AsyncClient, url: str, retries: int = 0) -> str:
    for attempt in range(retries + 1):
        current_url = url
        try:
            for _ in range(3):
                async with client.stream("GET", current_url) as response:
                    if response.is_redirect:
                        target = response.url.join(response.headers["location"])
                        if target.scheme != "https" or target.host not in {
                            "amazon.in",
                            "www.amazon.in",
                        }:
                            raise AccessRestrictedError("Amazon redirected outside Amazon India")
                        check_access("", str(target))
                        current_url = str(target)
                        continue
                    response.raise_for_status()
                    content = bytearray()
                    async for chunk in response.aiter_bytes():
                        content.extend(chunk)
                        if len(content) > 3_000_000:
                            raise ValueError("Amazon response exceeded the collection size limit")
                    html = content.decode("utf-8", errors="replace")
                    check_access(html, current_url)
                    return html
            raise ValueError("Amazon returned too many redirects")
        except (httpx.TimeoutException, httpx.NetworkError):
            if attempt == retries:
                raise
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code not in {500, 502, 503, 504} or attempt == retries:
                raise
        # Only transport failures and transient server errors receive one bounded retry.
        await asyncio.sleep(0.2)
    raise RuntimeError("Unreachable fetch attempt")


def balanced_review_sample(reviews: list[Review], limit: int) -> list[Review]:
    unique = deduplicate_reviews(reviews, MAX_REVIEWS * 10)
    negative = [review for review in unique if review.stars in (1, 2)]
    positive = [review for review in unique if review.stars in (4, 5)]
    other = [review for review in unique if review.stars not in (1, 2, 4, 5)]
    for group in (negative, positive, other):
        group.sort(key=lambda review: review.date_iso or "", reverse=True)
    positive_reserve = min(len(positive), max(1, limit // 3))
    selected = negative[: limit - positive_reserve] + positive[:positive_reserve]
    selected_ids = {review.id for review in selected}
    remaining = [review for review in negative + positive + other if review.id not in selected_ids]
    return selected + remaining[: limit - len(selected)]


async def collect_reviews(
    product: Product, config: Settings | None = None
) -> tuple[Product, list[Review], list[str]]:
    max_reviews = config.amazon_max_reviews if config else 30
    max_pages = config.amazon_max_pages if config else 6
    timeout = config.amazon_timeout_seconds if config else 10
    retries = config.amazon_transient_retries if config else 1
    collection_seconds = 90
    urls = review_page_urls(product)
    reviews: list[Review] = []
    limitations: list[str] = []
    blocked_review_route = False
    visited: set[str] = set()
    queue = deque(urls)
    deadline = time.monotonic() + collection_seconds
    try:
        async with asyncio.timeout(collection_seconds):
            async with httpx.AsyncClient(
                timeout=timeout,
                follow_redirects=False,
                headers={"User-Agent": "Mozilla/5.0", "Accept-Language": "en-IN,en;q=0.9"},
            ) as client:
                while queue and len(visited) < max_pages:
                    label, url = queue.popleft()
                    if url in visited:
                        continue
                    if blocked_review_route and label != "listing":
                        limitations.append(
                            f"{label.capitalize()}: unavailable because Amazon restricted "
                            "the review route."
                        )
                        continue
                    visited.add(url)
                    try:
                        html = await fetch_page(client, url, retries=retries)
                        _, page_reviews = parse_page(html, product)
                        reviews.extend(page_reviews)
                        next_page = next_review_page(html, url, product.asin)
                        if next_page and next_page not in visited:
                            queue.append((f"{label} next page", next_page))
                    except AccessRestrictedError as exc:
                        limitations.append(f"{label.capitalize()}: {exc}.")
                        if label != "listing":
                            blocked_review_route = True
                    except httpx.HTTPStatusError as exc:
                        limitations.append(
                            f"{label.capitalize()}: Amazon returned "
                            f"HTTP {exc.response.status_code}."
                        )
                        if label != "listing" and exc.response.status_code in {401, 403, 429}:
                            blocked_review_route = True
                    except (httpx.HTTPError, ValueError):
                        limitations.append(f"{label.capitalize()}: Amazon request failed.")
                if queue:
                    limitations.append(
                        f"Collection reached its {max_pages}-page limit; additional pages "
                        "were not requested."
                    )
            if not reviews and os.getenv("VERCEL") != "1":
                browser_urls = [url for _, url in urls[:max_pages]]
                try:
                    async with asyncio.timeout(min(30, max(0.1, deadline - time.monotonic()))):
                        title, browser_reviews, browser_limits = await collect_browser_reviews(
                            product, browser_urls
                        )
                        if title:
                            product.title = title
                        reviews.extend(browser_reviews)
                        limitations.extend(browser_limits)
                except (TimeoutError, BrowserError):
                    limitations.append("Local browser collection was unavailable or timed out.")
    except TimeoutError:
        limitations.append("Collection stopped at its 90-second overall time limit.")
    selected = balanced_review_sample(reviews, max_reviews)
    if len(deduplicate_reviews(reviews, MAX_REVIEWS * 10)) > len(selected):
        limitations.append(f"Collection was limited to {max_reviews} unique written reviews.")
    if not selected and os.getenv("VERCEL") == "1":
        limitations.append("Browser collection is unavailable on this deployment.")
    if not any(review.stars in (1, 2) for review in selected):
        limitations.append(
            "No 1-star or 2-star text was accessible in this sample. "
            "This does not establish that serious product concerns are absent."
        )
    if not any(review.stars in (4, 5) for review in selected):
        limitations.append("No 4-star or 5-star text was accessible for comparison.")
    if product.overall_rating is None:
        limitations.append("Overall product rating was unavailable.")
    if product.rating_count is None:
        limitations.append("Total product rating count was unavailable.")
    if len(product.rating_distribution) < 5:
        limitations.append("The full 5-star to 1-star rating distribution was unavailable.")
    limitations.append(
        "Review counts and issue mentions describe the collected sample, not buyer failure rates. "
        "Accessible reviews may include other variants."
    )
    return product, selected, list(dict.fromkeys(limitations))


async def collect_browser_reviews(
    product: Product, urls: list[str]
) -> tuple[str | None, list[Review], list[str]]:
    title = None
    reviews: list[Review] = []
    limitations = []
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(channel="chrome", headless=True)
        try:
            page = await browser.new_page()

            async def restrict_navigation(route):
                target = urlsplit(route.request.url)
                if route.request.is_navigation_request() and (
                    target.scheme != "https"
                    or target.hostname not in {"amazon.in", "www.amazon.in"}
                ):
                    await route.abort()
                else:
                    await route.continue_()

            await page.route("**/*", restrict_navigation)
            for url in urls:
                try:
                    await page.goto(url, wait_until="domcontentloaded", timeout=10000)
                    html = await page.content()
                    check_access(html, page.url)
                    parse_metadata(BeautifulSoup(html, "html.parser"), product)
                    await page.locator('[data-hook="review"]').first.wait_for(timeout=1500)
                    html = await page.content()
                    if len(html.encode("utf-8")) > 3_000_000:
                        continue
                    page_title, page_reviews = parse_page(html, product)
                    title = page_title or title
                    reviews.extend(page_reviews)
                except AccessRestrictedError as exc:
                    limitations.append(f"Local browser: {exc}.")
                    if "product-reviews" in url:
                        break
                except BrowserError:
                    limitations.append("A local browser page could not supply written reviews.")
        finally:
            await browser.close()
    return title, deduplicate_reviews(reviews, MAX_REVIEWS), limitations
