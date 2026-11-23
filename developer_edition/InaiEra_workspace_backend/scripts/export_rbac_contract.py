"""Export the platform permission contract deterministically (X-002, contract 01-CONTRACT.md).

Usage:
    python scripts/export_rbac_contract.py <out-path>      # write to a file
    python scripts/export_rbac_contract.py -               # write to stdout
    python scripts/export_rbac_contract.py --check <path>  # exit 1 if <path> is stale

The document is `app.core.rbac.rbac_contract()`: every permission ID published in `permissions.json` and where each one is
enforced downstream. It holds no roles: roles and what they grant live in the Admin Portal. Committed as
`contracts/rbac.contract.v1.json` (tests/test_rbac_contract.py fails if it is stale). The frontend generates its permission
types from its copy of `permissions.json` (`npm run codegen:rbac`).

Nothing is contacted; the env defaults only let Settings be constructed on a bare runner.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

_ENV_DEFAULTS = {
    "ENVIRONMENT": "dev",
    "ORG_ID": "00000000-0000-0000-0000-000000000001",
    "DATABASE_URL": "postgresql+asyncpg://openapi:openapi@localhost:5432/openapi",
    "REDIS_URL": "redis://localhost:6379/15",
    "STORAGE_BACKEND": "local",
    "TRUST_ENVOY_HEADERS": "true",
    "ENVOY_INTERNAL_SECRET": "contract-export",
}
for _k, _v in _ENV_DEFAULTS.items():
    os.environ.setdefault(_k, _v)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def render() -> str:
    from app.core.rbac import rbac_contract

    return json.dumps(rbac_contract(), indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def main(argv: list[str]) -> int:
    if len(argv) == 3 and argv[1] == "--check":
        path = Path(argv[2])
        if not path.exists() or path.read_text() != render():
            print(f"{path} is stale: re-run `python scripts/export_rbac_contract.py {path}`", file=sys.stderr)
            return 1
        return 0
    if len(argv) != 2:
        print(__doc__, file=sys.stderr)
        return 2
    text = render()
    if argv[1] == "-":
        sys.stdout.write(text)
    else:
        Path(argv[1]).write_text(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
