#!/usr/bin/env python3
"""Backdoor — animated onboarding wizard."""

import os, sys, time, math, random, signal, tty, termios, json
from pathlib import Path

# ─── Terminal ──────────────────────────────────────────────────────────────────

def _tsize():
    try:
        s = os.get_terminal_size()
        return max(s.columns, 80), max(s.lines, 24)
    except Exception:
        return 80, 24

TW, TH = _tsize()

# ─── ANSI ─────────────────────────────────────────────────────────────────────

RST  = "\033[0m"
HIDE = "\033[?25l"
SHOW = "\033[?25h"
BG   = "\033[92m"
DG   = "\033[32m"
DDG  = "\033[2;32m"
BW   = "\033[97m"
DW   = "\033[2;37m"
YL   = "\033[93m"
BLD  = "\033[1m"
DIM  = "\033[2m"
G = BG; W = BW; Y = YL

def w(s=""):
    sys.stdout.write(str(s)); sys.stdout.flush()

def goto(r, c):
    return f"\033[{r+1};{c+1}H"

def cls():
    sys.stdout.write("\033[2J\033[H"); sys.stdout.flush()

def cleanup(sig=None, _=None):
    w(SHOW + RST + "\n"); sys.exit(0)

signal.signal(signal.SIGINT, cleanup)

# ─── Single-keypress input ────────────────────────────────────────────────────

def getch():
    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    try:
        tty.setraw(fd)
        ch = sys.stdin.read(1)
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)
    if ch == '\x03':
        cleanup()
    return ch

# ─── Framebuffer ──────────────────────────────────────────────────────────────

class Screen:
    __slots__ = ('w', 'h', 'cur', 'prev')

    def __init__(self, width, height):
        self.w = width; self.h = height
        self.cur  = [[(' ', '') for _ in range(width)] for _ in range(height)]
        self.prev = [[None] * width for _ in range(height)]

    def put(self, r, c, ch, col=''):
        if 0 <= r < self.h and 0 <= c < self.w:
            self.cur[r][c] = (ch, col)

    def clear(self):
        for row in self.cur:
            for c in range(self.w): row[c] = (' ', '')

    def flip(self):
        parts = []
        for r in range(self.h):
            cr, pr = self.cur[r], self.prev[r]
            for c in range(self.w):
                cell = cr[c]
                if cell != pr[c]:
                    ch, col = cell
                    parts.append(f"{goto(r,c)}{col}{ch}{RST if col else ''}")
                    pr[c] = cell
        if parts:
            sys.stdout.write(''.join(parts)); sys.stdout.flush()

# ─── Digital rain ─────────────────────────────────────────────────────────────

RAIN_CHARS = list(
    "01アイウエカキクケコサシスセソタチツテトナニヌネノハヒフヘホ"
    "{}[]<>/\\!@#%^&*+-=~`|"
)

class RainCol:
    __slots__ = ('x', 'y', 'speed', 'length', 'chars', 'tick')

    def __init__(self, x):
        self.x = x
        self.y = random.uniform(-TH, TH)
        self.speed = random.uniform(9, 24)
        self.length = random.randint(6, 18)
        self.chars = [random.choice(RAIN_CHARS) for _ in range(22)]
        self.tick = 0

    def step(self, dt):
        self.y += self.speed * dt
        self.tick += 1
        if self.tick % 3 == 0:
            self.chars[random.randrange(len(self.chars))] = random.choice(RAIN_CHARS)
        if self.y > TH + self.length + 2:
            self.y = random.uniform(-self.length - 2, -1)
            self.speed = random.uniform(9, 24)
            self.length = random.randint(6, 18)

    def draw(self, screen, t):
        head = int(self.y)
        wave = (math.sin(t * 2.8 + self.x * 0.35) * 0.4
              + math.sin(t * 1.1 + self.x * 0.7) * 0.3 + 0.7)
        for i in range(self.length):
            row = head - i
            if not (0 <= row < screen.h): continue
            ch    = self.chars[i % len(self.chars)]
            depth = i / max(1, self.length - 1)
            if i <= 2:          col = BG if wave > 0.7 else DG
            elif depth < 0.4:   col = DG if wave > 0.5 else DDG
            else:               col = DDG
            screen.put(row, self.x, ch, col)
        clear_r = head - self.length
        if 0 <= clear_r < screen.h: screen.put(clear_r, self.x, ' ', '')

# ─── Smoke particles ──────────────────────────────────────────────────────────

_SMOKE = ['▓', '▒', '░', '·', '˙', '.', ' ']

class Smoke:
    __slots__ = ('x', 'y', 'vx', 'vy', 'life', 'decay')

    def __init__(self, x, y, vx=None, vy=None):
        self.x    = float(x) + random.uniform(-0.5, 0.5)
        self.y    = float(y)
        self.vx   = vx if vx is not None else random.uniform(-1.2, 1.2)
        self.vy   = vy if vy is not None else random.uniform(-3.5, -1.0)
        self.life = 1.0
        self.decay = random.uniform(0.22, 0.5)

    def step(self, dt):
        self.x += self.vx * dt; self.y += self.vy * dt
        self.vx += random.uniform(-0.3, 0.3) * dt * 6
        self.vy += 0.4 * dt; self.life -= self.decay * dt

    @property
    def alive(self): return self.life > 0.04

    def draw(self, screen):
        r, c = int(self.y), int(self.x)
        idx = min(len(_SMOKE)-1, int((1-self.life) * len(_SMOKE)))
        col = BW if self.life > 0.7 else (DG if self.life > 0.45 else (DDG if self.life > 0.2 else ''))
        screen.put(r, c, _SMOKE[idx], col)

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
ART_H = len(ART); ART_W = max(len(l) for l in ART)

def _art_origin():
    return max(1, TH//2 - ART_H//2 - 1), max(0, TW//2 - ART_W//2)

def _draw_art(screen, r0, c0, t, phase):
    for ar, line in enumerate(ART):
        for ac, ch in enumerate(line):
            if ch == ' ': continue
            r = r0 + ar; c = c0 + ac
            if phase >= 1.0:
                pulse = math.sin(t * 5.0 - ac * 0.18 + ar * 0.4) * 0.5 + 0.5
                screen.put(r, c, ch, BG if pulse > 0.2 else DG)
            else:
                thresh = (ar * ART_W + ac) / (ART_H * ART_W)
                if phase > thresh: screen.put(r, c, ch, BG)
                else: screen.put(r, c, random.choice(RAIN_CHARS), DDG)

def _draw_subtitle(screen, r0, c0, alpha):
    if alpha <= 0: return
    sr = r0 + ART_H + 1
    sc = c0 + ART_W//2 - len(SUBTITLE)//2
    col = DG if alpha < 0.6 else BG
    for i, ch in enumerate(SUBTITLE): screen.put(sr, sc+i, ch, col)

# ─── Phase 1: BACKDOOR animation ──────────────────────────────────────────────

def animate_backdoor():
    screen = Screen(TW, TH)
    drops = [RainCol(x) for x in range(0, TW, 2)]
    smoke: list[Smoke] = []
    art_r0, art_c0 = _art_origin()

    T_RAIN = 1.6; T_DECODE = 3.4; T_SMOKE = 5.2; T_HOLD = 6.8; T_END = 7.6
    dt = 1.0/22; t = 0.0

    w(HIDE + "\033[2J\033[H")

    while t < T_END:
        t0 = time.monotonic()
        screen.clear()

        rain_on = t < T_SMOKE or random.random() > min(1.0, (t-T_SMOKE)/(T_HOLD-T_SMOKE)*1.4)
        for d in drops:
            d.step(dt)
            if rain_on: d.draw(screen, t)

        if t >= T_RAIN:
            phase = min(1.0, (t-T_RAIN)/(T_DECODE-T_RAIN)) if t < T_DECODE else 1.0
            _draw_art(screen, art_r0, art_c0, t, phase)

        if t >= T_DECODE:
            age = t - T_DECODE
            for _ in range(min(int(age*2.5)+1, 6)):
                smoke.append(Smoke(art_c0+random.randint(2, ART_W-2), art_r0+ART_H, vy=random.uniform(-4,-1.5)))
            if random.random() < 0.5:
                smoke.append(Smoke(art_c0-2, art_r0+random.randint(1,ART_H-1), vx=random.uniform(-1.5,-0.2), vy=random.uniform(-2,-0.5)))
            if random.random() < 0.5:
                smoke.append(Smoke(art_c0+ART_W+1, art_r0+random.randint(1,ART_H-1), vx=random.uniform(0.2,1.5), vy=random.uniform(-2,-0.5)))
            if random.random() < 0.3:
                smoke.append(Smoke(art_c0+random.randint(0,ART_W-1), art_r0-1, vy=random.uniform(-2.5,-1)))
            smoke = [p for p in smoke if p.alive]
            for p in smoke: p.step(dt); p.draw(screen)
            _draw_art(screen, art_r0, art_c0, t, 1.0)
            _draw_subtitle(screen, art_r0, art_c0, min(1.0, (t-T_DECODE)/(T_SMOKE-T_DECODE)))

        screen.flip()
        time.sleep(max(0.0, dt - (time.monotonic() - t0)))
        t += dt

# ─── Phase 2: 3D spinning torus → genie rises ────────────────────────────────

_TORUS_CHARS = ' .·:;+=xX$&#@'

def _torus_frame(A, B, tw, th):
    zbuf = [0.0] * (tw * th)
    out  = [(' ', '')] * (tw * th)
    theta = 0.0
    while theta < 6.2832:
        cosT = math.cos(theta); sinT = math.sin(theta)
        phi = 0.0
        while phi < 6.2832:
            cosP = math.cos(phi); sinP = math.sin(phi)
            cosA = math.cos(A);   sinA = math.sin(A)
            cosB = math.cos(B);   sinB = math.sin(B)
            h = cosT + 2
            D = 1.0 / (sinP * h * sinA + sinT * cosA + 5)
            t_ = sinP * h * cosA - sinT * sinA
            xp = int(tw/2 + (tw * 0.38) * D * (cosP * h * cosB - t_ * sinB))
            yp = int(th/2 + (th * 0.42) * D * (cosP * h * sinB + t_ * cosB))
            if 0 <= xp < tw and 0 <= yp < th:
                idx = xp + tw * yp
                if D > zbuf[idx]:
                    zbuf[idx] = D
                    N = int(8 * ((sinT*sinA - sinP*cosT*cosA)*cosB
                                - sinP*cosT*sinA - sinT*cosA
                                - cosP*cosT*sinB))
                    N = max(0, min(N, len(_TORUS_CHARS)-1))
                    col = BG if N > 9 else (DG if N > 5 else DDG)
                    out[idx] = (_TORUS_CHARS[N], col)
            phi += 0.07
        theta += 0.10
    return out, tw, th

_GENIE_ART = """\
              ╭────────────────────╮
             ╱  ◈               ◈  ╲
            │    ╰─────────────╯    │
            │   ~  ~  ~  ~  ~  ~   │
             ╲         ω           ╱
              ╰────────────────────╯
             /     │           │     \\
            /      │           │      \\
        ░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░
      ░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░
    ▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒▒
   ▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓"""

_GENIE_LINES = _GENIE_ART.split('\n')
_GENIE_H = len(_GENIE_LINES)
_GENIE_W = max(len(l) for l in _GENIE_LINES)

def torus_to_genie():
    """3D torus spins, then genie rises from its center."""
    screen = Screen(TW, TH)

    tor_w = min(TW - 4, 56)
    tor_h = min(TH - 2, 20)
    tor_r0 = TH//2 - tor_h//2
    tor_c0 = TW//2 - tor_w//2

    gen_c0 = TW//2 - _GENIE_W//2
    gen_r_start = TH//2 - _GENIE_H//2          # starts center-screen (inside torus)
    gen_r_end   = max(1, TH//2 - _GENIE_H - 1) # rises to upper area

    A, B = 1.0, 0.5
    dt = 1.0/20
    T_SPIN = 2.4   # pure torus
    T_RISE = 4.2   # genie rises, torus dissolves
    T_END  = 4.8

    smoke: list[Smoke] = []
    t = 0.0

    while t < T_END:
        t0 = time.monotonic()
        screen.clear()

        # Torus dissolve probability
        if t < T_SPIN:
            torus_p = 1.0
        else:
            torus_p = max(0.0, 1.0 - (t - T_SPIN) / (T_RISE - T_SPIN))

        if torus_p > 0.02:
            frame, fw, fh = _torus_frame(A, B, tor_w, tor_h)
            for i, (ch, col) in enumerate(frame):
                if ch != ' ' and (torus_p >= 1.0 or random.random() < torus_p):
                    screen.put(tor_r0 + i // fw, tor_c0 + i % fw, ch, col)

        # Genie rises from torus center to final position
        if t > T_SPIN:
            rise = min(1.0, (t - T_SPIN) / (T_RISE - T_SPIN))
            # Ease-out
            ease = 1 - (1 - rise) ** 2
            gen_r0 = int(gen_r_start + (gen_r_end - gen_r_start) * ease)
            for ar, line in enumerate(_GENIE_LINES):
                for ac, ch in enumerate(line):
                    if ch != ' ':
                        screen.put(gen_r0 + ar, gen_c0 + ac, ch,
                                   BG if rise > 0.6 else DG)

            # Smoke bursts from torus center as genie emerges
            for _ in range(2):
                sx = TW//2 + random.randint(-12, 12)
                smoke.append(Smoke(sx, TH//2, vy=random.uniform(-4, -1.5)))

        smoke = [p for p in smoke if p.alive]
        for p in smoke: p.step(dt); p.draw(screen)

        screen.flip()
        A += 0.09; B += 0.04
        time.sleep(max(0.0, dt - (time.monotonic() - t0)))
        t += dt

# ─── Genie wizard UI ──────────────────────────────────────────────────────────

def _show_genie(extra_lines=4):
    pad = max(0, (TH - _GENIE_H - extra_lines) // 3)
    w('\n' * pad + G + _GENIE_ART + RST + '\n')

# ─── Providers ────────────────────────────────────────────────────────────────

PROVIDERS = [
    {"name": "DeepSeek",       "url": "https://api.deepseek.com/v1",              "model": "deepseek-chat",                    "note": "~2,000x less than Anthropic",   "key_url": "https://platform.deepseek.com"},
    {"name": "Groq",           "url": "https://api.groq.com/openai/v1",           "model": "llama-3.3-70b-versatile",          "note": "fastest · free tier",           "key_url": "https://console.groq.com"},
    {"name": "NVIDIA NIM",     "url": "https://integrate.api.nvidia.com/v1",      "model": "meta/llama-3.3-70b-instruct",      "note": "free tier · Llama 3.3 70B",     "key_url": "https://build.nvidia.com"},
    {"name": "OpenRouter",     "url": "https://openrouter.ai/api/v1",             "model": "meta-llama/llama-3.3-70b-instruct","note": "200+ models · one key",         "key_url": "https://openrouter.ai"},
    {"name": "Ollama (local)", "url": "http://localhost:11434/v1",                "model": "llama3.3",                         "note": "runs on your machine",          "key_url": None},
    {"name": "LM Studio",      "url": "http://localhost:1234/v1",                 "model": "",                                 "note": "runs on your machine",          "key_url": None},
    {"name": "Custom",         "url": "",                                          "model": "",                                 "note": "any OpenAI-compatible endpoint","key_url": None},
]

# ─── Power tools ──────────────────────────────────────────────────────────────

POWER_TOOLS = [
    {
        "id": "claude-mem", "label": "claude-mem",
        "desc": "persistent memory across sessions",
        "type": "script",
        # npx claude-mem install writes its own MCP config to ~/.claude.json
        "install_cmd": "npx claude-mem install",
        "requires": "node",
    },
    {
        "id": "browser-use", "label": "browser-use",
        "desc": "control a real browser",
        "type": "manual",
        "install_url": "https://github.com/co-browser/browser-use-mcp-server",
        "install_note": "requires git clone + build + playwright (see URL)",
    },
    {
        "id": "superpowers", "label": "superpowers",
        "desc": "structured workflow + TDD skills",
        "type": "plugin",
        "install_cmd": "/plugin install superpowers@claude-plugins-official",
    },
    {
        "id": "gstack", "label": "gstack",
        "desc": "23 AI specialist roles as slash commands",
        "type": "script", "requires": "bun",
        "install_cmd": (
            "git clone --depth 1 https://github.com/garrytan/gstack.git"
            " ~/.claude/skills/gstack && cd ~/.claude/skills/gstack && ./setup"
        ),
    },
]

def _has(cmd):
    import shutil; return shutil.which(cmd) is not None

# ─── Toggle screen ────────────────────────────────────────────────────────────

def ask_power_tools():
    bun_ok = _has("bun")
    enabled = [True] * len(POWER_TOOLS)

    while True:
        cls()
        _show_genie(extra_lines=len(POWER_TOOLS) + 7)
        w(f"\n  {G}{BLD}  Power tools  "
          f"{DIM}· press 1–4 to toggle · Enter to confirm{RST}\n\n")

        for i, tool in enumerate(POWER_TOOLS):
            tick  = f"{BG}{BLD}✓{RST}" if enabled[i] else f"{DIM}✗{RST}"
            label = f"{W}{BLD}{tool['label']}{RST}"
            desc  = f"{DIM}{tool['desc']}{RST}"
            req   = tool.get("requires")
            warn  = ""
            if req and not _has(req):
                warn = f"  {YL}⚠ {req} not found{RST}"
            elif tool["type"] == "manual":
                warn = f"  {DIM}manual install — we'll show you how{RST}"
            w(f"  {G}  [{i+1}] {tick}  {label}  {desc}{warn}\n")

        w(f"\n  {G}❯ {RST}")
        sys.stdout.flush()

        key = getch()
        if key in ('\r', '\n', ''):
            break
        if key in ('1', '2', '3', '4'):
            enabled[int(key)-1] = not enabled[int(key)-1]

    return [POWER_TOOLS[i] for i in range(len(POWER_TOOLS)) if enabled[i]]

# ─── Wizard ───────────────────────────────────────────────────────────────────

def run_wizard():
    animate_backdoor()
    torus_to_genie()
    w(SHOW)

    # Name
    cls(); _show_genie(extra_lines=4)
    w(f"  {G}{BLD}  What should I call you?{RST}\n  {G}❯ {RST}")
    name = input().strip() or "Builder"

    # Provider
    cls(); _show_genie(extra_lines=len(PROVIDERS) + 6)
    w(f"\n  {G}{BLD}  Hey {name}. Pick a provider:{RST}\n\n")
    for i, p in enumerate(PROVIDERS, 1):
        marker = f"{BG}▶{RST}" if i == 1 else " "
        w(f"  {G}  {marker} [{i}]  {W}{p['name']}{RST}  {DIM}{p['note']}{RST}\n")
    w(f"\n  {DIM}  Enter for DeepSeek{RST}\n  {G}❯ {RST}")
    raw = input().strip()
    try:
        provider = PROVIDERS[int(raw)-1] if raw else PROVIDERS[0]
    except (ValueError, IndexError):
        provider = PROVIDERS[0]

    # Model
    cls(); _show_genie(extra_lines=5)
    w(f"\n  {G}{BLD}  Provider: {W}{provider['name']}{RST}\n")
    if provider["model"]:
        w(f"\n  {G}  Model [{W}{provider['model']}{G}] — Enter to confirm:{RST}\n  {G}❯ {RST}")
        model = input().strip() or provider["model"]
    else:
        w(f"\n  {G}{BLD}  Model name:{RST}\n  {G}❯ {RST}")
        model = input().strip()

    # API key
    if provider["key_url"]:
        cls(); _show_genie(extra_lines=4)
        w(f"\n  {G}{BLD}  API Key  {DIM}(get one at {provider['key_url']}){RST}\n  {G}❯ {RST}")
        api_key = input().strip() or "your-api-key-here"
    else:
        api_key = "local"

    # Custom URL
    base_url = provider["url"]
    if provider["name"] == "Custom":
        cls(); _show_genie(extra_lines=4)
        w(f"\n  {G}{BLD}  Provider URL:{RST}\n  {G}❯ {RST}")
        base_url = input().strip()

    # Power tools
    selected   = ask_power_tools()
    auto_tools = [t for t in selected if t["type"] == "script" and _has(t.get("requires", "sh"))]
    post_tools = [t for t in selected if t not in auto_tools]

    # Confirm
    cls(); _show_genie(extra_lines=10)
    w(f"\n  {G}{BLD}  Ready, {name}.{RST}\n\n")
    w(f"  {DW}  Provider    {RST}{W}{provider['name']}  ·  {model}{RST}\n")
    w(f"  {DW}  Endpoint    {RST}{W}{base_url}{RST}\n")
    if auto_tools:
        w(f"  {DW}  Installing  {RST}{W}{' · '.join(t['label'] for t in auto_tools)}{RST}\n")
    if post_tools:
        w(f"  {DW}  After launch{RST}{W}  {' · '.join(t['label'] for t in post_tools)}{RST}\n")
    w(f"\n  {G}  Enter to launch — Ctrl+C to bail.{RST}\n  {G}❯ {RST}")
    input()

    # Write .env
    script_dir = Path(__file__).parent
    (script_dir / ".env").write_text(
        f"PROVIDER_BASE_URL={base_url}\n"
        f"PROVIDER_API_KEY={api_key}\n"
        f"PROVIDER_MODEL={model}\n"
        f"PROVIDER_MAX_TOKENS=32768\n"
        f"PROVIDER_TEMPERATURE=1.0\n"
        f"PROVIDER_TOP_P=1.0\n\n"
        f"HOST=127.0.0.1\nPORT=8082\nLOG_FILE=proxy.log\n\n"
        f"SKIP_QUOTA_PROBES=true\nSKIP_TITLE_GENERATION=true\n"
        f"SKIP_SUGGESTION_MODE=true\nMOCK_PREFIX_DETECTION=true\n"
        f"MOCK_FILEPATH_EXTRACTION=true\n"
    )

    # Auto-install script-type tools (claude-mem, gstack)
    for t in auto_tools:
        cls(); _show_genie(extra_lines=4)
        w(f"\n  {G}  Installing {t['label']}...{RST}\n\n")
        os.system(t["install_cmd"])
        time.sleep(0.5)

    # Launch
    cls(); _show_genie(extra_lines=8)
    w(f"\n  {G}{BLD}  Firing up Backdoor...{RST}\n")

    # Post-launch reminders
    plugins  = [t for t in post_tools if t["type"] == "plugin"]
    manuals  = [t for t in post_tools if t["type"] == "manual"]
    missing  = [t for t in selected if t["type"] == "script" and not _has(t.get("requires", "sh"))]

    if plugins:
        w(f"\n  {DW}  Inside Claude Code, run:{RST}\n")
        for t in plugins:
            w(f"  {G}    {t['install_cmd']}{RST}\n")
    if manuals:
        w(f"\n  {DW}  Manual installs (see links):{RST}\n")
        for t in manuals:
            w(f"  {G}    {t['label']}  →  {t['install_url']}{RST}\n")
    if missing:
        w(f"\n  {Y}  Skipped (missing prereq):{RST}\n")
        for t in missing:
            w(f"  {Y}    {t['label']}  needs {t['requires']} → install then re-run ./backdoor setup{RST}\n")
    if plugins or manuals or missing:
        w("\n")

    time.sleep(0.8)
    os.execv("/bin/bash", ["/bin/bash", str(script_dir / "run.sh")])


if __name__ == "__main__":
    run_wizard()
