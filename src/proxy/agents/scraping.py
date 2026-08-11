"""Base scraping types shared by the agents package.

Deliberately small. The only job here is to give concrete scrapers a common
result shape, a paginate loop, and one careful email extractor, so that a new
site adapter is a `fetch_page` implementation and nothing else.
"""

from __future__ import annotations

import logging
import re
import time
from typing import Any, Iterable

logger = logging.getLogger(__name__)

# Local part, @, domain, TLD of two or more letters. Deliberately not RFC 5322:
# that grammar admits quoted strings and comments no page actually publishes,
# and every extra branch here is another way to match something that is not an
# address.
EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")

# Asset filenames satisfy the pattern above. `logo@2x.png` is a local part, an
# @, and a "TLD" of `png`, so a bare regex pass over any modern page returns a
# pile of sprites. Retina suffixes are the common case; the rest are extensions
# that show up in srcset and CSS url() values.
_ASSET_SUFFIXES = (
    ".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp", ".avif",
    ".css", ".js", ".mjs", ".map", ".woff", ".woff2", ".ttf", ".ico",
)
_RETINA_RE = re.compile(r"@[0-9]+(\.[0-9]+)?x\.", re.IGNORECASE)

# Addresses that are real but never a person: build tooling, placeholder docs,
# and the sentinel domains RFC 2606 reserves for examples.
_JUNK_DOMAINS = ("example.com", "example.org", "example.net", "sentry.io", "localhost")
_JUNK_LOCALPARTS = ("noreply", "no-reply", "donotreply", "do-not-reply")


def looks_like_email(candidate: str) -> bool:
    """True when `candidate` is plausibly a contact address rather than an asset.

    Conservative on purpose: a false positive becomes a bounced send, which is
    worse than a missed address you never knew about.
    """
    value = candidate.strip().lower()
    if not value or value.count("@") != 1:
        return False
    if value.endswith(_ASSET_SUFFIXES) or _RETINA_RE.search(value):
        return False

    local, _, domain = value.partition("@")
    if not local or not domain or ".." in domain or domain.startswith(("-", ".")):
        return False
    if local in _JUNK_LOCALPARTS:
        return False
    return not any(domain == d or domain.endswith("." + d) for d in _JUNK_DOMAINS)


def extract_emails(text: str) -> list[str]:
    """Every plausible address in `text`, lowercased, deduped, in first-seen order.

    Order is stable so a re-run over unchanged input produces an identical file
    and a diff means the source actually changed.
    """
    seen: dict[str, None] = {}
    for match in EMAIL_RE.findall(text or ""):
        value = match.lower()
        if looks_like_email(value):
            seen.setdefault(value, None)
    return list(seen)


class ScrapedItem:
    """One page's worth of extracted data."""

    def __init__(self, content: dict[str, Any], source: str, timestamp: float | None = None):
        self.content = content
        self.source = source
        self.timestamp = time.time() if timestamp is None else timestamp

    def __repr__(self) -> str:
        return f"ScrapedItem(source={self.source!r}, keys={sorted(self.content)})"


class Scraper:
    """Base for site scrapers: pagination, rate limiting, and a stop condition.

    Subclasses implement `scrape(page)`. Everything else is here so adapters
    cannot each invent their own throttle.
    """

    def __init__(
        self,
        name: str = "scraper",
        domain: str | None = None,
        user_agent: str | None = None,
        rate_limit_seconds: float = 2.0,
        sleep: Any = time.sleep,
    ):
        self.name = name
        self.domain = domain
        self.user_agent = user_agent or "backdoor-hypercrawl/1.0"
        self.rate_limit_seconds = rate_limit_seconds
        self._sleep = sleep
        self._last_request_at: float | None = None

    def scrape(self, page: int = 1) -> ScrapedItem | None:
        raise NotImplementedError("Subclasses must implement scrape(page)")

    def throttle(self, now: Any = time.monotonic) -> None:
        """Block until `rate_limit_seconds` have passed since the last request.

        Sleeps the remaining time rather than the full interval, so a slow page
        that already covered the gap costs nothing extra.
        """
        current = now()
        if self._last_request_at is not None:
            elapsed = current - self._last_request_at
            remaining = self.rate_limit_seconds - elapsed
            if remaining > 0:
                self._sleep(remaining)
                current = now()
        self._last_request_at = current

    def scrape_all_pages(self, max_pages: int = 5) -> list[ScrapedItem]:
        """Walk pages 1..max_pages, stopping at the first that yields nothing.

        The page number is passed through. An earlier version called `scrape()`
        with no argument in a loop, which fetched page one `max_pages` times and
        reported the same addresses as five pages of results.
        """
        results: list[ScrapedItem] = []
        for page in range(1, max_pages + 1):
            try:
                item = self.scrape(page)
            except Exception:
                logger.exception("%s: page %d failed; stopping", self.name, page)
                break
            if item is None:
                logger.info("%s: page %d returned nothing; stopping", self.name, page)
                break
            results.append(item)
            logger.info("%s: page %d ok", self.name, page)
        return results


def merge_emails(items: Iterable[ScrapedItem]) -> list[str]:
    """Flatten `emails` across items, deduped, preserving first-seen order."""
    seen: dict[str, None] = {}
    for item in items:
        for email in item.content.get("emails", []):
            seen.setdefault(email.lower(), None)
    return list(seen)
