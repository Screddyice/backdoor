#!/usr/bin/env python3
"""CLI for the community-site scraper.

Deliberately a separate entry point. `run.sh` starts the proxy and launches
Claude Code, which is what the README sells and what every existing user runs;
folding a scraper into it would mean `./run.sh` no longer starts Backdoor.

    ./scripts/hypercrawl.py --domain lu.ma --max-pages 3
    COMMUNITY_SITE=lu.ma ./scripts/hypercrawl.py

Flags beat environment variables, which beat the defaults.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.proxy.agents.hypercrawl import CommunityScraper  # noqa: E402
from src.proxy.agents.scraping import merge_emails  # noqa: E402

logger = logging.getLogger("hypercrawl")


def _env_flag(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    return default if raw is None else raw.strip().lower() in {"1", "true", "yes", "on"}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="hypercrawl", description="Collect public contact addresses from a community site."
    )
    parser.add_argument(
        "--domain", default=os.environ.get("COMMUNITY_SITE"),
        help="Target domain or URL. Defaults to $COMMUNITY_SITE.",
    )
    parser.add_argument(
        "--max-pages", type=int, default=int(os.environ.get("HYPERCRAWL_MAX_PAGES", "5")),
        help="Stop after this many pages (default 5). Pagination also halts on the first empty page.",
    )
    parser.add_argument(
        "--rate-limit", type=float, default=float(os.environ.get("HYPERCRAWL_RATE_LIMIT", "2.0")),
        help="Minimum seconds between requests (default 2.0).",
    )
    parser.add_argument(
        "--output", default=os.environ.get("HYPERCRAWL_OUTPUT"),
        help="Where to write addresses. Defaults to ./out/community_emails_<timestamp>.txt",
    )
    parser.add_argument(
        "--ignore-robots", action="store_true", default=_env_flag("HYPERCRAWL_IGNORE_ROBOTS"),
        help="Skip the robots.txt check. Off by default, and worth leaving off.",
    )
    parser.add_argument("--log-level", default=os.environ.get("LOG_LEVEL", "INFO"))
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, str(args.log_level).upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    if not args.domain:
        # A default target would mean an accidental run hits somebody's site.
        print("error: no target. Pass --domain or set COMMUNITY_SITE.", file=sys.stderr)
        return 2

    if args.ignore_robots:
        logger.warning("robots.txt checking is DISABLED for this run")

    with CommunityScraper(
        domain=args.domain,
        rate_limit_seconds=args.rate_limit,
        respect_robots=not args.ignore_robots,
    ) as scraper:
        logger.info("target %s, up to %d page(s)", scraper.site.base_url, args.max_pages)
        items = scraper.scrape_all_pages(max_pages=args.max_pages)

    emails = merge_emails(items)
    if not emails:
        logger.warning("no addresses found across %d page(s)", len(items))
        return 1

    if args.output:
        out = Path(args.output)
    else:
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        out = Path("out") / f"community_emails_{stamp}.txt"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(emails) + "\n")

    logger.info("%d address(es) from %d page(s) -> %s", len(emails), len(items), out)
    print(out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
