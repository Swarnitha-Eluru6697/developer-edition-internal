"""Turn an exported template into a Platform template, and check the lot.

How a template gets from the InAiEra authoring instance into a release:

  1. Build and test it in Studio on the authoring instance.
  2. "Save as template" (a draft template there), then Export on the
     Templates page — that downloads a package file.
  3. Promote it into the repo:

         python -m app.promote_template ~/Downloads/lead-scoring.template.json \\
             --key lead-scoring --category Sales --vertical Banking

     This validates it, gives it a stable Platform key, marks it
     first-party, and writes `app/template_packages/lead-scoring.json`.
  4. Commit the file. CI runs `python -m app.promote_template --check`.
  5. The next release ships it, and the seeder (seed.sync_platform_templates)
     adds it to every data plane on deploy or upgrade.

To ship a NEW VERSION of an existing template, promote again with the
same --key and a higher --version (re-promoting at the same or a lower
version is refused, so a release cannot silently go backwards).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Dict, List, Optional

from .template_packages import (
    CATEGORIES, PACKAGE_VERSION, PACKAGES_DIR, PackageError, load_platform_packages, parse_package,
)


def _vtuple(v: str):
    return tuple(int(x) for x in v.split("."))


def _lock_path(directory: Path) -> Path:
    directory = Path(directory)
    return directory.parent / f"{directory.name}.lock.json"


def _canonical_package(p: dict) -> dict:
    return {
        "package_version": PACKAGE_VERSION,
        "manifest": {
            k: p.get(k)
            for k in (
                "key", "name", "version", "type", "app_type", "category",
                "vertical", "tags", "desc", "first_party",
            )
        },
        "flow": p["flow"],
        "requirements": p["requirements"],
    }


def _digest(p: dict) -> str:
    raw = json.dumps(
        _canonical_package(p), sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _read_lock(directory: Path) -> Dict[str, dict]:
    path = _lock_path(directory)
    if not path.exists():
        return {}
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PackageError(f"{path.name}: invalid package lock ({exc})") from exc
    packages = doc.get("packages") if isinstance(doc, dict) else None
    if not isinstance(packages, dict):
        raise PackageError(f"{path.name}: expected an object with a 'packages' map")
    return packages


def _write_lock(directory: Path, entries: Dict[str, dict]) -> None:
    path = _lock_path(directory)
    doc = {"lock_version": 1, "packages": dict(sorted(entries.items()))}
    path.write_text(json.dumps(doc, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def _flow_errors(p: dict) -> List[str]:
    """Publish-level structural validation for a shipped template package."""
    from .flow_validator import validate_flow

    result = validate_flow(
        (p.get("flow") or {}).get("nodes", []),
        (p.get("flow") or {}).get("edges", []),
        p.get("app_type"),
    )
    return [i.message for i in result.errors]


def _validate_package_flows(packages: List[dict]) -> List[str]:
    problems: List[str] = []
    for p in packages:
        for message in _flow_errors(p):
            problems.append(f"{p['key']}@{p['version']}: {message}")
    return problems


def promote(
    src: Path, *, key: str, category: Optional[str] = None, vertical: Optional[str] = None,
    version: Optional[str] = None, dest_dir: Path = PACKAGES_DIR,
) -> Path:
    """Validate `src` and write it into `dest_dir` as a Platform package.

    Raises PackageError, leaving `dest_dir` exactly as it was, if the
    result would not load — including when it would collide with a key or
    name already shipped."""
    try:
        doc = json.loads(Path(src).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        raise PackageError(f"{src}: cannot read as JSON ({e})") from e
    if not isinstance(doc, dict) or not isinstance(doc.get("manifest"), dict):
        raise PackageError(f"{src}: not a template package (no manifest)")

    m = dict(doc["manifest"])
    m["key"] = key
    m["first_party"] = True
    if category:
        m["category"] = category
    if vertical is not None:
        m["vertical"] = vertical or None
    if version:
        m["version"] = version
    doc = {"package_version": PACKAGE_VERSION, "manifest": m, "flow": doc.get("flow")}

    p = parse_package(doc, source_label=str(src))           # also enforces the key/category/version rules
    flow_errors = _flow_errors(p)
    if flow_errors:
        raise PackageError(
            f"{src}: flow is not publishable: " + "; ".join(flow_errors)
        )
    if key.startswith("private-"):
        raise PackageError(f"key {key!r} uses the 'private-' prefix, which is reserved for customer templates")

    dest_dir = Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)
    target = dest_dir / f"{key}.json"
    previous = target.read_text(encoding="utf-8") if target.exists() else None
    lock_entries = _read_lock(dest_dir)
    old_versions = []
    if previous is not None:
        old_versions.append(json.loads(previous)["manifest"]["version"] )
    if key in lock_entries and isinstance(lock_entries[key], dict) and lock_entries[key].get("version"):
        old_versions.append(str(lock_entries[key]["version"]))
    if old_versions:
        old_version = max(old_versions, key=_vtuple)
        if _vtuple(p["version"]) <= _vtuple(old_version):
            raise PackageError(
                f"{key} is already recorded at version {old_version}; pass --version higher than that "
                f"to release a new version (got {p['version']}).")

    out = {
        "package_version": PACKAGE_VERSION,
        "manifest": {k: p[k] for k in ("key", "name", "version", "type", "app_type", "category", "vertical", "tags", "desc", "first_party")},
        "flow": p["flow"],
        "requirements": p["requirements"],
    }
    target.write_text(json.dumps(out, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    try:
        packages = load_platform_packages(dest_dir)          # whole directory must still be valid and unique
        flow_problems = _validate_package_flows(packages)
        if flow_problems:
            raise PackageError("; ".join(flow_problems))
    except PackageError:
        if previous is None:
            target.unlink()
        else:
            target.write_text(previous, encoding="utf-8")
        raise

    # CI can now detect manual package edits or version bumps that bypass the
    # promotion command. Retired keys remain in the lock as immutable history.
    lock_entries[key] = {"version": p["version"], "sha256": _digest(p)}
    _write_lock(dest_dir, lock_entries)
    return target


def check(directory: Path = PACKAGES_DIR) -> List[str]:
    """Every shipped-package release defect as readable lines (empty = OK).

    Besides package-schema/name uniqueness, CI applies the same flow validator
    used by template Publish and checks the immutable package lock. This catches
    hand-edited package content without a version bump before it can ever reach
    a data plane.
    """
    try:
        packages = load_platform_packages(directory)
        lock_entries = _read_lock(directory)
    except PackageError as e:
        return [str(e)]

    problems = _validate_package_flows(packages)
    if not lock_entries:
        problems.append(
            f"{_lock_path(directory).name}: missing package lock; promote shipped templates to create it"
        )
        return problems

    for p in packages:
        entry = lock_entries.get(p["key"])
        if not isinstance(entry, dict):
            problems.append(f"{p['key']}@{p['version']}: missing from package lock; use promote_template")
            continue
        locked_version = str(entry.get("version") or "")
        locked_digest = str(entry.get("sha256") or "")
        actual_digest = _digest(p)
        if locked_version == p["version"] and locked_digest != actual_digest:
            problems.append(
                f"{p['key']}@{p['version']}: content changed without a version bump; use promote_template with a higher --version"
            )
        elif locked_version != p["version"] or locked_digest != actual_digest:
            problems.append(
                f"{p['key']}@{p['version']}: package/lock mismatch (locked {locked_version or 'missing'}); use promote_template"
            )
    return problems


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m app.promote_template", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("package", nargs="?", help="exported template file to promote")
    ap.add_argument("--key", help="stable Platform key: lowercase letters, digits, hyphens")
    ap.add_argument("--category", choices=CATEGORIES)
    ap.add_argument("--vertical", help="industry, or '' for cross-industry")
    ap.add_argument("--version", help="semantic version, e.g. 1.1.0")
    ap.add_argument("--check", action="store_true", help="validate every shipped package (for CI) and exit")
    a = ap.parse_args(argv)

    if a.check:
        problems = check()
        for line in problems:
            print(f"ERROR: {line}", file=sys.stderr)
        if not problems:
            print(f"OK: {len(load_platform_packages())} Platform template packages valid")
        return 1 if problems else 0

    if not a.package or not a.key:
        ap.error("a package file and --key are required (or use --check)")
    try:
        path = promote(Path(a.package), key=a.key, category=a.category, vertical=a.vertical, version=a.version)
    except PackageError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1
    print(f"Wrote {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
