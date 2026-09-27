#!/bin/bash
# Hermes cron runs this without a model call. The report owns one Gmail send.
set -euo pipefail
export HOME=/Users/screddy
export PATH=/Users/screddy/.composio:/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin
receipt="$HOME/.claude/state/hermes-weekly-savings.sent"
if [[ -f "$receipt" ]] && (( $(date +%s) - $(stat -f %m "$receipt") < 518400 )); then
  printf 'Weekly savings email sent within the past six days; skipping.\n'
  exit 0
fi
/usr/bin/python3 /Users/screddy/.claude/scripts/claude-savings-report.py --no-notify >/dev/null
touch "$receipt"
printf 'Weekly Codex and Claude savings report sent.\n'
