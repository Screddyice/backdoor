"""The registry is the single source for profile → port, key, and tier.

Every downstream guarantee rests on it. The tier decides whether a profile can
be conversed with, so a typo'd tier must fail loudly at load rather than
silently defaulting to something permissive. Duplicate ports must fail too:
gateways are separate processes on one host, and two profiles claiming a port
means one of them is silently unreachable or, worse, answering for the other.
"""

import pytest

from src.hermes_mcp.registry import (
    Profile,
    RegistryError,
    load_registry,
    registry_path,
)


def _write(tmp_path, body: str):
    p = tmp_path / "registry.toml"
    p.write_text(body, encoding="utf-8")
    return p


FULL = """
[profiles.alpha]
tier = "full"
port = 9001
key_env = "ALPHA_KEY"
unit = "gw-alpha.service"
home = "/srv/gw/alpha"
"""


def test_loads_a_full_profile(tmp_path):
    reg = load_registry(_write(tmp_path, FULL))
    assert set(reg) == {"alpha"}
    alpha = reg["alpha"]
    assert isinstance(alpha, Profile)
    assert (alpha.name, alpha.tier, alpha.port) == ("alpha", "full", 9001)
    assert alpha.key_env == "ALPHA_KEY"
    assert alpha.unit == "gw-alpha.service"


def test_unconfigured_profile_needs_no_port_or_key(tmp_path):
    reg = load_registry(_write(tmp_path, '[profiles.ghost]\ntier = "unconfigured"\n'))
    ghost = reg["ghost"]
    assert ghost.port is None and ghost.key_env is None and ghost.unit is None


def test_unknown_tier_is_rejected(tmp_path):
    body = '[profiles.alpha]\ntier = "fulll"\nport = 9001\nkey_env = "K"\n'
    with pytest.raises(RegistryError) as e:
        load_registry(_write(tmp_path, body))
    assert "fulll" in str(e.value)


def test_full_profile_without_port_is_rejected(tmp_path):
    with pytest.raises(RegistryError) as e:
        load_registry(_write(tmp_path, '[profiles.alpha]\ntier = "full"\nkey_env = "K"\n'))
    assert "port" in str(e.value)


def test_full_profile_without_key_env_is_rejected(tmp_path):
    with pytest.raises(RegistryError) as e:
        load_registry(_write(tmp_path, '[profiles.alpha]\ntier = "full"\nport = 9001\n'))
    assert "key_env" in str(e.value)


def test_duplicate_ports_are_rejected(tmp_path):
    body = (
        '[profiles.alpha]\ntier = "full"\nport = 9001\nkey_env = "A"\n'
        '[profiles.beta]\ntier = "full"\nport = 9001\nkey_env = "B"\n'
    )
    with pytest.raises(RegistryError) as e:
        load_registry(_write(tmp_path, body))
    msg = str(e.value)
    assert "9001" in msg and "alpha" in msg and "beta" in msg


def test_missing_file_is_rejected_with_the_path(tmp_path):
    missing = tmp_path / "nope.toml"
    with pytest.raises(RegistryError) as e:
        load_registry(missing)
    assert str(missing) in str(e.value)


def test_empty_registry_is_rejected(tmp_path):
    with pytest.raises(RegistryError):
        load_registry(_write(tmp_path, "\n"))


def test_registry_path_prefers_the_env_var(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_MCP_REGISTRY", str(tmp_path / "custom.toml"))
    assert registry_path() == tmp_path / "custom.toml"


def test_registry_path_defaults_under_config_home(monkeypatch):
    monkeypatch.delenv("HERMES_MCP_REGISTRY", raising=False)
    assert registry_path().parts[-3:] == (".config", "hermes-mcp", "registry.toml")


def test_the_real_registry_on_disk_is_loadable_and_consistent():
    """Guard the file that actually gets loaded, not just the parser.

    Skips where no registry is deployed (CI, a fresh clone). Where one exists,
    every guarantee the bridge rests on is asserted against it: load_registry
    itself enforces unique ports and valid tiers, and a full profile must name
    an env var that is actually set, or the bridge 401s that profile at runtime
    with everything looking correctly configured.
    """
    import os

    path = registry_path()
    if not path.exists():
        pytest.skip(f"no registry deployed at {path}")

    reg = load_registry(path)
    assert reg, "a deployed registry declares no profiles"

    for name, profile in reg.items():
        assert profile.tier in {"full", "control_only", "unconfigured"}
        if profile.tier == "unconfigured":
            continue
        assert profile.port, f"{name} is {profile.tier} with no port"
        assert profile.key_env, f"{name} is {profile.tier} with no key_env"
        assert os.environ.get(profile.key_env), (
            f"{name} names key_env {profile.key_env}, which is not set in this "
            "environment; the bridge would 401 that profile at runtime while "
            "the registry looks correct"
        )
