"""Scrape contact addresses from the public pages of a community site.

Public pages only. There is no login, no cookie jar, and no attempt to reach
anything a signed-out visitor cannot already see, because the moment a scraper
needs credentials it stops being a crawler and starts being an account.

Two guards are on by default and both cost you results: `robots.txt` is honoured
and requests are spaced by `rate_limit_seconds`. Leave them on.
"""

from __future__ import annotations

import json
import logging
import re
import urllib.robotparser
from dataclasses import dataclass
from typing import Any
from urllib.parse import urljoin, urlparse

import httpx

from .scraping import ScrapedItem, Scraper, extract_emails, looks_like_email

logger = logging.getLogger(__name__)

# `mailto:` is the high-confidence signal: a page that marks up an address as a
# link is telling you it is one, so these are collected separately from the
# regex sweep and never filtered as assets.
_MAILTO_RE = re.compile(r"""mailto:([^"'?>\s]+)""", re.IGNORECASE)

# Blobs that hydrate a client-side app. Addresses often live here and nowhere in
# the rendered HTML, which is why a text-only pass under-collects.
_JSON_BLOB_RE = re.compile(
    r"<script[^>]+type=[\"']application/(?:ld\+)?json[\"'][^>]*>(.*?)</script>",
    re.IGNORECASE | re.DOTALL,
)


@dataclass(frozen=True)
class CommunitySite:
    """A crawl target: where it lives and how its listing paginates."""

    domain: str
    path: str = "/"
    page_param: str = "page"
    scheme: str = "https"

    @classmethod
    def from_domain(cls, domain: str, **kwargs: Any) -> "CommunitySite":
        """Build from a bare domain or a full URL.

        Accepts either, because operators pass both and a scraper that only
        takes one shape gets a hostname with `https://` glued to the front.
        """
        raw = (domain or "").strip()
        if not raw:
            raise ValueError("domain is required")
        parsed = urlparse(raw if "//" in raw else f"//{raw}", scheme=cls.scheme)
        host = parsed.netloc or parsed.path.split("/")[0]
        if not host:
            raise ValueError(f"could not parse a host out of {domain!r}")
        path = parsed.path if parsed.netloc and parsed.path else kwargs.pop("path", "/")
        return cls(domain=host, path=path or "/", scheme=parsed.scheme or cls.scheme, **kwargs)

    @property
    def base_url(self) -> str:
        return f"{self.scheme}://{self.domain}"

    def url_for_page(self, page: int) -> str:
        """Page one is the bare URL; later pages carry `page_param`.

        Requesting `?page=1` on a site that does not expect it is a good way to
        get a redirect or a 404 on the one page that was guaranteed to work.
        """
        url = urljoin(self.base_url, self.path)
        return url if page <= 1 else f"{url}{'&' if '?' in url else '?'}{self.page_param}={page}"


class CommunityScraper(Scraper):
    """Collect addresses from a community site's public listing pages."""

    def __init__(
        self,
        domain: str,
        name: str = "hypercrawl",
        rate_limit_seconds: float = 2.0,
        respect_robots: bool = True,
        timeout: float = 20.0,
        client: httpx.Client | None = None,
        **kwargs: Any,
    ):
        site = CommunitySite.from_domain(domain)
        super().__init__(
            name=name, domain=site.domain, rate_limit_seconds=rate_limit_seconds, **kwargs
        )
        self.site = site
        self.respect_robots = respect_robots
        self._owns_client = client is None
        self._client = client or httpx.Client(
            timeout=timeout, follow_redirects=True, headers={"user-agent": self.user_agent}
        )
        self._robots: urllib.robotparser.RobotFileParser | None = None
        self._robots_loaded = False

    # -- robots -----------------------------------------------------------

    def _load_robots(self) -> None:
        """Fetch and parse robots.txt once.

        A missing or unreadable robots.txt means no stated restriction, which is
        allow. A 5xx is treated the same way rather than as a silent full stop,
        so one flaky response does not look like "this site is entirely closed."
        """
        self._robots_loaded = True
        parser = urllib.robotparser.RobotFileParser()
        url = urljoin(self.site.base_url, "/robots.txt")
        try:
            response = self._client.get(url)
        except Exception:
            logger.warning("%s: robots.txt unreachable at %s; proceeding", self.name, url)
            return
        if response.status_code >= 400:
            logger.info("%s: no robots.txt (%d); proceeding", self.name, response.status_code)
            return
        parser.parse(response.text.splitlines())
        self._robots = parser

    def allowed(self, url: str) -> bool:
        if not self.respect_robots:
            return True
        if not self._robots_loaded:
            self._load_robots()
        return True if self._robots is None else self._robots.can_fetch(self.user_agent, url)

    # -- extraction -------------------------------------------------------

    @staticmethod
    def _emails_from_json(html: str) -> list[str]:
        """Addresses inside embedded JSON blobs, walked recursively."""
        found: list[str] = []

        def walk(node: Any) -> None:
            if isinstance(node, dict):
                for value in node.values():
                    walk(value)
            elif isinstance(node, list):
                for value in node:
                    walk(value)
            elif isinstance(node, str) and "@" in node:
                found.extend(extract_emails(node))

        for blob in _JSON_BLOB_RE.findall(html):
            try:
                walk(json.loads(blob))
            except (ValueError, TypeError):
                continue
        return found

    def extract(self, html: str) -> list[str]:
        """Union of the three strategies, deduped, in first-seen order.

        `mailto:` first because it is the most reliable, so when the same
        address appears twice the ordering reflects the stronger signal.
        """
        ordered: dict[str, None] = {}
        for raw in _MAILTO_RE.findall(html or ""):
            value = raw.strip().lower()
            if looks_like_email(value):
                ordered.setdefault(value, None)
        for value in self._emails_from_json(html or ""):
            ordered.setdefault(value, None)
        for value in extract_emails(html or ""):
            ordered.setdefault(value, None)
        return list(ordered)

    # -- fetch ------------------------------------------------------------

    def scrape(self, page: int = 1) -> ScrapedItem | None:
        """Fetch one listing page and return its addresses.

        Returns None for "stop paginating": robots disallowed it, the request
        failed, the page 404'd, or it held no addresses. `scrape_all_pages`
        halts on the first None.
        """
        url = self.site.url_for_page(page)

        if not self.allowed(url):
            logger.warning("%s: robots.txt disallows %s", self.name, url)
            return None

        self.throttle()
        try:
            response = self._client.get(url)
        except Exception:
            logger.exception("%s: request failed for %s", self.name, url)
            return None

        if response.status_code >= 400:
            logger.info("%s: %s returned %d", self.name, url, response.status_code)
            return None

        emails = self.extract(response.text)
        if not emails:
            logger.info("%s: no addresses on %s", self.name, url)
            return None

        logger.info("%s: %d address(es) on %s", self.name, len(emails), url)
        return ScrapedItem(
            content={"emails": emails, "page": page, "count": len(emails)}, source=url
        )

    # -- lifecycle --------------------------------------------------------

    def close(self) -> None:
        """Close the HTTP client, but only if this scraper created it."""
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> "CommunityScraper":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()
