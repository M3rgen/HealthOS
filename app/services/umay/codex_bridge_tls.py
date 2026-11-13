"""Transport identity of the Umay bridge: TLS pinned to a key both ends derive from the token.

The bridge is an external binary that speaks ``ws://`` with a bearer token, so anything that
holds its address while it is down would be handed that bearer and the conversation. The token
is the one secret both ends already share (``.env`` and the bridge home, both private), so the
TLS key is derived from it: the launcher's front proves it holds the key in the handshake, the
application refuses any other certificate, and nothing (no bearer, no message) is sent before.
Needing no extra setting, this works in both a native and a Docker deployment.

Anyone holding the token can compute the key, which is no new power: the token already grants
the bridge itself. Used by the application and the launcher; ``cryptography`` is a runtime
dependency of both (``uv sync --no-dev``).
"""

import datetime
import os
import ssl
import tempfile
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

_SALT = b"healthos-bridge-transport-v1"
_NAME = x509.Name([x509.NameAttribute(x509.NameOID.COMMON_NAME, "healthos-bridge")])
# A fixed, generous validity: the identity is the key, and a wrong host clock must not lock
# the chat out. The certificate is reproducible byte for byte, like its Ed25519 signature.
_NOT_BEFORE = datetime.datetime(2000, 1, 1, tzinfo=datetime.UTC)
_NOT_AFTER = datetime.datetime(2200, 1, 1, tzinfo=datetime.UTC)


def secure_bridge_url(url: str) -> str:
    """The URL to dial: always ``wss://``, also when ``.env`` predates this module."""
    return "wss://" + url.removeprefix("ws://") if url.startswith("ws://") else url


def _private_key(token: str) -> Ed25519PrivateKey:
    derived = HKDF(hashes.SHA256(), 32, _SALT, b"tls-server-key").derive(token.encode())
    return Ed25519PrivateKey.from_private_bytes(derived)


def certificate_pem(token: str) -> bytes:
    """The one certificate the bridge may present for this token."""
    key = _private_key(token)
    builder = (
        x509.CertificateBuilder()
        .subject_name(_NAME)
        .issuer_name(_NAME)
        .public_key(key.public_key())
        .serial_number(1)
        .not_valid_before(_NOT_BEFORE)
        .not_valid_after(_NOT_AFTER)
        # The same certificate is the leaf and the only trust anchor.
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
    )
    certificate = builder.sign(key, algorithm=None)
    return certificate.public_bytes(serialization.Encoding.PEM)


def client_context(token: str) -> ssl.SSLContext:
    """TLS 1.3, trusting only the certificate derived from ``token`` (the name is not checked)."""
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.minimum_version = ssl.TLSVersion.TLSv1_3
    context.check_hostname = False
    context.load_verify_locations(cadata=certificate_pem(token).decode())
    return context


def server_context(token: str, directory: Path) -> ssl.SSLContext:
    """TLS 1.3 with the derived key, which reaches the disk only for the moment of loading."""
    key = _private_key(token).private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_3
    descriptor, name = tempfile.mkstemp(dir=directory, prefix=".bridge-tls.", suffix=".pem")
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(certificate_pem(token) + key)
        context.load_cert_chain(name)
    finally:
        Path(name).unlink(missing_ok=True)
    return context
