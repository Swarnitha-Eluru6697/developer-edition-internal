"""
Hardening: the migration chain is structurally sound — one head, no branches, every revision reversible. Runs everywhere (no database).

The chain's own limits are documented in tests/test_migrations_real_db.py and app/startup.py: it is a series of DELTAS written against a database
that `Base.metadata.create_all` already built, so it is deliberately never run from revision 0001 on a virgin database.
"""

import ast
import re
from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory

BACKEND = Path(__file__).resolve().parents[1]
VERSIONS = BACKEND / "alembic" / "versions"


def _script() -> ScriptDirectory:
    cfg = Config(str(BACKEND / "alembic.ini"))
    cfg.set_main_option("script_location", str(BACKEND / "alembic"))
    return ScriptDirectory.from_config(cfg)


def test_there_is_exactly_one_head() -> None:
    heads = _script().get_heads()
    assert len(heads) == 1, f"the migration history has branched: {heads} — merge them"


def test_the_history_is_one_straight_line_from_the_first_revision() -> None:
    script = _script()
    revisions = list(script.walk_revisions())
    assert len(revisions) == len(list(VERSIONS.glob("[0-9]*.py"))), "a version file is not part of the history"
    for revision in revisions:
        assert not isinstance(revision.down_revision, tuple), f"{revision.revision} is a merge point"
    roots = [r for r in revisions if r.down_revision is None]
    assert len(roots) == 1, "more than one root revision"


def test_every_revision_id_is_well_formed_numbered_like_its_file_and_unique() -> None:
    """The version table column is VARCHAR(255) (alembic/env.py widens it). The numeric prefix must match the file's so the order is
    readable and two migrations can never claim the same number. (0001 predates the convention: file `0001_initial_dataconnect.py`
    declares `0001_initial`; renaming a revision id buys nothing, so only the prefix is enforced.)"""
    seen: dict[str, str] = {}
    for revision in _script().walk_revisions():
        stem = Path(revision.path).stem
        number = revision.revision.split("_")[0]
        assert number == stem.split("_")[0], f"{stem}.py declares revision {revision.revision!r}"
        assert len(revision.revision) <= 255
        assert re.fullmatch(r"\d{4}_[a-z0-9_]+", revision.revision), revision.revision
        assert number not in seen, f"{number} is used by both {seen[number]} and {revision.revision}"
        seen[number] = revision.revision


def _downgrade_is_real(path: Path) -> bool:
    tree = ast.parse(path.read_text())
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == "downgrade":
            body = [n for n in node.body if not (isinstance(n, ast.Expr) and isinstance(n.value, ast.Constant))]   # drop the docstring
            if not body:
                return False
            if all(isinstance(n, ast.Pass) for n in body):
                return False
            return not any(isinstance(n, ast.Raise) and "NotImplementedError" in ast.dump(n) for n in body)
    return False


def test_every_recent_migration_can_be_reversed() -> None:
    """A deploy that goes wrong is undone by downgrading. From 0040 on, a no-op or NotImplemented downgrade is not acceptable."""
    irreversible = [p.name for p in sorted(VERSIONS.glob("00[4-9][0-9]_*.py")) if not _downgrade_is_real(p)]
    assert irreversible == [], f"these cannot be downgraded: {irreversible}"


def test_the_downgrade_check_itself_recognises_a_fake_one(tmp_path: Path) -> None:
    fake = tmp_path / "0099_fake.py"
    fake.write_text('def upgrade():\n    pass\n\n\ndef downgrade():\n    """nothing"""\n    pass\n')
    assert not _downgrade_is_real(fake)
    fake.write_text("def downgrade():\n    raise NotImplementedError\n")
    assert not _downgrade_is_real(fake)
    fake.write_text("def downgrade():\n    op.drop_column('t', 'c')\n")
    assert _downgrade_is_real(fake)
