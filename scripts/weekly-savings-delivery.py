#!/usr/bin/env python3
"""Collect local usage, then send one complete weekly snapshot through Hermes.

The week closes Sunday at 19:07 Pacific. Monday at 19:07 is the normal send
time. Hermes checks hourly through Thursday at 19:07; missing or stale local
data keeps the email pending, and the deadline ends that week's attempt.
"""
import fcntl
import importlib.util
import json
import os
import shlex
import subprocess
import sys
import tempfile
import urllib.request
from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("savings_report", HERE / "claude-savings-report.py")
report = importlib.util.module_from_spec(spec)
spec.loader.exec_module(report)

PACIFIC = ZoneInfo("America/Los_Angeles")
STATE_DIR = Path(os.environ.get("SAVINGS_STATE_DIR", Path.home() / ".claude/state/weekly-savings"))
QWEN_USAGE = Path(os.environ.get("SAVINGS_QWEN_USAGE", Path.home() / ".qwen/usage_record.jsonl"))
REMOTE_SSH = os.environ.get("SAVINGS_REMOTE_SSH", "hermes@5.161.126.205")
REMOTE_STATE_DIR = os.environ.get("SAVINGS_REMOTE_STATE_DIR", "/home/hermes/.hermes/savings")
PRICES_URL = "https://openrouter.ai/api/v1/models"
MODEL_IDS = ("openai/gpt-5.6-sol", "anthropic/claude-opus-5")
HOUR = 19
MINUTE = 7


class DataUnavailable(Exception):
    pass


def cycle_for(now, include_expired=False):
    """The active fixed week, or None outside its collection and send window."""
    local = now.astimezone(PACIFIC)
    monday = local.date() - timedelta(days=local.weekday())
    if local.weekday() == 6 and local.time() >= time(HOUR, MINUTE):
        monday += timedelta(days=7)
    nominal = datetime.combine(monday, time(HOUR, MINUTE), PACIFIC)
    end = nominal - timedelta(days=1)
    deadline = nominal + timedelta(days=3)
    if local < end or (local > deadline and not include_expired):
        return None
    start = end - timedelta(days=7)
    return {"id": end.strftime("%Y-%m-%d"), "start": start, "end": end,
            "nominal": nominal, "deadline": deadline}


def atomic_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temp = tempfile.mkstemp(dir=path.parent, prefix=".tmp-", suffix=".json")
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(value, stream, sort_keys=True, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temp, 0o600)
        os.replace(temp, path)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


def prices(fetch=None):
    """Fetch current published rates; a network or schema failure blocks mail."""
    if fetch is None:
        def fetch():
            with urllib.request.urlopen(PRICES_URL, timeout=15) as response:
                return json.load(response)
    try:
        models = {row["id"]: row for row in fetch()["data"]}
        result = {}
        for model_id in MODEL_IDS:
            pricing = models[model_id]["pricing"]
            rate = {key: float(pricing[key]) for key in
                    ("prompt", "completion", "input_cache_read")}
            if not all(0 <= value < 1 for value in rate.values()):
                raise ValueError("invalid price")
            result[model_id] = rate
        return result
    except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise DataUnavailable(f"OpenRouter price lookup failed: {type(exc).__name__}") from exc


def codex_equivalent(groups, rate):
    total = 0.0
    for bucket in groups.values():
        cached = min(bucket["cache_read"], bucket["input"])
        total += ((bucket["input"] - cached) * rate["prompt"]
                  + cached * rate["input_cache_read"]
                  + bucket["output"] * rate["completion"])
    return total


def claude_equivalent(groups, rate):
    return sum(bucket["input"] * rate["prompt"]
               + bucket["output"] * rate["completion"]
               + bucket["cache_read"] * rate["input_cache_read"]
               + bucket["cache_w5m"] * rate["prompt"] * 1.25
               + bucket["cache_w1h"] * rate["prompt"] * 2
               for bucket in groups.values())


def qwen_usage(start, end, path=QWEN_USAGE):
    """Count completed Qwen Code sessions separately from Claude/Codex logs."""
    totals = {"sessions": 0, "requests": 0, "input": 0, "cached": 0, "output": 0}
    seen = set()
    try:
        with open(path, errors="replace") as stream:
            for line in stream:
                row = json.loads(line)
                session_id = row["sessionId"]
                if session_id in seen:
                    raise DataUnavailable("duplicate Qwen usage session")
                seen.add(session_id)
                began = datetime.fromtimestamp(row["startTime"] / 1000, timezone.utc)
                finished = datetime.fromtimestamp(row["timestamp"] / 1000, timezone.utc)
                if finished <= start or began >= end:
                    continue
                if began < start or finished > end:
                    raise DataUnavailable("Qwen session crosses a week boundary")
                totals["sessions"] += 1
                for usage in row["models"].values():
                    for key, field in (("requests", "requests"), ("inputTokens", "input"),
                                       ("cachedTokens", "cached"), ("outputTokens", "output")):
                        value = int(usage.get(key, 0))
                        if value < 0:
                            raise DataUnavailable("negative Qwen usage")
                        totals[field] += value
    except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise DataUnavailable(f"Qwen usage unavailable: {type(exc).__name__}") from exc
    return totals


def build_snapshot(cycle, generated_at, get_prices=prices):
    """Read every required local source for one completed, fixed week."""
    required = (Path(report.PROJECTS_DIR), Path(report.CODEX_SESSIONS_DIR),
                QWEN_USAGE, Path(report.LLMJURY_SPEND_LEDGER))
    if any(not path.exists() for path in required):
        raise DataUnavailable("a required local usage source is missing")
    start = cycle["start"].astimezone(timezone.utc)
    end = cycle["end"].astimezone(timezone.utc)
    rates = get_prices()
    _, claude, _, claude_files, _ = report.scan(7, now=end)
    codex, codex_files = report.scan_codex(start, end=end)
    jury = report.llmjury_spend(7, now=end)
    if not jury["available"]:
        raise DataUnavailable("llm-jury ledger is unreadable")
    qwen = qwen_usage(start, end)
    local_claude = {k: v for k, v in claude.items() if report.is_local(k)}
    local_codex = {k: v for k, v in codex.items() if report.is_local(k)}
    claude_rate = rates["anthropic/claude-opus-5"]
    codex_rate = rates["openai/gpt-5.6-sol"]
    qwen_cached = min(qwen["cached"], qwen["input"])
    local = {
        "claude": {"turns": sum(v["turns"] for v in local_claude.values()),
                   "tokens": sum(v["input"] + v["output"] + v["cache_read"]
                                 + v["cache_w5m"] + v["cache_w1h"] for v in local_claude.values()),
                   "usd": claude_equivalent(local_claude, claude_rate)},
        "codex": {"turns": sum(v["turns"] for v in local_codex.values()),
                  "tokens": sum(v["input"] + v["output"] for v in local_codex.values()),
                  "usd": codex_equivalent(local_codex, codex_rate)},
        "qwen": {**qwen, "usd": (qwen["input"] - qwen_cached) * codex_rate["prompt"]
                 + qwen_cached * codex_rate["input_cache_read"]
                 + qwen["output"] * codex_rate["completion"]},
    }
    return {"schema": 1, "cycle_id": cycle["id"],
            "window_start": start.isoformat(), "window_end": end.isoformat(),
            "window_start_local": cycle["start"].isoformat(),
            "window_end_local": cycle["end"].isoformat(),
            "generated_at": generated_at.astimezone(timezone.utc).isoformat(),
            "pricing_source": PRICES_URL, "pricing": rates,
            "sources": {"claude_files": claude_files, "codex_files": codex_files,
                        "qwen_sessions": qwen["sessions"], "llmjury_available": True},
            "local": local,
            "local_total_usd": sum(row["usd"] for row in local.values()),
            "subscription_avoided_estimate_usd": jury["avoided_usd"],
            "openrouter_metered_usd": jury["usd"]}


def snapshot_path(cycle):
    return STATE_DIR / f"snapshot-{cycle['id']}.json"


def publish_snapshot(cycle):
    """Stage the complete local JSON on the Hermes host, then rename it."""
    local = snapshot_path(cycle)
    target = f"{REMOTE_STATE_DIR}/{local.name}"
    staged = f"{target}.tmp"
    try:
        subprocess.run(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=7",
                        REMOTE_SSH, f"install -d -m 700 {shlex.quote(REMOTE_STATE_DIR)}"],
                       check=True, capture_output=True, timeout=15)
        subprocess.run(["scp", "-q", "-o", "BatchMode=yes", "-o", "ConnectTimeout=7",
                        str(local), f"{REMOTE_SSH}:{staged}"],
                       check=True, capture_output=True, timeout=20)
        subprocess.run(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=7",
                        REMOTE_SSH, f"chmod 600 {shlex.quote(staged)} && "
                        f"mv {shlex.quote(staged)} {shlex.quote(target)}"],
                       check=True, capture_output=True, timeout=15)
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        raise DataUnavailable(f"Hermes snapshot transfer failed: {type(exc).__name__}") from exc


def read_json(path):
    try:
        with open(path) as stream:
            return json.load(stream)
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:
        raise DataUnavailable(f"saved weekly state is unreadable: {path.name}") from exc


def snapshot_fresh(snapshot, cycle, now):
    if not snapshot or snapshot.get("schema") != 1 or snapshot.get("cycle_id") != cycle["id"]:
        return False
    try:
        generated = datetime.fromisoformat(snapshot["generated_at"])
    except (KeyError, TypeError, ValueError):
        return False
    age = now - generated
    return (cycle["end"] <= generated.astimezone(PACIFIC)
            and timedelta(0) <= age <= timedelta(hours=24)
            and all(key in snapshot for key in ("local", "pricing", "sources")))


def collect(now):
    cycle = cycle_for(now)
    if cycle is None:
        return "No completed week awaits collection."
    state = read_json(STATE_DIR / f"delivery-{cycle['id']}.json") or {}
    if state.get("status") in ("attempted", "sent", "skipped"):
        return "Week already finalized."
    snapshot = build_snapshot(cycle, now)
    atomic_json(snapshot_path(cycle), snapshot)
    publish_snapshot(cycle)
    return f"Local snapshot saved for week ending {cycle['id']}."


def email_body(snapshot):
    local = snapshot["local"]
    start = snapshot["window_start_local"][:10]
    end = snapshot["window_end_local"][:10]
    return "\n".join([
        f"# Local-agent cost comparison, {start} to {end}", "",
        "| Local path | Measured work | OpenRouter-equivalent charge avoided |",
        "|---|---:|---:|",
        f"| Codex | {local['codex']['turns']} turns, {report.fmt_tok(local['codex']['tokens'])} tokens | ${local['codex']['usd']:,.2f} |",
        f"| Claude | {local['claude']['turns']} turns, {report.fmt_tok(local['claude']['tokens'])} tokens | ${local['claude']['usd']:,.2f} |",
        f"| Standalone Qwen | {local['qwen']['sessions']} sessions, {report.fmt_tok(local['qwen']['input'] + local['qwen']['output'])} tokens | ${local['qwen']['usd']:,.2f} |",
        "",
        f"**Local total: ${snapshot['local_total_usd']:,.2f}.**",
        "",
        f"Subscription-backed frontier calls avoided an estimated ${snapshot['subscription_avoided_estimate_usd']:,.2f} of OpenRouter charges. "
        f"The llm-jury ledger records ${snapshot['openrouter_metered_usd']:,.2f} of metered OpenRouter spend. "
        "These figures stay outside the local total.",
        "",
        "This compares measured local tokens with current published OpenRouter rates: "
        "GPT-5.6 Sol for Codex and standalone Qwen; Claude Opus 5 for Claude. "
        "It estimates the charge for equivalent token volume, not a reduction "
        "in an existing subscription bill. Hardware and electricity costs are excluded.",
        f"Local snapshot: {snapshot['generated_at']}. Pricing: {snapshot['pricing_source']}.",
    ])


def send(snapshot):
    subject = (f"${snapshot['local_total_usd']:,.2f} local model cost avoided "
               f"| week ending {snapshot['cycle_id']}")
    payload = {"recipient_email": report.EMAIL_TO, "subject": subject,
               "body": report.md_to_html(email_body(snapshot)), "is_html": True}
    result = subprocess.run(["composio", "execute", "GMAIL_SEND_EMAIL", "--account",
                             report.EMAIL_FROM, "-d", json.dumps(payload)],
                            capture_output=True, text=True, timeout=60)
    try:
        answer = json.loads(result.stdout)
    except ValueError:
        answer = {}
    if result.returncode or not answer.get("successful"):
        raise RuntimeError("Gmail send failed or returned an uncertain result")
    return answer


def dispatch(now, sender=send):
    cycle = cycle_for(now, include_expired=True)
    if cycle is None or now < cycle["nominal"]:
        return "Outside the weekly send window."
    STATE_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    with open(STATE_DIR / "delivery.lock", "a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        state_path = STATE_DIR / f"delivery-{cycle['id']}.json"
        state = read_json(state_path) or {}
        if state.get("status") in ("attempted", "sent", "skipped"):
            return f"Week already {state['status']}."
        if now > cycle["deadline"]:
            atomic_json(state_path, {"status": "skipped", "at": now.isoformat(),
                                     "reason": "three-day grace expired"})
            return "Three-day grace expired; week skipped."
        snapshot = read_json(snapshot_path(cycle))
        if not snapshot_fresh(snapshot, cycle, now):
            return "Fresh local snapshot unavailable; email deferred."
        atomic_json(state_path, {"status": "attempted", "at": now.isoformat()})
        try:
            receipt = sender(snapshot)
        except Exception:
            # A timeout can follow acceptance. Keep attempted until someone
            # reconciles Gmail Sent, so another hourly tick cannot duplicate it.
            raise
        data = receipt.get("data")
        message_id = data.get("messageId") or data.get("id") if isinstance(data, dict) else None
        atomic_json(state_path, {"status": "sent", "at": now.isoformat(),
                                 "receipt": message_id})
        return f"Weekly email sent for week ending {cycle['id']}."


def main():
    if len(sys.argv) != 2 or sys.argv[1] not in ("collect", "dispatch", "audit"):
        raise SystemExit("usage: weekly-savings-delivery.py collect|dispatch|audit")
    now = datetime.now(timezone.utc)
    try:
        if sys.argv[1] == "audit":
            end = now
            cycle = {"id": "audit", "start": end - timedelta(days=7), "end": end}
            print(json.dumps(build_snapshot(cycle, now), indent=2))
        elif sys.argv[1] == "collect":
            print(collect(now))
        else:
            print(dispatch(now))
    except DataUnavailable as exc:
        print(f"Local snapshot unavailable: {exc}", file=sys.stderr)
        raise SystemExit(2)


if __name__ == "__main__":
    main()
