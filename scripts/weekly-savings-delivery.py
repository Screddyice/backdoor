#!/usr/bin/env python3
"""Collect local usage, then send one complete weekly snapshot through Hermes.

The week closes Sunday at 19:07 Pacific. Monday at 19:07 is the normal send
time. Hermes checks hourly through Thursday at 19:07; missing or stale local
data keeps the email pending, and the deadline ends that week's attempt.
"""
import fcntl
import importlib.util
import json
import math
import os
import re
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
JEV_REMOTE_SSH = os.environ.get("SAVINGS_JEV_REMOTE_SSH", "neb-ops-gcp")
JEV_USAGE = Path(os.environ.get("SAVINGS_JEV_USAGE", Path.home() / ".config/jev/usage.jsonl"))
PRICES_URL = "https://openrouter.ai/api/v1/models"
MODEL_IDS = ("anthropic/claude-opus-5",)
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


def codex_baseline_model():
    selected = os.environ.get("SAVINGS_CODEX_MODEL")
    if not selected:
        config_path = Path(os.environ.get("SAVINGS_CODEX_CONFIG", Path.home() / ".codex/config.toml"))
        try:
            for line in config_path.read_text().splitlines():
                if line.strip().startswith("["):
                    break
                match = re.match(r"^\s*model\s*=\s*(['\"])([A-Za-z0-9_.:/-]+)\1\s*(?:#.*)?$", line)
                if match:
                    selected = match.group(2)
                    break
        except OSError as exc:
            raise DataUnavailable(f"Codex baseline configuration unavailable: {type(exc).__name__}") from exc
    if not selected or not re.fullmatch(r"[A-Za-z0-9_.:/-]+", selected):
        raise DataUnavailable("Set SAVINGS_CODEX_MODEL to a published OpenRouter Codex baseline")
    return selected if "/" in selected else f"openai/{selected}"


def prices(fetch=None, model_id=None):
    """Fetch current published rates; a network or schema failure blocks mail."""
    if fetch is None:
        def fetch():
            with urllib.request.urlopen(PRICES_URL, timeout=15) as response:
                return json.load(response)
    try:
        models = {row["id"]: row for row in fetch()["data"]}
        result = {}
        for selected_model in (model_id or codex_baseline_model(), *MODEL_IDS):
            pricing = models[selected_model]["pricing"]
            rate = {key: float(pricing[key]) for key in
                    ("prompt", "completion", "input_cache_read")}
            if not all(0 <= value < 1 for value in rate.values()):
                raise ValueError("invalid price")
            result[selected_model] = rate
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
                local_models = {name: usage for name, usage in row["models"].items()
                                if report.is_local(name)}
                if not local_models:
                    continue
                began = datetime.fromtimestamp(row["startTime"] / 1000, timezone.utc)
                finished = datetime.fromtimestamp(row["timestamp"] / 1000, timezone.utc)
                if finished <= start or began >= end:
                    continue
                if began < start or finished > end:
                    raise DataUnavailable("Qwen session crosses a week boundary")
                totals["sessions"] += 1
                for usage in local_models.values():
                    for key, field in (("requests", "requests"), ("inputTokens", "input"),
                                       ("cachedTokens", "cached"), ("outputTokens", "output")):
                        value = usage.get(key, 0)
                        if type(value) is not int or value < 0:
                            raise DataUnavailable("invalid native Qwen usage")
                        totals[field] += value
                    if usage.get("cachedTokens", 0) > usage.get("inputTokens", 0):
                        raise DataUnavailable("Qwen cache count exceeds input")
    except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise DataUnavailable(f"Qwen usage unavailable: {type(exc).__name__}") from exc
    return totals


def validate_jury_ledger(path):
    """Reject a damaged ledger instead of treating skipped rows as zero spend."""
    try:
        with open(path) as stream:
            for line in stream:
                if not line.strip():
                    continue
                row = json.loads(line)
                datetime.fromisoformat(row["ts"])
                amount = float(row.get("cost_usd", 0))
                avoided = float(row.get("avoided_usd", 0))
                if not all(math.isfinite(value) and value >= 0 for value in (amount, avoided)):
                    raise ValueError("negative ledger amount")
    except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise DataUnavailable(f"llm-jury ledger is invalid: {type(exc).__name__}") from exc


def usage_bucket():
    return {"calls": 0, "input": 0, "output": 0, "cached": 0, "actual_usd": 0.0}


def ledger_usage(text, start, end, deduplicate=False):
    result = {}
    seen = set()
    try:
        for line in text.splitlines():
            if not line.strip():
                continue
            row = json.loads(line)
            timestamp = datetime.fromisoformat(row["ts"])
            if timestamp.tzinfo is None:
                timestamp = timestamp.replace(tzinfo=timezone.utc)
            if not start <= timestamp < end:
                continue
            if row.get("billing") == "subscription":
                continue
            if deduplicate:
                identity = row["id"]
                if not isinstance(identity, str) or not identity:
                    raise ValueError("missing request ID")
                if identity in seen:
                    continue
                seen.add(identity)
            backend = row["backend"]
            if backend not in ("openrouter", "ollama"):
                raise ValueError("unsupported metered backend")
            prompt_tokens = row["prompt_tokens"]
            completion_tokens = row["completion_tokens"]
            cached_tokens = row.get("cached_tokens", 0)
            if any(type(value) is not int or value < 0
                   for value in (prompt_tokens, completion_tokens, cached_tokens)):
                raise ValueError("invalid native token count")
            if cached_tokens > prompt_tokens or row.get("cost_available") is False:
                raise ValueError("invalid cache count or unavailable cost")
            amount = float(row["cost_usd"])
            if not math.isfinite(amount) or amount < 0 or (backend == "ollama" and amount):
                raise ValueError("invalid cost")
            bucket = result.setdefault(backend, usage_bucket())
            bucket["calls"] += 1
            bucket["input"] += prompt_tokens
            bucket["output"] += completion_tokens
            bucket["cached"] += cached_tokens
            bucket["actual_usd"] += amount
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise DataUnavailable(f"provider usage ledger is invalid: {type(exc).__name__}") from exc
    return result


def jev_usage(start, end):
    try:
        desktop_receipts = JEV_USAGE.read_text()
        result = subprocess.run(
            ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8", JEV_REMOTE_SSH,
             "cat ~/.config/jev/usage.jsonl"],
            check=True, capture_output=True, text=True, timeout=45)
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        raise DataUnavailable(f"JEV receipt collection failed: {type(exc).__name__}") from exc
    return ledger_usage(desktop_receipts + "\n" + result.stdout, start, end,
                        deduplicate=True).get("openrouter", usage_bucket())


def comparison_row(label, usage, rate):
    cached = min(usage["cached"], usage["input"])
    baseline = ((usage["input"] - cached) * rate["prompt"]
                + cached * rate["input_cache_read"] + usage["output"] * rate["completion"])
    return {"label": label, **usage, "codex_equivalent_usd": baseline,
            "net_savings_usd": baseline - usage["actual_usd"]}


def subscription_value(subscription, cloud, comparison, rate):
    """Allocate a nominal weekly plan budget over recorded weighted workload."""
    plan = re.fullmatch(r"ChatGPT Pro (200|500)", subscription["current_plan"])
    if not plan:
        raise DataUnavailable("Codex plan has no supported nominal budget")
    monthly = float(plan.group(1))
    weekly = monthly * 12 / 52
    if any(type(rate[field]) not in (int, float) or not math.isfinite(rate[field])
           or not 0 < rate[field] < 1 for field in ("prompt", "completion")):
        raise DataUnavailable("Invalid token weighting rates")
    if (type(rate["input_cache_read"]) not in (int, float)
            or not math.isfinite(rate["input_cache_read"])
            or not 0 <= rate["input_cache_read"] <= rate["prompt"]):
        raise DataUnavailable("Invalid cache weighting rate")
    for row in (cloud, *comparison):
        values = [row[field] for field in ("calls", "input", "output", "cached")]
        if any(type(value) is not int or value < 0 for value in values):
            raise DataUnavailable("Invalid recorded token counts")
        if row["cached"] > row["input"]:
            raise DataUnavailable("Recorded cache count exceeds input")
        amount = row["actual_usd"]
        if type(amount) not in (int, float) or not math.isfinite(amount) or amount < 0:
            raise DataUnavailable("Invalid recorded provider cost")
    cloud_weight = comparison_row("Cloud Codex", cloud, rate)["codex_equivalent_usd"]
    offload_weight = sum(comparison_row(row["label"], row, rate)["codex_equivalent_usd"]
                         for row in comparison)
    spend = sum(row["actual_usd"] for row in comparison)
    measured = cloud_weight > 0
    share = offload_weight / (cloud_weight + offload_weight) if measured else None
    gross = weekly * share if measured else None
    return {"monthly_nominal_usd": monthly, "weekly_nominal_usd": weekly,
            "budget_basis": "Current confirmed Pro tier label interpreted as nominal USD/month; "
                            "not an invoice, tax, proration, refund or historical charge",
            "status": "estimated" if measured else "unavailable_without_cloud_codex_usage",
            "cloud_tokens": cloud["input"] + cloud["output"],
            "offloaded_tokens": sum(row["input"] + row["output"] for row in comparison),
            "cloud_weight": cloud_weight, "offloaded_weight": offload_weight,
            "offloaded_share": share, "preserved_value_usd": gross,
            "net_value_usd": gross - spend if measured else None}


def billing_state_from_messages(messages, now):
    verified = []
    for message in messages:
        if not isinstance(message, dict):
            continue
        sender = message.get("sender", "")
        if not isinstance(sender, str) or not re.fullmatch(
                r"(?:OpenAI\s*<)?noreply@tm\.openai\.com>?", sender.strip()):
            continue
        if message.get("subject") != "ChatGPT - Your updated plan":
            continue
        try:
            timestamp = datetime.fromisoformat(message["messageTimestamp"].replace("Z", "+00:00"))
            if timestamp.tzinfo is None or timestamp > now:
                continue
            text = " ".join(message["messageText"].split())
            identifier = message["messageId"]
        except (KeyError, TypeError, ValueError, AttributeError):
            continue
        if not isinstance(identifier, str) or not identifier:
            continue
        verified.append((timestamp, text, identifier))
    for timestamp, text, identifier in sorted(verified, reverse=True):
        pending = re.search(
            r"Your ChatGPT Pro (\d+) subscription will remain active until "
            r"([A-Za-z]+ \d{1,2}, \d{4}), when your ChatGPT Pro (\d+) "
            r"subscription will take effect", text)
        upgrade = re.search(
            r"Your subscription has been upgraded from ChatGPT Pro (\d+) "
            r"to ChatGPT Pro (\d+)", text)
        if pending:
            effective = None
            for date_format in ("%b %d, %Y", "%B %d, %Y"):
                try:
                    effective = datetime.strptime(pending.group(2), date_format).date()
                    break
                except ValueError:
                    pass
            if effective is None:
                raise DataUnavailable("Codex scheduled plan date is unrecognized")
            if now.astimezone(PACIFIC).date() >= effective:
                raise DataUnavailable("Scheduled Codex plan change needs fresh billing confirmation")
            return {"current_plan": f"ChatGPT Pro {pending.group(1)}",
                    "scheduled_plan": f"ChatGPT Pro {pending.group(3)}",
                    "scheduled_date": effective.isoformat(),
                    "evidence_message_id": identifier, "evidence_at": timestamp.isoformat()}
        if upgrade:
            return {"current_plan": f"ChatGPT Pro {upgrade.group(2)}",
                    "scheduled_plan": None, "scheduled_date": None,
                    "evidence_message_id": identifier, "evidence_at": timestamp.isoformat()}
    raise DataUnavailable("No supported Codex plan confirmation in the verified billing mailbox")


def billing_state(now):
    account = os.environ.get("SAVINGS_BILLING_ACCOUNT", report.EMAIL_FROM)
    expected_email = os.environ.get("SAVINGS_BILLING_EMAIL", "admin@teamnebula.ai")
    def execute(slug, payload):
        try:
            result = subprocess.run(
                ["composio", "execute", slug, "--account", account, "-d", json.dumps(payload)],
                check=True, capture_output=True, text=True, timeout=60)
            answer = json.loads(result.stdout)
            if answer.get("storedInFile"):
                answer = json.loads(Path(answer["outputFilePath"]).read_text())
            if not answer.get("successful") or not isinstance(answer.get("data"), dict):
                raise ValueError("billing read unsuccessful")
            return answer["data"]
        except (OSError, ValueError, KeyError, subprocess.SubprocessError) as exc:
            raise DataUnavailable(f"Codex billing read unavailable: {type(exc).__name__}") from exc
    profile = execute("GMAIL_GET_PROFILE", {})
    if profile.get("emailAddress") != expected_email:
        raise DataUnavailable("Codex billing mailbox identity did not match")
    data = execute("GMAIL_FETCH_EMAILS", {
        "query": 'from:noreply@tm.openai.com subject:"Your updated plan" newer_than:180d',
        "max_results": 30, "verbose": True})
    messages = data.get("messages")
    if not isinstance(messages, list):
        raise DataUnavailable("Codex billing messages are unavailable")
    return billing_state_from_messages(messages, now)


def transcript_usage(groups, claude=False):
    bucket = usage_bucket()
    for usage in groups.values():
        bucket["calls"] += usage["turns"]
        bucket["input"] += usage["input"]
        bucket["cached"] += usage["cache_read"]
        bucket["output"] += usage["output"]
        if claude:
            bucket["input"] += usage["cache_read"] + usage["cache_w5m"] + usage["cache_w1h"]
    return bucket


def build_snapshot(cycle, generated_at, get_prices=prices, get_jev_usage=jev_usage,
                   get_billing_state=billing_state):
    """Read every required local source for one completed, fixed week."""
    required = (Path(report.PROJECTS_DIR), Path(report.CODEX_SESSIONS_DIR),
                QWEN_USAGE, Path(report.LLMJURY_SPEND_LEDGER))
    if any(not path.exists() for path in required):
        raise DataUnavailable("a required local usage source is missing")
    start = cycle["start"].astimezone(timezone.utc)
    end = cycle["end"].astimezone(timezone.utc)
    rates = get_prices()
    validate_jury_ledger(report.LLMJURY_SPEND_LEDGER)
    try:
        _, claude, _, claude_files, _ = report.scan(7, now=end, strict=True, cutoff=start)
        codex, codex_files = report.scan_codex(start, sessions_dir=report.CODEX_SESSIONS_DIR,
                                              end=end, strict=True)
    except (OSError, ValueError, TypeError) as exc:
        raise DataUnavailable(f"transcript scan failed: {type(exc).__name__}") from exc
    jury = report.llmjury_spend(7, now=end)
    if not jury["available"]:
        raise DataUnavailable("llm-jury ledger is unreadable")
    qwen = qwen_usage(start, end, QWEN_USAGE)
    jury_usage = ledger_usage(Path(report.LLMJURY_SPEND_LEDGER).read_text(), start, end)
    jev = get_jev_usage(start, end)
    local_claude = {k: v for k, v in claude.items() if report.is_local(k)}
    local_codex = {k: v for k, v in codex.items() if report.is_local(k)}
    claude_rate = rates["anthropic/claude-opus-5"]
    codex_models = [model for model in rates if model.startswith("openai/")]
    if len(codex_models) != 1:
        raise DataUnavailable("Pricing must identify exactly one OpenAI Codex baseline")
    baseline_model = codex_models[0]
    codex_rate = rates[baseline_model]
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
    comparison = [
        comparison_row("Local Codex", transcript_usage(local_codex), codex_rate),
        comparison_row("Local Claude", transcript_usage(local_claude, claude=True), codex_rate),
        comparison_row("Standalone Qwen", {"calls": qwen["requests"], "input": qwen["input"],
                       "output": qwen["output"], "cached": qwen["cached"], "actual_usd": 0.0}, codex_rate),
        comparison_row("LLM-Jury local council", jury_usage.get("ollama", usage_bucket()), codex_rate),
        comparison_row("LLM-Jury OpenRouter (historical)", jury_usage.get("openrouter", usage_bucket()), codex_rate),
        comparison_row("JEV through OpenRouter", jev, codex_rate),
    ]
    metered_spend = sum(row["actual_usd"] for row in comparison)
    subscription = {**get_billing_state(generated_at), "baseline": "same_codex_subscription",
                    "subscription_savings_usd": 0.0,
                    "attribution": "No documented routing-related subscription charge reduction"}
    unknown = {model: usage for model, usage in codex.items()
               if model == "unknown-codex" and usage["turns"]}
    if unknown:
        raise DataUnavailable("Codex usage has no attributable model")
    cloud_codex = transcript_usage({model: usage for model, usage in codex.items()
                                  if not report.is_local(model)})
    value = subscription_value(subscription, cloud_codex, comparison, codex_rate)
    return {"schema": 4, "cycle_id": cycle["id"],
            "window_start": start.isoformat(), "window_end": end.isoformat(),
            "window_start_local": cycle["start"].isoformat(),
            "window_end_local": cycle["end"].isoformat(),
            "generated_at": generated_at.astimezone(timezone.utc).isoformat(),
            "pricing_source": PRICES_URL, "pricing": rates,
            "baseline_model": baseline_model,
            "sources": {"claude_files": claude_files, "codex_files": codex_files,
                        "qwen_sessions": qwen["sessions"], "llmjury_available": True},
            "local": local,
            "subscription": subscription,
            "cloud_codex": cloud_codex,
            "subscription_value": value,
            "comparison": comparison,
            "codex_equivalent_usd": sum(row["codex_equivalent_usd"] for row in comparison),
            "actual_metered_usd": metered_spend,
            "api_equivalent_difference_usd": sum(row["net_savings_usd"] for row in comparison),
            "net_savings_usd": -metered_spend,
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
    if not snapshot or snapshot.get("schema") != 4 or snapshot.get("cycle_id") != cycle["id"]:
        return False
    try:
        generated = datetime.fromisoformat(snapshot["generated_at"])
        if generated.tzinfo is None:
            return False
        if (datetime.fromisoformat(snapshot["window_start"]) != cycle["start"]
                or datetime.fromisoformat(snapshot["window_end"]) != cycle["end"]):
            return False
        subscription = snapshot["subscription"]
        spend = snapshot["actual_metered_usd"]
        net = snapshot["net_savings_usd"]
        if (subscription["baseline"] != "same_codex_subscription"
                or subscription["subscription_savings_usd"] != 0
                or not subscription["evidence_message_id"]
                or type(spend) not in (int, float) or not math.isfinite(spend) or spend < 0
                or type(net) not in (int, float) or not math.isfinite(net)
                or not math.isclose(net, -spend, abs_tol=1e-12)):
            return False
        expected = subscription_value(subscription, snapshot["cloud_codex"],
                                      snapshot["comparison"],
                                      snapshot["pricing"][snapshot["baseline_model"]])
        if expected != snapshot["subscription_value"] or not math.isclose(
                spend, sum(row["actual_usd"] for row in snapshot["comparison"]), abs_tol=1e-12):
            return False
    except (KeyError, TypeError, ValueError, ZeroDivisionError, DataUnavailable):
        return False
    age = now - generated
    return (cycle["end"] <= generated.astimezone(PACIFIC)
            and timedelta(0) <= age <= timedelta(hours=24)
            and all(key in snapshot for key in ("comparison", "pricing", "sources")))


def collect(now):
    cycle = cycle_for(now)
    if cycle is None:
        return "No completed week awaits collection."
    STATE_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    with open(STATE_DIR / "collection.lock", "a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        state = read_json(STATE_DIR / f"delivery-{cycle['id']}.json") or {}
        if state.get("status") in ("attempted", "sent", "skipped"):
            return "Week already finalized."
        snapshot = build_snapshot(cycle, now)
        atomic_json(snapshot_path(cycle), snapshot)
        publish_snapshot(cycle)
        return f"Local snapshot saved for week ending {cycle['id']}."


def email_body(snapshot):
    start = snapshot["window_start_local"][:10]
    end = snapshot["window_end_local"][:10]
    subscription = snapshot["subscription"]
    value = snapshot["subscription_value"]
    if value["status"] == "estimated":
        headline = (f"**Estimated net Codex subscription value preserved: "
                    f"${value['net_value_usd']:,.4f}.**")
        calculation = (f"${value['weekly_nominal_usd']:,.4f} nominal weekly budget × "
                       f"{value['offloaded_share']:.4%} recorded weighted offload share = "
                       f"${value['preserved_value_usd']:,.4f} gross value; minus "
                       f"${snapshot['actual_metered_usd']:,.4f} provider spend = "
                       f"${value['net_value_usd']:,.4f} estimated net value.")
    else:
        headline = "**Subscription-value estimate unavailable: no recorded cloud Codex usage.**"
        calculation = "Offloaded tokens and provider spend are measured; missing usage is not zero."
    scheduled = (f"The billing email schedules {subscription['scheduled_plan']} for "
                 f"{subscription['scheduled_date']}. This is pending, and no evidence attributes "
                 "that plan change to local models or JEV. It adds no savings to this report."
                 if subscription.get("scheduled_plan") else "No pending plan change in the billing confirmation.")
    return "\n".join([
        f"# Weekly Codex token offloading and subscription value, {start} to {end}", "",
        headline,
        f"**Measured offloaded tokens: {value['offloaded_tokens']:,}; "
        f"recorded cloud Codex tokens: {value['cloud_tokens']:,}.**",
        f"Current billing-confirmed plan at collection: **{subscription['current_plan']}**.",
        calculation,
        f"Budget assumption: ${value['monthly_nominal_usd']:,.2f}/month, annualized as "
        "monthly × 12 ÷ 52. This uses the current tier label as nominal USD, not a verified "
        "invoice amount or the price paid during a week with plan changes.",
        scheduled, "",
        "| Recorded path | Calls | Input tokens | Cached input (included) | Output tokens | Provider spend |",
        "|---|---:|---:|---:|---:|---:|",
        *[f"| {row['label']} | {row['calls']} | {row['input']:,} | {row['cached']:,} | "
          f"{row['output']:,} | ${row['actual_usd']:,.4f} |"
          for row in snapshot["comparison"]],
        "",
        f"**Additional recorded OpenRouter/JEV/Jury spend: ${snapshot['actual_metered_usd']:,.4f}.**",
        "Subscription bill reduction attributed to offloading: $0.00.",
        f"Net recorded cash difference versus the same Codex subscription: "
        f"${snapshot['net_savings_usd']:,.4f}. Negative means added cost, not an invoice saving.",
        "",
        "Method: value preserved = weekly nominal budget × offloaded weighted workload ÷ "
        "(recorded cloud Codex weighted workload + offloaded weighted workload). All paths "
        "use the same configured Codex model's input/output/cache rates as weighting factors. "
        "This is a workload allocation estimate, not measured Codex quota saved. Raw native "
        "tokens use different tokenizers; multiple council candidates and checks are not "
        "proof that one Codex answer would consume the same tokens. Negative net value is retained.",
        "",
        "Provider spend comes from native receipts. API-equivalent values are diagnostics only "
        "and are not the dollar headline. This does not measure an avoided credit purchase, "
        "upgrade, refund, or the tier a Codex-only workflow would require. "
        "Hardware, electricity, retries without receipts, unlogged local models and router "
        "failover, uninstrumented local diff checks, and unlogged Codex frontier calls are "
        "coverage gaps. These can skew the estimated share. JEV and local-council tracking starts with the accounting "
        "release; earlier calls are unmeasured, not assumed free. JEV receipts cover the "
        "desktop and Team Nebula JEV service; unrelated OpenRouter activity is excluded.",
        f"Local snapshot: {snapshot['generated_at']}. Billing evidence: "
        f"{subscription['evidence_message_id']} at {subscription['evidence_at']}.",
    ])


def send(snapshot):
    value = snapshot["subscription_value"]
    headline = (f"${value['net_value_usd']:,.4f} estimated Codex subscription value preserved"
                if value["status"] == "estimated" else "Codex subscription value estimate unavailable")
    subject = (f"{headline}; {value['offloaded_tokens']:,} tokens offloaded "
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
