"""Passphrase-based encryption for the SharePoint backup.

Encrypts every backed-up file so the nightly backup never ships plaintext to
SharePoint — required by the "all client data encrypted at rest" commitment.
Symmetric, passphrase-derived: the passphrase is the only thing needed to
restore, and it is NEVER stored in SharePoint or in git. For the automated
nightly run it comes from the BACKUP_SECRET_PASSPHRASE env var (set in .env); on
a bare-metal restore the operator is prompted for it (the .env that holds it is
itself one of the files being restored, so it can't be read from there at
restore time — keep the passphrase in a password manager).

Why home-grown framing rather than `age`/`gpg`: the `cryptography` lib is
already a dependency on the Pi, so this adds no new system package and behaves
identically on a fresh box.

STREAMING by design. The backup set includes a 3.5 GB warehouse on a 4 GB Pi,
so neither encrypt nor decrypt may hold a whole file in memory. Both sides work
on a stream of fixed-size frames:

    header  = MAGIC(7) + salt(16)
    then repeated:  length(4 bytes, big-endian) + Fernet_token(length bytes)

Each frame is an independent Fernet token over one CHUNK_SIZE slice of plaintext
(the last frame is short). Peak memory is ~one chunk of plaintext plus its
ciphertext, regardless of file size. Per-file random salt feeds scrypt, so the
same passphrase yields a different key per file. Frames are decrypted in order;
Fernet authenticates each, so tampering or truncation is detected.
"""
import base64
import os
import struct

from cryptography.fernet import Fernet
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt

MAGIC = b"S3RBK2\n"  # bump the trailing digit if the framing changes
_SALT_LEN = 16
# scrypt cost params. n=2**15 is interactive-grade and sub-second on the Pi.
_SCRYPT_N = 2 ** 15
_SCRYPT_R = 8
_SCRYPT_P = 1

# Plaintext bytes per frame. 4 MB keeps peak memory tiny even for multi-GB files
# and means a 3.5 GB warehouse is ~900 frames — negligible framing overhead.
CHUNK_SIZE = 4 * 1024 * 1024
_LEN_STRUCT = struct.Struct(">I")  # 4-byte big-endian frame length

PASSPHRASE_ENV = "BACKUP_SECRET_PASSPHRASE"


class BackupCryptoError(RuntimeError):
    """Raised for a missing passphrase or a failed/garbled decrypt."""


def _derive_key(passphrase: str, salt: bytes) -> bytes:
    """Derive a urlsafe-base64 Fernet key from a passphrase + salt via scrypt."""
    kdf = Scrypt(salt=salt, length=32, n=_SCRYPT_N, r=_SCRYPT_R, p=_SCRYPT_P)
    return base64.urlsafe_b64encode(kdf.derive(passphrase.encode("utf-8")))


def is_encrypted_header(first_bytes: bytes) -> bool:
    """True if a buffer starts with our magic header. Callers peek just the
    first few bytes of a stream so a large plaintext file is never buffered."""
    return first_bytes[: len(MAGIC)] == MAGIC


def encrypt_stream(src, dst, passphrase: str) -> None:
    """Encrypt file-like `src` to file-like `dst` (both binary), framing the
    plaintext into independently-encrypted chunks. Bounded memory."""
    if not passphrase:
        raise BackupCryptoError("empty passphrase")
    salt = os.urandom(_SALT_LEN)
    fernet = Fernet(_derive_key(passphrase, salt))
    dst.write(MAGIC)
    dst.write(salt)
    while True:
        chunk = src.read(CHUNK_SIZE)
        if not chunk:
            break
        token = fernet.encrypt(chunk)
        dst.write(_LEN_STRUCT.pack(len(token)))
        dst.write(token)


def decrypt_stream(src, dst, passphrase: str) -> None:
    """Reverse encrypt_stream: decrypt framed file-like `src` to `dst`. Raises
    BackupCryptoError on wrong passphrase, foreign data, or truncation."""
    if not passphrase:
        raise BackupCryptoError("empty passphrase")
    magic = src.read(len(MAGIC))
    if magic != MAGIC:
        raise BackupCryptoError("not an S3RBK-encrypted stream (bad magic header)")
    salt = src.read(_SALT_LEN)
    if len(salt) != _SALT_LEN:
        raise BackupCryptoError("truncated header (missing salt)")
    fernet = Fernet(_derive_key(passphrase, salt))
    while True:
        len_bytes = src.read(_LEN_STRUCT.size)
        if not len_bytes:
            break  # clean end of stream
        if len(len_bytes) != _LEN_STRUCT.size:
            raise BackupCryptoError("truncated frame length")
        (n,) = _LEN_STRUCT.unpack(len_bytes)
        token = src.read(n)
        if len(token) != n:
            raise BackupCryptoError("truncated frame body")
        try:
            dst.write(fernet.decrypt(token))
        except Exception as e:  # InvalidToken et al.
            raise BackupCryptoError(
                "decrypt failed — wrong passphrase or corrupt backup"
            ) from e


# --- small-buffer helpers (for callers holding bytes, e.g. tests) -----------

def encrypt_bytes(data: bytes, passphrase: str) -> bytes:
    import io

    out = io.BytesIO()
    encrypt_stream(io.BytesIO(data), out, passphrase)
    return out.getvalue()


def decrypt_bytes(blob: bytes, passphrase: str) -> bytes:
    import io

    out = io.BytesIO()
    decrypt_stream(io.BytesIO(blob), out, passphrase)
    return out.getvalue()


def resolve_passphrase(prompt_if_missing: bool = False) -> str:
    """Get the backup passphrase from the env var, optionally falling back to an
    interactive prompt (used by restore, where .env doesn't exist yet)."""
    pw = os.environ.get(PASSPHRASE_ENV, "").strip()
    if pw:
        return pw
    if prompt_if_missing:
        import getpass

        pw = getpass.getpass(f"  {PASSPHRASE_ENV} (hidden): ").strip()
        if pw:
            return pw
    raise BackupCryptoError(
        f"{PASSPHRASE_ENV} not set — cannot encrypt/decrypt the backup files"
    )
