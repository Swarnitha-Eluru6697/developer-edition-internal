import logging
import os

from sqlmodel import SQLModel, create_engine, Session
from sqlalchemy import text

logger = logging.getLogger(__name__)

# Postgres + pgvector, per §10 of the design doc — "already platform-standard,"
# not a new dependency. Configurable via env var for different environments
# (docker-compose service name in the container network, localhost for
# running the backend directly against a local/dev Postgres).
DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql+psycopg2://inaiera:inaiera_dev@127.0.0.1:5432/inaiera_agents",
)

# Real, tunable pool sizing (previously hardcoded via SQLAlchemy's own
# untuned defaults — pool_size=5, max_overflow=10 — see GAP_REPORT's
# scaling discussion). This is the actual lever a Kubernetes deployment
# needs: with N backend replicas, each replica's own real pool
# multiplies against Postgres's real connection cap, so the safe value
# here depends on how many replicas are actually running and whether
# they connect directly to Postgres or through a real pooler (PgBouncer
# — see k8s/04-pgbouncer-*.yaml) sitting in front of it. Defaults are
# unchanged from SQLAlchemy's own real defaults, so this is additive —
# nothing changes for an existing single-instance deployment that
# doesn't set these.
DB_POOL_SIZE = int(os.getenv("DB_POOL_SIZE", "5"))
DB_MAX_OVERFLOW = int(os.getenv("DB_MAX_OVERFLOW", "10"))
DB_POOL_TIMEOUT = int(os.getenv("DB_POOL_TIMEOUT", "30"))

engine = create_engine(
    DATABASE_URL, echo=False, pool_pre_ping=True,
    pool_size=DB_POOL_SIZE, max_overflow=DB_MAX_OVERFLOW, pool_timeout=DB_POOL_TIMEOUT,
)


def init_db():
    with engine.connect() as conn:
        # Enable pgvector up front — a real prerequisite EVERY real
        # migration below assumes is already installed (several tables
        # have a real VECTOR column). Not itself a schema-versioned
        # migration since CREATE EXTENSION IF NOT EXISTS is already
        # genuinely idempotent and near-instant — no real benefit to
        # tracking it as a numbered revision.
        #
        # Checked first rather than relying on IF NOT EXISTS: Azure
        # Database for PostgreSQL refuses CREATE EXTENSION for non-admin
        # logins ("only members of azure_pg_admin...") even when the
        # extension already exists. The platform pre-creates it with the
        # admin login, so a least-privilege login must not attempt it.
        exists = conn.execute(text("SELECT 1 FROM pg_extension WHERE extname = 'vector'")).scalar()
        if not exists:
            conn.execute(text("CREATE EXTENSION vector"))
        conn.commit()

    # Real, versioned schema migrations (§0.69, GAP_REPORT) — replaces
    # this function's own former reliance on SQLModel.metadata.
    # create_all() (which only ever creates tables that don't exist
    # yet — it can't safely evolve an existing production schema from
    # one real release to the next) plus a growing, unversioned list of
    # manual ALTER TABLE ADD COLUMN IF NOT EXISTS statements that used
    # to live right here, one per real GAP_REPORT fix that added a
    # column. Both are gone now — see migrations/versions/ for the real
    # history, and migrations/README.md for how to add a new one.
    #
    # AUTO_MIGRATE=true (the default, for local dev and this reference
    # environment) runs `alembic upgrade head` automatically on every
    # real app startup — safe and idempotent (a DB already at head is a
    # real no-op), and what actually keeps this environment's own
    # database schema current without a person needing to remember a
    # separate manual step.
    #
    # Under ENVIRONMENT=production the default flips to FALSE.
    #
    # The paragraph above used to end by saying a production deployment
    # "should set AUTO_MIGRATE=false" — advice in a comment, with the
    # permissive default left in place. A deployment that never read
    # this file got N replicas racing to apply the same migration on
    # startup, which is precisely the risk the advice describes, and the
    # failure is a partially-migrated schema rather than a clean crash.
    # Advice that has to be found to be followed is not a safeguard.
    #
    # Still overridable: a single-replica production deployment that
    # genuinely wants startup migrations can set AUTO_MIGRATE=true and
    # gets them, loudly. What is gone is the silent inheritance.
    #
    # See DEVELOPER_GUIDE.md's own "Database migrations" section for the
    # real, concrete deployment-pipeline guidance this implies.
    from .environment import env_flag, is_production

    production = is_production()
    if env_flag("AUTO_MIGRATE", default=not production):
        if production:
            logger.warning(
                "AUTO_MIGRATE=true under ENVIRONMENT=production: this replica is "
                "applying `alembic upgrade head` at startup. Safe for a single "
                "replica; with more than one, concurrent startups can race on the "
                "same migration. Prefer a single migration Job before rolling pods."
            )
        _run_alembic_upgrade()
    elif production:
        logger.info(
            "AUTO_MIGRATE is off (the default under ENVIRONMENT=production) — "
            "this process will NOT migrate. Run `alembic upgrade head` as a "
            "deployment step before starting application pods."
        )


def _run_alembic_upgrade():
    from alembic.config import Config
    from alembic import command
    backend_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    alembic_cfg = Config(os.path.join(backend_dir, "alembic.ini"))
    alembic_cfg.set_main_option("script_location", os.path.join(backend_dir, "migrations"))
    command.upgrade(alembic_cfg, "head")


def get_session():
    with Session(engine) as session:
        yield session
