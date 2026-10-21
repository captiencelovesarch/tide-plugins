"""Make the store's signing key: ed25519, the private half written 0600
outside this repo, the public half printed as hex for tide's
store.PUBLIC_KEY.

    python tools/keygen.py [--out PATH]
    python tools/keygen.py --show [--out PATH]    # print the public hex again

Back the private key up somewhere offline. Losing it means a new key and
a tide release before the store can change again. Leaking it means
anyone can sign an index tide trusts.
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import (
    Encoding, NoEncryption, PrivateFormat, PublicFormat, load_pem_private_key)

REPO = Path(__file__).resolve().parent.parent
DEFAULT = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") \
    / "tide-plugins" / "signing-key.pem"


def public_hex(key: Ed25519PrivateKey) -> str:
    return key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw).hex()


def load(path: Path) -> Ed25519PrivateKey:
    key = load_pem_private_key(path.read_bytes(), None)
    if not isinstance(key, Ed25519PrivateKey):
        raise SystemExit(f"{path} isn't an ed25519 key")
    return key


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--out", type=Path, default=DEFAULT, help=f"default: {DEFAULT}")
    ap.add_argument("--show", action="store_true", help="print the public key of an existing key")
    args = ap.parse_args()
    out = args.out.expanduser().resolve()

    if args.show:
        print(public_hex(load(out)))
        return 0
    if out == REPO or REPO in out.parents:
        print(f"refusing to write the key inside the repo ({REPO})", file=sys.stderr)
        return 1
    if out.exists():
        print(f"{out} exists already. not overwriting a signing key", file=sys.stderr)
        print(f"public key: {public_hex(load(out))}")
        return 1

    key = Ed25519PrivateKey.generate()
    pem = key.private_bytes(Encoding.PEM, PrivateFormat.PKCS8, NoEncryption())
    out.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    # created 0600 from the start, never readable by anyone else even briefly
    fd = os.open(out, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(pem)
    print(f"private key: {out} (0600). back it up offline.")
    print(f"public key:  {public_hex(key)}")
    print("the public key goes in tide's src/tide/store.py as PUBLIC_KEY.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
