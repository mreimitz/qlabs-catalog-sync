#!/usr/bin/env python3
"""Print a master key for the credential store, and how to install it.

Run once per deployment::

    uv run python scripts/make_secret_key.py

The key encrypts every credential an operator saves in the console
(``qlabs_catalog_sync.configstore.crypto``). It is *deployment* configuration -- set once,
at install time -- not per-client configuration: adding a client never touches it.

Two things this deliberately does not do:

* **It does not write the key anywhere.** Where a deployment keeps a secret is the
  deployment's decision (a mounted file, a systemd credential, a cloud secret manager),
  and a script that silently dropped one into the working directory would be the wrong
  default in most of them.
* **It does not read an existing key or check whether one is already set.** Generating a
  *second* key for a store that already holds credentials makes every one of them
  unreadable, so this prints a warning rather than pretending to be idempotent.

Mirrors ``scripts/make_admin_hash.py``: a plain script an operator can run, whose output
they paste into their own secret store.
"""

from __future__ import annotations

import sys
from pathlib import Path

_SRC = Path(__file__).resolve().parent.parent / "packages" / "qlabs-catalog-sync" / "src"
if str(_SRC) not in sys.path:  # pragma: no cover - import plumbing for a standalone script
    sys.path.insert(0, str(_SRC))

from qlabs_catalog_sync.configstore.crypto import (  # noqa: E402
    MASTER_KEY_ENV_VAR,
    MASTER_KEY_FILE_ENV_VAR,
    generate_master_key,
)


def main() -> int:
    key = generate_master_key()
    print()
    print("A new master key for the credential store:")
    print()
    print(f"  {MASTER_KEY_ENV_VAR}={key}")
    print()
    print("Install it one of two ways, in the environment the service runs in:")
    print()
    print(f"  * {MASTER_KEY_FILE_ENV_VAR}=/path/to/master.key   (preferred)")
    print("      Write ONLY the key above into that file, and give it mode 0600. A file")
    print("      has an owner and a mode, and does not leak into `ps`, container inspect")
    print("      output, or a crash report the way an environment variable does.")
    print()
    print(f"  * {MASTER_KEY_ENV_VAR}=<the key above>")
    print("      Simpler for a laptop or a one-box deployment.")
    print()
    print("WARNING: keep it. Every credential saved in the console is encrypted with it,")
    print("and a lost key means every one of them has to be entered again. Generating a")
    print("second key does NOT re-encrypt what is already stored.")
    print()
    print("Keep it off the same medium as your database backups -- holding both is the")
    print("one case this encryption does not protect against.")
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
