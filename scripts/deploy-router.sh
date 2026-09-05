#!/usr/bin/env bash
#
# Fast-forward a service checkout onto a ref and restart it, or refuse to.
#
# Written after a deploy on 2026-09-03 that failed in the least interesting way
# possible: the steps were a list of shell lines, the two git steps failed, and
# the restart at the bottom ran anyway. The router came back on unchanged code
# having dropped every in-flight request, which every live Claude session
# reported as an API error. Nothing deployed, and something still broke.
#
# The point of this script is that it CANNOT half-run:
#
#   * every step is gated on the previous one succeeding
#   * a checkout already at the target exits 0 without restarting, because a
#     restart that changes no code is pure cost
#   * it waits for the router to go quiet first: a restart kills in-flight
#     requests and there is no zero-downtime path here, since socket activation
#     on 8083/8084 is forbidden on this machine after the 2026-08-31 experiments
#     were reverted
#   * it verifies the NEW code is running, not merely that something restarted
#   * it rolls back automatically when that verification fails
#
# The restart itself is deliberately NOT in this file. It arrives in
# RESTART_CMD, supplied by the person running the deploy, because operating the
# live control plane on this machine is a human's job and this repo should not
# encode one host's launch agent anyway.
#
# Usage:
#   RESTART_CMD='<command that restarts the router>' \
#     scripts/deploy-router.sh <service-checkout-dir> [ref]
#
#   DRY_RUN=1  print the plan, change nothing
#   FORCE=1    skip the quiet-window wait (in-flight requests WILL be dropped)

set -euo pipefail

SVC="${1:-}"
# Trunk, since 2026-09-03: main now contains the deployed line (merged as
# 6e10eaf), so it is the branch that actually advances. This defaulted to
# origin/fix/codex-active-turn-budget while that was the live line; that branch
# is merged and will not move again, so leaving it here would have quietly
# pinned every future deploy to a frozen commit and reported "already at the
# target" forever.
REF="${2:-origin/main}"

if [ -z "$SVC" ]; then
  echo "usage: RESTART_CMD='...' $0 <service-checkout-dir> [ref]" >&2
  exit 2
fi

LOG="${ROUTER_LOG:-$HOME/Library/Logs/backdoor-router.log}"
HEALTH="${ROUTER_HEALTH:-http://127.0.0.1:8083/health}"
MARKER="${ROUTER_MARKER:-failover recovery ticker armed}"
DRY_RUN="${DRY_RUN:-0}"
FORCE="${FORCE:-0}"

g() { git -C "$SVC" "$@"; }
die() { echo "ABORT: $1" >&2; exit 1; }

bounce() { eval "$RESTART_CMD"; }

echo "== 1/5 preflight"

# Checked here rather than at the point of use. Discovering a missing restart
# command AFTER the fast-forward would leave the checkout advanced and the old
# code still serving: a half-run, which is the one outcome this script exists to
# make impossible. Found by its own test on 2026-09-03.
[ -n "${RESTART_CMD:-}" ] || die "RESTART_CMD is not set, refusing to guess how to restart this service"

[ -d "$SVC" ] || die "no such directory: $SVC"
g rev-parse --git-dir >/dev/null 2>&1 || die "not a git checkout: $SVC"

# Uncommitted work here is almost certainly a hand-edit made on the live box. A
# fast-forward would either carry it silently or refuse halfway, and either way
# a human should look before anything restarts.
if [ -n "$(g status --porcelain)" ]; then
  g status --short
  die "the service checkout has uncommitted changes, resolve them first"
fi

BEFORE=$(g rev-parse HEAD)
g fetch origin --quiet || die "git fetch failed, nothing has changed yet"
TARGET=$(g rev-parse "$REF") || die "cannot resolve ref: $REF"

echo "   running: $(g log -1 --format='%h %s' "$BEFORE")"
echo "   target:  $(g log -1 --format='%h %s' "$TARGET")"

if [ "$BEFORE" = "$TARGET" ]; then
  echo "== already at the target, not restarting"
  echo "   a restart that changes no code only costs you dropped requests"
  exit 0
fi

if ! g merge-base --is-ancestor "$BEFORE" "$TARGET"; then
  die "$REF is not a fast-forward from the running commit. The checkout has
       commits the target lacks. Reconcile deliberately, do not force it."
fi

echo "   fast-forward is clean: $(g rev-list --count "$BEFORE..$TARGET") commit(s)"

if [ "$DRY_RUN" = "1" ]; then
  # A dry run restarts nothing, so there are no in-flight requests to protect
  # and the wait buys nothing. It also cannot finish: on this machine a live
  # Claude session writes to the router log every few seconds, so the window
  # never opens, and on 2026-09-04 that turned "show me the plan" into three
  # minutes ending in ABORT with the plan never printed.
  echo "== 2/5 no quiet window needed, nothing is going to restart"
elif [ "$FORCE" = "1" ]; then
  echo "== 2/5 skipping the quiet window"
  echo "   FORCE=1. In-flight requests WILL be dropped."
else
  echo "== 2/5 waiting for the router to go idle"
  # Established connections never reach zero, because Claude Code holds
  # keep-alive sockets open while idle, so counting them says nothing. Log
  # silence is the better signal for "nothing is actually in flight".
  END=$(( SECONDS + ${QUIET_TIMEOUT:-180} ))
  while true; do
    A=$(wc -c < "$LOG" 2>/dev/null || echo 0)
    sleep "${QUIET_SECONDS:-8}"
    B=$(wc -c < "$LOG" 2>/dev/null || echo 0)
    if [ "$A" = "$B" ]; then
      echo "   idle, proceeding"
      break
    fi
    if [ "$SECONDS" -ge "$END" ]; then
      die "still busy after ${QUIET_TIMEOUT:-180}s. Retry when you are not
       mid-turn in another session, or set FORCE=1 to accept that
       in-flight requests will fail."
    fi
    echo "   still serving traffic, waiting..."
  done
fi

OFF=$(wc -c < "$LOG" 2>/dev/null || echo 0)

echo "== 3/5 fast-forwarding the checkout"

if [ "$DRY_RUN" = "1" ]; then
  echo "   DRY_RUN: would fast-forward to $(g rev-parse --short "$TARGET")"
  echo "   DRY_RUN: would run: ${RESTART_CMD:-<unset>}"
  echo "== dry run complete, nothing changed"
  exit 0
fi

# Deliberately no "git checkout <branch>": this checkout is a detached worktree
# sharing one repository with about twenty others, and the branch is held by a
# different one. Fast-forwarding the detached HEAD sidesteps that collision.
g merge --ff-only "$TARGET" || die "fast-forward failed, nothing restarted"
echo "   now at $(g log -1 --format='%h %s')"

echo "== 4/5 restarting"
bounce || die "restart command failed"

echo "== 5/5 verifying"

roll_back() {
  echo "VERIFY FAILED: $1" >&2
  g reset --hard "$BEFORE" >/dev/null
  bounce || true
  echo "rolled back to $(g rev-parse --short HEAD)" >&2
  exit 1
}

END=$(( SECONDS + ${HEALTH_TIMEOUT:-45} ))
while ! curl -fsS -m 5 "$HEALTH" >/dev/null 2>&1; do
  if [ "$SECONDS" -ge "$END" ]; then
    roll_back "the health endpoint never came back"
  fi
  sleep 2
done
echo "   health: $(curl -fsS -m 5 "$HEALTH")"

# The marker must appear in the log written AFTER the restart. Grepping the
# whole file would happily match a previous deploy and report a no-op restart as
# a success, which is the exact mistake this script exists to prevent.
END=$(( SECONDS + 20 ))
while ! tail -c "+$(( OFF + 1 ))" "$LOG" | grep -qF "$MARKER"; do
  if [ "$SECONDS" -ge "$END" ]; then
    roll_back "restarted, but the new code is not running (no '$MARKER' since the restart)"
  fi
  sleep 2
done
echo "   $(tail -c "+$(( OFF + 1 ))" "$LOG" | grep -F "$MARKER" | tail -1)"

echo "== deployed"
echo "   was: $(g rev-parse --short "$BEFORE")"
echo "   now: $(g rev-parse --short HEAD)"
echo "   roll back with: git -C '$SVC' reset --hard $(g rev-parse --short "$BEFORE")  then restart"
