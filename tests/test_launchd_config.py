"""Regression checks for the macOS LaunchAgent example."""

from pathlib import Path
import plistlib


REPO_ROOT = Path(__file__).resolve().parents[1]
PLIST_PATH = REPO_ROOT / "deploy" / "com.screddy.backdoor-router.plist.example"


def test_launchd_example_has_capacity_for_concurrent_claude_sessions() -> None:
    with PLIST_PATH.open("rb") as plist_file:
        config = plistlib.load(plist_file)

    assert config["Label"] == "com.screddy.backdoor-router"
    assert config["RunAtLoad"] is True
    assert config["KeepAlive"] is True
    assert config["SoftResourceLimits"]["NumberOfFiles"] == 4096

    arguments = config["ProgramArguments"]
    assert arguments[1:] == ["-m", "src.proxy.serve"]
    port = config["EnvironmentVariables"]["PORT"]
    assert config["EnvironmentVariables"]["ROUTER_MODE"] == "hybrid"
    assert config["EnvironmentVariables"]["FORWARD_PROXY"] == "true"
    assert config["EnvironmentVariables"]["FORWARD_ROUTER_PORT"] == port
    assert config["EnvironmentVariables"]["FORWARD_IDLE_TIMEOUT"] == "660"
    assert config["EnvironmentVariables"]["FORWARD_MAX_CONNECTIONS"] == "512"
    assert config["EnvironmentVariables"]["LOG_FILE"].startswith("/Users/you/")
    assert config["StandardOutPath"] == "/dev/null"
    assert config["StandardErrorPath"] == "/dev/null"

    assert config["ProgramArguments"][0].startswith("/Users/you/")
    assert config["WorkingDirectory"].startswith("/Users/you/")
