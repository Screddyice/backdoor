"""Candidate-level outage, recovery, fault, and rollback-boundary proof."""

import importlib.util
from pathlib import Path
import subprocess
import sys


SCRIPT = Path(__file__).parents[1] / "scripts" / "context-candidate-canary.py"


def load_canary():
    spec = importlib.util.spec_from_file_location("context_candidate_canary", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


async def test_candidate_cloud_outage_recovery_and_fault_sequence(tmp_path):
    report = await load_canary().run_candidate(temp_root=tmp_path)

    assert report["cloud"]["provider"] == "anthropic"
    assert report["local"]["provider"] == "qwen3.8:27b-obliterated"
    assert report["local"]["synthetic_input_tokens"] >= 500_000
    assert report["local"]["final_input_tokens"] <= 22_000
    assert report["local"]["max_output_tokens"] == 1_024
    assert report["cached_retry"]["answer"] == report["local"]["answer"]
    assert report["cached_retry"]["local_calls"] == 1
    assert report["recovery_probe"]["breaker_open"] is True
    assert report["recovered"]["breaker_open"] is False
    assert report["recovered"]["provider"] == "anthropic"
    assert report["sqlite_fault"]["continuity"] is True
    assert report["sqlite_fault"]["local_calls"] == 1
    assert report["global_configuration_unchanged"] is True
    assert report["protected_changes"] == []
    assert report["modified_paths"]
    assert all(str(path).startswith(str(tmp_path)) for path in report["modified_paths"])
    assert len(report["loopback_ports"]) == 2


def test_candidate_canary_runs_from_the_command_line():
    result = subprocess.run(
        [sys.executable, str(SCRIPT)],
        cwd=SCRIPT.parents[1],
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert "PASS bounded_local" in result.stdout
    assert "PASS global_configuration" in result.stdout
