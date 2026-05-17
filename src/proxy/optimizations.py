"""
Fast-path interceptors for Claude Code housekeeping requests.
These short-circuit before hitting NIM to avoid burning rate-limit quota.
"""

from .models import MessagesRequest


def _last_user_text(req: MessagesRequest) -> str:
    for msg in reversed(req.messages):
        if msg.role == "user":
            c = msg.content
            if isinstance(c, str):
                return c
            return " ".join(b.get("text", "") for b in c if b.get("type") == "text")
    return ""


def is_quota_probe(req: MessagesRequest) -> bool:
    text = _last_user_text(req).lower()
    return "quota" in text and len(text) < 120


def is_title_generation(req: MessagesRequest) -> bool:
    text = _last_user_text(req)
    return "generate a title" in text.lower() and len(text) < 300


def is_suggestion_mode(req: MessagesRequest) -> bool:
    sys = req.system or ""
    if isinstance(sys, list):
        sys = " ".join(b.get("text", "") for b in sys if b.get("type") == "text")
    return "suggestion" in sys.lower() and not req.tools


def is_prefix_detection(req: MessagesRequest) -> tuple[bool, str]:
    text = _last_user_text(req)
    marker = "<cmd_prefix_detect>"
    if marker in text:
        start = text.index(marker) + len(marker)
        end = text.find("</cmd_prefix_detect>", start)
        cmd = text[start:end].strip() if end != -1 else text[start:].strip()
        return True, cmd
    return False, ""


def extract_prefix(command: str) -> str:
    """Return the command prefix (everything up to the first space or flag)."""
    return command.split()[0] if command.strip() else ""


def is_filepath_extraction(req: MessagesRequest) -> tuple[bool, str, str]:
    text = _last_user_text(req)
    if "<extract_filepaths>" in text:
        try:
            cmd_start = text.index("<cmd>") + 5
            cmd_end = text.index("</cmd>")
            out_start = text.index("<output>") + 8
            out_end = text.index("</output>")
            return True, text[cmd_start:cmd_end], text[out_start:out_end]
        except ValueError:
            pass
    return False, "", ""


def extract_filepaths(output: str) -> str:
    """Extract file paths from command output (lines that look like paths)."""
    import re
    paths = re.findall(r"[\w./~-]+\.\w+", output)
    return "\n".join(paths[:20])
