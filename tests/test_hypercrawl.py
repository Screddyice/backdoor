"""Tests for the community-site scraper.

Weighted toward the two things that actually go wrong in a scraper: collecting
garbage that looks like an address, and pagination that silently refetches page
one. Both are quiet failures — you get a plausible file either way — so they are
asserted directly rather than inferred from a happy-path run.
"""

import httpx
import pytest

from src.proxy.agents.hypercrawl import CommunityScraper, CommunitySite
from src.proxy.agents.scraping import (
    Scraper,
    extract_emails,
    looks_like_email,
    merge_emails,
)

# --- address recognition -------------------------------------------------


@pytest.mark.parametrize("value", [
    "ada@example.io",
    "ada.lovelace+events@sub.domain.co.uk",
    "a1@b2.dev",
])
def test_accepts_real_addresses(value):
    assert looks_like_email(value)


@pytest.mark.parametrize("value", [
    "logo@2x.png",            # the retina sprite that started this
    "hero@3x.jpg",
    "bundle@1.5x.webp",
    "sprite@2x.svg",
    "styles@2x.css",
])
def test_rejects_retina_assets(value):
    """A bare regex matches these: local part, @, and a 3-letter "TLD"."""
    assert not looks_like_email(value)


@pytest.mark.parametrize("value", [
    "noreply@corp.com",       # real, but never a person
    "someone@example.com",    # RFC 2606 placeholder
    "abc123@sentry.io",       # build tooling
    "not-an-address",
    "two@@ats.com",
    "",
])
def test_rejects_junk(value):
    assert not looks_like_email(value)


def test_extract_dedupes_lowercases_and_keeps_order():
    text = "B@x.com then A@x.com then b@X.COM again"
    assert extract_emails(text) == ["b@x.com", "a@x.com"]


def test_extract_drops_assets_from_real_markup():
    html = '<img srcset="/logo@2x.png"> contact <a>ada@example.io</a>'
    assert extract_emails(html) == ["ada@example.io"]


# --- URL construction ----------------------------------------------------


def test_page_one_has_no_page_param():
    """Sending ?page=1 to a site that does not expect it invites a 404."""
    site = CommunitySite(domain="lu.ma")
    assert site.url_for_page(1) == "https://lu.ma/"
    assert "page=" not in site.url_for_page(1)


def test_later_pages_carry_the_param():
    assert CommunitySite(domain="lu.ma").url_for_page(3) == "https://lu.ma/?page=3"


def test_existing_query_string_gets_an_ampersand():
    site = CommunitySite(domain="lu.ma", path="/members?sort=new")
    assert site.url_for_page(2) == "https://lu.ma/members?sort=new&page=2"


@pytest.mark.parametrize("given,expected", [
    ("lu.ma", "https://lu.ma"),
    ("https://lu.ma", "https://lu.ma"),
    ("http://lu.ma/members", "http://lu.ma"),
])
def test_from_domain_accepts_bare_hosts_and_full_urls(given, expected):
    assert CommunitySite.from_domain(given).base_url == expected


def test_from_domain_rejects_empty():
    with pytest.raises(ValueError):
        CommunitySite.from_domain("")


# --- extraction strategies ----------------------------------------------


def _scraper(handler, **kwargs):
    client = httpx.Client(transport=httpx.MockTransport(handler))
    kwargs.setdefault("respect_robots", False)
    kwargs.setdefault("sleep", lambda _s: None)
    return CommunityScraper(domain="lu.ma", client=client, **kwargs)


def test_extract_finds_mailto_json_and_plain_text():
    html = """
      <a href="mailto:link@example.io">write</a>
      <script type="application/ld+json">{"organizer":{"email":"json@example.io"}}</script>
      <p>plain@example.io</p>
      <img src="/logo@2x.png">
    """
    found = _scraper(lambda r: httpx.Response(200)).extract(html)
    assert found == ["link@example.io", "json@example.io", "plain@example.io"]


def test_mailto_wins_ordering_over_the_regex_sweep():
    """Same address twice should rank by the stronger signal, not by position."""
    html = '<p>later@example.io</p><a href="mailto:later@example.io">x</a>'
    assert _scraper(lambda r: httpx.Response(200)).extract(html) == ["later@example.io"]


def test_malformed_json_blob_does_not_break_extraction():
    html = '<script type="application/json">{not json</script><p>ok@example.io</p>'
    assert _scraper(lambda r: httpx.Response(200)).extract(html) == ["ok@example.io"]


# --- pagination ----------------------------------------------------------


def test_each_page_is_fetched_once_with_its_own_number():
    """The regression guard. `scrape_all_pages` used to call `scrape()` with no
    argument in a loop, fetching page one N times and reporting the duplicate
    result as N pages of data."""
    seen = []

    def handler(request):
        seen.append(str(request.url))
        page = request.url.params.get("page", "1")
        return httpx.Response(200, text=f"user{page}@example.io")

    items = _scraper(handler).scrape_all_pages(max_pages=3)

    assert len(seen) == len(set(seen)) == 3, seen
    assert merge_emails(items) == ["user1@example.io", "user2@example.io", "user3@example.io"]


def test_pagination_stops_at_the_first_empty_page():
    def handler(request):
        page = request.url.params.get("page", "1")
        return httpx.Response(200, text="a@example.io" if page == "1" else "<p>nothing</p>")

    items = _scraper(handler).scrape_all_pages(max_pages=5)
    assert len(items) == 1


def test_http_error_stops_pagination_without_raising():
    items = _scraper(lambda r: httpx.Response(404)).scrape_all_pages(max_pages=3)
    assert items == []


def test_transport_failure_is_caught():
    def handler(request):
        raise httpx.ConnectError("network down")

    assert _scraper(handler).scrape(1) is None


# --- guards --------------------------------------------------------------


def test_robots_disallow_blocks_the_fetch():
    fetched = []

    def handler(request):
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nDisallow: /")
        fetched.append(str(request.url))
        return httpx.Response(200, text="ada@example.io")

    scraper = _scraper(handler, respect_robots=True)
    assert scraper.scrape(1) is None
    assert fetched == [], "fetched a page robots.txt disallowed"


def test_missing_robots_is_treated_as_allowed():
    def handler(request):
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        return httpx.Response(200, text="ada@example.io")

    assert _scraper(handler, respect_robots=True).scrape(1) is not None


def test_robots_is_fetched_once_across_pages():
    calls = []

    def handler(request):
        calls.append(request.url.path)
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nAllow: /")
        return httpx.Response(200, text="a@example.io")

    _scraper(handler, respect_robots=True).scrape_all_pages(max_pages=3)
    assert calls.count("/robots.txt") == 1, calls


def test_throttle_sleeps_only_the_remaining_interval():
    """A page that already took longer than the interval should cost nothing."""
    slept = []
    clock = iter([0.0, 0.5, 0.5, 10.0, 10.0])
    scraper = Scraper(rate_limit_seconds=2.0, sleep=slept.append)

    scraper.throttle(now=lambda: next(clock))   # first call: no wait
    scraper.throttle(now=lambda: next(clock))   # 0.5s elapsed -> sleep 1.5
    scraper.throttle(now=lambda: next(clock))   # 9.5s elapsed -> no sleep

    assert slept == [1.5]
