#!/usr/bin/env python3
"""Weekly savings report: what the local-router + llm-jury + caching stack saved.

Measures, from ~/.claude/projects transcript JSONLs (the authoritative per-turn
usage record), over the trailing window:

  local routing   turns served by qwen* through the :8083 router — zero cloud
                  tokens; counterfactual is the same turn at Opus 5 pricing.
  prompt caching  cache_read tokens billed at 0.1x input, net of the 1.25x/2x
                  write premium actually paid.
  llm-jury        solve runs in the window (from ~/.llmjury/tmp mtimes);
                  counterfactual is a configurable Opus-token estimate per run,
                  since the ladder keeps escalations off Opus. Estimate only.
  time            per-session active hours (gaps <= ACTIVE_GAP_MIN count as
                  work), summed across sessions, vs the wall-clock union.

Every number that rests on an assumption says so in the report. A week with no
usage reports zeros — silence is indistinguishable from a broken job.

Usage: claude-savings-report.py [--days N] [--dry-run] [--no-notify]
"""
import glob, json, os, subprocess, sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone

HOME          = os.path.expanduser("~")
PROJECTS_DIR  = os.path.join(HOME, ".claude", "projects")
JURY_TMP      = os.path.join(HOME, ".llmjury", "tmp")
REPORT_DIR    = os.environ.get("SAVINGS_REPORT_DIR",
                               os.path.join(HOME, "projects", "docs", "reports", "claude-savings"))
ENV_FILE      = os.path.join(HOME, "projects", ".env")
EMAIL_FROM    = os.environ.get("SAVINGS_EMAIL_FROM_ACCOUNT", "gmail_gegger-tyken")  # admin@teamnebula.ai
EMAIL_TO      = os.environ.get("SAVINGS_EMAIL_TO", "shawn@teamnebula.ai")

# --- assumptions, all overridable by env ------------------------------------
DAYS               = 7
ACTIVE_GAP_MIN     = float(os.environ.get("SAVINGS_ACTIVE_GAP_MIN", 10))
# Counterfactual model for locally-served turns (sessions default to Opus 5).
COUNTERFACTUAL     = os.environ.get("SAVINGS_COUNTERFACTUAL", "claude-opus-5")
# Human needs at least as long as the agent's active time to do the same work.
HOURS_MULTIPLIER   = float(os.environ.get("SAVINGS_HOURS_MULTIPLIER", 1.0))
# Rough Opus-shaped cost of one llm-jury solve run had it gone straight to Opus.
JURY_RUN_IN_TOK    = int(os.environ.get("SAVINGS_JURY_RUN_IN_TOK", 20_000))
JURY_RUN_OUT_TOK   = int(os.environ.get("SAVINGS_JURY_RUN_OUT_TOK", 5_000))
# --- the $200 plan benchmark ------------------------------------------------
PLAN_COST_MO       = float(os.environ.get("SAVINGS_PLAN_COST_MO", 200))
# Anthropic's published guidance for Max 20x: ~24-40 Opus-hours/week.
PLAN_OPUS_HRS_LO   = float(os.environ.get("SAVINGS_PLAN_OPUS_HRS_LO", 24))
PLAN_OPUS_HRS_HI   = float(os.environ.get("SAVINGS_PLAN_OPUS_HRS_HI", 40))
# Opus costs ~35x the open-weight OpenRouter tiers (per the fusion ladder docs),
# so $1 of measured OpenRouter spend stands in for ~$35 of Opus-equivalent work.
OPUS_COST_RATIO    = float(os.environ.get("SAVINGS_OPUS_COST_RATIO", 35))
LLMJURY_ENV        = os.path.join(HOME, ".llmjury", ".env")
LIMIT_PHRASES      = ("Claude usage limit reached",)

# $ per MTok (input, output). Anthropic first-party rates.
# Cache: reads 0.1x input; writes 1.25x (5m TTL) / 2x (1h TTL).
PRICES = {
    "claude-fable-5":   (10.0, 50.0),
    "claude-mythos-5":  (10.0, 50.0),
    "claude-opus-5":    (5.0, 25.0),
    "claude-opus-4-8":  (5.0, 25.0),
    "claude-opus-4-7":  (5.0, 25.0),
    "claude-opus-4-6":  (5.0, 25.0),
    "claude-sonnet-5":  (3.0, 15.0),   # intro 2/10 through 2026-08-31, handled below
    "claude-sonnet-4-6": (3.0, 15.0),
    "claude-haiku-4-5": (1.0, 5.0),
}
SONNET5_INTRO_UNTIL = datetime(2026, 8, 31, tzinfo=timezone.utc)
ALIASES = {"opus": "claude-opus-5", "sonnet": "claude-sonnet-5", "haiku": "claude-haiku-4-5"}
SKIP_MODELS = {"<synthetic>", "PAY_PER_EVENT", ""}
LOCAL_PREFIXES = ("qwen", "gemma", "llama", "phi")


def price_for(model, now):
    if model == "claude-sonnet-5" and now <= SONNET5_INTRO_UNTIL:
        return (2.0, 10.0)
    if model in PRICES:
        return PRICES[model]
    # date-suffixed ids (claude-haiku-4-5-20251001) and unknown claude models
    for known in PRICES:
        if model.startswith(known):
            return price_for(known, now)
    return (5.0, 25.0)  # unknown claude-* fallback, opus-tier


def is_local(model):
    return model.startswith(LOCAL_PREFIXES)


def count_real_limit_hits(line):
    """A genuine 'Claude usage limit reached' throttle, not the phrase echoed
    back inside a tool_result/tool_use blob (e.g. this script's own grep
    output, or a file a session happened to read)."""
    try:
        d = json.loads(line)
    except Exception:
        return 0
    if d.get("type") == "system":
        text = str(d.get("content") or "")
        return sum(text.count(p) for p in LIMIT_PHRASES)
    if d.get("type") in ("user", "assistant"):
        content = (d.get("message") or {}).get("content")
        if isinstance(content, str):
            return sum(content.count(p) for p in LIMIT_PHRASES)
        if isinstance(content, list):
            n = 0
            for block in content:
                if not isinstance(block, dict):
                    continue
                if block.get("type") in ("tool_result", "tool_use"):
                    continue  # echoed shell/tool output, not a real throttle
                text = str(block.get("text") or "")
                n += sum(text.count(p) for p in LIMIT_PHRASES)
            return n
    return 0


def parse_ts(s):
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except Exception:
        return None


def cost_usd(tok, now):
    """Cloud cost of a token bucket dict at its model's prices."""
    inp, outp = price_for(tok["model"], now)
    return (tok["input"] * inp
            + tok["output"] * outp
            + tok["cache_read"] * inp * 0.1
            + tok["cache_w5m"] * inp * 1.25
            + tok["cache_w1h"] * inp * 2.0) / 1e6


def counterfactual_usd(tok, now):
    """Same turns billed to the counterfactual cloud model, same cache behavior."""
    t = dict(tok); t["model"] = COUNTERFACTUAL
    return cost_usd(t, now)


def scan(days):
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=days)
    per_model = defaultdict(lambda: {"model": "", "input": 0, "output": 0, "cache_read": 0,
                                     "cache_w5m": 0, "cache_w1h": 0, "turns": 0})
    seen = set()
    limit_events = 0
    session_ts = defaultdict(list)   # transcript file -> [datetime]
    files = glob.glob(os.path.join(PROJECTS_DIR, "*", "*.jsonl"))
    scanned = 0
    for path in files:
        try:
            if datetime.fromtimestamp(os.path.getmtime(path), timezone.utc) < cutoff:
                continue
        except OSError:
            continue
        scanned += 1
        with open(path, errors="replace") as fh:
            for line in fh:
                if any(p in line for p in LIMIT_PHRASES):
                    limit_events += count_real_limit_hits(line)
                if '"assistant"' not in line or '"usage"' not in line:
                    continue
                try:
                    d = json.loads(line)
                except Exception:
                    continue
                if d.get("type") != "assistant":
                    continue
                ts = parse_ts(d.get("timestamp", ""))
                if ts is None or ts < cutoff:
                    continue
                msg = d.get("message") or {}
                usage = msg.get("usage") or {}
                if not usage:
                    continue
                model = ALIASES.get(msg.get("model", ""), msg.get("model", ""))
                if model in SKIP_MODELS:
                    continue
                key = (msg.get("id") or d.get("uuid"), d.get("requestId"))
                if key in seen:
                    continue
                seen.add(key)
                cc = usage.get("cache_creation") or {}
                t = per_model[model]
                t["model"] = model
                t["input"]      += usage.get("input_tokens", 0) or 0
                t["output"]     += usage.get("output_tokens", 0) or 0
                t["cache_read"] += usage.get("cache_read_input_tokens", 0) or 0
                w5 = cc.get("ephemeral_5m_input_tokens")
                w1 = cc.get("ephemeral_1h_input_tokens")
                if w5 is None and w1 is None:
                    w5 = usage.get("cache_creation_input_tokens", 0) or 0
                t["cache_w5m"] += w5 or 0
                t["cache_w1h"] += w1 or 0
                t["turns"] += 1
                session_ts[path].append(ts)
    return now, per_model, session_ts, scanned, limit_events


def openrouter_weekly():
    """Measured OpenRouter spend this week from the key endpoint, or None."""
    try:
        with open(LLMJURY_ENV) as fh:
            key = next((l.split("=", 1)[1].strip() for l in fh
                        if l.startswith("OPENROUTER_API_KEY=")), None)
        if not key:
            return None
        r = subprocess.run(["curl", "-sS", "--max-time", "20",
                            "-H", f"Authorization: Bearer {key}",
                            "https://openrouter.ai/api/v1/key"],
                           capture_output=True, text=True, timeout=30)
        return json.loads(r.stdout)["data"]["usage_weekly"]
    except Exception:
        return None


def active_hours(session_ts, gap_min):
    """(sum of per-session active hours, wall-clock union hours)."""
    gap = timedelta(minutes=gap_min)
    per_session_total = 0.0
    intervals = []
    for ts_list in session_ts.values():
        ts_list.sort()
        start = prev = ts_list[0]
        for ts in ts_list[1:]:
            if ts - prev > gap:
                per_session_total += (prev - start).total_seconds()
                start = ts
            prev = ts
        per_session_total += (prev - start).total_seconds()
        intervals.append((ts_list[0], ts_list[-1]))
    # wall-clock union of session spans
    union_secs = 0.0
    cur_s = cur_e = None
    for s, e in sorted(intervals):
        if cur_s is None:
            cur_s, cur_e = s, e
        elif s <= cur_e:
            cur_e = max(cur_e, e)
        else:
            union_secs += (cur_e - cur_s).total_seconds()
            cur_s, cur_e = s, e
    if cur_s is not None:
        union_secs += (cur_e - cur_s).total_seconds()
    return per_session_total / 3600.0, union_secs / 3600.0


def jury_runs(days):
    if not os.path.isdir(JURY_TMP):
        return 0
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    n = 0
    for name in os.listdir(JURY_TMP):
        p = os.path.join(JURY_TMP, name)
        try:
            if datetime.fromtimestamp(os.path.getmtime(p), timezone.utc) >= cutoff:
                n += 1
        except OSError:
            pass
    return n


def md_to_html(md):
    """Minimal markdown -> HTML: headers, bold, pipe tables, hr, paragraphs.
    No external dependency for a script that only ever renders its own output."""
    lines = md.splitlines()
    html, i, in_table = [], 0, False
    def inline(s):
        while "**" in s:
            s = s.replace("**", "<b>", 1).replace("**", "</b>", 1)
        return s
    while i < len(lines):
        line = lines[i]
        if line.startswith("#"):
            level = len(line) - len(line.lstrip("#"))
            html.append(f"<h{level}>{inline(line[level:].strip())}</h{level}>")
        elif line.strip() == "---":
            html.append("<hr>")
        elif line.strip().startswith("|"):
            cells = [c.strip() for c in line.strip().strip("|").split("|")]
            if all(set(c) <= set("-: ") for c in cells):
                i += 1; continue  # separator row
            if not in_table:
                html.append('<table style="border-collapse:collapse;width:100%">'); in_table = True
            tag = "th" if not html or "<table" in html[-1] else "td"
            row = "".join(f'<{tag} style="border:1px solid #ddd;padding:4px 8px;text-align:left">{inline(c)}</{tag}>' for c in cells)
            html.append(f"<tr>{row}</tr>")
        else:
            if in_table:
                html.append("</table>"); in_table = False
            if line.strip():
                html.append(f"<p>{inline(line)}</p>")
        i += 1
    if in_table:
        html.append("</table>")
    return ('<div style="font-family:-apple-system,Helvetica,Arial,sans-serif;font-size:14px;'
           'line-height:1.5;color:#1a1a1a">' + "\n".join(html) + "</div>")


def build_savings_email_md(s, week_of, today):
    """Savings-only email body: what was saved, in $, and where it came from.
    Deliberately omits cloud spend, plan-leverage, and working-time framing —
    the full breakdown with those numbers still lands in the saved report.
    Caching is excluded from $ saved: on a flat-rate plan there's no per-token
    bill for it to discount off of, so an API-list-price counterfactual isn't
    real money — it's noted separately, never added to the total."""
    lines = [
        f"# ${s['usd_saved']:,.2f} saved this week ({week_of} to {today})",
        "",
        "| Source | $ saved | How |",
        "|---|---|---|",
        f"| Local qwen routing | ${s['local_saved']:,.2f} | "
        f"{s['local_turns']} turns ran free on local hardware instead of the cloud |",
        f"| OpenRouter / llm-jury ladder | ${s['jury_saved']:,.2f} | "
        f"kept work off {COUNTERFACTUAL} pricing |",
        "",
        f"**Total: ${s['usd_saved']:,.2f} saved.**",
        "",
        f"(Not counted above: {s['cache_rate']:.1f}% of input tokens ran from cache this week — "
        f"real efficiency, but not $ saved, since the plan is flat-rate with no per-token bill.)",
        "",
        "---",
        "*Full weekly breakdown (spend, plan usage, working time) is in the saved report.*",
        "",
    ]
    return "\n".join(lines)


def send_weekly_email(savings, week_of, today, dry):
    key = None
    try:
        with open(ENV_FILE) as fh:
            key = next((l.split("=", 1)[1].strip() for l in fh
                       if l.startswith("TMN_COMPOSIO_API_KEY=")), None)
    except OSError:
        pass
    if not key:
        print("email skipped: TMN_COMPOSIO_API_KEY not found", file=sys.stderr)
        return False
    subject = f"${savings['usd_saved']:,.0f} saved this week — Claude automation ({week_of} to {today})"
    body_html = md_to_html(build_savings_email_md(savings, week_of, today))
    if dry:
        print(f"\n[dry-run] would email '{subject}' to {EMAIL_TO} from {EMAIL_FROM}", file=sys.stderr)
        return True
    env = dict(os.environ, COMPOSIO_API_KEY=key)
    payload = json.dumps({"recipient_email": EMAIL_TO, "subject": subject,
                          "body": body_html, "is_html": True})
    r = subprocess.run(["composio", "execute", "GMAIL_SEND_EMAIL",
                        "--account", EMAIL_FROM, "-d", payload],
                       capture_output=True, text=True, timeout=60, env=env)
    try:
        ok = json.loads(r.stdout).get("successful", False)
    except Exception:
        ok = False
    if not ok:
        print(f"email send failed: {r.stdout}\n{r.stderr}", file=sys.stderr)
    return ok


def fmt_tok(n):
    if n >= 1e9: return f"{n/1e9:.2f}B"
    if n >= 1e6: return f"{n/1e6:.1f}M"
    if n >= 1e3: return f"{n/1e3:.0f}K"
    return str(n)


def main():
    days = DAYS
    if "--days" in sys.argv:
        days = int(sys.argv[sys.argv.index("--days") + 1])
    dry = "--dry-run" in sys.argv
    notify = "--no-notify" not in sys.argv and not dry
    send_email = "--no-email" not in sys.argv

    now, per_model, session_ts, scanned, limit_events = scan(days)
    cloud = {m: t for m, t in per_model.items() if not is_local(m)}
    local = {m: t for m, t in per_model.items() if is_local(m)}

    # 1. local routing savings
    local_tokens = sum(t["input"] + t["output"] + t["cache_read"] + t["cache_w5m"] + t["cache_w1h"]
                       for t in local.values())
    local_saved = sum(counterfactual_usd(t, now) for t in local.values())
    local_turns = sum(t["turns"] for t in local.values())

    # 2. cache savings (net of write premium actually paid)
    cache_read_tok = sum(t["cache_read"] for t in cloud.values())
    cache_saved = cache_premium = 0.0
    for t in cloud.values():
        inp, _ = price_for(t["model"], now)
        cache_saved   += t["cache_read"] * inp * 0.9 / 1e6
        cache_premium += (t["cache_w5m"] * 0.25 + t["cache_w1h"] * 1.0) * inp / 1e6
    cache_net = cache_saved - cache_premium
    # full-price input tokens effectively avoided
    cache_tok_equiv = int(cache_read_tok * 0.9)

    # 3. OpenRouter ladder — measured spend when the key answers, else the
    #    per-run estimate from llm-jury tmp dirs
    n_jury = jury_runs(days)
    op_in, op_out = price_for(COUNTERFACTUAL, now)
    or_spent = openrouter_weekly()
    if or_spent is not None:
        jury_saved = or_spent * OPUS_COST_RATIO - or_spent
        jury_in = int(jury_saved / (op_in / 1e6) * 0.8)  # token-equivalent, input-weighted
        jury_out = 0
        jury_basis = (f"${or_spent:,.2f} measured OpenRouter spend x {OPUS_COST_RATIO:g}x "
                      f"Opus cost ratio, net of spend; {n_jury} jury runs (measured $ / est. ratio)")
    else:
        jury_in, jury_out = n_jury * JURY_RUN_IN_TOK, n_jury * JURY_RUN_OUT_TOK
        jury_saved = (jury_in * op_in + jury_out * op_out) / 1e6
        jury_basis = (f"{n_jury} solve runs kept off {COUNTERFACTUAL}; {JURY_RUN_IN_TOK//1000}K in / "
                      f"{JURY_RUN_OUT_TOK//1000}K out per run (estimate; OpenRouter key unreachable)")

    # 4. actual cloud spend for context
    cloud_cost = sum(cost_usd(t, now) for t in cloud.values())
    cloud_turns = sum(t["turns"] for t in cloud.values())

    # 5. time
    agent_hours, wall_hours = active_hours(session_ts, ACTIVE_GAP_MIN) if session_ts else (0.0, 0.0)
    human_hours = agent_hours * HOURS_MULTIPLIER
    parallelism = (agent_hours / wall_hours) if wall_hours else 0.0

    # Caching is deliberately excluded from $ saved: on a flat-rate Max plan
    # there is no per-token bill for cache to discount off of, so a cache-read
    # counterfactual against API list price isn't money that ever left (or
    # would have left) your account. It's tracked separately as an efficiency
    # stat (more work fit in the same session/quota), never as dollars.
    tokens_saved = local_tokens + jury_in + jury_out
    usd_saved = local_saved + jury_saved

    # plan benchmark
    plan_wk = PLAN_COST_MO * 12 / 52
    leverage = cloud_cost / plan_wk if plan_wk else 0.0
    opus_turns = sum(t["turns"] for m, t in cloud.items()
                     if "opus" in m or "fable" in m or "mythos" in m)
    opus_share = opus_turns / cloud_turns * 100 if cloud_turns else 0.0
    total_input = sum(t["input"] + t["cache_read"] + t["cache_w5m"] + t["cache_w1h"]
                      for t in cloud.values())
    cache_rate = cache_read_tok / total_input * 100 if total_input else 0.0
    hrs_lo = agent_hours / PLAN_OPUS_HRS_HI if PLAN_OPUS_HRS_HI else 0.0
    hrs_hi = agent_hours / PLAN_OPUS_HRS_LO if PLAN_OPUS_HRS_LO else 0.0
    off_plan_usd = local_saved + jury_saved

    week_of = (now - timedelta(days=days)).strftime("%Y-%m-%d")
    today = now.strftime("%Y-%m-%d")

    lines = [
        f"# Claude savings report — week of {week_of} to {today}",
        "",
        f"**${usd_saved:,.2f} saved this week** (local qwen + OpenRouter ladder; excludes "
        f"caching — see note below) · **${cloud_cost:,.2f} of API-equivalent work on a "
        f"${plan_wk:,.0f}/wk subscription ({leverage:,.0f}x)** · **{agent_hours:,.1f} agent-hours**",
        "",
        f"## The ${PLAN_COST_MO:,.0f} plan vs what you actually got",
        "",
        f"- Plan cost: **${plan_wk:,.2f}/week** (${PLAN_COST_MO:,.0f}/mo Max 20x). Anthropic's "
        f"guidance for this tier: ~{PLAN_OPUS_HRS_LO:g}-{PLAN_OPUS_HRS_HI:g} Opus-hours/week.",
        f"- Delivered: **{agent_hours:,.1f} agent-hours** ({opus_share:.0f}% of turns on "
        f"Opus-class models) — **{hrs_lo:,.1f}-{hrs_hi:,.1f}x the plan's Opus-hour band**.",
        f"- API-equivalent value pushed through the plan: **${cloud_cost:,.2f}** across "
        f"{cloud_turns} turns → **{leverage:,.0f}x** the subscription price.",
        f"- What makes that fit inside one plan: **{cache_rate:.1f}% of input tokens came from "
        f"cache** ({fmt_tok(cache_read_tok)} reads), and **${off_plan_usd:,.2f}** of "
        f"Opus-equivalent work ran off-plan entirely (local qwen + OpenRouter ladder), "
        f"touching zero plan quota.",
        f"- Recorded plan-limit hits in transcripts this week: **{limit_events}** "
        f"(undercount — the CLI does not log every throttle).",
        "",
        "## Where the $ saved came from",
        "",
        "| Source | Tokens avoided | $ saved | Basis |",
        "|---|---|---|---|",
        f"| Local qwen routing (:8083) | {fmt_tok(local_tokens)} | ${local_saved:,.2f} | "
        f"{local_turns} turns served locally; counterfactual = {COUNTERFACTUAL} pricing (measured) |",
        f"| llm-jury / OpenRouter ladder | {fmt_tok(jury_in + jury_out)} | ${jury_saved:,.2f} | "
        f"{jury_basis} |",
        "",
        f"**Note on caching:** {cache_rate:.1f}% of input tokens this week came from cache "
        f"({fmt_tok(cache_read_tok)} reads, worth ${cache_net:,.2f} against API list price). "
        f"That's **not counted as $ saved** — you're on a flat ${PLAN_COST_MO:,.0f}/mo plan with "
        f"no per-token bill, so there was no charge for caching to discount off of. What caching "
        f"actually buys you is efficiency: more work fits in the same session and weekly limits.",
        "",
        "## Working time",
        "",
        f"- Agent active time, summed per session: **{agent_hours:,.1f} h** "
        f"(events ≤ {ACTIVE_GAP_MIN:.0f} min apart count as continuous work)",
        f"- Wall-clock coverage: **{wall_hours:,.1f} h** → {parallelism:.1f} parallel agents on average",
        f"- Estimated human time to do the same work: **{human_hours:,.1f} h** "
        f"(assumes a human needs ≥ {HOURS_MULTIPLIER:g}x the agent's active time)",
        "",
        "## Cloud usage by model",
        "",
        "| Model | Turns | Input | Output | Cache read | Cache write | Cost |",
        "|---|---|---|---|---|---|---|",
    ]
    for m, t in sorted(cloud.items(), key=lambda kv: -cost_usd(kv[1], now)):
        lines.append(f"| {m} | {t['turns']} | {fmt_tok(t['input'])} | {fmt_tok(t['output'])} | "
                     f"{fmt_tok(t['cache_read'])} | {fmt_tok(t['cache_w5m'] + t['cache_w1h'])} | "
                     f"${cost_usd(t, now):,.2f} |")
    if local:
        lines += ["", "## Local model usage (zero cloud cost)", "",
                  "| Model | Turns | Input | Output |", "|---|---|---|---|"]
        for m, t in sorted(local.items()):
            lines.append(f"| {m} | {t['turns']} | "
                         f"{fmt_tok(t['input'] + t['cache_read'] + t['cache_w5m'] + t['cache_w1h'])} | "
                         f"{fmt_tok(t['output'])} |")
    lines += ["", "---",
              f"*Scanned {scanned} transcript files; window {days}d; generated {now.strftime('%Y-%m-%d %H:%M UTC')}. "
              f"Assumption knobs: SAVINGS_COUNTERFACTUAL, SAVINGS_HOURS_MULTIPLIER, "
              f"SAVINGS_JURY_RUN_IN_TOK/OUT_TOK, SAVINGS_ACTIVE_GAP_MIN, SAVINGS_PLAN_COST_MO, "
              f"SAVINGS_PLAN_OPUS_HRS_LO/HI, SAVINGS_OPUS_COST_RATIO.*", ""]
    report = "\n".join(lines)

    print(report)
    if not dry:
        os.makedirs(REPORT_DIR, exist_ok=True)
        out = os.path.join(REPORT_DIR, f"claude-savings-{today}.md")
        with open(out, "w") as fh:
            fh.write(report)
        latest = os.path.join(REPORT_DIR, "latest.md")
        try:
            if os.path.lexists(latest):
                os.remove(latest)
            os.symlink(out, latest)
        except OSError:
            pass
        print(f"\nwritten: {out}", file=sys.stderr)
    if send_email:
        savings = {"usd_saved": usd_saved, "cache_rate": cache_rate,
                  "local_saved": local_saved, "local_turns": local_turns,
                  "jury_saved": jury_saved}
        sent = send_weekly_email(savings, week_of, today, dry)
        print(f"email {'sent' if sent else 'FAILED'} to {EMAIL_TO}", file=sys.stderr)
    if notify:
        msg = (f"${cloud_cost:,.0f} of API-equivalent work on a ${plan_wk:,.0f}/wk plan "
               f"({leverage:,.0f}x), {agent_hours:,.0f} agent-hours, "
               f"{fmt_tok(tokens_saved)} tokens saved")
        subprocess.run(["osascript", "-e",
                        f'display notification "{msg}" with title "Claude savings report"'],
                       capture_output=True)


if __name__ == "__main__":
    main()
