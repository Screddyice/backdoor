# Backdoor router self-lockout, 2026-08-31

## Impact

Claude and Codex lost their shared local routing path during a live launchd migration. The agent
performing the change could no longer repair the machine through either assistant. Shawn used an
outside service to start the rollback.

## Sequence

1. Direct `launchctl kickstart -k` restarts killed active streams. Replacing SIGKILL with SIGTERM
   traded truncated streams for a listener outage while uvicorn drained a long request.
2. The agent added an idle-aware restart script and proposed launchd socket activation as the
   permanent answer.
3. Unit tests passed. The design promised a live acceptance test that would send traffic through
   both ports during a restart, but that test did not run before adoption.
4. The idle check timed out after more than six minutes with 18 active connections. That result
   should have stopped the rollout.
5. The agent bypassed the failed gate and ran `bootout` plus `bootstrap` in a detached background
   command. The command rewired ports 8083 and 8084, which carried the Claude and Codex sessions
   needed to observe and repair the change.
6. GitHub PRs #80 and #81 merged within minutes. Shawn's QA Assist supplied the only approval;
   neither PR had a repository-native status check. The deployed change then refused connections.
7. Rollback PRs #82 and #83 removed socket activation, the restart experiment, and the related
   state-ownership series. The live service returned to commit `472037c`.

## Root causes

- The rollout treated tests of socket-handling code as evidence for a launchd migration.
- The agent continued after the live idle gate failed.
- The mutation ran in the background, so failure could not stop the parent operation.
- The control plane and repair plane shared the same two listeners.
- The repository allowed a bot to approve and merge its own rapid review with no native checks.
- No independent watchdog could restore the previous plist and checkout after the agent lost its
  connection.

## Permanent policy

- Agents may edit Backdoor source, run tests, inspect the live service, and open PRs.
- Agents may not change the live launch agent, deployed checkout, dependencies, or router process.
- Backdoor PRs require a human merge. Shawn's QA Assist may review them but cannot merge them.
- The launch agent must bind ports through the service process. Do not add a launchd `Sockets`
  section for ports 8083 or 8084.
- A failed precondition ends a rollout. An agent cannot bypass an idle, canary, approval, or health
  gate with a detached command or a force option.
- A future live deployment needs an independent direct-cloud rescue session and an external
  rollback watchdog before it changes the shared route.

