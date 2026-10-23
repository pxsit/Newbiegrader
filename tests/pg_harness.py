"""Real-PostgreSQL test harness. There is no fake: tests that need the database
fail (never skip) when NEWBIEGRADER_TEST_DSN is missing.

NEWBIEGRADER_TEST_DSN is a superuser connection string to any existing database,
for example  postgresql://postgres:postgres@127.0.0.1:5432/postgres.
Each test module gets its own scratch database with db/schema.sql applied.
"""
import os
from pathlib import Path
import uuid

import psycopg
from psycopg import sql

ROOT = Path(__file__).resolve().parent.parent
SCHEMA = ROOT / "db" / "schema.sql"


def admin_dsn():
    dsn = os.environ.get("NEWBIEGRADER_TEST_DSN", "").strip()
    if not dsn:
        raise RuntimeError(
            "NEWBIEGRADER_TEST_DSN is not set. These tests need a real PostgreSQL 18 "
            "(see tools/dev_postgres.sh); they are never skipped.")
    return dsn


class ScratchDatabase:
    """Create a database with the schema applied; drop it on close()."""

    def __init__(self):
        self.admin = admin_dsn()
        self.name = "nbg_test_" + uuid.uuid4().hex[:12]
        with psycopg.connect(self.admin, autocommit=True) as conn:
            conn.execute(sql.SQL("create database {}").format(sql.Identifier(self.name)))
        self.dsn = psycopg.conninfo.make_conninfo(self.admin, dbname=self.name)
        with psycopg.connect(self.dsn, autocommit=True) as conn:
            conn.execute(SCHEMA.read_text(encoding="utf-8"))

    def connect(self):
        return psycopg.connect(self.dsn, autocommit=True)

    def reset(self):
        """Empty every table between tests."""
        with self.connect() as conn:
            names = [r[0] for r in conn.execute(
                "select quote_ident(tablename) from pg_tables where schemaname = 'public'")]
            conn.execute("truncate " + ", ".join("public." + n for n in names) + " cascade")

    def close(self):
        with psycopg.connect(self.admin, autocommit=True) as conn:
            conn.execute(sql.SQL("drop database if exists {} with (force)").format(
                sql.Identifier(self.name)))
