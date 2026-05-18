#!/usr/bin/env python3
"""Backdoor health check — verify the configured provider is reachable."""

import os
import socket
import sys
import time
from pathlib import Path
from urllib.parse import urlparse

# ── ANSI colours ─────────────────────────────────────────────────────────────
G  = "\033[92m"   # green
R  = "\033[91m"   # red
Y  = "\033[93m"   # yellow
DIM = "\033[2m"
RST = "\033[0m"

def ok(msg):  print(f"  {G}✓{RST} {msg}")
def err(msg): print(f"  {R}✗{RST} {msg}")
def warn(msg): print(f"  {Y}⚠{RST} {msg}")
def dim(msg): print(f"  {DIM}{msg}{RST}")

# ── Load .env ─────────────────────────────────────────────────────────────────
def load_env(path: Path) -> dict:
    cfg = {}
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        cfg[key.strip()] = val.strip()
    return cfg

env_path = Path(__file__).parent / ".env"
if not env_path.exists():
    err(".env not found — run ./backdoor first to configure your provider")
    sys.exit(1)

cfg = load_env(env_path)

base_url  = cfg.get("PROVIDER_BASE_URL", "").rstrip("/")
api_key   = cfg.get("PROVIDER_API_KEY", "")
model     = cfg.get("PROVIDER_MODEL", "unknown")
port      = int(cfg.get("PORT", 8082))
host      = cfg.get("HOST", "127.0.0.1")

if not base_url:
    err("PROVIDER_BASE_URL is not set in .env")
    sys.exit(1)

provider_host = urlparse(base_url).netloc or base_url
dim(f"checking {model} @ {provider_host}...")

# ── Provider connectivity ─────────────────────────────────────────────────────
try:
    import httpx
except ImportError:
    err("httpx not installed — run: uv pip install httpx")
    sys.exit(1)

url     = f"{base_url}/chat/completions"
headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
payload = {
    "model": model,
    "messages": [{"role": "user", "content": "hi"}],
    "max_tokens": 1,
    "stream": False,
}

t0 = time.monotonic()
try:
    with httpx.Client(timeout=15) as client:
        r = client.post(url, headers=headers, json=payload)
    rtt_ms = int((time.monotonic() - t0) * 1000)

    if r.status_code == 200:
        ok(f"connected  ({rtt_ms}ms)")
    else:
        body = r.text[:200].replace("\n", " ")
        err(f"provider returned HTTP {r.status_code}: {body}")
        sys.exit(1)

except httpx.ConnectError as e:
    err(f"cannot reach {provider_host} — {e}")
    sys.exit(1)
except httpx.TimeoutException:
    err(f"request timed out after 15s")
    sys.exit(1)
except Exception as e:
    err(f"unexpected error: {e}")
    sys.exit(1)

# ── Port check ────────────────────────────────────────────────────────────────
with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
    s.settimeout(0.5)
    in_use = s.connect_ex((host, port)) == 0

if in_use:
    warn(f"port {port} is already in use — stop the existing process first")
else:
    ok(f"port {port} is free")

print(f"\n  {G}ready to launch{RST}\n")
