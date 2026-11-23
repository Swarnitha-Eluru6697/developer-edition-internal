"""Offline migration of legacy plaintext credential files (WS-SEC-002).

    python -m app.storage.migrate_credentials --dir /path/with/old/files

Point it at a directory holding `.db-credentials.json` and/or
`.git-credentials.json` recovered from old pods; credentials are written to
the configured cloud secrets backend (SECRETS_BACKEND etc.) and each file is
removed once fully migrated (rewritten with only the failures otherwise).
Exit status 1 if anything remains.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys

from app.storage.secrets import migrate_legacy_plaintext_credentials


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dir", required=True, help="directory containing the legacy credential files")
    args = parser.parse_args()
    report = asyncio.run(migrate_legacy_plaintext_credentials(args.dir))
    print(json.dumps(report, indent=2))
    if not report:
        print("Nothing migrated: SECRETS_BACKEND is 'local' or no legacy files were found.", file=sys.stderr)
    return 1 if any(v.get("remaining") or v.get("error") for v in report.values()) else 0


if __name__ == "__main__":
    sys.exit(main())
