"""PostgreSQL access for the API and the judge workers (psycopg 3, sync pool).

All SQL is parameterized; values are never interpolated into statements. The
pool is synchronous because the API runs database calls in worker threads
(asyncio.to_thread), and it is sized for that thread pool on purpose.

Failure model:
  * DatabaseUnavailable - no statement was sent (pool timeout, cannot connect);
    always safe to retry.
  * AmbiguousResult - the connection died after a write was sent, so it may or
    may not have been applied. Never replayed here; the caller reports a 5xx.
Reads are retried once on a broken connection. Statement timeouts, constraint
violations and other server errors are raised unchanged (psycopg.Error).
"""
from contextlib import contextmanager
from datetime import date, datetime, timezone
from decimal import Decimal
import os
import re
from uuid import UUID

import psycopg
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool, PoolTimeout

DEFAULT_DSN = "host=/var/run/postgresql dbname=newbiegrader"
_FUNCTION = re.compile(r"[a-z_][a-z0-9_]*\Z")


class DatabaseError(Exception):
    pass


class DatabaseUnavailable(DatabaseError):
    """The statement was never sent."""


class AmbiguousResult(DatabaseError):
    """A write may or may not have been applied; do not replay it."""


def _iso(value):
    # Same text PostgREST produced: UTC offset, fractional zeros trimmed.
    text = value.astimezone(timezone.utc).isoformat()
    if "." in text:
        head, tail = text.split(".", 1)
        frac, zone = tail[:6], tail[6:]
        text = head + ("." + frac.rstrip("0") if frac.rstrip("0") else "") + zone
    return text


def _plain(value):
    if isinstance(value, datetime):
        return _iso(value)
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    if isinstance(value, date):
        return value.isoformat()
    return value


def json_row(cursor):
    """Rows as dicts holding JSON-compatible values (what PostgREST returned)."""
    names = [c.name for c in cursor.description] if cursor.description else []

    def make(values):
        return {name: _plain(v) for name, v in zip(names, values)}
    return make


def _param(value):
    return Jsonb(value) if isinstance(value, dict) else value


class _Operations:
    """Statement helpers shared by Database (pooled) and Transaction."""

    def _run(self, sql, params, *, write, fetch):  # pragma: no cover - abstract
        raise NotImplementedError

    def fetch_all(self, sql, params=None):
        return self._run(sql, params, write=False, fetch="all")

    def fetch_one(self, sql, params=None):
        return self._run(sql, params, write=False, fetch="one")

    def modify(self, sql, params=None):
        """INSERT/UPDATE/DELETE ... RETURNING; returns the rows."""
        return self._run(sql, params, write=True, fetch="all")

    def execute(self, sql, params=None):
        """Statement without result rows; returns the affected row count."""
        return self._run(sql, params, write=True, fetch="count")

    def call(self, function, *, read_only=False, **named):
        """Call public.<function>(name => value, ...); returns its single value.

        dict values are sent as jsonb; lists as arrays. Calls are writes unless
        read_only is set, so they are never replayed after a connection loss.
        """
        if not _FUNCTION.match(function):
            raise ValueError("invalid function name")
        for key in named:
            if not _FUNCTION.match(key):
                raise ValueError("invalid parameter name")
        args = ", ".join(f"{k} => %({k})s" for k in named)
        row = self._run(f"select public.{function}({args}) as result",
                        {k: _param(v) for k, v in named.items()},
                        write=not read_only, fetch="one")
        return row["result"]


class Database(_Operations):
    def __init__(self, dsn=DEFAULT_DSN, *, min_size=1, max_size=16,
                 checkout_timeout=10.0, statement_timeout_ms=30000,
                 idle_in_transaction_ms=30000, application_name="newbiegrader",
                 set_role=None):
        if set_role is not None and not _FUNCTION.match(set_role):
            raise ValueError("invalid role name")
        self._set_role = set_role
        options = (f"-c statement_timeout={int(statement_timeout_ms)} "
                   f"-c idle_in_transaction_session_timeout={int(idle_in_transaction_ms)} "
                   "-c timezone=UTC")
        self._pool = ConnectionPool(
            dsn, min_size=min_size, max_size=max_size, timeout=checkout_timeout,
            open=False, check=ConnectionPool.check_connection, configure=self._configure,
            kwargs={"autocommit": True, "row_factory": json_row, "options": options,
                    "application_name": application_name})

    def _configure(self, conn):
        if self._set_role:  # tests only: connect as a superuser, act as a role
            conn.execute(f"set role {self._set_role}")

    def open(self, timeout=30.0):
        self._pool.open(wait=True, timeout=timeout)

    def close(self):
        self._pool.close()

    @contextmanager
    def _connection(self):
        """Check out a connection; only checkout failures become DatabaseUnavailable."""
        cm = self._pool.connection()
        try:
            conn = cm.__enter__()
        except (PoolTimeout, psycopg.OperationalError) as exc:
            raise DatabaseUnavailable(str(exc)) from exc
        try:
            yield conn
        except BaseException as exc:
            cm.__exit__(type(exc), exc, exc.__traceback__)
            raise
        else:
            cm.__exit__(None, None, None)

    def _run(self, sql, params, *, write, fetch):
        broken = None
        for attempt in (1, 2):
            try:
                with self._connection() as conn:
                    try:
                        return _execute(conn, sql, params, fetch)
                    except (psycopg.OperationalError, psycopg.InterfaceError) as exc:
                        if not conn.broken:
                            raise
                        if write:
                            raise AmbiguousResult(str(exc)) from exc
                        broken = exc
            except DatabaseUnavailable as exc:
                # A pool timeout already waited; only a failed connect is retried.
                if attempt == 2 or isinstance(exc.__cause__, PoolTimeout):
                    raise
        raise DatabaseUnavailable(str(broken or "database unavailable")) from broken

    @contextmanager
    def transaction(self):
        """One atomic unit of work on a single connection (never retried)."""
        with self._connection() as conn:
            try:
                with conn.transaction():
                    yield Transaction(conn)
            except (psycopg.OperationalError, psycopg.InterfaceError) as exc:
                if conn.broken:
                    raise AmbiguousResult(str(exc)) from exc
                raise


class Transaction(_Operations):
    def __init__(self, conn):
        self._conn = conn

    def _run(self, sql, params, *, write, fetch):
        return _execute(self._conn, sql, params, fetch)


def _execute(conn, sql, params, fetch):
    with conn.cursor() as cur:
        cur.execute(sql, params)
        if fetch == "all":
            return cur.fetchall() if cur.description else []
        if fetch == "one":
            return cur.fetchone()
        return cur.rowcount


def from_env(*, role="api"):
    """Build a Database from GRADER_DB_* variables (no credentials: peer auth)."""
    def number(name, default, cast=int):
        return cast(os.environ.get(name, default))
    return Database(
        os.environ.get("GRADER_DB_DSN", DEFAULT_DSN),
        min_size=number("GRADER_DB_POOL_MIN", 1),
        max_size=number("GRADER_DB_POOL_MAX", 16 if role == "api" else 2),
        checkout_timeout=number("GRADER_DB_POOL_TIMEOUT", 10.0, float),
        statement_timeout_ms=number("GRADER_DB_STATEMENT_TIMEOUT_MS", 30000),
        idle_in_transaction_ms=number("GRADER_DB_IDLE_IN_TX_MS", 30000),
        application_name=f"newbiegrader-{role}")
