"""Tests for the local CA that fronts api.anthropic.com.

The forward proxy has to present a certificate for a hostname it does not own,
so it mints one. That is only safe because the CA never leaves this machine and
is never installed into the system trust store — a single process trusts it, via
NODE_EXTRA_CA_CERTS, and nothing else on the Mac does. These tests pin the two
properties that keep that true: the private key is not group- or world-readable,
and the CA is confined to the directory it was pointed at.

Leaf caching is also pinned. Minting is a 2048-bit RSA keygen, which is ~100ms
of CPU; doing it per CONNECT would put that on the front of every request the
router forwards.
"""

from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from src.proxy.ca import LocalCA


def _load(path: Path) -> x509.Certificate:
    return x509.load_pem_x509_certificate(path.read_bytes())


def test_ca_is_created_on_first_use(tmp_path):
    ca = LocalCA(tmp_path / "ca")
    ca.ensure()

    assert ca.ca_cert_path.exists()
    assert ca.ca_key_path.exists()

    cert = _load(ca.ca_cert_path)
    basic = cert.extensions.get_extension_for_class(x509.BasicConstraints).value
    assert basic.ca is True


def test_ca_private_key_is_owner_only(tmp_path):
    ca = LocalCA(tmp_path / "ca")
    ca.ensure()

    mode = ca.ca_key_path.stat().st_mode & 0o777
    assert mode == 0o600, f"CA key is {mode:o}, must be 0600"


def test_ca_is_reused_across_instances(tmp_path):
    first = LocalCA(tmp_path / "ca")
    first.ensure()
    original = first.ca_cert_path.read_bytes()

    second = LocalCA(tmp_path / "ca")
    second.ensure()

    assert second.ca_cert_path.read_bytes() == original


def test_leaf_is_signed_by_the_ca(tmp_path):
    ca = LocalCA(tmp_path / "ca")
    cert_path, key_path = ca.leaf_files("api.anthropic.com")

    leaf = _load(cert_path)
    root = _load(ca.ca_cert_path)

    # Raises InvalidSignature if the CA did not sign this leaf.
    root.public_key().verify(
        leaf.signature,
        leaf.tbs_certificate_bytes,
        padding.PKCS1v15(),
        leaf.signature_hash_algorithm,
    )

    assert key_path.exists()
    assert leaf.issuer == root.subject


def test_leaf_carries_the_host_as_a_san(tmp_path):
    ca = LocalCA(tmp_path / "ca")
    cert_path, _ = ca.leaf_files("api.anthropic.com")

    san = _load(cert_path).extensions.get_extension_for_class(
        x509.SubjectAlternativeName
    ).value
    assert "api.anthropic.com" in san.get_values_for_type(x509.DNSName)


def test_leaf_is_cached_per_host(tmp_path):
    ca = LocalCA(tmp_path / "ca")

    first_cert, first_key = ca.leaf_files("api.anthropic.com")
    first_bytes = first_cert.read_bytes()

    second_cert, second_key = ca.leaf_files("api.anthropic.com")

    assert (second_cert, second_key) == (first_cert, first_key)
    assert second_cert.read_bytes() == first_bytes


def test_distinct_hosts_get_distinct_leaves(tmp_path):
    ca = LocalCA(tmp_path / "ca")

    anthropic, _ = ca.leaf_files("api.anthropic.com")
    other, _ = ca.leaf_files("example.invalid")

    assert anthropic != other
    assert anthropic.read_bytes() != other.read_bytes()


def test_concurrent_minting_yields_a_consistent_ca(tmp_path):
    """Many threads racing on a cold CA must still agree on one keypair.

    Regression test for the failure this shipped with. `ForwardProxy` mints via
    `asyncio.to_thread`, and Claude Code opens ~8 connections at once, so a cold
    start put that many OS threads inside `ensure()` together. Each saw "no CA",
    generated its own, and wrote key and cert as two independent atomic writes —
    leaving `ca-key.pem` from one CA beside `ca-cert.pem` from another.

    The leaf was then signed by a key the published cert does not match, which
    reaches the client as `CERT_SIGNATURE_FAILURE`: the issuer resolves, the
    signature does not. Observed end-to-end 2026-08-15 before the lock existed.
    """
    from concurrent.futures import ThreadPoolExecutor

    ca = LocalCA(tmp_path / "ca")
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _: ca.leaf_files("api.anthropic.com"), range(8)))

    root = _load(ca.ca_cert_path)
    key = serialization.load_pem_private_key(ca.ca_key_path.read_bytes(), password=None)
    assert (
        root.public_key().public_numbers() == key.public_key().public_numbers()
    ), "ca-cert.pem and ca-key.pem are from different keypairs"

    for cert_path, _ in results:
        leaf = _load(cert_path)
        root.public_key().verify(
            leaf.signature,
            leaf.tbs_certificate_bytes,
            padding.PKCS1v15(),
            leaf.signature_hash_algorithm,
        )


def test_a_mismatched_ca_on_disk_is_repaired(tmp_path):
    """Self-heal rather than wedge.

    A torn pair already on disk (from a crash, or from the race above) would
    otherwise fail every handshake forever and need manual deletion — on a path
    the user has no reason to know about.
    """
    ca = LocalCA(tmp_path / "ca")
    ca.ensure()

    # Overwrite the key with an unrelated one, exactly as the race did.
    stranger = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    ca.ca_key_path.write_bytes(
        stranger.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.TraditionalOpenSSL,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )

    cert_path, _ = ca.leaf_files("api.anthropic.com")

    root = _load(ca.ca_cert_path)
    key = serialization.load_pem_private_key(ca.ca_key_path.read_bytes(), password=None)
    assert root.public_key().public_numbers() == key.public_key().public_numbers()

    leaf = _load(cert_path)
    root.public_key().verify(
        leaf.signature,
        leaf.tbs_certificate_bytes,
        padding.PKCS1v15(),
        leaf.signature_hash_algorithm,
    )


def test_a_leaf_signed_by_a_previous_ca_is_reminted(tmp_path):
    """A stale leaf must not survive a CA rotation."""
    ca = LocalCA(tmp_path / "ca")
    cert_path, _ = ca.leaf_files("api.anthropic.com")
    stale = cert_path.read_bytes()

    ca.ca_cert_path.unlink()
    ca.ca_key_path.unlink()
    ca._contexts.clear()

    fresh_path, _ = ca.leaf_files("api.anthropic.com")
    assert fresh_path.read_bytes() != stale

    root = _load(ca.ca_cert_path)
    leaf = _load(fresh_path)
    root.public_key().verify(
        leaf.signature,
        leaf.tbs_certificate_bytes,
        padding.PKCS1v15(),
        leaf.signature_hash_algorithm,
    )


def test_ca_writes_nothing_outside_its_directory(tmp_path):
    ca_dir = tmp_path / "ca"
    ca = LocalCA(ca_dir)
    ca.leaf_files("api.anthropic.com")

    assert {p.name for p in tmp_path.iterdir()} == {"ca"}
