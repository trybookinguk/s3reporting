#!/usr/bin/env python3
"""Decrypt a backup .enc file (e.g. .env.enc) produced by backup_to_sharepoint.py.

The nightly backup encrypts every file with the streaming Fernet format defined
in modules/utils/backup_crypto.py (magic header + per-file scrypt salt + framed
Fernet tokens). A generic tool like openssl/gpg cannot decrypt it — this uses
that same module, so it is guaranteed byte-compatible.

It loads backup_crypto DIRECTLY by file path, bypassing modules/utils/__init__.py,
so the only third-party dependency is `cryptography` (no pandas/boto3/pytz).

Usage:
  # decrypts .env.enc -> .env (asks for the passphrase; chmod 600 on .env)
  python3 deploy/decrypt_env.py

  # explicit input / output
  python3 deploy/decrypt_env.py path/to/.env.enc -o /root/s3reporting/.env

  # print to stdout instead of a file (does NOT touch disk)
  python3 deploy/decrypt_env.py .env.enc -o -

The passphrase (BACKUP_SECRET_PASSPHRASE) comes from that env var if set,
otherwise you are prompted (hidden input). Keep it in the password manager.
"""
import argparse
import importlib.util
import os
import stat
import sys
import tempfile
from pathlib import Path

# --- Load backup_crypto by path, without triggering modules/utils/__init__ ---
_CRYPTO_PATH = Path(__file__).resolve().parent.parent / "modules" / "utils" / "backup_crypto.py"


def _load_backup_crypto():
    if not _CRYPTO_PATH.exists():
        sys.exit(f"ERROR: cannot find {_CRYPTO_PATH} — run this from within the s3reporting repo.")
    spec = importlib.util.spec_from_file_location("backup_crypto", _CRYPTO_PATH)
    mod = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(mod)
    except ModuleNotFoundError as e:
        sys.exit(f"ERROR: missing dependency for decryption: {e.name}. "
                 f"Install it with:  pip install --break-system-packages cryptography")
    return mod


def main() -> int:
    p = argparse.ArgumentParser(description="Decrypt a backup .enc file (default: .env.enc -> .env).")
    p.add_argument("input", nargs="?", default=".env.enc",
                   help="encrypted input file (default: .env.enc)")
    p.add_argument("-o", "--output",
                   help="output path; '-' for stdout. Default: input with the .enc suffix removed.")
    p.add_argument("-f", "--force", action="store_true",
                   help="overwrite the output file if it already exists.")
    args = p.parse_args()

    bc = _load_backup_crypto()

    src_path = Path(args.input)
    if not src_path.is_file():
        sys.exit(f"ERROR: input file not found: {src_path}")

    # Resolve the passphrase (env var, else hidden prompt).
    try:
        passphrase = bc.resolve_passphrase(prompt_if_missing=True)
    except bc.BackupCryptoError as e:
        sys.exit(f"ERROR: {e}")

    to_stdout = args.output == "-"
    if to_stdout:
        # Peek/verify then stream plaintext bytes straight to stdout.
        try:
            with src_path.open("rb") as fsrc:
                bc.decrypt_stream(fsrc, sys.stdout.buffer, passphrase)
        except bc.BackupCryptoError as e:
            sys.exit(f"ERROR: {e}")
        return 0

    # Determine output path (strip a single trailing .enc if not given).
    if args.output:
        dst_path = Path(args.output)
    elif src_path.suffix == ".enc":
        dst_path = src_path.with_suffix("")
    else:
        dst_path = src_path.with_name(src_path.name + ".decrypted")

    if dst_path.exists() and not args.force:
        sys.exit(f"ERROR: {dst_path} already exists. Use --force to overwrite, or -o to pick another path.")

    # Decrypt to a temp file in the same dir, then atomically move into place.
    dst_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_fd, tmp_name = tempfile.mkstemp(prefix=".decrypt-", dir=str(dst_path.parent))
    try:
        with os.fdopen(tmp_fd, "wb") as fdst, src_path.open("rb") as fsrc:
            bc.decrypt_stream(fsrc, fdst, passphrase)
    except bc.BackupCryptoError as e:
        os.unlink(tmp_name)
        sys.exit(f"ERROR: {e}")
    except Exception:
        os.unlink(tmp_name)
        raise

    os.replace(tmp_name, dst_path)

    # Lock down secret output (any .env*, or an explicit .env target).
    if ".env" in dst_path.name or dst_path.suffix in (".env",):
        os.chmod(dst_path, stat.S_IRUSR | stat.S_IWUSR)  # 600
        print(f"Decrypted -> {dst_path}  (permissions set to 600)")
    else:
        print(f"Decrypted -> {dst_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
