#!/bin/bash
# Hermes cron runs this without a model call. The report owns one Gmail send.
set -euo pipefail
export HOME=/Users/screddy
export PATH=/Users/screddy/.composio:/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin
/usr/bin/python3 /Users/screddy/.claude/scripts/claude-savings-report.py --no-notify >/dev/null
printf 'Weekly Codex and Claude savings report sent.\n'
