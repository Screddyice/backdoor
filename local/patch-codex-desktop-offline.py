#!/usr/bin/env python3
"""Patch Codex Desktop so transient offline auth is not treated as logout."""

from __future__ import annotations

import argparse
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from src.proxy.codex_desktop_offline_patch import (
    PatchError,
    patch_desktop_app,
    restore_desktop_app,
)


parser = argparse.ArgumentParser(
    description="Keep Codex Desktop on its saved identity during internet outages."
)
parser.add_argument(
    "--app",
    type=Path,
    default=Path("/Applications/ChatGPT.app"),
    help="Codex Desktop application bundle",
)
parser.add_argument(
    "--backup-root",
    type=Path,
    default=Path.home()
    / "Library"
    / "Application Support"
    / "Backdoor"
    / "Codex Desktop Backups",
    help="Directory for recoverable application and ASAR backups",
)
parser.add_argument(
    "--restore",
    action="store_true",
    help="restore the complete original application bundle for the installed build",
)
args = parser.parse_args()

try:
    if args.restore:
        restored_from = restore_desktop_app(args.app, args.backup_root)
    else:
        outcome = patch_desktop_app(args.app, args.backup_root)
except (PatchError, OSError) as exc:
    parser.exit(1, f"Codex Desktop offline patch failed: {exc}\n")

if args.restore:
    print(f"Codex Desktop restored from: {restored_from}")
elif outcome.changed:
    print("Codex Desktop offline-auth patch installed. Restart the app to activate it.")
else:
    print("Codex Desktop offline-auth patch is already installed.")
if not args.restore and outcome.backup_path is not None:
    print(f"Archive backup: {outcome.backup_path}")
