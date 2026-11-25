"""Export the Workspace service's OpenAPI document deterministically (WS-FE-014).

Usage:
    python scripts/export_openapi.py <out-path>      # write to a file
    python scripts/export_openapi.py -               # write to stdout
    python scripts/export_openapi.py --check <path>  # exit 1 if <path> is stale

Imports ``app.main:app`` and serialises ``app.openapi()`` with sorted keys and
a trailing newline, so the same code always produces byte-identical output.
That is what makes the committed contract copies diffable:

  * ``openapi/workspace.json`` in this repo — compared in CI by
    .github/workflows/api-contract.yml (drift fails the build).
  * ``openapi/workspace.v<version>.json`` in the frontend repo — the snapshot
    the SPA generates its TypeScript types from (``npm run codegen:api``).

No server, database, or Redis is contacted: building the schema only walks the
route table. The env defaults below mirror tests/conftest.py (except
DATABASE_URL, see below) so Settings can be
constructed on a bare CI runner; anything already set in the environment wins.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

_ENV_DEFAULTS = {
    "ENVIRONMENT": "dev",
    "ORG_ID": "00000000-0000-0000-0000-000000000001",
    # asyncpg (a runtime dependency) rather than conftest's aiosqlite (a dev
    # extra): creating the engine never connects, so any well-formed URL works.
    "DATABASE_URL": "postgresql+asyncpg://openapi:openapi@localhost:5432/openapi",
    "REDIS_URL": "redis://localhost:6379/15",
    "STORAGE_BACKEND": "local",
    "TRUST_ENVOY_HEADERS": "true",
    "ENVOY_INTERNAL_SECRET": "openapi-export",
}


def render() -> str:
    for key, value in _ENV_DEFAULTS.items():
        os.environ.setdefault(key, value)
    # Make `import app` work when run as `python scripts/export_openapi.py`.
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from app.main import app  # noqa: PLC0415 — env must be set first

    return json.dumps(app.openapi(), indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def main(argv: list[str]) -> int:
    if len(argv) == 2 and argv[0] == "--check":
        target = Path(argv[1])
        current = render()
        committed = target.read_text(encoding="utf-8") if target.exists() else ""
        if current != committed:
            sys.stderr.write(
                f"OpenAPI contract drift: {target} does not match the app's schema.\n"
                f"Regenerate with: python scripts/export_openapi.py {target}\n"
            )
            return 1
        return 0
    if len(argv) != 1:
        sys.stderr.write(__doc__ or "")
        return 2
    output = render()
    if argv[0] == "-":
        sys.stdout.write(output)
    else:
        path = Path(argv[0])
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(output, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
