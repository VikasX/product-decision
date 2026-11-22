import asyncio
import hashlib
import re
from urllib.parse import urlsplit

import httpx
from bs4 import BeautifulSoup
from playwright.async_api import Error as BrowserError
from playwright.async_api import async_playwright

from app.core.config import Settings
from app.schemas.analysis import FeedbackTopic, Product, RatingBucket, Review
from app.services.review_evidence import (
    deduplicate_reviews,
    normalize_text,
    parse_review_date,
    select_page_reviews,
)

# Limit how many matching review elements the HTML parser examines, before the final eight-review selection.
MAX_REVIEWS = 100
# These names belong to the older filtered-page helpers below, not the current collector.
STAR_FILTERS = (
    ("1-star reviews", "one_star"),
    ("2-star reviews", "two_star"),
    ("3-star reviews", "three_star"),
    ("4-star reviews", "four_star"),
    ("5-star reviews", "five_star"),
)


# Use a distinct exception for explicit Amazon access restrictions so they are not retried.
class AccessRestrictedError(ValueError):
    pass


# Update only observed listing fields; missing data remains unknown.
def parse_metadata(soup: BeautifulSoup, product: Product) -> None:
    # CSS selectors find known Amazon elements; select_one returns None when the field is absent.
    title = soup.select_one("#productTitle")
    # Only update listing details when a real product title element was found.
    if title:
        # Remove excess spacing and cap title length to keep downstream reports and inputs bounded.
        product.title = normalize_text(title.get_text(" ", strip=True))[:500]
        # Listing-scoped fields only; review pages and sponsored offers cannot replace them.
        image = soup.select_one("#landingImage, #imgBlkFront")
        if image:
            # Prefer the high-resolution listing image, falling back to its normal image source.
            image_url = image.get("data-old-hires") or image.get("src")
            if isinstance(image_url, str):
                # Split a URL into scheme, host, path and credentials so each part can be checked separately.
                parsed = urlsplit(image_url)
                if (
                    parsed.scheme == "https"
                    and parsed.hostname
                    in {
                        "m.media-amazon.com",
                        "images-na.ssl-images-amazon.com",
                        "images-eu.ssl-images-amazon.com",
                    }
                    and not parsed.username
                    and not parsed.password
                    and parsed.port in {None, 443}
                ):
                    # Retain the image only after checking HTTPS and the approved Amazon image hosts.
                    product.image_url = image_url
        # Look for the displayed selling price; exclude crossed-out reference prices where possible.
        price = soup.select_one(
            "#corePrice_feature_div .a-price:not(.a-text-price) .a-offscreen, "
            "#corePriceDisplay_desktop_feature_div .a-price:not(.a-text-price) .a-offscreen, "
            "#priceblock_ourprice, #priceblock_dealprice"
        )
        if price:
            product.price_text = normalize_text(price.get_text(" ", strip=True))[:100]
        # Collect observed overview labels/values for display; they are not inferred product claims.
        specs = []
        # Inspect at most eight overview rows so large pages do not create excessive metadata.
        for row in soup.select("#productOverview_feature_div tr")[:8]:
            # A useful overview row has two cells: the feature label and its value.
            cells = row.select("td")
            # Skip unexpected table layouts instead of guessing which text belongs to which field.
            if len(cells) == 2:
                label = normalize_text(cells[0].get_text(" ", strip=True))[:100]
                value = normalize_text(cells[1].get_text(" ", strip=True))[:250]
                if label and value:
                    specs.append({"label": label, "value": value})
        if specs:
            product.specifications = specs
    # Find a known listing-level average-rating element, separate from individual review stars.
    rating = soup.select_one(
        "#acrPopover[title], #averageCustomerReviews .a-icon-alt, [data-hook='rating-out-of-text']"
    )
    if rating:
        # The average rating may be stored in a title attribute or as visible element text.
        value = rating.get("title") or rating.get_text(" ", strip=True)
        match = re.search(r"\b([1-5](?:[.,]\d+)?)\s*(?:out of|/|von|sur)\s*5", str(value), re.I)
        if match:
            # Accept comma or dot decimals, then check that the observed rating is between one and five.
            parsed_rating = float(match.group(1).replace(",", "."))
            if 1 <= parsed_rating <= 5:
                product.overall_rating = parsed_rating
    # Read Amazon summary/topics from this same parsed document; no additional request is made.
    parse_customer_feedback(soup, product)
    # Percentages must come from explicit Amazon histogram labels, never the collected sample.
    histogram = soup.select_one("#histogramTable, #cm_cr_dp_d_rating_histogram")
    if histogram:
        count = re.search(
            r"([\d,]+)\s+(?:global\s+)?ratings?\b", histogram.get_text(" ", strip=True), re.I
        )
        if count:
            product.rating_count = int(count.group(1).replace(",", ""))
        # Map each star number to its observed percentage, avoiding duplicate histogram rows.
        buckets: dict[int, float] = {}
        # Amazon layouts use links or table rows; inspect either supported representation.
        for element in histogram.select("a, tr"):
            # Prefer accessible labels such as "55 percent of reviews have 5 stars" over decorative text.
            label = str(element.get("aria-label") or element.get_text(" ", strip=True))
            match = re.search(r"(\d+(?:\.\d+)?)\s*(?:%|percent).*?([1-5])\s*stars?", label, re.I)
            if match:
                # The first label format puts the percentage before the star number.
                percentage, star = float(match.group(1)), int(match.group(2))
            else:
                match = re.search(r"([1-5])\s*stars?.*?(\d+(?:\.\d+)?)\s*%", label, re.I)
                if not match:
                    continue
                # The fallback format puts the star number before a percent sign.
                star, percentage = int(match.group(1)), float(match.group(2))
            # Discard invalid percentages instead of showing impossible chart values.
            if 0 <= percentage <= 100:
                buckets[star] = percentage
        if buckets:
            product.rating_distribution = [
                RatingBucket(stars=star, percentage=percentage)
                for star, percentage in sorted(buckets.items(), reverse=True)
            ]
    # A separate count label may be available even when the histogram did not contain a total.
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


# Extract Amazon AI summary/topics as secondary evidence, distinct from written reviews.
def parse_customer_feedback(soup: BeautifulSoup, product: Product) -> None:
    # Try the older customer-summary container first; newer widget selectors are checked afterward.
    section = soup.select_one("#product-summary, [data-hook='cr-product-insights-summary']")
    if section:
        summary = section.select_one("[data-hook='product-summary'], .a-spacing-small .a-size-base")
        if summary:
            product.customers_say = normalize_text(summary.get_text(" ", strip=True))[:1800] or None
    # Amazon's newer product-review widget uses test IDs instead of the legacy hooks.
    summary = soup.select_one("#customerReviews [data-testid='overall-summary']")
    if summary:
        product.customers_say = normalize_text(summary.get_text(" ", strip=True))[:1800] or None
    # Store recognized topics, each with its own explicit sentiment and optional counts.
    topics = []
    # Track topic names without capitalization differences so repeated widgets do not duplicate topics.
    seen = set()
    # Examine only known review containers, with a hard limit on parsing work.
    for element in soup.select(
        "[data-hook='cr-insights-widget-aspects'] [data-csa-c-item-id], "
        "#cr-product-insights-cards [data-csa-c-item-id], "
        "#customerReviews [data-testid='aspect-list'] [role='tab']"
    ):
        name_element = element.select_one("[data-hook='cr-insights-widget-aspects-title']")
        # Accessible labels provide explicit sentiment/counts without guessing from colors.
        aspect_label = re.fullmatch(
            r"(Positive|Negative|Mixed) aspect, (.+), ([\d,]+) mentions?",
            str(element.get("aria-label", "")),
            re.I,
        )
        # Skip elements that have neither a supported title nor a supported accessible topic label.
        if not name_element and not aspect_label:
            continue
        name = normalize_text(
            name_element.get_text(" ", strip=True) if name_element else aspect_label.group(2)
        )[:80]
        # Skip blank topic names and duplicates before adding their evidence.
        if not name or name.casefold() in seen:
            continue
        seen.add(name.casefold())
        label = normalize_text(str(element.get("aria-label") or element.get_text(" ", strip=True)))
        # Some older widgets include the sentiment in their tracking item ID.
        indicator = str(element.get("data-csa-c-item-id", "")).lower()
        # Read a dedicated sentiment attribute when Amazon provides one.
        explicit = str(element.get("data-sentiment", "")).lower()
        sentiment = explicit if explicit in {"positive", "negative", "mixed"} else None
        # The newer accessible label directly states positive, negative or mixed sentiment.
        if aspect_label:
            sentiment = aspect_label.group(1).lower()
        # Only try the older item-ID format when a recognized sentiment is still missing.
        if sentiment is None:
            for value in ("mixed", "negative", "positive"):
                if re.search(rf"(?:^|[_ -]){value}(?:$|[_ -])", indicator):
                    sentiment = value
                    break

        # This small helper reads a count from the current label; missing counts stay None, not zero.
        def count(pattern, label=label):
            match = re.search(pattern, label, re.I)
            return int(match.group(1).replace(",", "")) if match else None

        detail = element.select_one("[data-hook='cr-insights-widget-aspects-summary']")
        # Pydantic checks the extracted topic fields before they become part of the product evidence.
        topics.append(
            FeedbackTopic(
                name=name,
                sentiment=sentiment,
                mention_count=count(r"([\d,]+)\s+(?:customers?\s+)?mentions?"),
                positive_count=count(r"([\d,]+)\s+positive"),
                negative_count=count(r"([\d,]+)\s+negative"),
                text=normalize_text(detail.get_text(" ", strip=True))[:500] if detail else None,
            )
        )
        # Bound scanning even if unexpected markup supplies many repeated topic-like elements.
        if len(topics) == 64:
            break
    # Keep up to eight topics, placing negative and mixed evidence before positives and unknowns.
    product.feedback_topics = sorted(
        topics, key=lambda t: {"negative": 0, "mixed": 1, "positive": 2, None: 3}[t.sentiment]
    )[:8]


# HTML and mutable Product in; updated title and direct source reviews out. No network calls.
def parse_page(html: str, product: Product) -> tuple[str | None, list[Review]]:
    # Turn HTML into a searchable element tree; this does not execute Amazon JavaScript.
    soup = BeautifulSoup(html, "html.parser")
    # Remember the incoming title so the return value can signal whether this page supplied a new one.
    previous_title = product.title
    parse_metadata(soup, product)
    # Build the list of direct written reviews found in this document.
    reviews = []
    # Examine only known review containers, with a hard limit on parsing work.
    for element in soup.select('[data-hook="review"]')[:MAX_REVIEWS]:
        # Support both older plain review bodies and newer rich-content containers.
        body = element.select_one(
            '[data-hook="review-body"], [data-hook="reviewRichContentContainer"]'
        )
        # A review container without body text is not useful written evidence.
        if body is None:
            continue
        # Keep normalized review words, capped at 4,000 characters, rather than raw HTML.
        text = normalize_text(body.get_text(" ", strip=True))[:4000]
        # Ignore empty bodies; they cannot support a buying claim or an exact quote.
        if not text:
            continue
        # Stable IDs support citations; text hashes identify reviews whose source ID is missing.
        review_id = str(element.get("id", "")).removeprefix("customer_review-")
        if not re.fullmatch(r"R[A-Z0-9]+", review_id):
            review_id = hashlib.sha256(text.encode()).hexdigest()[:16]
        rating = element.select_one(
            '[data-hook="review-star-rating"], [data-hook="cmps-review-star-rating"]'
        )
        # Read the leading star number only when the review includes a supported rating element.
        stars_match = re.match(r"\s*([1-5](?:[.,]\d+)?)\b", rating.get_text()) if rating else None
        stars = float(stars_match.group(1).replace(",", ".")) if stars_match else None
        # Keep missing or invalid review stars unknown rather than forcing them into a sentiment bucket.
        if stars is not None and not 1 <= stars <= 5:
            stars = None
        # Read the displayed review-date label; the shared helper also attempts an ISO date.
        date_element = element.select_one('[data-hook="review-date"]')
        date = normalize_text(date_element.get_text(" ", strip=True)) if date_element else None
        # Preserve variant text because reviews on one listing may describe other product variations.
        variant = element.select_one('[data-hook="format-strip"]')
        title = element.select_one('[data-hook="review-title"], [data-hook="reviewTitle"]')
        title_text = normalize_text(title.get_text(" ", strip=True)) if title else None
        # Remove a leading star-rating phrase from older review-title formats.
        if title_text:
            title_text = (
                re.sub(r"^[1-5](?:\.\d)?\s+out of\s+5\s+stars?\s*", "", title_text, flags=re.I)
                or None
            )
        # Build a typed Review with source text, date, rating and a link suitable for citations.
        reviews.append(
            Review(
                id=review_id,
                text=text,
                stars=stars,
                date=date,
                date_iso=parse_review_date(date),
                title=title_text,
                variant=normalize_text(variant.get_text(" ", strip=True)) if variant else None,
                # Use a direct review link for a real review ID; otherwise link back to the product page.
                source_url=f"https://www.amazon.in/gp/customer-reviews/{review_id}"
                if review_id.startswith("R")
                else product.url,
            )
        )
    return product.title if product.title != previous_title else None, reviews


# Legacy helper; the active collector does not fetch these filtered review URLs.
def review_page_urls(product: Product) -> list[tuple[str, str]]:
    # Construct the old review-page base URL; this helper does not perform network requests.
    base = f"https://www.amazon.in/product-reviews/{product.asin}/"
    return (
        [("listing", product.url)]
        + [(label, f"{base}?filterByStar={star}&sortBy=recent") for label, star in STAR_FILTERS]
        + [("recent reviews", f"{base}?sortBy=recent")]
    )


# Legacy pagination helper, inactive in the current product-page collection flow.
def next_review_page(html: str, current_url: str, asin: str) -> str | None:
    # Turn HTML into a searchable element tree; this does not execute Amazon JavaScript.
    soup = BeautifulSoup(html, "html.parser")
    link = soup.select_one("#cm_cr-pagination_bar li.a-last a, ul.a-pagination li.a-last a")
    if not link or not isinstance(link.get("href"), str):
        return None
    # Resolve a relative next-page link before checking whether its destination is allowed.
    target = httpx.URL(current_url).join(link["href"])
    # Split a URL into scheme, host, path and credentials so each part can be checked separately.
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


# Stop on restrictions instead of trying to bypass sign-in or CAPTCHA.
def check_access(html: str, url: str) -> None:
    # A login destination or password field means Amazon requires authentication.
    if "/ap/signin" in url or 'id="ap_password"' in html:
        raise AccessRestrictedError("Amazon requires sign-in")
    # A CAPTCHA marker means stop collection and report the restriction.
    if "validateCaptcha" in html or 'id="captchacharacters"' in html:
        raise AccessRestrictedError("Amazon returned a CAPTCHA")


# Return bounded HTML using Amazon-only redirects and transient retries, never restriction retries.
async def fetch_page(client: httpx.AsyncClient, url: str, retries: int = 0) -> str:
    # Run the initial attempt plus only the configured number of transient retries.
    for attempt in range(retries + 1):
        # Start each transport attempt from the original product URL.
        current_url = url
        try:
            # Allow only a bounded number of redirects so the request cannot follow an endless chain.
            for _ in range(3):
                # Open a streamed response; async with closes it reliably when the block exits.
                async with client.stream("GET", current_url) as response:
                    # Check the redirect destination before requesting it; redirects are not followed blindly.
                    if response.is_redirect:
                        target = response.url.join(response.headers["location"])
                        if target.scheme != "https" or target.host not in {
                            "amazon.in",
                            "www.amazon.in",
                        }:
                            raise AccessRestrictedError("Amazon redirected outside Amazon India")
                        check_access("", str(target))
                        # Use the approved redirect as the next URL within this bounded loop.
                        current_url = str(target)
                        continue
                    # Convert non-success HTTP responses into exceptions handled by the collector/retry policy.
                    response.raise_for_status()
                    # Collect incoming bytes incrementally so the size limit is enforced during download.
                    content = bytearray()
                    # Wait for each received chunk without loading an unbounded response at once.
                    async for chunk in response.aiter_bytes():
                        content.extend(chunk)
                        # Stop at three million bytes rather than allowing an unexpectedly large document.
                        if len(content) > 3_000_000:
                            raise ValueError("Amazon response exceeded the collection size limit")
                    # Decode the bytes as UTF-8; replace invalid sequences instead of crashing on a malformed character.
                    html = content.decode("utf-8", errors="replace")
                    # A successful HTTP status can still contain a CAPTCHA or login page, so inspect its content too.
                    check_access(html, current_url)
                    return html
            raise ValueError("Amazon returned too many redirects")
        except (httpx.TimeoutException, httpx.NetworkError):
            # Once the configured retry allowance is exhausted, let the caller handle the failure.
            if attempt == retries:
                raise
        # Record the Amazon HTTP status separately from any later OpenAI status.
        except httpx.HTTPStatusError as exc:
            # Retry only the listed temporary server errors; ordinary denials must not trigger another attempt.
            if exc.response.status_code not in {500, 502, 503, 504} or attempt == retries:
                raise
        # Only transport failures and transient server errors receive one bounded retry.
        await asyncio.sleep(0.2)
    raise RuntimeError("Unreachable fetch attempt")


# Legacy 7+7+7 selection; active collection uses select_page_reviews instead.
def balanced_review_sample(reviews: list[Review], limit: int) -> list[Review]:
    unique = deduplicate_reviews(reviews, MAX_REVIEWS * 10)
    # The legacy selector groups low, neutral and high star values before its round-robin selection.
    groups = [
        [review for review in unique if review.stars in stars] for stars in ((1, 2), (3,), (4, 5))
    ]
    for group in groups:
        group.sort(key=lambda review: review.date_iso or "", reverse=True)
    # Seven per category, never fill a missing category with extra reviews from another.
    # Round-robin also preserves comparison when the AI input budget requires a subset.
    return [group[index] for index in range(7) for group in groups if index < len(group)][
        : min(limit, 21)
    ]


# Active entry: one listing fetch -> up to eight unique reviews -> source limitations.
async def collect_reviews(
    product: Product, config: Settings | None = None
) -> tuple[Product, list[Review], list[str]]:
    # The active output cap is eight even if an older environment setting requests more.
    max_reviews = min(config.amazon_max_reviews, 8) if config else 8
    # Use the configured per-request timeout, or the default when no settings object was supplied.
    timeout = config.amazon_timeout_seconds if config else 10
    # Read the bounded transient-retry allowance; this never enables restriction bypasses.
    retries = config.amazon_transient_retries if config else 1
    reviews: list[Review] = []
    # Keep human-readable collection limitations alongside whatever evidence was accessible.
    limitations: list[str] = []
    try:
        # Put a 30-second limit around the entire collection operation, not only one HTTP request.
        async with asyncio.timeout(30):
            # Create an async HTTP client and close its connections when the block exits.
            async with httpx.AsyncClient(
                timeout=timeout,
                follow_redirects=False,
                headers={"User-Agent": "Mozilla/5.0", "Accept-Language": "en-IN,en;q=0.9"},
            ) as client:
                # Wait for the allowed product-page HTML before trying to parse its evidence.
                html = await fetch_page(client, product.url, retries=retries)
                # parse_page updates product in place; we only need its returned review list here.
                _, reviews = parse_page(html, product)
    # Restrictions become evidence limitations, not fake empty reviews or automatic bypass attempts.
    except AccessRestrictedError as exc:
        limitations.append(f"Product page: {exc}.")
    # Record the Amazon HTTP status separately from any later OpenAI status.
    except httpx.HTTPStatusError as exc:
        limitations.append(f"Product page: Amazon returned HTTP {exc.response.status_code}.")
    # Transport, size or timeout failures leave available evidence visible with a collection limitation.
    except (httpx.HTTPError, ValueError, TimeoutError):
        limitations.append("Product page could not be retrieved within collection limits.")
    # Deduplicate and select the final bounded sample using the same logic as model preparation.
    selected = select_page_reviews(reviews, max_reviews)
    # Metadata alone does not prove review access; explain when no review evidence arrived.
    if not selected and not product.customers_say and not product.rating_distribution:
        limitations.append(
            "Amazon did not supply review evidence in the retrieved HTML. "
            "A product title, price or average rating alone does not establish review access; "
            "the browser page may contain more data than the server response."
        )
    # Tell the user explicitly when Amazon did not supply a usable summary field.
    if not product.customers_say:
        limitations.append("Amazon's Customers say summary was unavailable.")
    # Missing histogram buckets must remain unavailable rather than being calculated from the average.
    if len(product.rating_distribution) < 5:
        limitations.append("Some overall rating percentages were unavailable.")
    limitations.append(
        "Evidence is limited to the accessible product page and may include other variants. "
        "Amazon's AI-generated summary/topics are secondary evidence; topic mentions and "
        "written-review samples are not failure rates."
    )
    # Return the observed product, selected direct reviews and honest limitations to the service.
    return product, selected, limitations


# Legacy local browser helper; neither active analysis workflow invokes it.
async def collect_browser_reviews(
    product: Product, urls: list[str]
) -> tuple[str | None, list[Review], list[str]]:
    title = None
    reviews: list[Review] = []
    limitations = []
    # In this inactive legacy helper, start Playwright and clean it up when the block exits.
    async with async_playwright() as playwright:
        # The legacy helper opens an isolated headless Chrome, not the user browser session.
        browser = await playwright.chromium.launch(channel="chrome", headless=True)
        try:
            page = await browser.new_page()

            # This callback checks every browser navigation and blocks destinations outside Amazon India.
            async def restrict_navigation(route):
                target = urlsplit(route.request.url)
                if route.request.is_navigation_request() and (
                    target.scheme != "https"
                    or target.hostname not in {"amazon.in", "www.amazon.in"}
                ):
                    # Cancel an unapproved navigation rather than letting the browser follow it.
                    await route.abort()
                else:
                    # Allow approved requests to proceed.
                    await route.continue_()

            await page.route("**/*", restrict_navigation)
            # The legacy helper visits only the explicit URLs supplied by its caller.
            for url in urls:
                try:
                    await page.goto(url, wait_until="domcontentloaded", timeout=10000)
                    html = await page.content()
                    # A successful HTTP status can still contain a CAPTCHA or login page, so inspect its content too.
                    check_access(html, page.url)
                    parse_metadata(BeautifulSoup(html, "html.parser"), product)
                    await page.locator('[data-hook="review"]').first.wait_for(timeout=1500)
                    html = await page.content()
                    if len(html.encode("utf-8")) > 3_000_000:
                        continue
                    page_title, page_reviews = parse_page(html, product)
                    title = page_title or title
                    reviews.extend(page_reviews)
                # Restrictions become evidence limitations, not fake empty reviews or automatic bypass attempts.
                except AccessRestrictedError as exc:
                    limitations.append(f"Local browser: {exc}.")
                    if "product-reviews" in url:
                        break
                # A browser-specific failure becomes a limitation rather than a fabricated review.
                except BrowserError:
                    limitations.append("A local browser page could not supply written reviews.")
        finally:
            # Always release the legacy browser process, including when a page fails.
            await browser.close()
    return title, deduplicate_reviews(reviews, MAX_REVIEWS), limitations
