"""
Shared helper for the real-PostgreSQL tests: a scratch database on the server named by DATACONNECT_DATABASE_URL, created for one test and dropped
afterwards (the configured database itself is never touched), with helpers to run alembic / startup / SQL / the schema-drift check against it.
"""

import asyncio
import json
import os
import subprocess
import sys
import uuid
from contextlib import contextmanager
from pathlib import Path

import asyncpg
from sqlalchemy.engine import make_url

BACKEND = Path(__file__).resolve().parents[1]


def _server() -> dict:
    url = make_url(os.environ["DATACONNECT_DATABASE_URL"])
    return {"user": url.username, "password": url.password, "host": url.host, "port": url.port}


async def _admin(sql: str) -> None:
    conn = await asyncpg.connect(database="postgres", **_server())
    try:
        await conn.execute(sql)
    finally:
        await conn.close()


class Scratch:
    def __init__(self, name: str) -> None:
        self.name = name
        server = _server()
        self.url = f"postgresql+asyncpg://{server['user']}:{server['password']}@{server['host']}:{server['port']}/{name}"

    def env(self) -> dict:
        return {**os.environ, "DATACONNECT_DATABASE_URL": self.url, "PYTHONPATH": str(BACKEND)}

    def alembic(self, *args: str, check: bool = True) -> subprocess.CompletedProcess:
        result = subprocess.run([sys.executable, "-m", "alembic", *args], cwd=BACKEND, env=self.env(), capture_output=True, text=True, timeout=300)
        if check and result.returncode != 0:
            raise AssertionError(f"alembic {' '.join(args)} failed:\n{result.stderr[-2500:]}")
        return result

    def startup_migrations(self) -> subprocess.CompletedProcess:
        code = "from app.startup import run_migrations; run_migrations()"
        return subprocess.run([sys.executable, "-c", code], cwd=BACKEND, env=self.env(), capture_output=True, text=True, timeout=300)

    def query(self, sql: str):
        async def run():
            conn = await asyncpg.connect(database=self.name, **_server())
            try:
                return await conn.fetch(sql)
            finally:
                await conn.close()
        return asyncio.run(run())

    def execute(self, sql: str) -> None:
        async def run():
            conn = await asyncpg.connect(database=self.name, **_server())
            try:
                await conn.execute(sql)
            finally:
                await conn.close()
        asyncio.run(run())

    def scalar(self, sql: str):
        return self.query(sql)[0][0]

    def version(self) -> str | None:
        rows = self.query("SELECT version_num FROM alembic_version")
        return rows[0][0] if rows else None

    def drift(self) -> list[str]:
        """Differences between this database and the ORM models, ignoring column COMMENTS (documentation, not schema)."""
        code = (
            "import asyncio, json\n"
            "from alembic.autogenerate import compare_metadata\n"
            "from alembic.migration import MigrationContext\n"
            "from sqlalchemy.ext.asyncio import create_async_engine\n"
            "from app import models  # noqa\n"
            "from app.db.base import Base\n"
            "def flat(d):\n"
            "    for x in d:\n"
            "        if isinstance(x, list): yield from flat(x)\n"
            "        else: yield x\n"
            "async def main():\n"
            "    e = create_async_engine(__import__('os').environ['DATACONNECT_DATABASE_URL'])\n"
            "    async with e.connect() as c:\n"
            "        out = await c.run_sync(lambda s: [d for d in flat(compare_metadata(MigrationContext.configure(s, opts={'compare_type': True}), Base.metadata)) if d[0] != 'modify_comment'])\n"
            "    await e.dispose()\n"
            "    print(json.dumps([f'{d[0]}:{getattr(d[1], \"name\", d[2] if len(d) > 2 else \"\")}:{d[3] if len(d) > 3 and isinstance(d[3], str) else \"\"}' for d in out]))\n"
            "asyncio.run(main())\n"
        )
        result = subprocess.run([sys.executable, "-c", code], cwd=BACKEND, env=self.env(), capture_output=True, text=True, timeout=120)
        assert result.returncode == 0, result.stderr[-1500:]
        return json.loads(result.stdout.strip().splitlines()[-1])


@contextmanager
def scratch_database(prefix: str = "dc_scratch"):
    name = f"{prefix}_{uuid.uuid4().hex[:10]}"
    asyncio.run(_admin(f'CREATE DATABASE "{name}"'))
    try:
        yield Scratch(name)
    finally:
        asyncio.run(_admin(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
