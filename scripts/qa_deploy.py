#!/usr/bin/env python3
"""Execute QA Assist's exact-SHA GitHub Deployments from the local Mac.

This controller runs outside the router and uses direct GitHub connections.
It never executes commands supplied in a deployment payload.
"""
from __future__ import annotations

import argparse
import fcntl
import json
import os
from pathlib import Path
import plistlib
import re
import subprocess
import time
import urllib.request

REPOSITORY = "Screddyice/backdoor"
BOT = "shawns-qa-assist[bot]"
LABEL = "com.screddy.backdoor-router"
ENVIRONMENT = {"CODEX_LOCAL_MODEL": "qwen3.5:4b-64k", "FAILOVER_PROFILE": "local-qwen4b"}
EXPECTED = {
    "default_qwen_model": "qwen3.5:4b-64k",
    "codex_local_model": "qwen3.5:4b-64k",
    "failover_model": "qwen3.5:4b-64k",
    "explicit_27b_model": "qwen3.8:27b-obliterated",
}


class Refused(RuntimeError):
    pass


def direct_environment():
    return {k: v for k, v in os.environ.items()
            if k.lower() not in {"http_proxy", "https_proxy", "all_proxy"}}


def run(*args, timeout=120):
    result = subprocess.run(args, capture_output=True, text=True, timeout=timeout,
                            env=direct_environment())
    if result.returncode:
        # Avoid copying command output or environment values into public statuses.
        raise Refused(f"{Path(args[0]).name} operation failed (exit {result.returncode})")
    return result.stdout.strip()


def atomic_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    with temporary.open("w") as output:
        os.chmod(temporary, 0o600)
        json.dump(value, output)
        output.flush()
        os.fsync(output.fileno())
    temporary.replace(path)


def validate_request(deployment, pr, branch, checks):
    """Only the QA app's merged main commit with green native CI can deploy."""
    sha = deployment.get("sha", "")
    payload = deployment.get("payload")
    if not isinstance(payload, dict):
        raise Refused("deployment payload must be an object")
    if deployment.get("creator", {}).get("login") != BOT:
        raise Refused("deployment was not created by Shawn's QA Assist")
    if deployment.get("environment") != "production" or payload.get("requested_by") != "pr-qa-agent":
        raise Refused("deployment is outside the QA production contract")
    if not re.fullmatch(r"[a-f0-9]{40}", sha) or payload.get("sha") != sha:
        raise Refused("deployment does not name one exact commit")
    if not pr.get("merged") or pr.get("merge_commit_sha") != sha:
        raise Refused("deployment commit is not the associated PR's merge commit")
    if pr.get("number") != payload.get("pull_request") or pr.get("base", {}).get("ref") != "main":
        raise Refused("deployment does not match a main-branch PR")
    if pr.get("base", {}).get("repo", {}).get("full_name", "").lower() != REPOSITORY.lower():
        raise Refused("PR belongs to another repository")
    if branch.get("commit", {}).get("sha") != sha:
        raise Refused("deployment was superseded by another main commit")
    runs = checks.get("check_runs", [])
    verified = [c for c in runs if c.get("name") == "verify" and c.get("head_sha") == sha
                and c.get("app", {}).get("slug") == "github-actions"]
    if any(c.get("status") == "completed" and c.get("conclusion") != "success" for c in verified):
        raise Refused("main CI failed; deployment refused")
    return bool(verified) and all(c.get("status") == "completed" and c.get("conclusion") == "success"
                                  for c in verified)


class Controller:
    def __init__(self, service, plist, state, log):
        self.service, self.plist, self.state, self.log = map(Path, (service, plist, state, log))
        self.journal = self.state / "transaction.json"
        self.health_url = "http://127.0.0.1:8083/health"

    def git(self, *args):
        return run("git", "-C", str(self.service), *args)

    def api(self, endpoint, data=None):
        args = ["gh", "api", f"repos/{REPOSITORY}/{endpoint}"]
        if data is None:
            return json.loads(run(*args))
        result = subprocess.run([*args, "--method", "POST", "--input", "-"],
                                input=json.dumps(data), capture_output=True, text=True,
                                timeout=45, env=direct_environment())
        if result.returncode:
            raise Refused("GitHub deployment status could not be confirmed")
        return json.loads(result.stdout)

    def status(self, deployment_id, state, description):
        self.api(f"deployments/{deployment_id}/statuses", {
            "state": state, "description": description[:140],
            "environment": "production", "auto_inactive": False,
        })

    def health(self):
        try:
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            with opener.open(self.health_url, timeout=3) as response:
                return json.load(response)
        except (OSError, ValueError):
            return {}

    def matches(self, sha):
        health = self.health()
        return health.get("status") == "ok" and health.get("revision") == sha and all(
            health.get(key) == value for key, value in EXPECTED.items())

    def quiet(self):
        deadline = time.monotonic() + 180
        while time.monotonic() < deadline:
            before = self.log.stat().st_size
            health = self.health()
            if health.get("status") != "ok":
                raise Refused("router is unhealthy before deployment")
            time.sleep(10)
            after_health = self.health()
            if (self.log.stat().st_size == before and after_health.get("status") == "ok"
                    and health.get("active_requests", 0) == 0
                    and after_health.get("active_requests", 0) == 0):
                return
        raise Refused("router remained busy; submit a new deployment during a quiet window")

    def restart(self):
        domain = f"gui/{os.getuid()}"
        # Graceful launchd removal, never kickstart -k or SIGKILL.
        result = subprocess.run(["launchctl", "bootout", f"{domain}/{LABEL}"],
                                capture_output=True, timeout=90, env=direct_environment())
        if result.returncode:
            loaded = subprocess.run(["launchctl", "print", f"{domain}/{LABEL}"],
                                    capture_output=True, timeout=10)
            if loaded.returncode == 0:
                raise Refused("router could not be unloaded")
        # A failed bootout must not leave an untracked old process serving traffic.
        deadline = time.monotonic() + 60
        while self.health() and time.monotonic() < deadline:
            time.sleep(1)
        if self.health():
            raise Refused("old router still responds after unload")
        run("launchctl", "bootstrap", domain, str(self.plist), timeout=90)

    def wait_for_release(self, sha):
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            if self.matches(sha):
                return
            time.sleep(2)
        raise Refused("new process did not report the expected commit and model routes")

    def rollback(self, record):
        self.git("checkout", "--detach", record["previous_sha"])
        self.plist.write_bytes((self.state / "previous.plist").read_bytes())
        run("uv", "sync", "--frozen", "--project", str(self.service), timeout=180)
        self.restart()
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            health = self.health()
            # Older releases did not expose a revision. Require the old checkout
            # plus health after bootstrap; do not report the new release deployed.
            if (health.get("status") == "ok" and self.git("rev-parse", "HEAD") == record["previous_sha"]
                    and health.get("revision", record["previous_sha"]) == record["previous_sha"]):
                return
            time.sleep(2)
        raise Refused("rollback did not recover a healthy previous release")

    def preflight(self):
        if self.git("remote", "get-url", "origin").removesuffix(".git") not in {
            f"https://github.com/{REPOSITORY}", f"git@github.com:{REPOSITORY}"
        }:
            raise Refused("service checkout has the wrong origin")
        if self.git("status", "--porcelain"):
            raise Refused("service checkout has uncommitted changes")
        # Only detached service worktrees are eligible; never switch a developer's branch.
        result = subprocess.run(["git", "-C", str(self.service), "symbolic-ref", "-q", "HEAD"],
                                capture_output=True)
        if result.returncode != 1:
            raise Refused("service checkout must use detached HEAD")
        config = plistlib.loads(self.plist.read_bytes())
        if config.get("Label") != LABEL or config.get("WorkingDirectory") != str(self.service):
            raise Refused("LaunchAgent does not target the service checkout")
        expected_args = [str(self.service / ".venv/bin/python"), "-m", "src.proxy.serve"]
        if config.get("ProgramArguments") != expected_args:
            raise Refused("LaunchAgent has an unexpected program")
        return config

    def finish(self, record, outcome, message):
        record.update(phase="terminal", outcome=outcome, message=message)
        atomic_json(self.journal, record)
        self.status(record["id"], outcome, message)
        record["reported"] = True
        atomic_json(self.journal, record)

    def recover(self):
        if not self.journal.exists():
            return False
        record = json.loads(self.journal.read_text())
        if record["phase"] == "terminal":
            if not record.get("reported"):
                self.finish(record, record["outcome"], record["message"])
            return False
        if self.git("rev-parse", "HEAD") == record["sha"] and self.matches(record["sha"]):
            self.finish(record, "success", "Recovered receipt: exact release and 4B routing verified")
        else:
            self.rollback(record)
            self.finish(record, "failure", "Interrupted deployment rolled back to the previous release")
        return True

    def deploy(self, deployment):
        deployment_id, sha = deployment["id"], deployment["sha"]
        config = self.preflight()
        self.git("fetch", "origin", "main")
        if self.git("rev-parse", "origin/main") != sha:
            raise Refused("main changed before deployment")
        self.git("merge-base", "--is-ancestor", "HEAD", sha)
        # No weights are pulled or loaded by this controller.
        tags = json.loads(run("curl", "--noproxy", "*", "-fsS", "--max-time", "5",
                              "http://127.0.0.1:11434/api/tags"))
        names = {m["name"] for m in tags.get("models", [])}
        if not {"qwen3.5:4b-64k", "qwen3.5:4b-256k"}.issubset(names):
            raise Refused("required 4B model tags are not installed")
        self.quiet()
        if self.matches(sha):
            self.status(deployment_id, "success", "Requested release is already healthy with 4B routing")
            return
        self.status(deployment_id, "in_progress", "Quiet window confirmed; applying merged release")
        self.state.mkdir(parents=True, exist_ok=True, mode=0o700)
        backup = self.state / "previous.plist"
        backup.write_bytes(self.plist.read_bytes())
        backup.chmod(0o600)
        record = {"id": deployment_id, "sha": sha, "previous_sha": self.git("rev-parse", "HEAD"),
                  "phase": "applying"}
        atomic_json(self.journal, record)
        try:
            self.git("checkout", "--detach", sha)
            run("uv", "sync", "--frozen", "--project", str(self.service), timeout=180)
            config.setdefault("EnvironmentVariables", {}).update(ENVIRONMENT)
            config["ExitTimeOut"] = 60
            temporary = self.plist.with_suffix(".qa-deploy.tmp")
            temporary.write_bytes(plistlib.dumps(config))
            temporary.chmod(self.plist.stat().st_mode & 0o777)
            temporary.replace(self.plist)
            self.restart()
            self.wait_for_release(sha)
        except Exception:
            self.rollback(record)
            self.finish(record, "failure", "Deployment failed verification; previous release restored")
            return
        self.finish(record, "success", "Exact merged commit and default 4B / explicit obliterated 27B verified")

    def poll(self):
        if self.recover():
            return
        for deployment in self.api("deployments?environment=production&per_page=20"):
            if deployment.get("creator", {}).get("login") != BOT:
                continue
            statuses = self.api(f"deployments/{deployment['id']}/statuses")
            if statuses and statuses[0].get("state") not in {"queued", "pending"}:
                continue
            try:
                payload = deployment.get("payload", {})
                number = payload.get("pull_request") if isinstance(payload, dict) else None
                if not isinstance(number, int) or number <= 0:
                    raise Refused("deployment has no valid PR number")
                pr = self.api(f"pulls/{number}")
                branch = self.api("branches/main")
                checks = self.api(f"commits/{deployment['sha']}/check-runs?per_page=100")
                if validate_request(deployment, pr, branch, checks):
                    self.deploy(deployment)
                # Main CI may still be running. The next poll rechecks it.
                return
            except Refused as exc:
                if self.journal.exists() and json.loads(self.journal.read_text()).get("phase") != "terminal":
                    raise  # Preserve the recovery journal; never conceal a failed rollback.
                self.status(deployment["id"], "failure", str(exc))
                return


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--service", type=Path, required=True)
    parser.add_argument("--plist", type=Path, required=True)
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument("--log", type=Path, required=True)
    args = parser.parse_args()
    args.state.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (args.state / "worker.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return
        Controller(args.service, args.plist, args.state, args.log).poll()


if __name__ == "__main__":
    main()
