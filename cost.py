#!/usr/bin/env python3
"""
cost.py — Parse proxy.log and print a cost report.

Usage:
    python3 cost.py
    python3 cost.py --log /path/to/proxy.log
"""

import re
import argparse
from collections import defaultdict
from datetime import datetime
from pathlib import Path

# Output token pricing (per 1M tokens)
OUTPUT_PRICING = {
    "deepseek-chat":                    1.10,
    "deepseek-reasoner":               16.00,
    "llama-3.3-70b-versatile":          0.79,  # groq
    "meta/llama-3.3-70b-instruct":      0.42,  # nvidia
    "claude-opus-4-7":                 75.00,
}
OPUS_PRICE = 75.00   # Claude Opus 4.7 output $/1M — reference
FALLBACK_PRICE = 1.00

# ANSI colours
GREEN  = "\033[32m"
BOLD   = "\033[1m"
DIM    = "\033[2m"
CYAN   = "\033[36m"
YELLOW = "\033[33m"
RESET  = "\033[0m"

# Log line patterns
# → deepseek-chat [complete] tools=0 | 'preview'
RE_REQUEST = re.compile(
    r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}),\d+ INFO .+: → (\S+) \[(complete|stream)\]"
)
# ← deepseek-chat [complete] stop=end_turn out_tokens=6
RE_COMPLETE_RESP = re.compile(
    r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}),\d+ INFO .+: ← (\S+) \[complete\].*out_tokens=(\d+)(?:.*in_tokens=(\d+))?"
)
# ← deepseek-chat [stream] done  (optionally: in_tokens=123)
RE_STREAM_RESP = re.compile(
    r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}),\d+ INFO .+: ← (\S+) \[stream\] done(?:.*in_tokens=(\d+))?"
)


def parse_log(log_path: Path) -> dict:
    stats: dict = {
        "first_ts": None,
        "last_ts":  None,
        "models":   defaultdict(lambda: {
            "complete": 0, "stream": 0,
            "out_tokens": 0, "in_tokens": 0,
        }),
    }

    def update_ts(ts_str: str):
        ts = datetime.strptime(ts_str, "%Y-%m-%d %H:%M:%S")
        if stats["first_ts"] is None or ts < stats["first_ts"]:
            stats["first_ts"] = ts
        if stats["last_ts"] is None or ts > stats["last_ts"]:
            stats["last_ts"] = ts

    with open(log_path) as f:
        for line in f:
            line = line.rstrip()

            m = RE_COMPLETE_RESP.match(line)
            if m:
                ts_str, model, out_tok, in_tok = m.groups()
                update_ts(ts_str)
                stats["models"][model]["complete"] += 1
                stats["models"][model]["out_tokens"] += int(out_tok)
                if in_tok:
                    stats["models"][model]["in_tokens"] += int(in_tok)
                continue

            m = RE_STREAM_RESP.match(line)
            if m:
                ts_str, model, in_tok = m.groups()
                update_ts(ts_str)
                stats["models"][model]["stream"] += 1
                if in_tok:
                    stats["models"][model]["in_tokens"] += int(in_tok)
                continue

            # Capture timestamps from any INFO line for date range
            m = RE_REQUEST.match(line)
            if m:
                update_ts(m.group(1))

    return stats


def price_for(model: str) -> float:
    for key, price in OUTPUT_PRICING.items():
        if key in model:
            return price
    return FALLBACK_PRICE


def fmt_cost(dollars: float) -> str:
    if dollars < 0.0001:
        return f"${dollars * 1_000_000:.4f} (micro-dollars)"
    if dollars < 0.01:
        return f"${dollars:.6f}"
    return f"${dollars:.4f}"


def main():
    parser = argparse.ArgumentParser(description="Backdoor proxy cost report")
    parser.add_argument("--log", default="proxy.log", help="Path to proxy.log")
    args = parser.parse_args()

    log_path = Path(args.log)
    if not log_path.exists():
        print(f"{YELLOW}Log file not found: {log_path}{RESET}")
        return

    stats = parse_log(log_path)
    models = stats["models"]

    if not models:
        print(f"{YELLOW}No request/response lines found in {log_path}{RESET}")
        return

    total_complete = sum(v["complete"] for v in models.values())
    total_stream   = sum(v["stream"]   for v in models.values())
    total_requests = total_complete + total_stream
    total_out_tok  = sum(v["out_tokens"] for v in models.values())
    total_in_tok   = sum(v["in_tokens"]  for v in models.values())
    has_in_tokens  = total_in_tok > 0

    actual_cost = sum(
        v["out_tokens"] / 1_000_000 * price_for(model)
        for model, v in models.items()
    )
    opus_cost = total_out_tok / 1_000_000 * OPUS_PRICE
    multiplier = (opus_cost / actual_cost) if actual_cost > 0 else float("inf")

    date_range = "unknown"
    if stats["first_ts"] and stats["last_ts"]:
        if stats["first_ts"].date() == stats["last_ts"].date():
            date_range = stats["first_ts"].strftime("%Y-%m-%d")
        else:
            date_range = (f"{stats['first_ts'].strftime('%Y-%m-%d')} → "
                          f"{stats['last_ts'].strftime('%Y-%m-%d')}")

    G, B, D, C, Y, R = GREEN, BOLD, DIM, CYAN, YELLOW, RESET

    print()
    print(f"{G}{B}╔══════════════════════════════════════════╗{R}")
    print(f"{G}{B}║        BACKDOOR  —  COST  REPORT         ║{R}")
    print(f"{G}{B}╚══════════════════════════════════════════╝{R}")
    print(f"  {D}Date range:{R}  {date_range}")
    print()
    print(f"  {B}Requests{R}")
    print(f"    Complete  {C}{total_complete:>6}{R}")
    print(f"    Stream    {C}{total_stream:>6}{R}")
    print(f"    Total     {C}{total_requests:>6}{R}")
    print()
    print(f"  {B}Per-model breakdown{R}")
    for model, v in sorted(models.items()):
        rate = price_for(model)
        cost = v["out_tokens"] / 1_000_000 * rate
        print(f"    {G}{model}{R}")
        print(f"      requests: {v['complete']} complete + {v['stream']} stream  "
              f"| out_tokens: {v['out_tokens']:,}  | est. cost: {fmt_cost(cost)}")
    print()
    print(f"  {B}Tokens tracked{R}")
    print(f"    Output tokens  {C}{total_out_tok:>10,}{R}")
    if has_in_tokens:
        print(f"    Input tokens   {C}{total_in_tok:>10,}{R}")
    print()
    print(f"  {B}Cost estimate  {D}(output tokens only){R}")
    print(f"    Current provider   {G}{B}{fmt_cost(actual_cost)}{R}")
    print(f"    Claude Opus 4.7    {Y}{fmt_cost(opus_cost)}{R}")
    if actual_cost > 0:
        print(f"    Savings            {G}{B}{multiplier:.0f}x cheaper{R}  than Opus 4.7")
    print()
    print(f"  {D}⚠  Input tokens not yet fully tracked in logs (stream requests).{R}")
    if not has_in_tokens:
        print(f"  {D}   Costs shown are output-only — true spend is higher.{R}")
    print()


if __name__ == "__main__":
    main()
