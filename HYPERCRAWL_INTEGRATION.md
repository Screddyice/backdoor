# Hypercrawl

Collects publicly listed contact addresses from a community site and writes them to a file.

## Scope

Public pages only. There is no login, no cookie jar, and no attempt to reach anything a signed-out visitor cannot already see. A scraper that needs credentials is not a crawler, it is an account, and that is a different tool with different consequences.

Two guards ship on by default and both cost you results:

- **robots.txt is honoured.** A disallowed path stops the crawl. `--ignore-robots` exists; leaving it alone is the right call.
- **Requests are spaced.** Two seconds between them by default, tunable with `--rate-limit`.

## Layout

```
src/proxy/agents/
├── __init__.py       exports
├── scraping.py       ScrapedItem, Scraper, address recognition
└── hypercrawl.py     CommunitySite, CommunityScraper
scripts/hypercrawl.py the CLI
tests/test_hypercrawl.py
```

`scripts/hypercrawl.py` is a separate entry point on purpose. `run.sh` starts the proxy and launches Claude Code, which is what the README documents and what existing users run. Folding a scraper into it would mean `./run.sh` no longer starts Backdoor.

## Use

```bash
./scripts/hypercrawl.py --domain lu.ma --max-pages 3
COMMUNITY_SITE=lu.ma ./scripts/hypercrawl.py
```

Flags beat environment variables, which beat defaults. `COMMUNITY_SITE` has no default, so an accidental run hits nothing rather than somebody's site.

| Flag | Env | Default |
|---|---|---|
| `--domain` | `COMMUNITY_SITE` | none, required |
| `--max-pages` | `HYPERCRAWL_MAX_PAGES` | 5 |
| `--rate-limit` | `HYPERCRAWL_RATE_LIMIT` | 2.0 |
| `--output` | `HYPERCRAWL_OUTPUT` | `out/community_emails_<timestamp>.txt` |
| `--ignore-robots` | `HYPERCRAWL_IGNORE_ROBOTS` | off |

Exit codes: `0` wrote addresses, `1` found none, `2` no target given.

## As a module

```python
from src.proxy.agents.hypercrawl import CommunityScraper
from src.proxy.agents.scraping import merge_emails

with CommunityScraper(domain="lu.ma") as scraper:
    items = scraper.scrape_all_pages(max_pages=3)

emails = merge_emails(items)
```

`CommunityScraper` accepts an `httpx.Client`, which is how the tests drive it through `MockTransport` without touching the network.

## Extraction

Three passes, unioned and deduped, first-seen order preserved:

1. **`mailto:` links.** A page that marks an address up as a link is telling you it is one, so these rank first and skip the asset filter.
2. **Embedded JSON.** `application/json` and `application/ld+json` blocks, walked recursively. Client-rendered sites often carry addresses here and nowhere in the visible HTML.
3. **Regex over the body.**

### Why the filter exists

The obvious pattern (local part, `@`, domain, two-letter-plus TLD) matches `logo@2x.png`. So does `hero@3x.jpg` and every retina sprite in a modern `srcset`. Run a bare regex over a real page and a good fraction of what comes back is images.

`looks_like_email` drops asset extensions, retina suffixes, RFC 2606 placeholder domains, `noreply` local parts, and Sentry DSN fragments. It errs toward rejecting, because a false positive becomes a bounced send and a missed address is one you never knew about.

## Pagination

Page one is the bare URL. Later pages append `?page=N`, or `&page=N` when the path already carries a query string. Requesting `?page=1` on a site that does not expect it is a good way to 404 the one page guaranteed to work.

The walk stops at the first page that yields no addresses, errors, or 404s, so `--max-pages` is a ceiling rather than a target.

## Tests

```bash
.venv/bin/python -m pytest tests/test_hypercrawl.py -q
```

Weighted toward the two failures that stay quiet: collecting garbage that looks like an address, and pagination that refetches page one. An earlier version of `scrape_all_pages` called `scrape()` with no argument in a loop, so five pages of "results" were five copies of page one. `test_each_page_is_fetched_once_with_its_own_number` fails if that returns.
