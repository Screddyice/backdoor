"""A local certificate authority for the CONNECT forward proxy.

## Why this exists

Claude Code refuses Remote Control unless `ANTHROPIC_BASE_URL` is unset or
points at `api.anthropic.com` — an exact host allowlist, checked in `eit()`:

    function eit(){ let e=process.env.ANTHROPIC_BASE_URL; if(!e) return true;
                    return Oxe(e) }
    function Oxe(e){ let t=new URL(e).host;
                     return ["api.anthropic.com"].includes(t) }

Pointing the variable at the router (`http://127.0.0.1:8083`) therefore trades
Remote Control away for failover. `_CLAUDE_CODE_ASSUME_FIRST_PARTY_BASE_URL`
does not buy it back; the binary carries an explicit string saying that flag
"does not apply to Remote Control".

The way out is that the router does not have to be the *base URL* — it only has
to be *in the path*. Claude Code honours `HTTPS_PROXY` (verified 2026-08-14: it
issues `CONNECT api.anthropic.com:443` through it), so with the base URL unset
the RC gate passes while the router still sees every request. Reading a CONNECT
tunnel means terminating TLS for a hostname we do not own, which means minting a
certificate for it. Hence a CA.

## Why this is not as alarming as it sounds

The CA is trusted by exactly one process: the `claude` launcher, via
`NODE_EXTRA_CA_CERTS`. It is never added to the system keychain, so no browser,
no `curl`, and no other application on this Mac will accept anything it signs.
Compromise of the key buys an attacker the ability to MITM a process they would
already need local execution to influence.

Two properties are load-bearing and are pinned by tests:

- the private key is written `0600`, and
- nothing is written outside `ca_dir`.

`~/.backdoor` is the established home for router state (the failover breaker
already publishes there), so the CA lives at `~/.backdoor/ca` by default.
"""

from __future__ import annotations

import datetime as _dt
import logging
import re
import ssl
import threading
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.x509.oid import NameOID

logger = logging.getLogger(__name__)

# RSA-2048/SHA-256 rather than something more modern: this certificate is only
# ever validated by Node's OpenSSL, which accepts it everywhere, and 2048 keeps
# the first mint near 100ms instead of the several seconds 4096 would cost.
_KEY_BITS = 2048
_CA_YEARS = 10
_LEAF_DAYS = 397

# Clock skew between minting and validating is possible when the machine sleeps
# and resumes; backdate so a freshly minted leaf is never "not yet valid".
_BACKDATE = _dt.timedelta(minutes=5)

_SAFE_HOST = re.compile(r"[^a-z0-9.-]")


def _utcnow() -> _dt.datetime:
    return _dt.datetime.now(_dt.timezone.utc)


def _slug(host: str) -> str:
    """Filesystem-safe form of a hostname.

    Hostnames are already restricted, but a CONNECT target is attacker-supplied
    in principle, so anything outside `[a-z0-9.-]` is replaced rather than
    trusted. Leading dots are stripped so a crafted host cannot write a hidden
    file, and `..` cannot survive the substitution into a traversal.
    """
    cleaned = _SAFE_HOST.sub("_", host.lower()).lstrip(".")
    return cleaned or "unnamed"


def _atomic_write(path: Path, data: bytes, mode: int) -> None:
    """Write via a temp file in the same directory, then rename.

    A half-written certificate is worse than a missing one: the missing case
    regenerates, the truncated case fails the TLS handshake on every request
    until someone deletes it by hand.
    """
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_bytes(data)
    tmp.chmod(mode)
    tmp.replace(path)


class LocalCA:
    """Mints and caches certificates under a single directory.

    Thread-safe. `ForwardProxy` mints from `asyncio.to_thread`, and a cold start
    puts as many threads in here as the client opened connections — Claude Code
    opens roughly eight. Everything that reads-then-writes the CA or a leaf
    therefore runs under `_lock`, and everything loaded from disk is validated
    before use. See `test_concurrent_minting_yields_a_consistent_ca`.
    """

    def __init__(self, ca_dir: Path | str) -> None:
        self.ca_dir = Path(ca_dir).expanduser()
        self.leaf_dir = self.ca_dir / "leaf"
        self._contexts: dict[str, ssl.SSLContext] = {}
        # Reentrant: `leaf_files` holds the lock and calls `ensure`.
        self._lock = threading.RLock()

    # ── paths ────────────────────────────────────────────────────────────────

    @property
    def ca_cert_path(self) -> Path:
        return self.ca_dir / "ca-cert.pem"

    @property
    def ca_key_path(self) -> Path:
        return self.ca_dir / "ca-key.pem"

    # ── CA ───────────────────────────────────────────────────────────────────

    def _ca_is_usable(self) -> bool:
        """True only if the stored cert and key are the same keypair.

        Existence is not enough. The cert and key are two files, so a crash — or,
        before the lock existed, a second thread — can leave one from a CA that
        no longer matches the other. That state signs leaves the published cert
        cannot verify, which surfaces at the client as CERT_SIGNATURE_FAILURE
        rather than as anything pointing back here.
        """
        if not (self.ca_cert_path.exists() and self.ca_key_path.exists()):
            return False
        try:
            cert, key = self._load_ca()
        except Exception:
            logger.warning("CA at %s is unreadable; regenerating", self.ca_dir)
            return False
        if cert.public_key().public_numbers() != key.public_key().public_numbers():
            logger.warning(
                "CA cert and key at %s are different keypairs; regenerating",
                self.ca_dir,
            )
            return False
        return True

    def ensure(self) -> None:
        """Create the CA if it is not already on disk and intact. Idempotent."""
        with self._lock:
            self._ensure_locked()

    def _ensure_locked(self) -> None:
        if self._ca_is_usable():
            return

        self.ca_dir.mkdir(parents=True, exist_ok=True)
        self.ca_dir.chmod(0o700)

        key = rsa.generate_private_key(public_exponent=65537, key_size=_KEY_BITS)
        name = x509.Name(
            [
                x509.NameAttribute(NameOID.COMMON_NAME, "backdoor local CA"),
                x509.NameAttribute(NameOID.ORGANIZATION_NAME, "backdoor"),
            ]
        )
        now = _utcnow()
        cert = (
            x509.CertificateBuilder()
            .subject_name(name)
            .issuer_name(name)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - _BACKDATE)
            .not_valid_after(now + _dt.timedelta(days=365 * _CA_YEARS))
            # pathlen 0: this CA may sign leaves but not further CAs, so a
            # leaked leaf key cannot be used to build a second authority.
            .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
            .add_extension(
                x509.KeyUsage(
                    digital_signature=False,
                    content_commitment=False,
                    key_encipherment=False,
                    data_encipherment=False,
                    key_agreement=False,
                    key_cert_sign=True,
                    crl_sign=True,
                    encipher_only=False,
                    decipher_only=False,
                ),
                critical=True,
            )
            .sign(key, hashes.SHA256())
        )

        _atomic_write(
            self.ca_key_path,
            key.private_bytes(
                encoding=serialization.Encoding.PEM,
                format=serialization.PrivateFormat.TraditionalOpenSSL,
                encryption_algorithm=serialization.NoEncryption(),
            ),
            0o600,
        )
        _atomic_write(
            self.ca_cert_path, cert.public_bytes(serialization.Encoding.PEM), 0o644
        )

    def _load_ca(self) -> tuple[x509.Certificate, rsa.RSAPrivateKey]:
        cert = x509.load_pem_x509_certificate(self.ca_cert_path.read_bytes())
        key = serialization.load_pem_private_key(
            self.ca_key_path.read_bytes(), password=None
        )
        return cert, key  # type: ignore[return-value]

    # ── leaves ───────────────────────────────────────────────────────────────

    def _leaf_is_usable(
        self, cert_path: Path, key_path: Path, ca_cert: x509.Certificate
    ) -> bool:
        """True only if this leaf still verifies against the current CA.

        Catches both halves of the same class of problem: a leaf whose key file
        does not match its cert, and a leaf left behind by a CA that has since
        been regenerated. Either one hands the client a certificate it will
        reject, so re-minting is always cheaper than serving it.
        """
        if not (cert_path.exists() and key_path.exists()):
            return False
        try:
            leaf = x509.load_pem_x509_certificate(cert_path.read_bytes())
            key = serialization.load_pem_private_key(key_path.read_bytes(), password=None)
            if leaf.public_key().public_numbers() != key.public_key().public_numbers():
                return False
            if leaf.issuer != ca_cert.subject:
                return False
            ca_cert.public_key().verify(
                leaf.signature,
                leaf.tbs_certificate_bytes,
                padding.PKCS1v15(),
                leaf.signature_hash_algorithm,
            )
        except Exception:
            return False
        return True

    def leaf_files(self, host: str) -> tuple[Path, Path]:
        """Return `(cert_path, key_path)` for `host`, minting on first use.

        Cached on disk, so a router restart does not pay the keygen again.
        """
        with self._lock:
            return self._leaf_files_locked(host)

    def _leaf_files_locked(self, host: str) -> tuple[Path, Path]:
        self._ensure_locked()
        self.leaf_dir.mkdir(parents=True, exist_ok=True)
        self.leaf_dir.chmod(0o700)

        slug = _slug(host)
        cert_path = self.leaf_dir / f"{slug}.crt.pem"
        key_path = self.leaf_dir / f"{slug}.key.pem"

        ca_cert, ca_key = self._load_ca()
        if self._leaf_is_usable(cert_path, key_path, ca_cert):
            return cert_path, key_path

        # A leaf being replaced invalidates any SSLContext already built from it.
        self._contexts.pop(host, None)
        key = rsa.generate_private_key(public_exponent=65537, key_size=_KEY_BITS)
        now = _utcnow()
        cert = (
            x509.CertificateBuilder()
            .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, host)]))
            .issuer_name(ca_cert.subject)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - _BACKDATE)
            .not_valid_after(now + _dt.timedelta(days=_LEAF_DAYS))
            .add_extension(
                x509.SubjectAlternativeName([x509.DNSName(host)]), critical=False
            )
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
            .add_extension(
                x509.ExtendedKeyUsage([x509.oid.ExtendedKeyUsageOID.SERVER_AUTH]),
                critical=False,
            )
            .sign(ca_key, hashes.SHA256())
        )

        _atomic_write(
            key_path,
            key.private_bytes(
                encoding=serialization.Encoding.PEM,
                format=serialization.PrivateFormat.TraditionalOpenSSL,
                encryption_algorithm=serialization.NoEncryption(),
            ),
            0o600,
        )
        _atomic_write(cert_path, cert.public_bytes(serialization.Encoding.PEM), 0o600)
        return cert_path, key_path

    # ── TLS ──────────────────────────────────────────────────────────────────

    def server_ssl_context(self, host: str) -> ssl.SSLContext:
        """Server-side context presenting this host's leaf.

        ALPN advertises **only** `http/1.1`. That is not a detail: after the
        handshake the proxy splices the plaintext straight into uvicorn, which
        speaks HTTP/1.1. Let the client negotiate h2 and it would frame every
        request in a protocol the router cannot parse, which surfaces as a hang
        rather than a clean error.
        """
        with self._lock:
            cached = self._contexts.get(host)
            if cached is not None:
                return cached

            # Inside the lock: `_leaf_files_locked` clears this host's cached
            # context when it re-mints, and that must not race the insert below.
            cert_path, key_path = self._leaf_files_locked(host)
            ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            ctx.load_cert_chain(certfile=str(cert_path), keyfile=str(key_path))
            ctx.set_alpn_protocols(["http/1.1"])
            self._contexts[host] = ctx
            return ctx
