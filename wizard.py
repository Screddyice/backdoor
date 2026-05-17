#!/usr/bin/env python3
"""Backdoor — setup wizard."""

import os
import re
import sys
import time
import signal
import subprocess
from pathlib import Path

# ── ANSI ──────────────────────────────────────────────────────────────────────
G    = "\033[92m"    # bright green
DG   = "\033[32m"    # dark green
GGG  = "\033[2;32m"  # dim green
Y    = "\033[93m"    # yellow
C    = "\033[96m"    # cyan
W    = "\033[97m"    # white
DW   = "\033[2;37m"  # dim white
BLD  = "\033[1m"
DIM  = "\033[2m"
RST  = "\033[0m"
CLS  = "\033[2J\033[H"
HIDE = "\033[?25l"
SHOW = "\033[?25h"
UP   = "\033[A"
CR   = "\r"

def w(s=""):
    sys.stdout.write(str(s))
    sys.stdout.flush()

def cls():
    w(CLS)

def show_frame(art, delay=0.13):
    cls()
    w(art)
    sys.stdout.flush()
    time.sleep(delay)

def cleanup(sig=None, _=None):
    w(SHOW + RST + "\n")
    sys.exit(0)

signal.signal(signal.SIGINT, cleanup)

# ── ASCII frames ───────────────────────────────────────────────────────────────

FRAME_STARS = f"""{DW}


         ·  ˚  ·     *  ·  ˚     ·  *  ·  ˚  ·
      ˚      *         ·              *      ˚
   ·       *     ˚           ·    *       ˚       ·
      *         ·     ˚   *         ·        *
   ˚      ·         *         ˚         *       ˚
      ·      ˚    *     ·         ˚   ·      *
   *         ·       ˚      *         ·     ˚
      ˚   ·      *      ˚      ·   *     ˚    ·
   ·    *      ˚    ·      *    ˚      ·    *    ˚
      *    ·      *    ˚    ·       *     ˚    ·
   ˚      ·   *     ˚    *    ·       ˚    ·
      *         ·       ˚      *       ·       *
   ·       *     ˚           ·    *       ˚       ·
      ˚      *         ·              *      ˚
         ·  ˚  ·     *  ·  ˚     ·  *  ·  ˚  ·

{RST}"""

FRAME_EARTH = f"""{DW}
         ·  ˚  ·     *  ·  ˚     ·  *  ·  ˚  ·
      ˚      *         ·              *      ˚
   ·     {C}  _____________________________  {DW}   ·
      *  {C} /  .  ·  ˚  ·  .  ·  ˚  .  \  {DW}  ˚
   ˚    {C}/  ˚  ___---~~~~~~~~~~~---___  ˚ \{DW} ·
      · {C}|  · /    ·    *    ·    .   \ · |{DW}
   ·    {C}|   /   ·    ___   .  ·    ·  \  |{DW}   ·
      * {C}|  |   .  · /   \  ·   .  ·   | |{DW} *
   ˚    {C}|  |  ·   · \___/   ·   *  .  | |{DW}
      · {C}|   \   ·    . ·  ·    ·  .  /  |{DW}
   ·    {C} \  ˚ `---___________---` ˚  / {DW}    ·
      *  {C} \___________________________/ {DW}  *
   ˚      ·         ·    *       ˚       ·
      *         ·              *      ˚
         ·  ˚  ·     *  ·  ˚     ·  *  ·  ˚  ·
{RST}"""

FRAME_DOOR = f"""{DW}
         ·  ˚  ·     *  ·  ˚     ·  *  ·  ˚  ·
      ˚      *         ·              *      ˚
   ·     {C}  _____________________________  {DW}   ·
      *  {C} /  .  ·  ˚  ·  .  ·  ˚  .  \  {DW}  ˚
   ˚    {C}/  ˚  ___---~~~~~~~~~~~---___  ˚ \{DW} ·
      · {C}|  · /    ·    *    ·    .   \ · |{DW}
   ·    {C}|   /   · {W} ╔═══════╗ {C} .  ·    ·  \  |{DW}   ·
      * {C}|  |   . · {W} ║ BACK  ║ {C} ·   .  ·   | |{DW} *
   ˚    {C}|  |  ·   · {W} ║ DOOR  ║ {C}  ·   *  .  | |{DW}
      · {C}|   \   · · {W} ╚═══════╝ {C}·    ·  .  /  |{DW}
   ·    {C} \  ˚ `---___________---` ˚  / {DW}    ·
      *  {C} \___________________________/ {DW}  *
   ˚      ·         ·    *       ˚       ·
      *         ·              *      ˚
         ·  ˚  ·     *  ·  ˚     ·  *  ·  ˚  ·
{RST}"""

FRAME_DOOR_OPEN = f"""{DW}
         ·  ˚  ·     *  ·  ˚     ·  *  ·  ˚  ·
      ˚      *         ·              *      ˚
   ·     {C}  _____________________________  {DW}   ·
      *  {C} /  .  ·  ˚  ·  .  ·  ˚  .  \  {DW}  ˚
   ˚    {C}/  ˚  ___---~~~~~~~~~~~---___  ˚ \{DW} ·
      · {C}|  · /    ·    *    ·    .   \ · |{DW}
   ·    {C}|   /   · {GGG} ╔═══════╗ {C} .  ·    ·  \  |{DW}   ·
      * {C}|  |   . · {G}  ║{GGG}░░░░░░░{G}║  {C} ·   .  ·   | |{DW} *
   ˚    {C}|  |  ·   · {G} ║{GGG}░░░░░░░{G}║  {C}  ·   *  .  | |{DW}
      · {C}|   \   · · {GGG} ╚═══════╝ {C}·    ·  .  /  |{DW}
   ·    {C} \  ˚ `---___________---` ˚  / {DW}    ·
      *  {C} \___________________________/ {DW}  *
   ˚    {G}       ░░░    ░░░    ░░░         {DW}    ·
      *  {G}    ░░░░░░░░░░░░░░░░░░░░     {DW}  ˚
         ·  ˚  ·     *  ·  ˚     ·  *  ·  ˚  ·
{RST}"""

FRAME_SMOKE = f"""{DW}
         ·  ˚  ·     *  ·  ˚     ·  *  ·  ˚  ·
      ˚      *         ·              *      ˚
   ·     {C}  _____________________________  {DW}   ·
      *  {C} /  .  ·  ˚  ·  .  ·  ˚  .  \  {DW}  ˚
   ˚    {C}/  ˚  ___---~~~~~~~~~~~---___  ˚ \{DW} ·
      · {C}|  · /    ·    *    ·    .   \ · |{DW}
   ·    {C}|   /   · {GGG} ╔═══════╗ {C} .  ·    ·  \  |{DW}   ·
      * {C}|  |   . · {G}  ║{GGG}▒▒▒▒▒▒▒{G}║  {C} ·   .  ·   | |{DW} *
   ˚    {C}|  |  ·   · {G} ║{GGG}▒▒▒▒▒▒▒{G}║  {C}  ·   *  .  | |{DW}
      · {C}|   \   · · {GGG} ╚═══════╝ {C}·    ·  .  /  |{DW}
   ·    {C} \  ˚ `---___________---` ˚  / {DW}    ·
      *  {C} \___________________________/ {DW}  *
        {G}        ▒▒░   ░░▒▒▒   ░▒▒░        {DW}
     {G}      ░▒▒▓▒░░ ▒▒░  ░▒▒░ ▒▓▒▒░░       {DW}
     {G}    ░░░░  ░    .·      .   ░░░░░      {DW}
{RST}"""

FRAME_GENIE_RISING = f"""{DW}
         ·  ˚  ·     *  ·  ˚     ·  *  ·  ˚  ·{RST}
{G}                       .-~~~-.
                      (  ^ ^ )
                       |  ω  |
                        \   /
                     .---`-`---.{DW}
   ·     {C}  _____________________________  {DW}   ·
      *  {C} /  .  ·  ˚  ·  .  ·  ˚  .  \  {DW}  ˚
   ˚    {C}/  ˚  ___---~~~~~~~~~~~---___  ˚ \{DW} ·
      · {C}|  · /    ·    *    ·    .   \ · |{DW}
   ·    {C}|   /   · {GGG} ╔═══════╗ {C} .  ·    ·  \  |{DW}   ·
      * {C}|  |   . · {G}  ║{GGG}▓▓▓▓▓▓▓{G}║  {C} ·   .  ·   | |{DW} *
   ˚    {C}|  |  ·   · {G} ║{GGG}▓▓▓▓▓▓▓{G}║  {C}  ·   *  .  | |{DW}
      · {C}|   \   · · {GGG} ╚═══════╝ {C}·    ·  .  /  |{DW}
   ·    {C} \  ˚ `---___________---` ˚  / {DW}    ·
      *  {C} \___________________________/ {DW}  *
        {G}        ▓▓▒   ▒▒▓▓▓   ▒▓▓▒        {DW}
     {G}      ▒▓▓▓▓▒▒▒ ▒▓▒  ▒▓▓▒ ▒▓▓▓▒▒       {DW}
{RST}"""

FRAME_GENIE_FULL = f"""{G}

                    ░░░   ░░░   ░░░
                  ░  ·  ░  ·  ░  ·  ░
                    ░░░░░░░░░░░░░░░

                    ╭───────────╮
                   ╱  ◉       ◉  ╲
                  │    ╰─────╯    │
                  │               │
                   ╲      ω      ╱
                    ╰───────────╯
                   /  │       │  \\
                  /   │       │   \\
                 ╱    │       │    ╲
              ░░░░░░░░░░░░░░░░░░░░░░░
            ░░░░░░░░░░░░░░░░░░░░░░░░░░
          ░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░
        ░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░
{RST}"""

FRAME_GENIE_SPEAK = f"""{G}

                    ░░░   ░░░   ░░░
                  ░  ·  ░  ·  ░  ·  ░
                    ░░░░░░░░░░░░░░░

                    ╭───────────╮
                   ╱  ◉       ◉  ╲
                  │    ╰─────╯    │   {BLD}{W}╔══════════════════════════════╗{RST}{G}
                  │               │   {BLD}{W}║                              ║{RST}{G}
                   ╲      ω      ╱    {BLD}{W}║   Your wish is my command.   ║{RST}{G}
                    ╰───────────╯     {BLD}{W}║                              ║{RST}{G}
                   /  │       │  \\   {BLD}{W}╚══════════════════════════════╝{RST}{G}
                  /   │       │   \\
                 ╱    │       │    ╲
              ░░░░░░░░░░░░░░░░░░░░░░░
            ░░░░░░░░░░░░░░░░░░░░░░░░░░
          ░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░
        ░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░
{RST}"""

GENIE_PROMPT = f"""{G}

                    ░░░   ░░░   ░░░
                  ░  ·  ░  ·  ░  ·  ░
                    ░░░░░░░░░░░░░░░

                    ╭───────────╮
                   ╱  ◉       ◉  ╲
                  │    ╰─────╯    │
                  │               │
                   ╲      ω      ╱
                    ╰───────────╯
                   /  │       │  \\
                  /   │       │   \\
                 ╱    │       │    ╲
              ░░░░░░░░░░░░░░░░░░░░░░░
            ░░░░░░░░░░░░░░░░░░░░░░░░░░
          ░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░
        ░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░
{RST}"""

# ── Providers ─────────────────────────────────────────────────────────────────

PROVIDERS = [
    {
        "name":    "DeepSeek",
        "url":     "https://api.deepseek.com/v1",
        "model":   "deepseek-chat",
        "note":    "Cheapest. ~2,000x less than Anthropic at scale.",
        "key_url": "https://platform.deepseek.com",
    },
    {
        "name":    "Groq",
        "url":     "https://api.groq.com/openai/v1",
        "model":   "llama-3.3-70b-versatile",
        "note":    "Fastest inference. Free tier available.",
        "key_url": "https://console.groq.com",
    },
    {
        "name":    "NVIDIA NIM",
        "url":     "https://integrate.api.nvidia.com/v1",
        "model":   "meta/llama-3.3-70b-instruct",
        "note":    "Free tier. Llama 3.3 70B.",
        "key_url": "https://build.nvidia.com",
    },
    {
        "name":    "OpenRouter",
        "url":     "https://openrouter.ai/api/v1",
        "model":   "meta-llama/llama-3.3-70b-instruct",
        "note":    "200+ models. One key.",
        "key_url": "https://openrouter.ai",
    },
    {
        "name":    "Ollama (local)",
        "url":     "http://localhost:11434/v1",
        "model":   "llama3.3",
        "note":    "Runs on your machine. No API key needed.",
        "key_url": None,
    },
    {
        "name":    "LM Studio (local)",
        "url":     "http://localhost:1234/v1",
        "model":   "",
        "note":    "Runs on your machine. No API key needed.",
        "key_url": None,
    },
    {
        "name":    "Custom",
        "url":     "",
        "model":   "",
        "note":    "Enter your own OpenAI-compatible endpoint.",
        "key_url": None,
    },
]

# ── Animation ─────────────────────────────────────────────────────────────────

def animate():
    w(HIDE)
    show_frame(FRAME_STARS,       0.4)
    show_frame(FRAME_STARS,       0.3)
    show_frame(FRAME_EARTH,       0.5)
    show_frame(FRAME_EARTH,       0.4)
    show_frame(FRAME_DOOR,        0.5)
    show_frame(FRAME_DOOR,        0.4)
    show_frame(FRAME_DOOR_OPEN,   0.3)
    show_frame(FRAME_SMOKE,       0.25)
    show_frame(FRAME_SMOKE,       0.2)
    show_frame(FRAME_GENIE_RISING,0.3)
    show_frame(FRAME_GENIE_RISING,0.25)
    show_frame(FRAME_GENIE_FULL,  0.4)
    show_frame(FRAME_GENIE_FULL,  0.3)
    show_frame(FRAME_GENIE_SPEAK, 0.5)
    time.sleep(1.2)
    w(SHOW)

# ── Wizard ────────────────────────────────────────────────────────────────────

def genie_ask(question):
    cls()
    w(GENIE_PROMPT)
    w(f"  {G}{BLD}  {question}{RST}\n")
    w(f"  {G}❯ {RST}")
    return input().strip()

def genie_say(line):
    w(f"\n  {G}  {line}{RST}\n")
    time.sleep(0.4)

def pick_provider():
    cls()
    w(GENIE_PROMPT)
    w(f"\n  {G}{BLD}  Choose your provider:{RST}\n\n")
    for i, p in enumerate(PROVIDERS, 1):
        w(f"  {G}  {BLD}[{i}]{RST}  {W}{p['name']}{RST}  {DIM}{p['note']}{RST}\n")
    w(f"\n  {G}❯ {RST}")
    raw = input().strip()
    try:
        idx = int(raw) - 1
        if 0 <= idx < len(PROVIDERS):
            return PROVIDERS[idx]
    except ValueError:
        pass
    return PROVIDERS[0]

def run_wizard():
    animate()

    # Name
    name = genie_ask("What should I call you?")
    if not name:
        name = "Builder"

    genie_say(f"Good to meet you, {name}.")
    time.sleep(0.3)

    # Provider
    provider = pick_provider()

    # Model
    cls()
    w(GENIE_PROMPT)
    w(f"\n  {G}{BLD}  Provider: {W}{provider['name']}{RST}\n")

    if provider["model"]:
        w(f"\n  {G}  Model [{W}{provider['model']}{G}] — press Enter to confirm or type another:{RST}\n")
        w(f"  {G}❯ {RST}")
        model_input = input().strip()
        model = model_input if model_input else provider["model"]
    else:
        w(f"\n  {G}{BLD}  Model name:{RST}\n")
        w(f"  {G}❯ {RST}")
        model = input().strip()

    # API Key
    if provider["key_url"]:
        cls()
        w(GENIE_PROMPT)
        w(f"\n  {G}{BLD}  API Key  {DIM}(get one at {provider['key_url']}){RST}\n")
        w(f"  {G}❯ {RST}")
        api_key = input().strip()
        if not api_key:
            w(f"\n  {Y}  No key entered — you can add it to .env later.{RST}\n")
            api_key = "your-api-key-here"
    else:
        api_key = "local"

    # Base URL (custom provider)
    base_url = provider["url"]
    if provider["name"] == "Custom":
        cls()
        w(GENIE_PROMPT)
        w(f"\n  {G}{BLD}  Provider URL:{RST}\n")
        w(f"  {G}❯ {RST}")
        base_url = input().strip()

    # Confirm
    cls()
    w(GENIE_PROMPT)
    w(f"\n  {G}{BLD}  Ready to go, {name}.{RST}\n\n")
    w(f"  {DW}  Provider  {RST}{W}{provider['name']}{RST}\n")
    w(f"  {DW}  Model     {RST}{W}{model}{RST}\n")
    w(f"  {DW}  Endpoint  {RST}{W}{base_url}{RST}\n\n")
    w(f"  {G}  Press Enter to launch — or Ctrl+C to bail.{RST}\n")
    w(f"  {G}❯ {RST}")
    input()

    # Write .env
    script_dir = Path(__file__).parent
    env_path = script_dir / ".env"
    env_content = f"""PROVIDER_BASE_URL={base_url}
PROVIDER_API_KEY={api_key}
PROVIDER_MODEL={model}
PROVIDER_MAX_TOKENS=32768
PROVIDER_TEMPERATURE=1.0
PROVIDER_TOP_P=1.0

HOST=127.0.0.1
PORT=8082
LOG_FILE=proxy.log

SKIP_QUOTA_PROBES=true
SKIP_TITLE_GENERATION=true
SKIP_SUGGESTION_MODE=true
MOCK_PREFIX_DETECTION=true
MOCK_FILEPATH_EXTRACTION=true

CLAUDE_WORKSPACE=./workspace
MAX_CLI_SESSIONS=5
"""
    env_path.write_text(env_content)

    # Launch
    cls()
    w(GENIE_PROMPT)
    w(f"\n  {G}{BLD}  .env written. Firing up Backdoor...{RST}\n\n")
    time.sleep(0.8)

    # Hand off to run.sh
    run_sh = script_dir / "run.sh"
    os.execv("/bin/bash", ["/bin/bash", str(run_sh)])


if __name__ == "__main__":
    run_wizard()
