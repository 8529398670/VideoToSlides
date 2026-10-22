"""The one place this project does cryptography.

Nothing else in the app should import secrets, hashlib, hmac, bcrypt, nacl,
or any other primitive directly -- routing through here means there is a
single file to audit when you want to answer "how does this app handle
secrets", and a single file to change if a primitive ever needs swapping.

Two deliberate rules:

1. Random bytes come straight from the operating system's CSPRNG (the
   `secrets` module). A hand-rolled generator layered on top of a CSPRNG
   cannot be stronger than the CSPRNG, only harder to review, and every value
   here (session secrets, login secrets, keys) is one an attacker would love
   to predict.
2. Anything on an authentication path raises instead of swallowing a
   failure. A decrypt that quietly returns "" turns a tampered cookie into an
   empty-but-successful value, which is exactly the kind of failure that
   becomes an auth bypass.

The libraries are libsodium (through PyNaCl) for authenticated encryption and
key exchange, and bcrypt for one-time login secrets. Both ship prebuilt wheels
for Alpine (musllinux), so the Docker build needs no compiler.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import secrets

import bcrypt as _bcrypt
from nacl import bindings as _sodium
from nacl.exceptions import CryptoError as _CryptoError
from nacl.secret import SecretBox as _SecretBox


class BadKeyError(ValueError):
    """A key that is not 32 bytes of hex."""

    def __init__(self) -> None:
        super().__init__("encryption: key must be 32 bytes hex-encoded")


class CipherTextError(ValueError):
    """Cipher text that is malformed or was tampered with."""

    def __init__(self) -> None:
        super().__init__("encryption: cipher text is malformed or was tampered with")


# ---------------------------------------------------------------------------
# randomness and hashing
# ---------------------------------------------------------------------------


def random_bytes(length: int) -> bytes:
    return secrets.token_bytes(length)


def random_hex(length: int) -> str:
    """A hex string of exactly `length` characters."""
    return secrets.token_hex((length + 1) // 2)[:length]


def url_token(byte_length: int) -> str:
    """A URL-safe random token. Used for credential ids and secrets, so it
    must stay free of characters that would need escaping in a path segment
    or a cookie value -- base64url without padding is exactly that."""
    return secrets.token_urlsafe(byte_length)


def sha256_hex(value: str) -> str:
    """The lookup hash for high-entropy secrets (sessions, API keys). bcrypt
    is the wrong tool there: it is deliberately slow, and it would run on
    every authenticated request. Against a 256-bit random secret there is
    nothing to brute force, so a fast hash is the right call."""
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def constant_time_equal(a: str, b: str) -> bool:
    """Compare two digests or tokens without leaking, through how long the
    comparison takes, how many leading characters matched. `==` returns at
    the first difference, which is exactly the signal a timing attack feeds
    on."""
    return hmac.compare_digest(a.encode("utf-8"), b.encode("utf-8"))


def pkce_pair() -> tuple[str, str]:
    """(verifier, challenge) for OAuth PKCE with S256 (RFC 7636): a random
    verifier kept server-side, and the base64url SHA-256 of it sent with the
    consent request. The code Google returns is then useless to anyone who
    intercepts it without the verifier."""
    verifier = secrets.token_urlsafe(64)
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return verifier, challenge


# ---------------------------------------------------------------------------
# bcrypt -- for one-time login secrets
# ---------------------------------------------------------------------------


def bcrypt_hash(plain_text: str) -> str:
    """Used on login-link secrets, which are redeemed at most once. The cost
    is paid on a single redemption rather than on every request, so bcrypt's
    slowness buys real protection here without hurting throughput. Call it off
    the event loop -- it takes a noticeable fraction of a second on purpose."""
    return _bcrypt.hashpw(plain_text.encode("utf-8"), _bcrypt.gensalt()).decode("ascii")


def bcrypt_compare(hashed: str, plain_text: str) -> bool:
    try:
        return _bcrypt.checkpw(plain_text.encode("utf-8"), hashed.encode("ascii"))
    except ValueError:
        return False


# ---------------------------------------------------------------------------
# keys and encoding
# ---------------------------------------------------------------------------


def generate_key_hex() -> str:
    """A fresh 256-bit key as 64 hex characters -- the shape of
    server.secret_key in config.yaml."""
    return secrets.token_hex(32)


def _decode_key(key_hex: str) -> bytes:
    try:
        raw = bytes.fromhex(key_hex)
    except (ValueError, TypeError):
        raise BadKeyError() from None
    if len(raw) != 32:
        raise BadKeyError()
    return raw


def _b64encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64decode(encoded: str) -> bytes:
    """Strict base64url: anything outside the alphabet is an error rather
    than silently skipped, which is what the lenient decoder would do."""
    try:
        padded = encoded + "=" * (-len(encoded) % 4)
        return base64.b64decode(padded, altchars=b"-_", validate=True)
    except (binascii.Error, ValueError):
        raise CipherTextError() from None


# ---------------------------------------------------------------------------
# XChaCha20-Poly1305 -- cookie payloads and records at rest
# ---------------------------------------------------------------------------

_XCHACHA_NONCE_BYTES = _sodium.crypto_aead_xchacha20poly1305_ietf_NPUBBYTES
_XCHACHA_TAG_BYTES = _sodium.crypto_aead_xchacha20poly1305_ietf_ABYTES


def xchacha_encrypt_bytes(key_hex: str, plain_text: bytes) -> bytes:
    """The extended-nonce variant, so a 24-byte random nonce can be generated
    per message without any realistic risk of collision. The nonce is
    prepended to the cipher text; the Poly1305 tag means a modified value fails
    to open rather than decrypting to garbage."""
    key = _decode_key(key_hex)
    nonce = random_bytes(_XCHACHA_NONCE_BYTES)
    sealed = _sodium.crypto_aead_xchacha20poly1305_ietf_encrypt(plain_text, None, nonce, key)
    return nonce + sealed


def xchacha_decrypt_bytes(key_hex: str, encrypted: bytes) -> bytes:
    key = _decode_key(key_hex)
    # Too short to hold a nonce and a tag is refused here: libsodium's binding
    # reports that case as a ValueError rather than a decryption failure, and
    # a tampered cookie must never surface as anything but CipherTextError.
    if len(encrypted) < _XCHACHA_NONCE_BYTES + _XCHACHA_TAG_BYTES:
        raise CipherTextError()
    nonce, sealed = encrypted[:_XCHACHA_NONCE_BYTES], encrypted[_XCHACHA_NONCE_BYTES:]
    try:
        return _sodium.crypto_aead_xchacha20poly1305_ietf_decrypt(sealed, None, nonce, key)
    except (_CryptoError, ValueError, TypeError):
        raise CipherTextError() from None


def xchacha_encrypt_string(key_hex: str, plain_text: str) -> str:
    return _b64encode(xchacha_encrypt_bytes(key_hex, plain_text.encode("utf-8")))


def xchacha_decrypt_string(key_hex: str, encrypted: str) -> str:
    opened = xchacha_decrypt_bytes(key_hex, _b64decode(encrypted))
    try:
        return opened.decode("utf-8")
    except UnicodeDecodeError:
        raise CipherTextError() from None


# ---------------------------------------------------------------------------
# secretbox (XSalsa20-Poly1305)
#
# Not used by the base template. Here for authenticated blobs an app might
# want to hand out and accept back -- a signed download link, say.
# ---------------------------------------------------------------------------


def secretbox_encrypt(key_hex: str, plain_text: str) -> str:
    box = _SecretBox(_decode_key(key_hex))
    return _b64encode(bytes(box.encrypt(plain_text.encode("utf-8"))))


def secretbox_decrypt(key_hex: str, encrypted: str) -> str:
    box = _SecretBox(_decode_key(key_hex))
    try:
        return box.decrypt(_b64decode(encrypted)).decode("utf-8")
    except (_CryptoError, ValueError, TypeError):
        raise CipherTextError() from None


# ---------------------------------------------------------------------------
# X25519 key exchange
#
# Not wired into the login flow -- session cookies are opaque random values
# checked against the database, which needs no key exchange at all. It is
# here because apps built on this template sometimes need to hand a browser
# or a peer service a public key (end-to-end encrypted messages, payloads
# between two instances).
#
# The Go template also ships Kyber (post-quantum KEM) here. There is no
# well-maintained Python implementation with Alpine wheels to pair with it,
# so it is left out rather than vendored; add it when a feature needs it.
# ---------------------------------------------------------------------------


def x25519_generate_keypair() -> tuple[bytes, bytes]:
    """Returns (public_key, private_key), 32 bytes each."""
    private_key = random_bytes(_sodium.crypto_scalarmult_SCALARBYTES)
    public_key = _sodium.crypto_scalarmult_base(private_key)
    return public_key, private_key


def x25519_key_exchange(private_key: bytes, other_public_key: bytes) -> bytes:
    """The 32-byte shared secret. Feed it to the shared-secret helpers below
    rather than using it as a key directly anywhere else."""
    return _sodium.crypto_scalarmult(private_key, other_public_key)


def shared_secret_encrypt(shared_secret: bytes, message: bytes) -> bytes:
    return xchacha_encrypt_bytes(shared_secret.hex(), message)


def shared_secret_decrypt(shared_secret: bytes, encrypted: bytes) -> bytes:
    return xchacha_decrypt_bytes(shared_secret.hex(), encrypted)
