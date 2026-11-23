# Database migrations: how they really work, and how they are tested

## The model

* `Base.metadata.create_all` (the ORM models) is the **source of truth for a database's initial shape**.
* The Alembic revisions are **deltas** written against a database that `create_all` already built. They are *not* a script that can build one
  from nothing: run from `0001` on an empty database the chain fails at `0042`, and even if that is patched it produces a schema that differs
  from the models in 142 ways (UUID instead of VARCHAR template ids, missing indexes, different nullability) — a database the app cannot use.

## What `python -m app.startup` therefore does (`run_migrations`)

| Database state | Action |
|---|---|
| **Virgin** (no `alembic_version`, no `dc_*` table) | create the schema from the models, then `alembic stamp head` |
| **Existing** | `alembic upgrade head` — as always |

Before this, a virgin database made the backend container fail at startup (`operator does not exist: text = uuid` in `0042`), contradicting
`docker-compose.fresh.yml`'s "a virgin database self-initialises".

## Releases

There are no release tags, so "the previous release" is the constant `PREVIOUS_RELEASE` in `tests/test_migrations_real_db.py` (currently
`0043_topic_separator`). **Move it forward when you cut a release.**

## Migrations that refuse to run

A migration that would have to choose between two running things stops instead, names them, and leaves the database exactly as it was.

| Migration | Refuses while | What to do |
|---|---|---|
| `0044_drop_topic_separator` | a pipeline still has a `.`-separated topic | delete or recreate it, then run again |
| `0053_dead_letter_topic_unique` | two or more **sink** pipelines with a dead-letter policy share a dead-letter topic (same org, domain, source system, entity and version) | the error lists each group. Change the entity or version of all but one pipeline in the group, or give all but one a policy without a dead-letter queue (`FAIL_FAST` or `SKIP_AND_LOG`), then run again |

`0053` adds the unique index that makes "one sink per dead-letter topic" a rule the database enforces (NEW-08). It cannot pick which running
connector keeps its queue: changing one moves its failed records to a different topic.

## Tests

| Test | Needs a DB | Proves |
|---|---|---|
| `tests/test_migration_chain.py` | no | one head, no branches, numbering unique, every recent migration has a real `downgrade` |
| `tests/test_migrations_real_db.py` | yes (`DATACONNECT_REAL_DB_TESTS=1`, role with `CREATEDB`) | virgin DB -> models + stamped at head, zero drift; previous release -> head keeps its data and backfills correctly; a database with dotted topics, or with two sinks sharing a dead-letter queue, is **refused** and left untouched; every step downgrades and upgrades again losslessly; the result equals the models |

The real-DB tests create and drop their own scratch database; the configured database is never touched.

```bash
DATACONNECT_DATABASE_URL=postgresql+asyncpg://dataconnect:dataconnect@localhost:5435/dataconnect \
DATACONNECT_REAL_DB_TESTS=1 .venv/bin/python -m pytest tests/test_migrations_real_db.py --no-cov
```

## Known debt (not fixed here)

The chain cannot build a database from nothing, and a database built that way would drift from the models. Making it able to would mean
replacing the early revisions with a baseline migration generated from the current models. Until then the bootstrap above is the supported
path, and `test_the_chain_still_refuses_to_run_from_nothing…` fails loudly if the chain is "repaired" without making the schema match.
