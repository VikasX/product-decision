import hashlib
import os
import re
from urllib.parse import urlsplit

import httpx
from bs4 import BeautifulSoup
from playwright.async_api import Error as BrowserError
from playwright.async_api import async_playwright

from app.schemas.analysis import Product, Review

MAX_REVIEWS = 30


def parse_page(html: str, product: Product) -> tuple[str | None, list[Review]]:
    soup = BeautifulSoup(html, "html.parser")
    title_element = soup.select_one("#productTitle")
    title = title_element.get_text(" ", strip=True)[:500] if title_element else None
    reviews = []
    for element in soup.select('[data-hook="review"]')[:MAX_REVIEWS]:
        body = element.select_one(
            '[data-hook="review-body"], [data-hook="reviewRichContentContainer"]'
        )
        if body is None:
            continue
        text = body.get_text(" ", strip=True)[:2000]
        if not text:
            continue
        review_id = element.get("id")
        if not isinstance(review_id, str) or not re.fullmatch(r"R[A-Z0-9]+", review_id):
            review_id = hashlib.sha256(text.encode()).hexdigest()[:16]
        rating = element.select_one(
            '[data-hook="review-star-rating"], [data-hook="cmps-review-star-rating"]'
        )
        stars_match = re.search(r"[1-5](?:\.\d)?", rating.get_text()) if rating else None
        date = element.select_one('[data-hook="review-date"]')
        variant = element.select_one('[data-hook="format-strip"]')
        reviews.append(
            Review(
                id=review_id,
                text=text,
                stars=float(stars_match.group()) if stars_match else None,
                date=date.get_text(" ", strip=True) if date else None,
                variant=variant.get_text(" ", strip=True) if variant else None,
                source_url=f"https://www.amazon.in/gp/customer-reviews/{review_id}"
                if review_id.startswith("R")
                else product.url,
            )
        )
    return title, reviews


async def fetch_page(client: httpx.AsyncClient, url: str) -> str:
    # All URLs are constructed here. Redirects are explicitly restricted to Amazon India.
    for _ in range(3):
        async with client.stream("GET", url) as response:
            if response.is_redirect:
                target = response.url.join(response.headers["location"])
                if target.scheme != "https" or target.host not in {"amazon.in", "www.amazon.in"}:
                    raise ValueError("Amazon redirected outside the supported marketplace")
                if "/ap/signin" in target.path:
                    raise ValueError("Amazon requires sign-in to access these reviews")
                url = str(target)
                continue
            response.raise_for_status()
            content = bytearray()
            async for chunk in response.aiter_bytes():
                content.extend(chunk)
                if len(content) > 3_000_000:
                    raise ValueError("Amazon response exceeded the collection size limit")
            html = content.decode("utf-8", errors="replace")
            if "validateCaptcha" in html or 'id="captchacharacters"' in html:
                raise ValueError("Amazon returned a CAPTCHA to the backend")
            return html
    raise ValueError("Amazon returned too many redirects")


async def collect_reviews(product: Product) -> tuple[Product, list[Review], list[str]]:
    urls = [
        product.url,
        f"https://www.amazon.in/product-reviews/{product.asin}/?sortBy=recent",
        f"https://www.amazon.in/product-reviews/{product.asin}/?filterByStar=critical",
    ]
    reviews: dict[str, Review] = {}
    seen_text: set[str] = set()
    limitations = []
    async with httpx.AsyncClient(
        timeout=12,
        follow_redirects=False,
        headers={"User-Agent": "Mozilla/5.0", "Accept-Language": "en-IN,en;q=0.9"},
    ) as client:
        for label, url in zip(("listing", "recent reviews", "critical reviews"), urls, strict=True):
            try:
                html = await fetch_page(client, url)
                title, page_reviews = parse_page(html, product)
                if title:
                    product.title = title
                for review in page_reviews:
                    normalized = " ".join(review.text.casefold().split())
                    if normalized not in seen_text and len(reviews) < MAX_REVIEWS:
                        reviews[review.id] = review
                        seen_text.add(normalized)
            except (httpx.HTTPError, ValueError) as exc:
                message = str(exc) if isinstance(exc, ValueError) else "Amazon request failed"
                message = f"{label.capitalize()}: {message}."
                if message not in limitations:
                    limitations.append(message)
    if not reviews and os.getenv("VERCEL") == "1":
        limitations.append(
            "Browser collection is unavailable on this deployment. "
            "Amazon did not provide accessible written reviews."
        )
    elif not reviews:
        try:
            browser_title, browser_reviews, browser_limits = await collect_browser_reviews(
                product, urls
            )
            if browser_title:
                product.title = browser_title
            reviews.update({review.id: review for review in browser_reviews})
            if browser_reviews:
                limitations = ["Direct HTTP collection failed; used a local Chrome browser."]
            limitations.extend(browser_limits)
        except BrowserError:
            limitations.append("Browser collection unavailable; install Chrome and Playwright.")
    limitations.append(
        "Collection covers accessible listing, recent and critical review pages only. "
        "It may include other variants and is not a representative sample of all buyers."
    )
    return product, list(reviews.values()), limitations


async def collect_browser_reviews(
    product: Product, urls: list[str]
) -> tuple[str | None, list[Review], list[str]]:
    title = None
    reviews: dict[str, Review] = {}
    seen_text: set[str] = set()
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
                label = "listing"
                if "sortBy=recent" in url:
                    label = "recent review"
                elif "filterByStar=critical" in url:
                    label = "critical review"
                try:
                    await page.goto(url, wait_until="domcontentloaded", timeout=18000)
                    if "/ap/signin" in page.url:
                        limitations.append(f"Amazon requires sign-in for the {label} page.")
                        continue
                    initial_html = await page.content()
                    if (
                        "validateCaptcha" in initial_html
                        or 'id="captchacharacters"' in initial_html
                    ):
                        limitations.append(f"Amazon returned a CAPTCHA for the {label} page.")
                        continue
                    await page.locator('[data-hook="review"]').first.wait_for(timeout=3000)
                    html = await page.content()
                    if len(html) > 3_000_000:
                        continue
                    page_title, page_reviews = parse_page(html, product)
                    title = page_title or title
                    for review in page_reviews:
                        normalized = " ".join(review.text.casefold().split())
                        if normalized not in seen_text and len(reviews) < MAX_REVIEWS:
                            reviews[review.id] = review
                            seen_text.add(normalized)
                except BrowserError:
                    limitations.append(f"The {label} page could not supply written reviews.")
                    continue
        finally:
            await browser.close()
    return title, list(reviews.values()), limitations
