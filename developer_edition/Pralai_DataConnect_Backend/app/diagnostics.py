"""
`python -m app.diagnostics > bundle.json` — the support bundle for the whole deployment (aggregated across organizations), from a shell, even
when the API is down. Same redaction and per-section isolation as the HTTP endpoint (app/services/diagnostics.py); exits non-zero only if the
bundle could not be produced at all, never merely because a section failed (that is reported inside it).
"""

import asyncio
import json
import sys


def main() -> None:
    # stdout carries ONLY the JSON bundle. Logging (and SQLAlchemy's echo) write to whatever sys.stdout is when they are configured, so
    # point it at stderr for the whole run — before anything is configured — and write the bundle to the real stdout at the end.
    real_stdout, sys.stdout = sys.stdout, sys.stderr
    try:
        from app.core.logging import configure_logging
        from app.services.diagnostics import build_diagnostics_bundle

        configure_logging()
        bundle = asyncio.run(build_diagnostics_bundle(org_id=None, in_service_process=False))
    finally:
        sys.stdout = real_stdout
    json.dump(bundle, sys.stdout, indent=2, default=str)
    sys.stdout.write("\n")
    if bundle["failed_sections"]:
        print(f"note: sections that failed (reported inside the bundle): {', '.join(bundle['failed_sections'])}", file=sys.stderr)


if __name__ == "__main__":
    main()
