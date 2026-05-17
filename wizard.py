#!/usr/bin/env python3
"""Backdoor — animated onboarding wizard."""

import os
import re
import sys
import time
import math
import random
import signal
from pathlib import Path

# ─── Terminal size ─────────────────────────────────────────────────────────────

def _tsize():
    try:
        s = os.get_terminal_size()
        return max(s.columns, 80), max(s.lines, 24)
    except:
        return 80, 24

TW, TH = _tsize()

# ─── ANSI ─────────────────────────────────────────────────────────────────────

RST  = "\033[0m"
HIDE = "\033[?25l"
SHOW = "\033[?25h"

BG   = "\033[92m"    # bright green
DG   = "\033[32m"    # dark green
DDG  = "\033[2;32m"  # dim green
BW   = "\033[97m"    # bright white
DW   = "\033[2;37m"  # dim white
CY   = "\033[96m"    # cyan
YL   = "\033[93m"    # yellow
BLD  = "\033[1m"
DIM  = "\033[2m"
G    = BG
GGG  = DDG
W    = BW
C    = CY
Y    = YL

def w(s=""):
    sys.stdout.write(str(s))
    sys.stdout.flush()

def goto(r, c):
    return f"\033[{r+1};{c+1}H"

def cleanup(sig=None, _=None):
    w(SHOW + RST + "\n")
    sys.exit(0)

signal.signal(signal.SIGINT, cleanup)

# ─── Framebuffer ──────────────────────────────────────────────────────────────

class Screen:
    __slots__ = ('w', 'h', 'cur', 'prev')

    def __init__(self, width, height):
        self.w = width
        self.h = height
        blank = [(' ', '') for _ in range(width)]
        self.cur  = [list(blank) for _ in range(height)]
        self.prev = [[None] * width for _ in range(height)]

    def put(self, r, c, ch, col=''):
        if 0 <= r < self.h and 0 <= c < self.w:
            self.cur[r][c] = (ch, col)

    def clear(self):
        for row in self.cur:
            for c in range(self.w):
                row[c] = (' ', '')

    def flip(self):
        parts = []
        for r in range(self.h):
            cr = self.cur[r]
            pr = self.prev[r]
            for c in range(self.w):
                cell = cr[c]
                if cell != pr[c]:
                    ch, col = cell
                    parts.append(f"{goto(r,c)}{col}{ch}{RST if col else ''}")
                    pr[c] = cell
        if parts:
            sys.stdout.write(''.join(parts))
            sys.stdout.flush()

# ─── Digital rain ─────────────────────────────────────────────────────────────

RAIN_CHARS = list(
    "01アイウエカキクケコサシスセソタチツテトナニヌネノハヒフヘホ"
    "{}[]<>/\\!@#%^&*+-=~`|"
)

class RainCol:
    __slots__ = ('x', 'y', 'speed', 'length', 'chars', 'tick')

    def __init__(self, x, start_spread=True):
        self.x      = x
        self.y      = random.uniform(-TH, TH) if start_spread else random.uniform(-TH, 0)
        self.speed  = random.uniform(9, 24)
        self.length = random.randint(6, 18)
        self.chars  = [random.choice(RAIN_CHARS) for _ in range(22)]
        self.tick   = 0

    def step(self, dt):
        self.y += self.speed * dt
        self.tick += 1
        if self.tick % 3 == 0:
            self.chars[random.randrange(len(self.chars))] = random.choice(RAIN_CHARS)
        if self.y > TH + self.length + 2:
            self.y      = random.uniform(-self.length - 2, -1)
            self.speed  = random.uniform(9, 24)
            self.length = random.randint(6, 18)

    def draw(self, screen, t):
        head = int(self.y)
        # Two overlapping sine waves create a ripple across columns and time
        wave = (
            math.sin(t * 2.8 + self.x * 0.35) * 0.4
            + math.sin(t * 1.1 + self.x * 0.7) * 0.3
            + 0.7
        )  # ~0.0 .. 1.4, typically 0.3..1.1
        for i in range(self.length):
            row = head - i
            if not (0 <= row < screen.h):
                continue
            ch    = self.chars[i % len(self.chars)]
            depth = i / max(1, self.length - 1)  # 0=head 1=tail
            if i == 0:
                col = BG                                    # bright head always
            elif i <= 2:
                col = BG if wave > 0.7 else DG
            elif depth < 0.4:
                col = DG if wave > 0.5 else DDG
            else:
                col = DDG
            screen.put(row, self.x, ch, col)
        # Erase one row below tail to clean up
        clear_r = head - self.length
        if 0 <= clear_r < screen.h:
            screen.put(clear_r, self.x, ' ', '')

# ─── Smoke particles ──────────────────────────────────────────────────────────

SMOKE_STAGES = ['▓', '▒', '░', '·', '˙', '.', ' ']

class Smoke:
    __slots__ = ('x', 'y', 'vx', 'vy', 'life', 'decay')

    def __init__(self, x, y, vx=None, vy=None):
        self.x     = float(x) + random.uniform(-0.5, 0.5)
        self.y     = float(y)
        self.vx    = vx if vx is not None else random.uniform(-1.2, 1.2)
        self.vy    = vy if vy is not None else random.uniform(-3.5, -1.0)
        self.life  = 1.0
        self.decay = random.uniform(0.22, 0.5)

    def step(self, dt):
        self.x   += self.vx * dt
        self.y   += self.vy * dt
        self.vx  += random.uniform(-0.3, 0.3) * dt * 6
        self.vy  += 0.4 * dt   # slight gravity pulls wisps back
        self.life -= self.decay * dt

    @property
    def alive(self):
        return self.life > 0.04

    def draw(self, screen):
        r, c = int(self.y), int(self.x)
        idx  = min(len(SMOKE_STAGES) - 1, int((1.0 - self.life) * len(SMOKE_STAGES)))
        ch   = SMOKE_STAGES[idx]
        if self.life > 0.7:
            col = BW
        elif self.life > 0.45:
            col = DG
        elif self.life > 0.2:
            col = DDG
        else:
            col = ''
        screen.put(r, c, ch, col)

# ─── BACKDOOR ASCII art ───────────────────────────────────────────────────────

ART = [
    "██████╗  █████╗  ██████╗██╗  ██╗██████╗  ██████╗  ██████╗ ██████╗ ",
    "██╔══██╗██╔══██╗██╔════╝██║ ██╔╝██╔══██╗██╔═══██╗██╔═══██╗██╔══██╗",
    "██████╔╝███████║██║     █████╔╝ ██║  ██║██║   ██║██║   ██║██████╔╝",
    "██╔══██╗██╔══██║██║     ██╔═██╗ ██║  ██║██║   ██║██║   ██║██╔══██╗",
    "██████╔╝██║  ██║╚██████╗██║  ██╗██████╔╝╚██████╔╝╚██████╔╝██║  ██║",
    "╚═════╝ ╚═╝  ╚═╝ ╚═════╝╚═╝  ╚═╝╚═════╝  ╚═════╝  ╚═════╝ ╚═╝  ╚═╝",
]
SUBTITLE = "run claude code.  bring your own model."

ART_H = len(ART)
ART_W = max(len(line) for line in ART)

def art_origin():
    r0 = max(1, TH // 2 - ART_H // 2 - 1)
    c0 = max(0, TW // 2 - ART_W // 2)
    return r0, c0

def draw_art(screen, r0, c0, t, phase):
    """phase 0→1 = decoding scramble → fully revealed. >1 = pulse mode."""
    for ar, line in enumerate(ART):
        for ac, ch in enumerate(line):
            if ch == ' ':
                continue
            r = r0 + ar
            c = c0 + ac
            if phase >= 1.0:
                # Travelling brightness wave across the letters
                pulse = math.sin(t * 5.0 - ac * 0.18 + ar * 0.4) * 0.5 + 0.5
                col   = BG if pulse > 0.2 else DG
                screen.put(r, c, ch, col)
            else:
                # Each cell resolves left-to-right, top-to-bottom
                thresh = (ar * ART_W + ac) / (ART_H * ART_W)
                if phase > thresh:
                    screen.put(r, c, ch, BG)
                else:
                    screen.put(r, c, random.choice(RAIN_CHARS), DDG)

def draw_subtitle(screen, r0, c0, t, alpha):
    if alpha <= 0:
        return
    sub_r = r0 + ART_H + 1
    sub_c = c0 + ART_W // 2 - len(SUBTITLE) // 2
    col   = DG if alpha < 0.6 else BG
    for i, ch in enumerate(SUBTITLE):
        screen.put(sub_r, sub_c + i, ch, col)

# ─── Main animation ───────────────────────────────────────────────────────────

def animate():
    screen = Screen(TW, TH)

    # Rain: one column every 2 chars across full width
    drops = [RainCol(x, start_spread=True) for x in range(0, TW, 2)]

    smoke: list[Smoke] = []
    art_r0, art_c0 = art_origin()

    # Timeline (seconds)
    T_RAIN   = 1.6   # pure digital rain
    T_DECODE = 3.4   # rain + BACKDOOR decoding in
    T_SMOKE  = 5.2   # smoke billows, rain fades
    T_HOLD   = 6.8   # everything settled
    T_END    = 7.6

    FPS = 22
    dt  = 1.0 / FPS
    t   = 0.0

    w(HIDE + "\033[2J\033[H")

    while t < T_END:
        t0 = time.monotonic()
        screen.clear()

        # ── Rain fade-out curve ────────────────────────────────────────────
        if t < T_SMOKE:
            rain_on = True
        else:
            fade_frac = (t - T_SMOKE) / (T_HOLD - T_SMOKE)
            rain_on   = random.random() > min(1.0, fade_frac * 1.4)

        for d in drops:
            d.step(dt)
            if rain_on:
                d.draw(screen, t)

        # ── BACKDOOR decode ────────────────────────────────────────────────
        if t >= T_RAIN:
            if t < T_DECODE:
                phase = (t - T_RAIN) / (T_DECODE - T_RAIN)
            else:
                phase = 1.0
            draw_art(screen, art_r0, art_c0, t, phase)

        # ── Smoke ──────────────────────────────────────────────────────────
        if t >= T_DECODE:
            smoke_age = t - T_DECODE

            # Bottom billow — main source
            burst = max(1, int(smoke_age * 2.5))
            for _ in range(min(burst, 6)):
                sx = art_c0 + random.randint(2, ART_W - 2)
                smoke.append(Smoke(sx, art_r0 + ART_H, vy=random.uniform(-4, -1.5)))

            # Left fringe
            if random.random() < 0.5:
                smoke.append(Smoke(
                    art_c0 - 2,
                    art_r0 + random.randint(1, ART_H - 1),
                    vx=random.uniform(-1.5, -0.2),
                    vy=random.uniform(-2.0, -0.5),
                ))
            # Right fringe
            if random.random() < 0.5:
                smoke.append(Smoke(
                    art_c0 + ART_W + 1,
                    art_r0 + random.randint(1, ART_H - 1),
                    vx=random.uniform(0.2, 1.5),
                    vy=random.uniform(-2.0, -0.5),
                ))
            # Top wisps drift upward
            if random.random() < 0.3:
                smoke.append(Smoke(
                    art_c0 + random.randint(0, ART_W - 1),
                    art_r0 - 1,
                    vy=random.uniform(-2.5, -1.0),
                ))

            smoke = [p for p in smoke if p.alive]
            for p in smoke:
                p.step(dt)
                p.draw(screen)

            # Art always on top of smoke
            draw_art(screen, art_r0, art_c0, t, 1.0)

            # Subtitle fades in
            sub_alpha = min(1.0, (t - T_DECODE) / (T_SMOKE - T_DECODE))
            draw_subtitle(screen, art_r0, art_c0, t, sub_alpha)

        screen.flip()

        elapsed = time.monotonic() - t0
        time.sleep(max(0.0, dt - elapsed))
        t += dt

    w(SHOW)

# ─── Providers ────────────────────────────────────────────────────────────────

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

# ─── Genie wizard UI ──────────────────────────────────────────────────────────

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

def cls():
    sys.stdout.write("\033[2J\033[H")
    sys.stdout.flush()

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

# ─── Wizard flow ──────────────────────────────────────────────────────────────

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
    env_path   = script_dir / ".env"
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

    run_sh = script_dir / "run.sh"
    os.execv("/bin/bash", ["/bin/bash", str(run_sh)])


if __name__ == "__main__":
    run_wizard()
