#!/usr/bin/env python3
"""Web server for the brutal CP grader.

Stack: FastAPI + Uvicorn (backend) + vanilla HTML/CSS/JS (frontend, no build step).
Judging logic is reused directly from grader.py (no subprocess, no duplication).

Run:
    python3 server.py                  # http://127.0.0.1:8000
    python3 server.py --port 8080
"""
from __future__ import annotations

import asyncio
import collections
import datetime as dt
import hashlib
import hmac
import ipaddress
import json
import logging
import math
import os
import re
import secrets
import sys
import tempfile
import threading
import time
import traceback
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Dict, List, Optional
from urllib.parse import urlencode
from urllib.parse import urlsplit, unquote
from urllib.request import Request as URLRequest
from urllib.request import urlopen


from fastapi import FastAPI, File, Form, HTTPException, Request, Response, UploadFile
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware
from starlette.responses import RedirectResponse
from itsdangerous import URLSafeTimedSerializer

try:
    from database_client import create_database_client as create_client
    _has_supabase_lib = True
except ImportError:  # optional dep: history disabled without it
    create_client = None
    _has_supabase_lib = False

ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT))

import grader  # noqa: E402  (CLI grader module, import-safe: guarded by __main__)
import problem_store  # noqa: E402  (local|supabase problem repository, storage only)
import email_auth  # noqa: E402
import judge_execution  # noqa: E402
from judge_execution import (  # noqa: E402
    REVEAL_ALL, REVEAL_SAMPLES, REVEAL_NONE, REVEAL_PRACTICE,
    DETAIL_HEAD, RUN_OUTPUT_MAX, _plain_text, _redact_results, _sanitize_compile_error,
)

class AuthQueryLogFilter(logging.Filter):
    """Uvicorn logs a raw request target, including OAuth bearer query values."""
    def filter(self, record):
        args = record.args
        if isinstance(args, tuple) and len(args) == 5 and isinstance(args[2], str):
            target = args[2]
            if unquote(target.split("?", 1)[0]).startswith("/api/auth/"):
                record.args = (*args[:2], target.split("?", 1)[0], *args[3:])
        return True


logging.getLogger("uvicorn.access").addFilter(AuthQueryLogFilter())

STATIC_DIR = ROOT / "static"

BUILD_SOURCE_FILES = ("grader.py", "server.py", "judge_runner.py", "judge_execution.py", "job_resources.py", "problem_store.py", "backend_problem_files.py",
                      "container_entrypoint.py", "worker.py", "job_sandbox.py", "email_auth.py", "database_client.py")


def _source_fingerprint(root: Path = ROOT) -> str:
    """Identify deployed backend code without Git, credentials or environment."""
    digest = hashlib.sha256()
    for name in BUILD_SOURCE_FILES:
        digest.update(name.encode("ascii") + b"\0")
        digest.update((root / name).read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


# Compute once at boot; version requests never read source files or secrets.
BUILD_SHA256 = _source_fingerprint()


def _load_dotenv() -> None:
    """Local-dev convenience only: load ROOT/.env into env (never commit it).
    Real env vars always win. Production uses host env vars, no .env file."""
    try:
        with open(ROOT / ".env", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                k = k.strip()
                v = v.strip().strip('"').strip("'")
                if k and k not in os.environ:
                    os.environ[k] = v
    except FileNotFoundError:
        pass
    except Exception as e:
        print(f"[web] warning: cannot read .env: {e}", file=sys.stderr)


RUNNER_CHILD = os.environ.get("GRADER_RUNNER_CHILD") == "1"
if not RUNNER_CHILD:
    _load_dotenv()

LANGS: Dict[str, Dict[str, object]] = {
    "python": {"name": "Python 3", "ext": ".py", "accept": [".py"]},
    "c": {"name": "C (C11)", "ext": ".c", "accept": [".c"]},
    "cpp": {"name": "C++ (C++17)", "ext": ".cpp", "accept": [".cpp", ".cc", ".cxx"]},
}
EXT2LANG = {".py": "python", ".c": "c", ".cpp": "cpp", ".cc": "cpp", ".cxx": "cpp"}

MAX_CODE_BYTES = 256 * 1024   # 256 KB source cap

# ---- capacity, derived from the host/container this runs in ----
# Each production worker has one CPU and its own bounded allocation. Running
# tests of one submission in parallel would oversubscribe that quota and can
# create unrelated wall-clock TLEs. Capacity below comes from cgroup limits
# (grader.cpu_quota / grader.ram_limit_mb), with explicit host overrides.
CPU_QUOTA = grader.CPU_QUOTA
RAM_MB = grader.RAM_MB


def _env_int(name: str, auto: int) -> int:
    """`0`/unset = auto (host derived); any positive value wins."""
    raw = os.environ.get(name, "").strip()
    if raw:
        try:
            val = int(raw)
            if val > 0:
                return val
        except ValueError:
            print(f"[web] warning: bad {name}={raw!r}, using auto", file=sys.stderr)
    return auto

def _derive_capacity(cpu: float, ram: int) -> dict:
    # Reserve API/interpreter memory, then budget the larger of compiler and
    # runtime *ceilings*. Python's allowance and virtual headroom also consume
    # the host budget; the declared compact-language limit alone is insufficient.
    reserve = min(192, max(64, ram // 3))
    available = max(1, ram - reserve)
    requested = _env_int("GRADER_MEMORY_CAP_MB", max(64, ram // 2))

    def job_mb(cap: int) -> int:
        runtime = max(grader.enforced_memory_mb(cap, lang) for lang in LANGS)
        return max(runtime, max(cap, 256)) + 64

    # Find the highest declared cap that fits one slot without dropping the
    # language allowance or lying about the enforced address-space limit.
    lo, hi = 1, requested
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if job_mb(mid) <= available:
            lo = mid
        else:
            hi = mid - 1
    cap = lo
    per_job = job_mb(cap)
    safe = min(max(1, int(cpu)), available // per_job)
    return {"workers": min(_env_int("GRADER_JUDGE_WORKERS", safe), safe),
            "judge_jobs": 1, "memory_cap_mb": cap, "reserve_mb": reserve,
            "per_job_mb": per_job}


_capacity = _derive_capacity(CPU_QUOTA, RAM_MB)
JUDGE_JOBS = _capacity["judge_jobs"]
JUDGE_WORKERS = _capacity["workers"]
MEMORY_CAP_MB = _capacity["memory_cap_mb"]
QUEUE_MODE = os.environ.get("GRADER_QUEUE_MODE", "local").strip().lower()
GRADER_ROLE = os.environ.get("GRADER_ROLE", "combined").strip().lower()
# Problem storage source: local problems/ (default) or Supabase Postgres +
# private Storage (judge-assets). Explicit switch, no silent fallback: supabase
# mode never reads local files (see problem_store.py + docs). Configure via
# GRADER_PROBLEM_SOURCE=local|supabase, GRADER_PROBLEM_BUCKET, SUPABASE_URL/KEY.
PROBLEM_SOURCE = (os.environ.get("GRADER_PROBLEM_SOURCE", "local").strip().lower()
                  or "local")
CAPACITY_DIR = Path(os.environ.get("GRADER_CAPACITY_DIR") or
                    str(Path(tempfile.gettempdir()) / "newbiegrader-capacity"))
QUEUE_MAX = _env_int("GRADER_QUEUE_MAX", 25)
# Worst case per submission = compile + every test at its limit, serially.
OVERALL_TIMEOUT = float(os.environ.get("GRADER_OVERALL_TIMEOUT", "") or 0) or (
    90.0 if CPU_QUOTA >= 2 else 240.0)

print(f"[web] host: cpu={CPU_QUOTA:g} core(s), ram={RAM_MB}MB -> "
      f"workers={JUDGE_WORKERS}, tests/submission={JUDGE_JOBS}, "
      f"memory_cap={MEMORY_CAP_MB}MB, queue_max={QUEUE_MAX}, "
      f"overall_timeout={OVERALL_TIMEOUT:g}s, "
      f"sample_interval={grader.SAMPLE_INTERVAL * 1000:g}ms, "
      f"compile_timeout={grader.COMPILE_TIMEOUT:g}s, "
      f"role={GRADER_ROLE}, queue_mode={QUEUE_MODE}", file=sys.stderr)

# ---- async judging (B9) ----
# The HTTP path never judges inline: it queues a job and the client polls
# /api/jobs/{id}. Production uses dedicated database worker processes. Local
# combined mode runs worker tasks alongside the API, with blocking execution
# delegated to threads. Concurrency and limits come from the capacity block.
JOB_TTL_S = 3600              # in-memory job records kept this long (no-DB mode)
JOB_MAX_AGE_S = 24 * 3600     # queued jobs older than this are dropped, not run
REJUDGE_MAX = 200             # cap per /api/admin/rejudge call

# ---- /api/run (custom input playground) ----
RUN_INPUT_MAX = 64 * 1024     # max bytes of user-supplied stdin
RUN_TIMEOUT_MAX = 5.0         # hard cap for playground runs
RUN_BUDGET = 30.0             # total CPU-seconds a single /api/run may spend

# ---- per-user quotas (fair-use; "N/WINDOW_SECONDS", 0 = off) ----
def _parse_quota(raw: str, default: tuple) -> tuple:
    try:
        n, w = raw.split("/", 1)
        n_i, w_i = int(n), int(w)
        if n_i <= 0 or w_i <= 0:
            return (0, 0)  # disabled
        return (n_i, w_i)
    except ValueError:
        return default

SUBMIT_QUOTA = _parse_quota(os.environ.get("GRADER_SUBMIT_QUOTA", "60/600"), (60, 600))
RUN_QUOTA = _parse_quota(os.environ.get("GRADER_RUN_QUOTA", "120/600"), (120, 600))

@asynccontextmanager
async def _lifespan(_app: "FastAPI"):
    await validate_runtime()
    try:
        await _recover_jobs()
    except Exception as e:
        if QUEUE_MODE == "database":
            raise
        print(f"[web] local job recovery failed: {e}", file=sys.stderr)
    workers = ([] if GRADER_ROLE == "api" else
               [asyncio.create_task(_judge_worker(i)) for i in range(JUDGE_WORKERS)])
    # Realtime admins live in Postgres; keep a small in-process snapshot so the
    # many synchronous _is_admin() checks never wait on the network.
    admin_task = None
    if _sb is not None:
        await asyncio.to_thread(_load_db_admins_sync)
        admin_task = asyncio.create_task(_admin_cache_loop())
    try:
        yield
    finally:
        for w in workers:
            w.cancel()
        await asyncio.gather(*workers, return_exceptions=True)
        if admin_task is not None:
            admin_task.cancel()
            await asyncio.gather(admin_task, return_exceptions=True)


class RequestBodyLimit:
    """Bound raw API bytes before FastAPI parses forms, files or JSON."""
    def __init__(self, app, max_bytes: int = 1024 * 1024):
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope.get("method") not in ("POST", "PUT", "PATCH", "DELETE") \
                or not scope.get("path", "").startswith("/api/"):
            return await self.app(scope, receive, send)
        headers = dict(scope.get("headers", []))
        try:
            declared = int(headers.get(b"content-length", b"0"))
            if declared < 0:
                raise ValueError
        except ValueError:
            return await JSONResponse({"detail": "invalid content length"}, 400)(scope, receive, send)
        if declared > self.max_bytes:
            return await JSONResponse({"detail": "request body too large"}, 413)(scope, receive, send)
        body = bytearray()
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            chunk = message.get("body", b"")
            if len(body) + len(chunk) > self.max_bytes:
                return await JSONResponse({"detail": "request body too large"}, 413)(scope, receive, send)
            body.extend(chunk)
            if not message.get("more_body", False):
                break
        delivered = False

        async def replay():
            nonlocal delivered
            if not delivered:
                delivered = True
                return {"type": "http.request", "body": bytes(body), "more_body": False}
            return await receive()

        await self.app(scope, replay, send)


class MemberBoundaryMiddleware:
    """Require verified identity inside the existing CSRF/CORS/header layers."""
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        path = scope.get("path", "")
        public_api = path in {"/api/home", "/api/health", "/api/version", "/api/languages"} \
            or path.startswith("/api/auth/")
        protected = (path.startswith("/api/") and not public_api) \
            or path in {"/docs", "/redoc", "/openapi.json"}

        async def private_send(message):
            if protected and message["type"] == "http.response.start":
                headers = [(k, v) for k, v in message.get("headers", [])
                           if k.lower() != b"cache-control"]
                message["headers"] = headers + [(b"cache-control", b"private, no-store")]
            await send(message)

        if protected and scope.get("method") != "OPTIONS" \
                and not (scope.get("session", {}).get("user") or {}).get("email"):
            return await JSONResponse({"detail": "login required"}, 401)(scope, receive, private_send)
        await self.app(scope, receive, private_send)


async def _json_object(request: Request) -> dict:
    try:
        body = await request.json()
    except (ValueError, UnicodeError):
        raise HTTPException(status_code=400, detail="invalid JSON")
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="JSON body must be an object")
    return body


app = FastAPI(title="Newbiegrader API", version="2.0.0", lifespan=_lifespan)
app.add_middleware(GZipMiddleware, minimum_size=500)
app.add_middleware(RequestBodyLimit)
app.add_middleware(MemberBoundaryMiddleware)
@app.exception_handler(Exception)
async def _unexpected_exception_handler(request: Request, exc: Exception):
    """Convert unexpected exceptions to a generic JSON 500 instead of FastAPI's
    default traceback HTML, preventing internal detail leakage to clients."""
    print(f"[web] unhandled exception on {request.method} {request.url.path}: {exc!r}",
          file=sys.stderr)
    return JSONResponse(
        status_code=500,
        content={"detail": "internal server error"},
    )


@app.middleware("http")
async def _security_headers(request: Request, call_next):
    resp = await call_next(request)
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["X-Frame-Options"] = "SAMEORIGIN"
    resp.headers["Referrer-Policy"] = "no-referrer"
    resp.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
    if request.url.path.startswith("/api/auth/"):
        resp.headers["Cache-Control"] = "no-store"
        resp.headers["Pragma"] = "no-cache"
    if request.url.path == "/api/history" or request.url.path.startswith("/api/history/"):
        resp.headers["Cache-Control"] = "private, no-store"
        resp.headers["Vary"] = "Cookie"
    if request.url.path == "/api/admin" or request.url.path.startswith("/api/admin/"):
        resp.headers["Cache-Control"] = "private, no-store"
        resp.headers["Vary"] = "Cookie"
    resp.headers["Content-Security-Policy"] = (
        "default-src 'self'; base-uri 'none'; object-src 'none'; script-src 'self'; "
        "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
        "font-src https://fonts.gstatic.com; img-src 'self' data: https:; "
        "frame-src 'self'; frame-ancestors 'self'; "
        "form-action 'self' https://accounts.google.com")
    return resp


# ---- CSRF defence: Origin validation with Referer fallback (P0-6) ----
# Browsers always attach `Origin` to unsafe methods (POST/PUT/PATCH/DELETE);
# a cross-site page cannot forge it. Policy:
#   * Origin present -> must match canonical trust, else 403 (Origin always
#     wins over Referer).
#   * Origin absent + Referer present -> Referer origin must match, else 403.
#   * Both absent + session cookie present -> 403. A browser request that
#     carries our cookie but neither header is either forged or stripped by
#     a hostile intermediary — fail closed. Only the cookie NAME is checked;
#     values are never read or logged, and session validation itself stays
#     with the auth layers downstream.
#   * Both absent + no session cookie -> allowed through to the existing
#     authentication/authorization layers (non-browser clients such as curl
#     and tools/load_test.py send neither header; unauthenticated calls are
#     still rejected by the route handlers, never granted anything here).
# Canonical trust = origin of FRONTEND_URL (the explicitly configured public
# origin) + operator CORS origins + any http loopback origin (local dev, where
# the server serves the frontend itself). Comparison is exact
# (scheme, host, port) tuples — never substring or suffix matching.
_CSRF_UNSAFE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}
# Must stay in sync with SessionMiddleware(session_cookie=...) below.
_SESSION_COOKIE_NAME = "ng_session"


def _origin_tuple(value: object) -> tuple | None:
    """Normalized (scheme, host, port) or None when malformed/untrusted."""
    try:
        parts = urlsplit(str(value or "").strip())
    except (ValueError, TypeError, AttributeError):
        return None
    scheme = (parts.scheme or "").lower()
    if scheme not in ("http", "https"):
        return None
    try:
        host = (parts.hostname or "").lower()
        port = parts.port or (443 if scheme == "https" else 80)
    except ValueError:
        return None
    if not host:
        return None
    return (scheme, host, port)


def _is_dev_mode() -> bool:
    """True when the server is running in local-dev (insecure) mode.

    SESSION_SECURE=false means cookies are not marked Secure, which only
    makes sense on plain-HTTP localhost dev. Production defaults true.
    """
    return os.environ.get("SESSION_SECURE", "true").lower() == "false"


def _is_loopback_origin(origin: tuple) -> bool:
    scheme, host, _port = origin
    if scheme != "http":
        return False
    # IPv4/IPv6 loopback addresses only; reject hostname-based tricks like
    # "127.evil.com" that would bypass CSRF protection. "localhost" and
    # "::1" are accepted as they always resolve to loopback.
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return host == "localhost"



def _trusted_origins() -> set:
    """Canonical origins for unsafe browser requests (computed per call so
    tests and late env configuration are honoured)."""
    trusted: set = set()
    for raw in [FRONTEND_URL] + _cors_origins:
        parsed = _origin_tuple(raw)
        if parsed is not None:
            trusted.add(parsed)
    return trusted


def _origin_allowed(value: object) -> bool:
    origin = _origin_tuple(value)
    if origin is None:
        return False
    if origin in _trusted_origins():
        return True
    if _is_loopback_origin(origin) and _is_dev_mode():
        return True
    return False


def _has_session_cookie(request: Request) -> bool:
    """True when the request carries our session cookie (NAME check only).

    Only the cookie name is inspected — values are never read or logged.
    """
    for part in (request.headers.get("cookie") or "").split(";"):
        if part.split("=", 1)[0].strip() == _SESSION_COOKIE_NAME:
            return True
    return False


@app.middleware("http")
async def _csrf_origin_check(request: Request, call_next):
    if request.method in _CSRF_UNSAFE_METHODS:
        origin = request.headers.get("origin")
        if origin is not None:
            allowed = _origin_allowed(origin)
        else:
            referer = request.headers.get("referer")
            if referer is not None:
                allowed = _origin_allowed(referer)
            else:
                allowed = not _has_session_cookie(request)
        if not allowed:
            # Log method+path only: never headers or cookie values.
            print(f"[web] csrf refused {request.method} {request.url.path}",
                  file=sys.stderr)
            return JSONResponse(status_code=403,
                                content={"detail": "cross-origin request refused"})
    return await call_next(request)


# Tiny in-memory cache for slow aggregate endpoints (Supabase RTT dominates).
# Invalidated on every stored submission.
_cache: dict = {}
_cache_inflight: dict = {}
_cache_generation = 0
CACHE_TTL = {"stats": 30}


def _cached(key: str, ttl: int, build):
    now = time.monotonic()
    hit = _cache.get(key)
    if hit and now - hit[0] < ttl:
        return hit[1]
    val = build()  # raises HTTPException on DB error: never cached
    _cache[key] = (now, val)
    return val


async def _acached(key: str, ttl: int, build):
    """Share one aggregate read on a cold cache, without blocking the loop.

    A cancelled HTTP request must not cancel the shared read. Invalidation
    starts a new generation so an older read cannot repopulate stale stats.
    """
    now = time.monotonic()
    hit = _cache.get(key)
    if hit and now - hit[0] < ttl:
        return hit[1]
    generation = _cache_generation
    slot = (asyncio.get_running_loop(), key, generation)
    task = _cache_inflight.get(slot)
    if task is None:
        async def refresh():
            try:
                val = await asyncio.to_thread(build)  # errors are never cached
                if generation == _cache_generation:
                    _cache[key] = (time.monotonic(), val)
                return val
            finally:
                _cache_inflight.pop(slot, None)

        task = asyncio.create_task(refresh())
        _cache_inflight[slot] = task
        # Retrieve failures even if every HTTP waiter disconnects. Awaiters
        # still receive the same exception and the next request can retry.
        task.add_done_callback(lambda done: None if done.cancelled() else done.exception())
    return await asyncio.shield(task)


def _bust_cache() -> None:
    global _cache_generation
    _cache_generation += 1
    _cache.pop("stats", None)

# Split deployment: same-origin via frontend /api proxy (Cloudflare Pages
# Function or legacy vercel.json), so the browser never talks cross-origin.
# GRADER_CORS_ORIGINS stays empty in prod (e.g. https://newbiegrader.pages.dev
# needs no CORS). Set it only when serving the frontend from a host WITHOUT
# the /api proxy, e.g. GRADER_CORS_ORIGINS=https://newbiegrader.pages.dev
# Empty (default) = same-origin only, no CORS headers.
_cors_origins = [o.strip() for o in
                 os.environ.get("GRADER_CORS_ORIGINS", "").split(",") if o.strip()]
if _cors_origins:
    if any(o == "*" for o in _cors_origins):
        raise RuntimeError("GRADER_CORS_ORIGINS must not be '*': use explicit origins")
    app.add_middleware(
        CORSMiddleware,
        allow_origins=_cors_origins,
        allow_methods=["GET", "POST", "PUT", "DELETE"],
        allow_headers=["Content-Type", "Authorization", "X-Requested-With"],
        allow_credentials=True,  # kept for non-proxied hosts; proxied prod is same-origin
    )

# ---- Supabase (optional): submission history + stats ----
# Server-side only. SUPABASE_KEY must be the service_role key (never the
# anon key, never exposed to the frontend). Unset = history disabled.
_sb = None
_sb_url = os.environ.get("SUPABASE_URL", "").strip()
_sb_key = os.environ.get("SUPABASE_KEY", "").strip()
if not RUNNER_CHILD and _sb_url and _sb_key:
    if not _has_supabase_lib:
        print("[web] supabase lib not installed, history disabled", file=sys.stderr)
    else:
        try:
            _sb = create_client(_sb_url, _sb_key, role=GRADER_ROLE)
        except Exception as e:
            print(f"[web] supabase disabled: {e}", file=sys.stderr)
            _sb = None


def _db_err(e: Exception) -> HTTPException:
    """Log the real DB error server-side, return a generic 500 to clients."""
    print(f"[web] database error: {e}", file=sys.stderr)
    return HTTPException(status_code=500, detail="database error")


def _missing_submission_column(error: Exception) -> bool:
    """True only for an explicit PostgREST/PostgreSQL missing-column signal.

    A generic error containing the word "column" can be an integrity failure;
    retrying that would silently drop provenance fields.
    """
    msg = str(error)
    low = msg.lower()
    return ("42703" in msg or "pgrst204" in low
            or "undefined_column" in low
            or ("column" in low and ("does not exist" in low
                                     or "could not find" in low)))


def _legacy_submission_row(row: dict, error: Exception, *, operation: str) -> dict:
    """Strip only the 2026-10 traceability columns for an old schema.

    A contest row may never lose contest_id. A practice row has no provenance
    to lose, so it may also use the old schema when contest_id itself is absent.
    Raises the original error for any non-missing-column failure.
    """
    if not _missing_submission_column(error):
        raise error
    low = str(error).lower()
    if "contest_id" in low and row.get("contest_id"):
        print("[web] refusing to drop contest_id from a contest submission; "
              "run supabase/schema.sql", file=sys.stderr)
        raise error
    if row.get("contest_id"):
        traceability = tuple(c for c in _NEW_SUB_COLS if c != "contest_id")
    else:
        traceability = _NEW_SUB_COLS
    slim = {k: v for k, v in row.items() if k not in traceability}
    if len(slim) == len(row):
        raise error
    print(f"[web] submissions {operation} retried without {traceability[0]}.. "
          f"({error}); run supabase/schema.sql to add them", file=sys.stderr)
    return slim


async def _store_submission(row: dict, target_id: Optional[str] = None) -> Optional[str]:
    """Persist one submission (insert, or update when rejudging).

    Never fails the judging response: returns the row id, or None when history is
    disabled / the write failed.
    """
    if _sb is None:
        return None

    def _write():
        if target_id:
            try:
                return _sb.table("submissions").update(row).eq("id", target_id).execute()
            except Exception as first:
                # Keep rejudge compatible with a pre-2026-10 schema too, under
                # the same no-provenance-downgrade rule as inserts.
                slim = _legacy_submission_row(row, first, operation="update")
                return (_sb.table("submissions").update(slim)
                        .eq("id", target_id).execute())
        try:
            return _sb.table("submissions").insert(row).execute()
        except Exception as first:
            # Pre-2026-10 database: the extra traceability columns do not exist
            # yet. Drop them rather than losing the submission. Retry ONLY for
            # missing-column errors; network/timeouts must not double-insert and
            # integrity errors must not silently drop provenance columns.
            slim = _legacy_submission_row(row, first, operation="insert")
            return _sb.table("submissions").insert(slim).execute()

    try:
        res = await asyncio.wait_for(asyncio.to_thread(_write), timeout=15)
        _bust_cache()  # aggregates changed: drop cached stats
        data = getattr(res, "data", None) or []
        if data:
            return data[0].get("id") or target_id
        return target_id
    except Exception as e:
        print(f"[web] supabase write failed: {e}", file=sys.stderr)
        return None


# ---- feedback policy: which test data may be shown to whom ----
# Contest feedback exposes public samples only (or none). The owner may inspect
# failed practice cases, by explicit product policy. Upcoming/running contests
# also restrict the practice path and reads of previously stored practice data.
# Redact before returning AND persisting; practice provenance is server-generated.
def _iso_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


# ---- per-user fair-use quota (sliding window, in memory) ----
_quota_hits: Dict[str, collections.deque] = {}


def _evict_oldest(store: dict, now: float, window: int) -> None:
    """Evict only the oldest entries instead of clearing the whole map.

    Clearing everything on overflow lets an attacker flush victims' quotas
    with garbage keys; evicting oldest bounds memory without that bypass.
    """
    if len(store) <= 20000:
        return
    # Drop ~10% oldest-touched keys (cheapest LRU approximation for dicts).
    try:
        keys = list(store.keys())
        for k in keys[:2000]:
            dq = store.get(k)
            if not dq or (dq and now - dq[-1] > window):
                store.pop(k, None)
        while len(store) > 20000:
            store.pop(next(iter(store)), None)
    except (StopIteration, RuntimeError):
        pass


def _quota_check(kind: str, key: str, quota: tuple) -> None:
    limit, window = quota
    if limit <= 0 or not key:
        return  # disabled
    now = time.monotonic()
    key = f"{kind}:{key}"
    dq = _quota_hits.get(key)
    if dq is None:
        dq = _quota_hits[key] = collections.deque()
        _evict_oldest(_quota_hits, now, window)
        dq = _quota_hits.get(key)
        if dq is None:
            dq = _quota_hits[key] = collections.deque()
    while dq and now - dq[0] > window:
        dq.popleft()
    if len(dq) >= limit:
        retry = max(1, int(window - (now - dq[0])) + 1)
        raise HTTPException(
            status_code=429,
            detail=f"quota exceeded ({limit} per {window}s) — retry in {retry}s",
            headers={"Retry-After": str(retry)})
    dq.append(now)


# ---- judge job queue (B9) ----
# Every submission/rejudge becomes a job: the HTTP handler only enqueues, a
# worker thread-pool runs it, and the client polls /api/jobs/{id}. With Supabase
# the jobs table is the durable queue (restart-safe); without it the queue is
# in-memory and the API still behaves the same.
_jobs: Dict[str, dict] = {}
# The queue belongs to one event loop at a time. It is created on first use
# inside the running loop (asyncio.Queue binds to a loop on Python 3.9), and if
# we are later called from a different loop the pending job ids are carried over
# into a fresh queue instead of being stranded (bites the test suite, where each
# TestClient runs its own loop, and uvicorn's reloader).
_job_queue: Optional["asyncio.Queue[str]"] = None
_job_loop: Optional[object] = None
_active_jobs: Dict[str, set] = {}       # user_email -> {job ids} queued/running
_admission_lock = threading.RLock()
LEASE_SECONDS = max(60, int(OVERALL_TIMEOUT + 60))
HEARTBEAT_SECONDS = 10
WORKER_ID = f"{os.getpid()}-{uuid.uuid4()}"


async def _queue_rpc(name: str, **params):
    if _sb is None:
        raise HTTPException(status_code=503, detail="judge database unavailable",
                            headers={"Retry-After": "30"})
    try:
        response = await asyncio.to_thread(
            lambda: _sb.rpc("judge_queue_" + name, params).execute())
        return response.data
    except Exception as e:
        print(f"[web] queue {name} failed: {e}", file=sys.stderr)
        raise HTTPException(status_code=503, detail="judge database unavailable",
                            headers={"Retry-After": "30"})


def _judge_isolation_posture(*, role: str, strict: bool, acknowledged: bool,
                             can_sandbox: bool, netns_ok: bool) -> str:
    """Fail-closed boot contract for judge runtimes (pure decision, no I/O).

    Production judge roles (combined/worker) must declare their isolation
    posture explicitly; anything that would silently judge without guaranteed
    isolation raises RuntimeError before any worker starts. `strict` always
    wins over `acknowledged`. Returns "api" (never executes), "strict",
    "acknowledged", or "dev".
    """
    if role == "api":
        return "api"
    if strict:
        if not can_sandbox:
            raise RuntimeError(
                "GRADER_REQUIRE_NETNS=1 needs a Linux+root judge runtime "
                "(this host cannot drop privileges or isolate networks); "
                "refusing to boot workers")
        if not netns_ok:
            raise RuntimeError(
                "GRADER_REQUIRE_NETNS=1 but unshare(CLONE_NEWNET) is unavailable "
                "on this host; refusing to boot workers (do not grant "
                "CAP_SYS_ADMIN as a shortcut — see grader.py)")
        return "strict"
    if acknowledged:
        return "acknowledged"
    if can_sandbox:
        raise RuntimeError(
            "Linux+root judge runtime must declare its isolation posture: set "
            "GRADER_REQUIRE_NETNS=1 (public hosts; verified namespace support "
            "required) or GRADER_ALLOW_UNISOLATED_JUDGE=1 (explicitly accept "
            "rlimits-only judging); refusing to boot workers")
    return "dev"


async def validate_runtime() -> None:
    await asyncio.to_thread(problem_store.backend_problem_files.validate, GRADER_ROLE)
    if GRADER_ROLE != "api" and JUDGE_WORKERS < 1:
        raise RuntimeError("host RAM cannot safely fit a judge, compiler and API reserve")
    if QUEUE_MODE not in ("local", "database"):
        raise RuntimeError("GRADER_QUEUE_MODE must be local or database")
    if GRADER_ROLE not in ("combined", "api", "worker"):
        raise RuntimeError("GRADER_ROLE must be combined, api or worker")
    if PROBLEM_SOURCE not in ("local", "supabase"):
        raise RuntimeError("GRADER_PROBLEM_SOURCE must be local or supabase")
    if PROBLEM_SOURCE == "supabase" and _sb is None:
        raise RuntimeError("GRADER_PROBLEM_SOURCE=supabase needs SUPABASE_URL + "
                           "SUPABASE_KEY (service_role); refusing to boot without "
                           "a problem store (no local fallback)")
    if GRADER_ROLE != "combined" and (QUEUE_MODE != "database" or
                                      not os.environ.get("SESSION_SECRET", "").strip()):
        raise RuntimeError("separated roles require database mode and stable SESSION_SECRET")
    workers_env = (os.environ.get("WEB_CONCURRENCY") or
                   os.environ.get("UVICORN_WORKERS") or "1").strip()
    if QUEUE_MODE == "local" and workers_env not in ("", "1"):
        raise RuntimeError("local queue mode requires a single process "
                           "(in-memory queue/quota are per-process): use "
                           "GRADER_QUEUE_MODE=database with multiple workers")
    try:
        _trusted_proxy_nets()
    except ValueError as e:
        raise RuntimeError(f"GRADER_TRUSTED_PROXY_CIDRS is invalid ({e}); "
                           f"refusing to boot")
    # Fail-closed isolation contract for judge roles (api never executes, so
    # it is exempt). The namespace probe runs only when it can change the
    # outcome: strict mode on a sandbox-capable host.
    strict = grader._env_flag("GRADER_REQUIRE_NETNS")
    acknowledged = grader._env_flag("GRADER_ALLOW_UNISOLATED_JUDGE")
    can_sandbox = sys.platform.startswith("linux") and os.geteuid() == 0
    netns_ok = False
    if GRADER_ROLE in ("combined", "worker") and strict and can_sandbox:
        netns_ok = await asyncio.to_thread(grader.netns_available)
    posture = _judge_isolation_posture(
        role=GRADER_ROLE, strict=strict, acknowledged=acknowledged,
        can_sandbox=can_sandbox, netns_ok=netns_ok)
    if GRADER_ROLE in ("combined", "worker"):
        import job_resources
        await asyncio.to_thread(job_resources.initialize)
    if can_sandbox and GRADER_ROLE in ("combined", "worker"):
        try:
            await asyncio.to_thread(grader.job_sandbox.landlock_abi)
        except OSError as exc:
            raise RuntimeError("Linux judge requires Landlock ABI >= 3; refusing to boot workers") from exc
        try:
            await asyncio.to_thread(grader.job_sandbox.verify_network_filter)
        except OSError as exc:
            raise RuntimeError("Linux judge requires a network syscall filter; refusing to boot workers") from exc
    if posture == "dev":
        print("[web] dev judge runtime: network namespaces unavailable here; "
              "judging runs rlimits-only (set GRADER_REQUIRE_NETNS=1 on public hosts)",
              file=sys.stderr)
    if QUEUE_MODE == "database":
        await _queue_rpc("diagnostics")  # verify migration and database before accepting traffic


async def serve_workers() -> None:
    if GRADER_ROLE == "api":
        raise RuntimeError("API role cannot start judge workers")
    await validate_runtime()
    await _recover_jobs()
    await asyncio.gather(*(_judge_worker(i) for i in range(JUDGE_WORKERS)))


def _try_host_slot():
    """OS locks coordinate all worker processes sharing this host directory.

    A lock is held through runner cleanup. No expiry or process-local semaphore
    can accidentally release it while a timed-out descendant still runs.
    """
    CAPACITY_DIR.mkdir(parents=True, exist_ok=True)
    for idx in range(JUDGE_WORKERS):
        handle = open(CAPACITY_DIR / f"slot-{idx}.lock", "a+b")
        try:
            if os.name == "nt":
                import msvcrt
                handle.seek(0)
                if not handle.read(1):
                    handle.write(b"0")
                    handle.flush()
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            os.set_inheritable(handle.fileno(), False)
            return handle
        except (OSError, BlockingIOError):
            handle.close()
    return None


@asynccontextmanager
async def _host_slot():
    handle = None
    while handle is None:
        handle = _try_host_slot()
        if handle is None:
            await asyncio.sleep(0.1)
    try:
        yield
    finally:
        handle.close()


def _queue() -> "asyncio.Queue[str]":
    """The job queue, (re)bound to the running loop. Safe to call from a worker
    thread (sync endpoints): with no running loop it returns the existing queue
    instead of raising."""
    global _job_queue, _job_loop
    try:
        loop: Optional[object] = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    if _job_queue is None:
        _job_queue = asyncio.Queue()
        _job_loop = loop
        return _job_queue
    if loop is not None and _job_loop is not loop:
        old = _job_queue
        _job_queue = asyncio.Queue()
        _job_loop = loop
        while not old.empty():
            try:
                _job_queue.put_nowait(old.get_nowait())
            except Exception:
                break
    return _job_queue


def _queue_depth() -> int:
    """Queued job count without needing an event loop (sync endpoints)."""
    with _admission_lock:
        return sum(j.get("status") == "queued" for j in _jobs.values())


_queue_depth_cache: dict = {"at": 0.0, "value": 0}


def _queue_depth_cached(ttl: float = 1.5) -> int:
    """Cached depth for status polls. Admission still uses exact _queue_depth."""
    now = time.monotonic()
    if now - float(_queue_depth_cache.get("at", 0.0)) < ttl:
        return int(_queue_depth_cache.get("value", 0))
    value = _queue_depth()
    _queue_depth_cache.update({"at": now, "value": value})
    return value


# Columns added by the 2026-10 migration; if the migration was not run yet, the
# insert is retried without them instead of losing the submission.
_NEW_SUB_COLS = ("tests_hash", "grader_version", "kind", "contest_id", "job_id")


def _prune_jobs() -> None:
    cut = time.monotonic() - JOB_TTL_S
    stale = [k for k, j in _jobs.items()
             if j.get("created_monotonic", 0) < cut and j.get("status") in ("done", "error")]
    for k in stale:
        _jobs.pop(k, None)


def _remember_job(row: dict) -> dict:
    _prune_jobs()
    job = _jobs.get(row["id"]) or {}
    job.update(row)
    job.setdefault("created_monotonic", time.monotonic())
    _jobs[row["id"]] = job
    return job


async def _queue_position_hint() -> Optional[int]:
    """Advisory only: a diagnostics outage cannot undo durable admission."""
    if QUEUE_MODE != "database":
        return _queue_depth_cached()
    try:
        return int((await _queue_rpc("diagnostics"))["queued"])
    except (HTTPException, KeyError, TypeError, ValueError):
        return None


async def _load_job(jid: str) -> Optional[dict]:
    if _sb is None:
        if QUEUE_MODE == "database":
            raise HTTPException(status_code=503, detail="judge database unavailable")
        return None
    try:
        # Narrow columns: exclude source/payload (up to 200k) on status polls.
        res = await asyncio.to_thread(
            lambda: _sb.table("judge_jobs").select(
                "id,status,problem,kind,user_email,error,result,"
                "created_at,started_at,finished_at,worker_id,attempt,"
                "contest_id,tests_hash,grader_version,reveal"
            ).eq("id", jid).limit(1).execute())
    except Exception as e:
        if QUEUE_MODE == "database":
            raise HTTPException(status_code=503, detail="judge database unavailable")
        print(f"[web] job lookup failed: {e}", file=sys.stderr)
        return None
    rows = res.data or []
    if not rows:
        return None
    row = dict(rows[0])
    # A status poll may refresh progress and the terminal result, never the
    # ownership: if this process is running the job, the identity it claimed with
    # is what the completion fence must use. Refreshing worker_id/attempt here
    # would let a worker whose lease already expired finish the job under the new
    # owner's identity.
    running = _jobs.get(jid) or {}
    if running.get("status") == "running" and running.get("worker_id"):
        for key in ("worker_id", "attempt", "started_at"):
            if running.get(key) is not None:
                row[key] = running[key]
    if row.get("status") == "done" and isinstance(row.get("result"), dict):
        job = _remember_job(row)
        job["result"] = row["result"]
        return job
    return _remember_job(row)


async def _update_job_db(jid: str, patch: dict) -> None:
    if _sb is None:
        return
    try:
        await asyncio.to_thread(
            lambda: _sb.table("judge_jobs").update(patch).eq("id", jid).execute())
    except Exception as e:
        print(f"[web] judge_jobs update failed: {e}", file=sys.stderr)


async def _create_job(row: dict, track_active: bool = True) -> dict:
    """Reserve capacity and per-account ownership atomically before any yield."""
    if os.environ.get("GRADER_ACCEPT_JOBS", "1") != "1":
        raise HTTPException(status_code=503, detail="submission admission is paused",
                            headers={"Retry-After": "60"})
    jid = str(uuid.uuid4())
    row = {**row, "id": jid, "status": "queued", "result": None, "error": "",
           "created_at": _iso_now()}
    if QUEUE_MODE == "database":
        quota = RUN_QUOTA if row.get("kind") == "playground" else SUBMIT_QUOTA
        response = await _queue_rpc("admit", p_job=row, p_queue_max=QUEUE_MAX,
                                    p_track_active=track_active,
                                    p_quota_limit=quota[0] if track_active else 0,
                                    p_quota_window=quota[1] if track_active else 0)
        if response.get("error"):
            code = int(response.get("status_code", 503))
            raise HTTPException(status_code=code, detail=response["error"],
                                headers={"Retry-After": str(response.get("retry_after", 30))}
                                if code in (409, 429, 503) else None)
        return response["job"]
    with _admission_lock:
        email = row.get("user_email") or ""
        if track_active and _active_jobs.get(email):
            raise HTTPException(status_code=409, detail="you already have a submission judging, please wait",
                                headers={"Retry-After": "30"})
        if _queue_depth() >= QUEUE_MAX:
            raise HTTPException(status_code=503, detail="judge queue is full — try again in a moment",
                                headers={"Retry-After": "30"})
        job = _remember_job(row)
        if track_active and email:
            _active_jobs.setdefault(email, set()).add(jid)
    if _sb is not None:
        try:
            await asyncio.to_thread(lambda: _sb.table("judge_jobs").insert(row).execute())
        except Exception as e:
            # No judge_jobs table (migration not run) — degrade to memory-only
            # queueing instead of refusing the submission.
            print(f"[web] judge_jobs insert failed ({e}); queueing in memory", file=sys.stderr)
    await _queue().put(jid)
    return job


async def _recover_jobs() -> None:
    """On boot: requeue jobs orphaned by a crash, and remember queued ones."""
    if QUEUE_MODE == "database" or _sb is None:
        return
    # Local mode is single-process only. Interrupted jobs have no live lease;
    # the durable database mode recovers ownership exclusively in claim RPC.
    await asyncio.to_thread(lambda: _sb.table("judge_jobs").update(
        {"status": "queued"}).eq("status", "running").execute())
    res = await asyncio.to_thread(
        lambda: _sb.table("judge_jobs").select("*").eq("status", "queued")
        .order("created_at").limit(REJUDGE_MAX).execute())
    rows = res.data or []
    dropped = 0
    for row in rows:
        if row["id"] in _jobs:
            continue
        try:
            created = dt.datetime.fromisoformat(str(row.get("created_at")).replace("Z", "+00:00"))
            age = (dt.datetime.now(dt.timezone.utc) - created).total_seconds()
        except Exception:
            age = 0
        if age > JOB_MAX_AGE_S:
            dropped += 1
            await _update_job_db(row["id"], {"status": "error", "error": "stale job dropped",
                                             "finished_at": _iso_now()})
            continue
        _remember_job(row)
        if row.get("user_email"):
            _active_jobs.setdefault(row["user_email"], set()).add(row["id"])
        await _queue().put(row["id"])
    if rows:
        print(f"[web] recovered {len(rows) - dropped} queued job(s), dropped {dropped} stale",
              file=sys.stderr)


async def _finish_job(jid: str, *, result: Optional[dict] = None, stored: Optional[dict] = None,
                      error: str = "", submission: Optional[dict] = None) -> None:
    job = _jobs.get(jid) or {"id": jid}
    if job.get("kind") != "playground":
        if result is not None:
            result = _result_with_reveal(job, result, job.get("reveal") or REVEAL_SAMPLES)
        if stored is not None or result is not None:
            stored = _stored_result(job, stored if stored is not None else result)
    if QUEUE_MODE == "database":
        response = await _queue_rpc("finish", p_id=jid, p_worker=job.get("worker_id"),
                                    p_attempt=job.get("attempt"), p_error=error,
                                    p_result=stored if stored is not None else result,
                                    p_submission=submission)
        if response.get("applied"):
            _bust_cache()
        _jobs.pop(jid, None)
        return
    job["status"] = "error" if error else "done"
    job["error"] = error
    if result is not None:
        # Local mode keeps the same redacted variant the database queue stores.
        # The requester-privileged view (REVEAL_ALL on admin rejudges) must not
        # live in a record the submission owner can poll; job_status re-applies
        # the job's policy per request.
        job["result"] = result if job.get("kind") == "playground" else (
            stored if stored is not None else result)
    job["finished_at"] = _iso_now()
    job.setdefault("created_monotonic", time.monotonic())
    # The submission row already owns the validated source. Polling terminal
    # jobs needs only the result, not another retained source/input copy.
    job.pop("source", None)
    job.pop("payload", None)
    _jobs[jid] = job
    email = job.get("user_email") or ""
    if email in _active_jobs:
        _active_jobs[email].discard(jid)
        if not _active_jobs[email]:
            _active_jobs.pop(email, None)
    await _update_job_db(jid, {"status": job["status"], "error": error,
                               "finished_at": job["finished_at"],
                               "result": stored if stored is not None else result,
                               # The row already owns the source. Do not keep
                               # a second copy in the status record.
                               "source": ""})
    _prune_jobs()


async def _judge_worker(idx: int) -> None:
    if GRADER_ROLE == "api":
        raise RuntimeError("API role cannot run judge workers")
    poll_delay = .25
    while True:
        import job_resources
        job_resources.assert_healthy()
        if QUEUE_MODE == "database":
            try:
                async with _host_slot():
                    job = await _queue_rpc("claim", p_worker=f"{WORKER_ID}:{idx}",
                                           p_lease_seconds=LEASE_SECONDS)
                    if job:
                        await _run_job(job)
                    # Idle wakeup is user-visible latency. Keep the fallback
                    # poll reasonably responsive; drain ready work without an
                    # extra sleep. The durable claim remains the only authority.
                    poll_delay = 0.0 if job else min(1.0, max(.5, poll_delay * 2))
            except asyncio.CancelledError:
                raise
            except Exception as e:
                print(f"[web] worker unavailable: {e}", file=sys.stderr)
                poll_delay = min(30.0, max(1.0, poll_delay * 2))
            await asyncio.sleep(poll_delay)
            continue
        # A queue token is a wakeup; choose student work before a rejudge.
        await _queue().get()
        try:
            async with _host_slot():
                with _admission_lock:
                    pending = [j for j in _jobs.values() if j.get("status") == "queued"]
                    job = min(pending, key=lambda j: (j.get("kind") == "rejudge",
                                                     j.get("created_monotonic", 0))) if pending else None
                    if job:
                        job["status"] = "running"
                if job:
                    await _run_job(job)
        except asyncio.CancelledError:
            raise
        except Exception:
            traceback.print_exc()
            if job:
                await _finish_job(job["id"], error="worker crashed")
        finally:
            _queue().task_done()


async def _run_job(job: dict) -> None:
    if GRADER_ROLE == "api":
        raise RuntimeError("API role cannot execute judge jobs")
    jid = job["id"]
    _jobs[jid] = job
    playground = job.get("kind") == "playground"
    prob = PROBLEMS.get(job.get("problem", ""))
    if not playground and PROBLEM_SOURCE == "supabase":
        # Revalidate each job, even if this worker already holds a full suite.
        # Keep the executed suite consistent with the admitted test hash.
        slug = str(job.get("problem", ""))
        try:
            prob = await asyncio.to_thread(problem_store.load_problem_supabase, slug, _sb)
            actual_hash = grader.tests_hash(prob)
            if job.get("tests_hash") and job["tests_hash"] != actual_hash:
                await _finish_job(jid, error="problem version changed; resubmit required")
                return
            if problem_store.backend_problem_files.backend() != "local":
                PROBLEMS[prob.pid] = prob
            _TESTS_HASH_CACHE[prob.pid] = actual_hash
        except problem_store.ProblemNotFound:
            await _finish_job(jid, error="unknown problem")
            return
        except Exception:
            await _finish_job(jid, error="problem store unavailable")
            return
    if prob is None and not playground:
        await _finish_job(jid, error=f"unknown problem: {job.get('problem')}")
        return
    if prob is not None and not getattr(prob, "is_published", True):
        # Unpublished after enqueue: refuse to judge (explicit, not silent).
        await _finish_job(jid, error=f"unknown problem: {job.get('problem')}")
        return
    ext = str(LANGS.get(job.get("language", ""), {}).get("ext") or ".py")
    job["status"] = "running"
    job["started_at"] = _iso_now()
    _jobs[jid] = job
    if QUEUE_MODE == "local":
        await _update_job_db(jid, {"status": "running", "started_at": job["started_at"]})
    reveal = job.get("reveal") or REVEAL_SAMPLES
    payload = {"problem": prob.pid if prob else None, "ext": ext,
               "source": job.get("source", ""), "reveal": reveal,
               "memory_cap_mb": MEMORY_CAP_MB, "judge_jobs": JUDGE_JOBS}
    if prob is not None and not playground:
        # Trusted server-side channel: the isolated runner judges these bytes
        # without disk or Supabase access (no per-test Storage downloads there).
        # Local mode too: avoids a second full disk read + re-hash in the child.
        payload["problem_data"] = problem_store.serialize_problem(prob)
    if playground:
        payload.update(job.get("payload") or {})
        if PROBLEM_SOURCE == "supabase" and prob is not None:
            payload["problem_data"] = problem_store.serialize_problem(prob)
    try:
        from judge_runner import run_isolated
        execution = asyncio.create_task(run_isolated(
            "playground" if playground else "submission", payload,
            (payload.get("time_limit", 2) + RUN_BUDGET) if playground else OVERALL_TIMEOUT))
        heartbeat = asyncio.create_task(_heartbeat_job(job)) if QUEUE_MODE == "database" else None
        try:
            if heartbeat:
                done, _ = await asyncio.wait((execution, heartbeat), return_when=asyncio.FIRST_COMPLETED)
                if heartbeat in done:
                    await heartbeat  # raises on lost lease or database failure
            result = await execution
        finally:
            execution.cancel()
            if heartbeat:
                heartbeat.cancel()
            await asyncio.gather(execution, *([heartbeat] if heartbeat else []), return_exceptions=True)
    except asyncio.TimeoutError:
        await _finish_job(jid, error="judging timed out (server overloaded?)")
        return
    except Exception:
        traceback.print_exc()
        # Never echo exception text to the client: it can contain paths/env.
        await _finish_job(jid, error="internal judging error")
        return
    if "error" in result:
        await _finish_job(jid, error=str(result["error"]))
        return
    if playground:
        await _finish_job(jid, result=result, stored=result)
        return
    stored = _stored_result(job, result)
    sid = None
    row = _submission_row(job, result, stored)
    if QUEUE_MODE == "local" and _sb is not None:
        sid = await _store_submission(row, target_id=job.get("target_id"))
    result["id"] = sid
    result["job_id"] = jid
    result["name"] = job.get("name", "")
    stored.update(id=sid, job_id=jid, name=job.get("name", ""))
    await _finish_job(jid, result=result, stored=stored,
                      submission=row if QUEUE_MODE == "database" else None)


async def _heartbeat_job(job: dict) -> None:
    while True:
        await asyncio.sleep(HEARTBEAT_SECONDS)
        valid = await _queue_rpc("heartbeat", p_id=job["id"], p_worker=job["worker_id"],
                                p_attempt=job["attempt"], p_lease_seconds=LEASE_SECONDS)
        if not valid:
            raise RuntimeError("judge ownership lost")


def _result_with_reveal(job: dict, result: dict, reveal: str) -> dict:
    if job.get("contest_id") and reveal == REVEAL_PRACTICE:
        reveal = REVEAL_SAMPLES
    prob = PROBLEMS.get(job.get("problem", ""))
    samples = grader.sample_names(prob) if prob else set()
    raw = [{**r, "public": r.get("test") in samples and not r.get("hidden", False)}
           for r in result.get("results", [])]
    return {**result, "results": _redact_results(raw, reveal)}


def _stored_result(job: dict, result: dict) -> dict:
    reveal = REVEAL_NONE if job.get("reveal") == REVEAL_NONE else REVEAL_SAMPLES
    if job.get("reveal") == REVEAL_PRACTICE and not job.get("contest_id"):
        reveal = REVEAL_PRACTICE
    return _result_with_reveal(job, result, reveal)


def _submission_row(job: dict, result: dict, stored: Optional[dict] = None) -> dict:
    """Persist only feedback permitted by the server-selected job policy."""
    prob = PROBLEMS.get(job.get("problem", ""))
    results = _stored_result(job, stored if stored is not None else result).get("results", [])
    return {
        "problem": job.get("problem", ""), "language": job.get("language", ""),
        "filename": job.get("filename"), "name": job.get("name", ""),
        "user_email": job.get("user_email", ""),
        "verdict": result.get("verdict", ""), "accepted": result.get("accepted", False),
        "passed": result.get("passed", 0), "total": result.get("total", 0),
        "score": result.get("score", 0), "counts": result.get("counts", {}),
        "results": results,
        "code": job.get("source") or "",
        "detail": (result.get("compile_error", "") or "")[:4000],
        "tests_hash": (result.get("tests_hash") or job.get("tests_hash")
                       or (_tests_hash(prob.pid) if prob else "")),
        "grader_version": grader.GRADER_VERSION,
        "kind": job.get("kind", "submit"),
        "contest_id": job.get("contest_id"),
        "job_id": job.get("id"),
    }


# ---- contest policy (B7) ----
def _contest_times_valid(contest: Optional[dict]) -> bool:
    """True only for timezone-aware, ordered contest bounds.

    _contest_window's legacy fallback treats malformed dates as ended for
    display purposes.  Feedback authorities must never reuse that fallback:
    an invalid bound is an unavailable policy, not permission to show samples.
    """
    if not isinstance(contest, dict):
        return False
    try:
        start = dt.datetime.fromisoformat(
            str(contest.get("starts_at")).replace("Z", "+00:00"))
        end = dt.datetime.fromisoformat(
            str(contest.get("ends_at")).replace("Z", "+00:00"))
    except (TypeError, ValueError, OverflowError):
        return False
    return start.tzinfo is not None and end.tzinfo is not None and end > start


def _contest_window(contest: dict) -> str:
    """'upcoming' | 'running' | 'ended' (UTC now vs the contest window)."""
    now = dt.datetime.now(dt.timezone.utc)
    try:
        start = dt.datetime.fromisoformat(str(contest.get("starts_at")).replace("Z", "+00:00"))
        end = dt.datetime.fromisoformat(str(contest.get("ends_at")).replace("Z", "+00:00"))
    except Exception:
        return "ended"
    if now < start:
        return "upcoming"
    if now > end:
        return "ended"
    return "running"


def _contest_or_404(cid: str) -> dict:
    if _sb is None:
        raise HTTPException(status_code=404, detail="contests not configured")
    try:
        res = _sb.table("contests").select("*").eq("id", cid).limit(1).execute()
    except Exception as e:
        raise _db_err(e)
    rows = res.data or []
    if not rows:
        raise HTTPException(status_code=404, detail="unknown contest")
    return dict(rows[0])


def _parse_bool(value: object, default: bool = True) -> bool:
    """Strict bool parsing: string 'false'/'0'/'no' must not become True."""
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    s = str(value).strip().lower()
    if s in ("1", "true", "yes", "y", "on"):
        return True
    if s in ("0", "false", "no", "n", "off"):
        return False
    return default


def _reveal_for(contest: Optional[dict], email: str) -> str:
    if _is_admin(email):
        return REVEAL_ALL
    if contest is not None and not _contest_times_valid(contest):
        return REVEAL_NONE
    if contest and _contest_window(contest) == "running" and contest.get("show_samples") is False:
        return REVEAL_NONE
    return REVEAL_SAMPLES


def _live_hidden_pids(exclude_cid: str = "") -> set:
    """Problems inside a *running* contest with show_samples=false.

    Submissions for these problems must not reveal samples even as practice
    (contest='' path), and /sample/* must refuse to serve them.
    """
    hidden: set = set()
    if _sb is None:
        return hidden
    try:
        # Sample bytes are a live disclosure boundary: never let the 20s
        # visibility cache keep serving them after another process turns
        # show_samples off. Policy failures still raise (fail closed).
        for row in _contest_policy_rows(fresh=True):
            if exclude_cid and row.get("id") == exclude_cid:
                continue
            # A malformed contest bound is not proof samples are public.
            # Hide its listed problems until the policy can be trusted.
            if not _contest_times_valid(row):
                for x in (row.get("problems") or []):
                    try:
                        hidden.add(_resolve_pid(str(x)))
                    except Exception:
                        pass
                continue
            if _contest_window(row) != "running":
                continue
            if row.get("show_samples") is False:
                for x in (row.get("problems") or []):
                    try:
                        hidden.add(_resolve_pid(str(x)))
                    except Exception:
                        pass
    except HTTPException:
        raise
    return hidden


def _effective_reveal(contest: Optional[dict], email: str, pid: str) -> str:
    """Practice failures are open only outside any upcoming/running contest.

    Omitting contest= must never grant extra feedback for a protected problem.
    Unknown policy/times fail closed, including reads of prior practice history.
    """
    if contest is not None:
        return _reveal_for(contest, email)
    protected = False
    try:
        for row in _contest_policy_rows(fresh=True):
            if pid not in {_resolve_pid(str(x)) for x in (row.get("problems") or [])}:
                continue
            # _contest_window's legacy invalid-date fallback is not a basis
            # for declassifying fixtures. Require valid, timezone-aware dates.
            if not _contest_times_valid(row):
                return REVEAL_NONE
            window = _contest_window(row)
            if window != "ended":
                protected = True
                if window == "running" and row.get("show_samples") is False:
                    return REVEAL_NONE
    except Exception:
        return REVEAL_NONE
    return REVEAL_SAMPLES if protected else REVEAL_PRACTICE


def _live_reveal(contest_id: object, owner: str, pid: str) -> str:
    """Owner-facing feedback policy for a stored row, evaluated now.

    Contest rows use their own row so an admin owner keeps the same ALL view
    the submit/rejudge path used; everyone else follows the live window.
    Practice rows follow the contest-aware practice policy. Unknown contests
    and policy failures fail closed to REVEAL_NONE.
    """
    if not contest_id:
        return _effective_reveal(None, owner, pid)
    try:
        for row in _contest_policy_rows(fresh=True):
            if str(row.get("id")) == str(contest_id):
                return _reveal_for(row, owner)
    except Exception:
        return REVEAL_NONE
    return REVEAL_NONE


# ---- contest registration (Codeforces style) ----
# Missing registration storage must never turn a registration gate off.
_reg_table_ok: Dict[str, bool] = {"value": True}


def _registrations_supported() -> bool:
    return _sb is not None and _reg_table_ok["value"]


def _register_note_missing_table(e: Exception) -> None:
    if "contest_registrations" in str(e) and _reg_table_ok["value"]:
        _reg_table_ok["value"] = False
        print("[web] contest_registrations unavailable — submissions require "
              "schema repair; run supabase/schema.sql", file=sys.stderr)


def _registered_emails(cid: str) -> set:
    """Emails registered for one contest (empty set when unavailable)."""
    if _sb is None or not _reg_table_ok["value"]:
        return set()
    try:
        res = _sb.table("contest_registrations").select("user_email").eq(
            "contest_id", cid).limit(5000).execute()
        return {r.get("user_email", "") for r in (res.data or []) if r.get("user_email")}
    except Exception as e:
        _register_note_missing_table(e)
        return set()


def _is_registered(cid: str, email: str) -> bool:
    if not email:
        return False
    if not _registrations_supported():
        return False
    if _sb is None:
        return False
    try:
        # Single-row EXISTS-style check: no 5000-row download per submit.
        res = _sb.table("contest_registrations").select("user_email").eq(
            "contest_id", cid).eq("user_email", email).limit(1).execute()
        return bool(res.data)
    except Exception as e:
        _register_note_missing_table(e)
        return False


def _registered_contest_ids(email: str, contest_ids: list) -> set:
    """Contest ids one user is registered for, fetched in batches.

    /api/contests lists up to 200 contests; calling _is_registered per row was
    an N+1 query (200 sequential round trips for every logged-in visitor).
    """
    if not email or not contest_ids or not _registrations_supported() or _sb is None:
        return set()
    found = set()
    ids = list(contest_ids)
    try:
        # Chunk the IN list so a 200-contest listing never builds a URL near
        # the proxy/PostgREST request-line limit.
        for start in range(0, len(ids), 100):
            chunk = ids[start:start + 100]
            offset = 0
            while offset <= len(chunk):
                res = _sb.table("contest_registrations").select("contest_id", count="exact").eq(
                    "user_email", email).in_("contest_id", chunk).order("contest_id").range(
                    offset, offset + len(chunk)).execute()
                batch = res.data or []
                found.update(r.get("contest_id") for r in batch if r.get("contest_id"))
                offset += len(batch)
                if res.count is not None and offset >= res.count:
                    break
                if not batch:
                    if res.count is not None and offset < res.count:
                        raise RuntimeError("incomplete user registrations")
                    break
            else:
                raise RuntimeError("user registrations exceed unique contest budget")
        return found
    except Exception as e:
        _register_note_missing_table(e)
        return set()


def _registration_count(cid: str) -> int:
    return len(_registered_emails(cid))


def _contest_started(contest: dict) -> bool:
    """Problems of a contest are readable/submittable once the window opened."""
    return _contest_window(contest) in ("running", "ended")


_reg_count_cache: Dict[str, object] = {"at": 0.0, "counts": {}}


def _registration_counts() -> Dict[str, int]:
    """{contest_id: participants} for every contest (one query, 20 s cache)."""
    now = time.monotonic()
    if _reg_count_cache["at"] and now - float(_reg_count_cache["at"]) < 20:
        return _reg_count_cache["counts"]
    counts: Dict[str, int] = {}
    if _sb is not None and _reg_table_ok["value"]:
        try:
            offset = 0
            while offset < 50000:
                res = _sb.table("contest_registrations").select(
                    "contest_id", count="exact").order("contest_id").order(
                    "user_email").range(offset, offset + 999).execute()
                batch = res.data or []
                for r in batch:
                    cid = r.get("contest_id") or ""
                    counts[cid] = counts.get(cid, 0) + 1
                offset += len(batch)
                if res.count is not None and offset >= res.count:
                    break
                if not batch:
                    if res.count is not None and offset < res.count:
                        raise RuntimeError("incomplete registration counts")
                    break
            else:
                raise RuntimeError("registration counts exceed safe query budget")
        except Exception as e:
            _register_note_missing_table(e)
            raise _db_err(e)
    _reg_count_cache.update({"at": now, "counts": counts})
    return counts


# ---- Google login (OAuth 2.0 code flow, session cookie) ----
# Env: GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET (from Google Auth Platform),
#   GOOGLE_REDIRECT_URI (must match the Authorised redirect URI exactly),
#   FRONTEND_URL (where to send the user after login),
#   SESSION_SECRET (random string; sessions reset on restart if unset),
#   SESSION_SAMESITE (lax: same-origin proxy prod and local http),
#   SESSION_SECURE (true in prod, false for local http).
GOOGLE_CLIENT_ID = os.environ.get("GOOGLE_CLIENT_ID", "").strip()
GOOGLE_CLIENT_SECRET = os.environ.get("GOOGLE_CLIENT_SECRET", "").strip()
GOOGLE_REDIRECT_URI = os.environ.get(
    "GOOGLE_REDIRECT_URI",
    "https://newbiegrader.pages.dev/api/auth/callback").strip()
FRONTEND_URL = os.environ.get(
    "FRONTEND_URL", "https://newbiegrader.pages.dev").rstrip("/")

# ADMIN_EMAILS is the immutable bootstrap set: those accounts always keep
# admin access, even if the database is down or a row is deleted by mistake.
ADMIN_EMAILS = {e.strip().lower() for e in os.environ.get(
    "ADMIN_EMAILS", "sphetpitak@gmail.com").split(",") if e.strip()}

# Additional admins are managed from the admin page and stored in public.admins.
# _is_admin() stays synchronous and network-free: this snapshot is refreshed by
# a background task (and updated immediately after an add/remove).
_ADMIN_CACHE_TTL = max(2.0, float(os.environ.get("GRADER_ADMIN_CACHE_TTL", "5") or 5))
_db_admin_emails: frozenset = frozenset()
_admin_cache_error = False


def _norm_email(value: object) -> str:
    return str(value or "").strip().lower()


def _admin_email_valid(email: str) -> bool:
    return (0 < len(email) <= 254
            and re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email) is not None)


def _load_db_admins_sync() -> bool:
    """Refresh the DB-backed admin snapshot; env bootstrap is always kept."""
    global _db_admin_emails, _admin_cache_error
    if _sb is None:
        _db_admin_emails = frozenset()
        return False
    try:
        res = _sb.table("admins").select("email").execute()
        _db_admin_emails = frozenset(
            _norm_email(row.get("email")) for row in (res.data or [])
            if _norm_email(row.get("email")))
        _admin_cache_error = False
        return True
    except Exception as e:
        # Fail closed for DB-backed admins: never keep stale grants when the
        # list cannot be verified. Env bootstrap accounts stay active, and the
        # error is logged once per outage instead of every few seconds.
        _db_admin_emails = frozenset()
        if not _admin_cache_error:
            print(f"[web] admin list unavailable (env admins still active): {e}",
                  file=sys.stderr)
            _admin_cache_error = True
        return False


async def _admin_cache_loop() -> None:
    while True:
        await asyncio.sleep(_ADMIN_CACHE_TTL)
        await asyncio.to_thread(_load_db_admins_sync)


def _is_admin(email: str) -> bool:
    value = _norm_email(email)
    return bool(value) and (value in ADMIN_EMAILS or value in _db_admin_emails)


REPORT_KINDS = ("statement", "tests", "other")
REPORT_STATUS = ("open", "closed")

_session_secret = os.environ.get("SESSION_SECRET", "").strip()
if not _session_secret:
    _session_secret = secrets.token_hex(32)
    print("[web] WARNING: SESSION_SECRET not set, sessions die on restart", file=sys.stderr)
_login_signer = URLSafeTimedSerializer(_session_secret, salt="login-once-v3")
_remember_signer = URLSafeTimedSerializer(_session_secret, salt="remember-me-v3")
REMEMBER_MAX_AGE = 365 * 24 * 3600
_REMEMBER_COOKIE = "ng_remember"
_SESSION_KEY = "auth_session"


def _cookie_secure() -> bool:
    return os.environ.get("SESSION_SECURE", "true").lower() != "false"


def _session_hash(token) -> str:
    if not isinstance(token, str) or not re.fullmatch(r"[A-Za-z0-9_-]{43}", token):
        return ""
    return hashlib.sha256(token.encode("ascii")).hexdigest()


def _set_remember_choice(resp, token: str, remember: bool) -> None:
    if remember is True:
        resp.set_cookie(_REMEMBER_COOKIE, _remember_signer.dumps({"sid": token}),
                        max_age=REMEMBER_MAX_AGE, httponly=True, samesite="lax",
                        secure=_cookie_secure(), path="/")
    else:
        resp.delete_cookie(_REMEMBER_COOKIE, path="/", httponly=True,
                           samesite="lax", secure=_cookie_secure())


def _read_remember(request: Request) -> str:
    try:
        value = _remember_signer.loads(request.cookies.get(_REMEMBER_COOKIE, ""),
                                       max_age=REMEMBER_MAX_AGE).get("sid")
        return value if _session_hash(value) else ""
    except Exception:
        return ""


def _browser_session_hashes(request: Request) -> list[str]:
    return list(dict.fromkeys(value for value in (
        _session_hash(request.session.get(_SESSION_KEY)),
        _session_hash(_read_remember(request))) if value))


def _revoke_browser_sessions(request: Request) -> None:
    hashes = _browser_session_hashes(request)
    if hashes:
        result = _auth_rpc("auth_session_revoke", {"p_token_hashes": hashes})
        if result.get("ok") is not True:
            raise HTTPException(status_code=503, detail="logout unavailable; please retry")


def _start_web_session(request: Request, user: dict, remember: bool, *, pending=False) -> str:
    token = secrets.token_urlsafe(32)
    result = _auth_rpc("auth_session_start", {
        "p_token_hash": _session_hash(token), "p_email": user["email"],
        "p_auth_version": user["auth_version"], "p_remember": remember is True,
        "p_pending": pending, "p_provider": user.get("provider", "password"),
        "p_picture": str(user.get("picture") or "")[:2048],
        "p_previous": _browser_session_hashes(request)})
    if result.get("valid") is not True:
        raise HTTPException(status_code=401, detail="please sign in again")
    return token


def _session_identity(token, *, mode="session", previous=()) -> dict:
    hashed = _session_hash(token)
    if not hashed:
        return {}
    result = _auth_rpc("auth_session_identity", {
        "p_token_hash": hashed, "p_mode": mode, "p_previous": list(previous)})
    if result.get("valid") is not True:
        return {}
    user = result.get("user")
    if not isinstance(user, dict) or type(user.get("auth_version")) is not int \
            or user["auth_version"] < 1:
        raise HTTPException(status_code=503, detail="identity service unavailable")
    try:
        email = email_auth.normalize_email(user.get("email"))
    except ValueError:
        raise HTTPException(status_code=503, detail="identity service unavailable") from None
    return {"email": email, "name": user.get("name") or email,
            "picture": user.get("picture") or "", "provider": user.get("provider") or "",
            "auth_version": user["auth_version"]}


class VerifiedIdentityMiddleware:
    """Validate current DB session + identity in one RPC; persist only opaque SID.

    SessionMiddleware must wrap this middleware. Removing user before its send
    hook prevents old cookie-only backends from accepting new sessions on rollback.
    """
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        session = scope.get("session", {})

        async def opaque_send(message):
            if message["type"] == "http.response.start":
                session.pop("user", None)
            await send(message)

        if scope.get("path") != "/api/auth/logout":
            session.pop("user", None)  # Legacy identity cookies never upgrade silently.
            token = session.get(_SESSION_KEY)
            if token:
                try:
                    user = await asyncio.to_thread(_session_identity, token)
                except HTTPException:
                    return await JSONResponse({"detail": "identity service unavailable"}, 503,
                        headers={"Cache-Control": "no-store"})(scope, receive, opaque_send)
                if user:
                    session["user"] = user
                else:
                    session.pop(_SESSION_KEY, None)
        await self.app(scope, receive, opaque_send)


def _json_response(payload: dict):
    # Local import: the top-level fastapi.responses import belongs to another
    # in-flight change; this keeps remember-me self-contained.
    from fastapi.responses import JSONResponse
    return JSONResponse(payload)
app.add_middleware(VerifiedIdentityMiddleware)
app.add_middleware(
    SessionMiddleware,
    secret_key=_session_secret,
    session_cookie="ng_session",
    max_age=30 * 24 * 3600,
    same_site=os.environ.get("SESSION_SAMESITE", "lax"),
    https_only=os.environ.get("SESSION_SECURE", "true").lower() != "false",
)


def _google_userinfo(code: str) -> dict:
    """Exchange code for tokens, return verified Google profile (blocking)."""
    token_body = urlencode({
        "client_id": GOOGLE_CLIENT_ID, "client_secret": GOOGLE_CLIENT_SECRET,
        "code": code, "grant_type": "authorization_code",
        "redirect_uri": GOOGLE_REDIRECT_URI,
    }).encode()
    req = URLRequest("https://oauth2.googleapis.com/token", data=token_body,
                     headers={"Content-Type": "application/x-www-form-urlencoded"})
    with urlopen(req, timeout=15) as r:
        tok = json.load(r)
    access = tok.get("access_token")
    if not access:
        raise RuntimeError("google token exchange failed")
    req2 = URLRequest("https://www.googleapis.com/oauth2/v3/userinfo",
                      headers={"Authorization": f"Bearer {access}"})
    with urlopen(req2, timeout=15) as r2:
        info = json.load(r2)
    if not info.get("email") or info.get("email_verified") is not True or not info.get("sub"):
        raise RuntimeError("google did not return a verified email")
    return info


@app.get("/api/auth/login")
def auth_login(request: Request, next: str = "#/", remember: bool = False):
    if not GOOGLE_CLIENT_ID:
        raise HTTPException(status_code=500, detail="google oauth not configured")
    if not next.startswith("#/"):
        next = "#/"
    state = secrets.token_urlsafe(16)
    request.session["oauth_state"] = state
    request.session["oauth_next"] = next
    request.session["oauth_remember"] = remember
    q = urlencode({
        "client_id": GOOGLE_CLIENT_ID, "redirect_uri": GOOGLE_REDIRECT_URI,
        "response_type": "code", "scope": "openid email profile",
        "state": state, "prompt": "select_account",
    })
    return RedirectResponse("https://accounts.google.com/o/oauth2/v2/auth?" + q,
                            status_code=307)


@app.get("/api/auth/callback")
async def auth_callback(request: Request, code: str = "", state: str = "", error: str = ""):
    if error or not code or not state or state != request.session.get("oauth_state"):
        return RedirectResponse(FRONTEND_URL + "/#/?login=failed", status_code=307)
    request.session.pop("oauth_state", None)
    try:
        info = await asyncio.to_thread(_google_userinfo, code)
    except Exception as e:
        print("[web] oauth exchange failed", file=sys.stderr)
        return RedirectResponse(FRONTEND_URL + "/#/?login=failed", status_code=307)
    try:
        email = email_auth.normalize_email(info["email"])
        # Google warns that email_verified alone is not current inbox proof for
        # third-party addresses. Require Gmail or the matching hosted domain.
        if not (email.endswith("@gmail.com") or info.get("hd") == email.split("@", 1)[1]):
            raise ValueError("email proof required")
        identity = await asyncio.to_thread(_auth_rpc, "auth_google_identity", {
            "p_email": email, "p_subject": str(info["sub"]),
            "p_name": str(info.get("name") or email)[:50]})
        if identity.get("link_required"):
            return RedirectResponse(FRONTEND_URL + "/#/login?email_proof=required", status_code=307)
        user = {**identity, "picture": info.get("picture", ""), "provider": "google"}
        remember = request.session.get("oauth_remember") is True
        sid = await asyncio.to_thread(_start_web_session, request, user, remember, pending=True)
    except Exception:
        return RedirectResponse(FRONTEND_URL + "/#/login?email_proof=required", status_code=307)
    next_url = request.session.pop("oauth_next", "#/")
    if not isinstance(next_url, str) or not next_url.startswith("#/"):
        next_url = "#/"
    # Hand the session to the frontend origin via one-time token, so the
    # session cookie is set first-party (works even when browsers block
    # third-party cookies). /api/auth/finish is proxied by the frontend host.
    token = _login_signer.dumps({"sid": sid, "remember": remember})
    request.session.clear()
    return RedirectResponse(
        FRONTEND_URL + "/api/auth/finish?" + urlencode({"token": token, "next": next_url}),
        status_code=307)


@app.get("/api/auth/finish")
def auth_finish(request: Request, token: str = "", next: str = "#/"):
    try:
        data = _login_signer.loads(token, max_age=300)
    except Exception:
        return RedirectResponse(FRONTEND_URL + "/#/?login=failed", status_code=307)
    sid = data.get("sid") if isinstance(data, dict) else None
    user = _session_identity(sid, mode="finish", previous=_browser_session_hashes(request))
    if not user:
        return RedirectResponse(FRONTEND_URL + "/#/?login=failed", status_code=307)
    if not isinstance(next, str) or not next.startswith("#/"):
        next = "#/"
    request.session.clear()
    request.session.update({_SESSION_KEY: sid, "user": user})
    resp = RedirectResponse(FRONTEND_URL + "/#/" + next.lstrip("#/"), status_code=307)
    _set_remember_choice(resp, sid, data.get("remember") is True)
    return resp


@app.get("/api/auth/me")
def auth_me(request: Request) -> dict:
    user = request.session.get("user") or None
    email = (user or {}).get("email", "")
    # Bundle profile in the same round trip (saves a sequential request).
    profile = {"display_name": "", "default_language": ""}
    if user and _sb is not None:
        try:
            res = _sb.table("profiles").select(
                "display_name,default_language").eq("user_email", email).limit(1).execute()
            rows = res.data or []
            if rows:
                profile["display_name"] = rows[0].get("display_name", "") or ""
                profile["default_language"] = rows[0].get("default_language", "") or ""
        except Exception:
            pass
    return {"user": user, "is_admin": _is_admin(email), "profile": profile,
            "profile_configured": _sb is not None,
            # Presence is a request hint, never proof of identity. Only the
            # restore endpoint validates the signed cookie and live account.
            "restore_available": bool(request.cookies.get(_REMEMBER_COOKIE))}


@app.get("/api/auth/restore")
def auth_restore(request: Request) -> dict:
    """Re-establish the session from a valid remember-me cookie.

    Read-only otherwise: returns {restored:false} when already logged in or
    when there is no usable remember cookie (401 would force the frontend to
    treat it as an error; a quiet false keeps init simple).
    """
    if (request.session.get("user") or {}).get("email"):
        return {"ok": True, "restored": False}
    sid = _read_remember(request)
    user = _session_identity(sid, mode="remember")
    if not user:
        return {"ok": True, "restored": False}
    request.session.update({_SESSION_KEY: sid, "user": user})
    return {"ok": True, "restored": True}


@app.post("/api/auth/logout")
def auth_logout(request: Request) -> dict:
    _revoke_browser_sessions(request)
    request.session.clear()
    resp = _json_response({"ok": True})
    _set_remember_choice(resp, "", False)
    return resp


# ---- password login (no Google needed) ----
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
PW_MIN, PW_MAX = 8, 128
# Two buckets, because the proxy chain hides the real client IP: everything that
# comes through the frontend host arrives with the same source address (Cloudflare
# rewrites it), so an IP-only limit would be SHARED BY EVERY USER on the site —
# 20 logins and the whole class sees "too many attempts". Brute force is therefore
# limited per account (the thing being protected), and the IP bucket stays loose
# as a flood guard for direct callers.
LOGIN_WINDOW_S = 300          # per-IP flood guard window
LOGIN_MAX_TRIES = 200         # per IP per window
ACCOUNT_WINDOW_S = 900        # per-account window
ACCOUNT_MAX_TRIES = 8         # wrong credentials per account per window
_login_tries: Dict[str, list] = {}
_account_tries: Dict[str, list] = {}


def _trusted_proxy_nets() -> list:
    """Immediate PEER addresses/CIDRs whose forwarding headers may be honored.

    Parsed from GRADER_TRUSTED_PROXY_CIDRS (comma-separated IPs or CIDRs,
    v4/v6). These are the network peers that physically deliver requests to
    us — e.g. your edge proxy's egress — never addresses claimed by clients.
    Empty (default) = trust no forwarded headers. Raises ValueError on any
    malformed entry so misconfiguration fails at startup, never silently.
    """
    nets = []
    for part in os.environ.get("GRADER_TRUSTED_PROXY_CIDRS", "").split(","):
        part = part.strip()
        if not part:
            continue
        nets.append(ipaddress.ip_network(part, strict=False))
    return nets


_proxy_config_warned = False


def _client_ip(request: Request) -> str:
    """Effective client IP with peer-first proxy trust.

    - Immediate peer not in GRADER_TRUSTED_PROXY_CIDRS (or no list
      configured): forwarding headers are ignored entirely; the socket
      peer is authoritative. Direct callers cannot spoof their identity.
    - Peer trusted: X-Forwarded-For is resolved first using the
      right-to-left walk (skipping entries that are themselves trusted
      proxies); a usable XFF result always wins and a conflicting
      CF-Connecting-IP can never override or rescue it. CF-Connecting-IP
      is consulted only when XFF is absent, and then only as a valid,
      untrusted address. Forged prepends are left of any selection and
      never used; appends are impossible (the proxy appends after the
      client). A malformed entry stops the walk at the last safe value —
      falling back to the peer, never to the other header, never 500.
    """
    global _proxy_config_warned
    peer = (request.client.host if request.client else "?") or "?"
    try:
        nets = _trusted_proxy_nets()
    except ValueError as e:
        # Misconfiguration must never silently trust everything: peer-only,
        # warn once (boot validation rejects it outright — see below).
        if not _proxy_config_warned:
            _proxy_config_warned = True
            print(f"[web] WARNING: {e}; ignoring forwarded headers",
                  file=sys.stderr)
        return peer[:64]
    if not nets:
        return peer[:64]
    try:
        peer_ip = ipaddress.ip_address(peer)
    except ValueError:
        return peer[:64]  # unparseable peer cannot be a trusted proxy
    if not any(peer_ip in net for net in nets):
        return peer[:64]
    # XFF takes precedence: a usable chain result wins and CF-Connecting-IP
    # is never consulted alongside it (a non-sanitizing proxy may forward a
    # client-forged CF value while appending a correct XFF chain).
    xff = [p.strip() for p in
           request.headers.get("x-forwarded-for", "").split(",")
           if p.strip()][-32:]  # bound attacker-controlled parsing work
    if xff:
        chosen = peer
        for entry in reversed(xff):
            try:
                addr = ipaddress.ip_address(entry)
            except ValueError:
                break  # malformed entry: stop at the last safe value
            if any(addr in net for net in nets):
                continue  # another trusted hop: keep walking left
            chosen = entry
            break
        return chosen[:64]
    # No XFF: never trust CF-Connecting-IP — it is client-forgable.
    # The socket peer is authoritative. (Cloudflare always sends XFF; when
    # XFF is absent the peer IP is the only trustworthy identity.)
    return peer[:64]


def _recent(store: Dict[str, list], key: str, window: int) -> list:
    now = time.monotonic()
    hits = [t for t in store.get(key, []) if now - t < window]
    store[key] = hits
    return hits


def _check_rate_limit(request: Request, email: str = "") -> None:
    ip = _client_ip(request)
    if len(_recent(_login_tries, ip, LOGIN_WINDOW_S)) >= LOGIN_MAX_TRIES:
        raise HTTPException(status_code=429,
                            detail="too many attempts from this network, try later",
                            headers={"Retry-After": "60"})
    _login_tries[ip].append(time.monotonic())
    if len(_login_tries) > 10000:
        # Evict oldest-idle IPs only; never clear the whole map (quota bypass).
        try:
            now = time.monotonic()
            for k in list(_login_tries.keys())[:1000]:
                hits = _login_tries.get(k) or []
                if not hits or now - hits[-1] > LOGIN_WINDOW_S:
                    _login_tries.pop(k, None)
            while len(_login_tries) > 10000:
                _login_tries.pop(next(iter(_login_tries)), None)
        except (StopIteration, RuntimeError):
            pass
    email = (email or "").strip().lower()
    if not email:
        return
    hits = _recent(_account_tries, email, ACCOUNT_WINDOW_S)
    if len(hits) >= ACCOUNT_MAX_TRIES:
        retry = max(1, int(ACCOUNT_WINDOW_S - (time.monotonic() - hits[0])) + 1)
        raise HTTPException(
            status_code=429,
            detail="too many attempts for this account, please wait a few minutes",
            headers={"Retry-After": str(retry)})
    hits.append(time.monotonic())
    if len(_account_tries) > 10000:
        try:
            now = time.monotonic()
            for k in list(_account_tries.keys())[:1000]:
                h = _account_tries.get(k) or []
                if not h or now - h[-1] > ACCOUNT_WINDOW_S:
                    _account_tries.pop(k, None)
            while len(_account_tries) > 10000:
                _account_tries.pop(next(iter(_account_tries)), None)
        except (StopIteration, RuntimeError):
            pass


def _clear_account_attempts(email: str) -> None:
    """A correct password resets the account's counter (typos are not attacks)."""
    _account_tries.pop((email or "").strip().lower(), None)


PBKDF2_ITERS = 210_000


def _hash_password(pw: str) -> str:
    salt = os.urandom(16)
    dk = hashlib.pbkdf2_hmac("sha256", pw.encode(), salt, PBKDF2_ITERS)
    return f"pbkdf2${PBKDF2_ITERS}${salt.hex()}${dk.hex()}"


def _verify_password(pw: str, stored: str) -> bool:
    try:
        _, iters_s, salt_hex, dk_hex = stored.split("$")
        iters = int(iters_s)
        # Cap iterations from DB: a planted huge value would otherwise DoS
        # the worker with a single login attempt.
        if iters < 1000 or iters > 1_000_000:
            return False
        if len(salt_hex) > 128 or len(dk_hex) > 256:
            return False
        dk = hashlib.pbkdf2_hmac("sha256", pw.encode(),
                                 bytes.fromhex(salt_hex), iters)
        return hmac.compare_digest(dk.hex(), dk_hex)
    except Exception:
        return False


def _dummy_verify(pw: str) -> None:
    """Constant-time dummy check so unknown emails cost as much as real ones.

    Prevents timing/user-enumeration via response time differences.
    """
    try:
        hashlib.pbkdf2_hmac("sha256", pw.encode(), b"\x00" * 16, PBKDF2_ITERS)
    except Exception:
        pass


def _require_users_table() -> None:
    if _sb is None:
        raise HTTPException(status_code=404, detail="accounts not configured")


def _auth_rpc(name: str, params: dict) -> dict:
    if _sb is None:
        raise HTTPException(status_code=503, detail="identity service unavailable")
    try:
        result = _sb.rpc(name, params).execute().data
        if not isinstance(result, dict):
            raise ValueError()
        return result
    except Exception:
        raise HTTPException(status_code=503, detail="identity service unavailable") from None


def _auth_email(body: dict) -> str:
    try:
        return email_auth.normalize_email(body.get("email"))
    except ValueError:
        raise HTTPException(status_code=400, detail="bad email") from None


def _email_budget(request: Request, email: str, action: str) -> None:
    result = _auth_rpc("auth_email_budget", {
        "p_email_key": hashlib.sha256(email.encode()).hexdigest(),
        "p_network_key": hashlib.sha256(_client_ip(request).encode()).hexdigest(),
        "p_action": action})
    if result.get("allowed") is not True:
        raise HTTPException(status_code=429, detail="too many attempts, please wait",
                            headers={"Retry-After": str(max(1, int(result.get("retry_after") or 60)))})


def _email_provider_config() -> tuple:
    key = os.environ.get("SUPABASE_AUTH_KEY", "").strip()
    if not key or not _sb_url:
        raise HTTPException(status_code=503, detail="email verification unavailable")
    return _sb_url, key


@app.post("/api/auth/register")
@app.post("/api/auth/email/request")
async def auth_register(request: Request):
    """Begin signup/recovery with the same response; never reserve an app account."""
    _require_users_table()
    body = await _json_object(request)
    email = _auth_email(body)
    if _is_admin(email):
        raise HTTPException(status_code=403, detail="this email is reserved; use Google")
    config = _email_provider_config()
    await asyncio.to_thread(_email_budget, request, email, "request")
    # Replacing this browser's auth context must also invalidate copied cookies.
    await asyncio.to_thread(_revoke_browser_sessions, request)
    # Do not keep a preselected password or issue an authenticated session.
    request.session.clear()
    try:
        await asyncio.to_thread(email_auth.request_code, *config, email)
    except email_auth.Unavailable:
        raise HTTPException(status_code=503, detail="email verification unavailable") from None
    request.session["email_challenge"] = {"email": email, "issued_at": int(time.time())}
    resp = _json_response({"ok": True, "verification_required": True,
                           "message": "check your email for a verification code"})
    resp.status_code = 202
    _set_remember_choice(resp, "", False)
    return resp


@app.post("/api/auth/email/verify")
async def auth_email_verify(request: Request):
    _require_users_table()
    body = await _json_object(request)
    config = _email_provider_config()
    challenge = request.session.get("email_challenge") or {}
    email = _auth_email(body)
    issued = challenge.get("issued_at")
    if challenge.get("email") != email or type(issued) is not int \
            or not 0 <= time.time() - issued <= 600:
        raise HTTPException(status_code=400, detail="request a new verification code")
    password = body.get("password")
    if not isinstance(password, str) or not PW_MIN <= len(password) <= PW_MAX:
        raise HTTPException(status_code=400, detail="password must be 8-128 characters")
    await asyncio.to_thread(_email_budget, request, email, "verify")
    code = body.get("code")
    if not isinstance(code, str) or not re.fullmatch(r"[0-9]{8}", code):
        raise HTTPException(status_code=400, detail="invalid or expired verification code")
    try:
        subject = await asyncio.to_thread(email_auth.verify_code, *config, email, code)
    except email_auth.InvalidCode:
        raise HTTPException(status_code=400, detail="invalid or expired verification code") from None
    except email_auth.Unavailable:
        raise HTTPException(status_code=503, detail="email verification unavailable") from None
    # Consumed OTP + failed DB write requires a fresh code; never trust a replay.
    request.session.clear()
    pw_hash = await asyncio.to_thread(_hash_password, password)
    name = str(body.get("name") or "").strip()[:50] or email.split("@")[0]
    user = await asyncio.to_thread(_auth_rpc, "auth_email_complete", {
        "p_email": email, "p_subject": subject, "p_password_hash": pw_hash, "p_name": name})
    user = {**user, "provider": "password", "picture": ""}
    sid = await asyncio.to_thread(_start_web_session, request, user, body.get("remember") is True)
    request.session.update({_SESSION_KEY: sid, "user": user})
    resp = _json_response({"ok": True, "user": user})
    _set_remember_choice(resp, sid, body.get("remember") is True)
    return resp


@app.post("/api/auth/password")
async def auth_password(request: Request):
    _require_users_table()
    body = await _json_object(request)
    email = _auth_email(body)
    password = body.get("password")
    if not isinstance(password, str) or not PW_MIN <= len(password) <= PW_MAX:
        raise HTTPException(status_code=401, detail="wrong email or password")
    _check_rate_limit(request, email)
    try:
        res = await asyncio.to_thread(lambda: _sb.table("users").select(
            "email,name,password_hash,email_verified_at,auth_version").eq(
            "email", email).limit(1).execute())
    except Exception:
        raise HTTPException(status_code=503, detail="identity service unavailable") from None
    rows = res.data or []
    account = rows[0] if rows else {}
    if not account:
        await asyncio.to_thread(_dummy_verify, password)
        raise HTTPException(status_code=401, detail="wrong email or password")
    stored = account.get("password_hash", "")
    if not stored:
        await asyncio.to_thread(_dummy_verify, password)
        raise HTTPException(status_code=401, detail="wrong email or password")
    ok = await asyncio.to_thread(_verify_password, password, stored)
    if not ok:
        raise HTTPException(status_code=401, detail="wrong email or password")
    if not account.get("email_verified_at") or not account.get("auth_version"):
        raise HTTPException(status_code=403, detail="verify your email and choose a new password",
                            headers={"X-Email-Verification-Required": "true"})
    _clear_account_attempts(email)
    user = {"email": account["email"], "name": account.get("name") or email,
            "picture": "", "provider": "password", "auth_version": account["auth_version"]}
    sid = await asyncio.to_thread(_start_web_session, request, user, body.get("remember") is True)
    request.session.clear()
    request.session.update({_SESSION_KEY: sid, "user": user})
    resp = _json_response({"ok": True, "user": user})
    _set_remember_choice(resp, sid, body.get("remember") is True)
    return resp


def _profile_display_name(email: str, fallback: str) -> str:
    """Display name from profiles table, else Google name (blocking).

    Cached briefly because submissions are the hot path and a display name is
    not an authorization decision. Profile updates clear the entry.
    """
    now = time.monotonic()
    if email:
        hit = _PROFILE_NAME_CACHE.get(email)
        if hit is not None and now - hit[1] < _PROFILE_NAME_TTL:
            return hit[0]
    name = (fallback or "")[:50]
    if _sb is not None and email:
        try:
            res = _sb.table("profiles").select("display_name").eq(
                "user_email", email).limit(1).execute()
            rows = res.data or []
            if rows and (rows[0].get("display_name") or "").strip():
                name = rows[0]["display_name"].strip()[:50]
        except Exception as e:
            print(f"[web] profile lookup failed: {e}", file=sys.stderr)
    if email:
        if len(_PROFILE_NAME_CACHE) >= _PROFILE_NAME_MAX:
            _PROFILE_NAME_CACHE.pop(next(iter(_PROFILE_NAME_CACHE)), None)
        _PROFILE_NAME_CACHE[email] = (name, now)
    return name


@app.get("/api/profile")
def get_profile(request: Request) -> dict:
    user = request.session.get("user") or {}
    if not user.get("email"):
        raise HTTPException(status_code=401, detail="login required")
    base = {"email": user["email"], "name": user.get("name", ""),
            "display_name": "", "default_language": ""}
    if _sb is None:
        return {"configured": False, "profile": base}
    try:
        res = _sb.table("profiles").select("*").eq(
            "user_email", user["email"]).limit(1).execute()
        rows = res.data or []
        if rows:
            base["display_name"] = rows[0].get("display_name", "") or ""
            base["default_language"] = rows[0].get("default_language", "") or ""
        return {"configured": True, "profile": base}
    except Exception as e:
        raise _db_err(e)


@app.put("/api/profile")
async def update_profile(request: Request) -> dict:
    user = request.session.get("user") or {}
    if not user.get("email"):
        raise HTTPException(status_code=401, detail="login required")
    if _sb is None:
        raise HTTPException(status_code=404, detail="profile not configured")
    body = await _json_object(request)
    display_name = str(body.get("display_name", "")).strip()[:50]
    default_language = str(body.get("default_language", "")).strip()
    if default_language and default_language not in LANGS:
        raise HTTPException(status_code=400, detail="bad default_language")
    row = {"user_email": user["email"], "display_name": display_name,
           "default_language": default_language}
    try:
        await asyncio.to_thread(lambda: _sb.table("profiles").upsert(row, on_conflict="user_email").execute())
    except Exception as e:
        raise _db_err(e)
    _PROFILE_NAME_CACHE.pop(user["email"], None)
    return {"ok": True, "profile": {"email": user["email"], **row}}

# Preload problems at startup (fail fast on broken tests, skip with warning).
# Source is explicit via GRADER_PROBLEM_SOURCE (local|supabase); supabase mode
# never falls back to local files. Supabase rows are cached with TTL (see
# _ensure_problems_fresh) versioned by tests_hash — hidden test bytes stay
# cached, only changed problems re-download.
PROBLEMS: Dict[str, grader.Problem] = {}


def _load_local_problems() -> Dict[str, grader.Problem]:
    out: Dict[str, grader.Problem] = {}
    if grader.PROBLEMS_DIR.is_dir():
        for _pdir in sorted(grader.PROBLEMS_DIR.iterdir()):
            if not _pdir.is_dir():
                continue
            try:
                _p = grader.load_problem(_pdir.name)
                out[_p.pid] = _p
            except SystemExit as e:
                print(f"[web] skip problem {_pdir.name}: {e}", file=sys.stderr)
    return out


def _rebuild_problem_indexes() -> None:
    PROBLEMS_BY_NO.clear()
    for _pid, _p in sorted(PROBLEMS.items()):
        if _p.no > 0:
            if _p.no in PROBLEMS_BY_NO:
                print(f"[web] WARNING: problems {PROBLEMS_BY_NO[_p.no]} and {_pid} share "
                      f"number {_p.no}; the first one wins "
                      "(fix with tools/number_problems.py)", file=sys.stderr)
            else:
                PROBLEMS_BY_NO[_p.no] = _pid
    # tests_hash คำนวณแบบ lazy เมื่อเข้าถึงจริง (ดู _tests_hash())
    # ในโหมด supabase ปัญหาที่โหลดตอน boot ยังมีแค่ตัวอย่างสาธารณะ
    # จึงคำนวณ hash ตอนนี้ไม่ได้ — ต้องรอโหลดเต็มก่อน
    if PROBLEM_SOURCE != "supabase":
        _TESTS_HASH_CACHE.clear()
        for _pid in PROBLEMS:
            _TESTS_HASH_CACHE[_pid] = grader.tests_hash(PROBLEMS[_pid])


# numeric id -> pid (a problem may also be addressed as #19 in URLs and the CLI)
PROBLEMS_BY_NO: Dict[int, str] = {}
# tests_hash คำนวณแบบ lazy เมื่อเข้าถึงจริง (ดู _tests_hash())
# ในโหมด supabase เติมจากแถวเมตาดาต้าตอน boot (ไม่ต้องดาวน์โหลดไฟล์เทสต์)
_TESTS_HASH_CACHE: Dict[str, str] = {}
# Supabase row extras: {slug: {"tests": total count, "has_pdf": bool}}.
# Lets list/detail/version/contest serve correct counts with zero Storage
# downloads (one SELECT per boot/refresh, not per request).
_PROBLEM_EXTRA: Dict[str, dict] = {}
_problems_refresh_at: float = time.monotonic()
_problem_refresh_lock = threading.Lock()

# Display names are not security-sensitive and change rarely; one query per
# submit is avoidable. Bounded so a long-lived process cannot grow without
# limit. Cleared on profile update.
_PROFILE_NAME_CACHE: Dict[str, tuple] = {}
_PROFILE_NAME_TTL = 60.0
_PROFILE_NAME_MAX = 2048

if PROBLEM_SOURCE == "supabase":
    if _sb is None:
        print("[web] GRADER_PROBLEM_SOURCE=supabase but SUPABASE_URL/KEY missing: "
              "problem store unavailable (no local fallback)", file=sys.stderr)
    else:
        try:
            # โหลดเฉพาะเมตาดาต้า (ตัวอย่างสาธารณะ) ตอน boot — ไม่ดาวน์โหลด
            # เทสต์ลบทั้งหมด (~พันไฟล์) เพื่อให้เซิร์ฟเวอร์เริ่มต้นได้ไว
            # เทสต์ลับจะโหลดแบบ lazy ตอนมีการส่งงานจริง (ดู _problem_or_404 / _run_job)
            _snap_meta, _snap_hashes, _snap_extra = problem_store.load_snapshot(client=_sb)
            PROBLEMS.update(_snap_meta)
            # เติม tests_hash จากแถวเมตาดาต้าโดยตรง (ไม่ต้องคำนวณจากไฟล์เทสต์)
            _TESTS_HASH_CACHE.clear()
            _TESTS_HASH_CACHE.update(_snap_hashes)
            _PROBLEM_EXTRA.clear()
            _PROBLEM_EXTRA.update(_snap_extra)
        except problem_store.ProblemUnavailable as e:
            print(f"[web] supabase problem preload failed: {e} "
                  "(no local fallback)", file=sys.stderr)
        except Exception as e:
            print(f"[web] supabase problem preload failed: {e} "
                  "(no local fallback)", file=sys.stderr)
else:
    if PROBLEM_SOURCE != "local":
        print(f"[web] warning: unknown GRADER_PROBLEM_SOURCE={PROBLEM_SOURCE!r}, "
              "using local", file=sys.stderr)
    PROBLEMS.update(_load_local_problems())


_problems_refresh_at: float = time.monotonic()
_rebuild_problem_indexes()


def _tests_hash(pid: str) -> str:
    """Cached grader.tests_hash (same value, no per-request SHA256 over tests).

    ในโหมด supabase ปัญหาอาจยังโหลดแค่ตัวอย่างสาธารณะ (metadata-only)
    ตอน boot — ถ้าเจอปัญหาที่ยังโหลดไม่ครบ ให้โหลดเต็มก่อนคำนวณ hash
    เพื่อไม่ให้ hash ที่เก็บไว้ตรงกับเทสต์ชุดที่ไม่ครบ
    """
    hit = _TESTS_HASH_CACHE.get(pid)
    if hit is not None:
        return hit
    prob = PROBLEMS.get(pid)
    if prob is None:
        return ""
    if PROBLEM_SOURCE == "supabase" and len(prob.tests) == len(prob.samples):
        # Metadata-only: tests มีแต่ตัวอย่างสาธารณะ — ต้องโหลดเต็มก่อน
        try:
            full_prob = problem_store.get_problem(pid, source="supabase", client=_sb)
            PROBLEMS[pid] = full_prob
            if full_prob.no > 0 and full_prob.no not in PROBLEMS_BY_NO:
                PROBLEMS_BY_NO[full_prob.no] = full_prob.pid
            prob = full_prob
        except Exception:
            return ""
    hashed = grader.tests_hash(prob)
    _TESTS_HASH_CACHE[pid] = hashed
    return hashed


def _apply_problem_snapshot(meta: dict, hashmap: dict, extra: dict) -> None:
    """Install a refreshed catalog without blanking unchanged statements.

    The periodic refresh selects the lean metadata columns (no statement_md).
    Carrying the old description forward for problems whose version did not
    change keeps detail responses stable; changed problems fetch it lazily.
    Updating the dicts in place avoids an empty-catalog window for concurrent
    request threads.
    """
    for slug, prob in meta.items():
        old = PROBLEMS.get(slug)
        if old is None or prob.description:
            continue
        old_hash = _TESTS_HASH_CACHE.get(slug)
        new_hash = hashmap.get(slug)
        old_updated = (_PROBLEM_EXTRA.get(slug) or {}).get("updated_at")
        new_updated = (extra.get(slug) or {}).get("updated_at")
        if old_hash == new_hash and str(old_updated or "") == str(new_updated or ""):
            prob.description = old.description
    for slug in list(PROBLEMS):
        if slug not in meta:
            PROBLEMS.pop(slug, None)
    PROBLEMS.update(meta)
    _TESTS_HASH_CACHE.clear()
    _TESTS_HASH_CACHE.update(hashmap)
    _PROBLEM_EXTRA.clear()
    _PROBLEM_EXTRA.update(extra)
    _rebuild_problem_indexes()


def _refresh_problems_background() -> None:
    """Refresh the catalog off the request path; stale data stays readable."""
    global _problems_refresh_at
    try:
        meta, hashmap, extra = problem_store.load_snapshot(client=_sb, lean=True)
        dropped = problem_store.invalidate_stale(hashmap, extra)
        if dropped:
            print(f"[web] problem versions changed, dropped cache: {sorted(dropped)}",
                  file=sys.stderr)
        _apply_problem_snapshot(meta, hashmap, extra)
    except Exception as e:
        print(f"[web] problem refresh failed, keeping {len(PROBLEMS)} cached: {e}",
              file=sys.stderr)
    finally:
        # Set the timestamp even on failure: do not hammer a failing database.
        _problems_refresh_at = time.monotonic()
        _problem_refresh_lock.release()


def _ensure_problems_fresh() -> None:
    """TTL refresh for supabase mode (metadata-only, versioned by tests_hash).

    Local mode: PROBLEMS are boot-loaded from disk (restart after edits).
    Supabase mode: re-list ROWS only (no Storage downloads); drop cached full
    problems whose hash changed so the next access lazy-reloads them. A refresh
    costs one small SELECT, never ~1700 file downloads — safe on the tiny
    prod box. Failures keep the stale cache and log; callers surface an empty
    store as an explicit 500, never silent fallback.

    The refresh runs in a daemon thread so the request that notices the TTL
    expiry is served from the current snapshot instead of blocking on a
    Supabase round trip. Only one refresh can be in flight.
    """
    global _problems_refresh_at
    if PROBLEM_SOURCE != "supabase":
        return
    ttl = problem_store.cache_ttl_s()
    if ttl > 0 and time.monotonic() - _problems_refresh_at < ttl:
        return
    if _sb is None:
        return
    if not _problem_refresh_lock.acquire(blocking=False):
        return
    # Claim the TTL before the thread starts so another request does not spawn
    # a duplicate refresh while this one is connecting to Supabase.
    _problems_refresh_at = time.monotonic()
    try:
        threading.Thread(target=_refresh_problems_background,
                         name="problem-refresh", daemon=True).start()
    except Exception as e:
        _problem_refresh_lock.release()
        print(f"[web] cannot start problem refresh: {e}", file=sys.stderr)


def _problem_total(pid: str, p: grader.Problem) -> int:
    """True test count (supabase rows carry it; local Problems know their tests)."""
    if PROBLEM_SOURCE == "supabase":
        hit = (_PROBLEM_EXTRA.get(pid) or {}).get("tests") or 0
        if hit:
            return int(hit)
    return len(p.tests)


def _problem_description(p: grader.Problem) -> str:
    """Statement markdown, fetched lazily when a lean refresh omitted it."""
    if p.description or PROBLEM_SOURCE != "supabase" or _sb is None:
        return p.description
    try:
        p.description = problem_store.load_statement_md(p.pid, client=_sb)
    except Exception as e:
        print(f"[web] statement unavailable for {p.pid}: {e}", file=sys.stderr)
    return p.description


def _browse_cache_control(pid: str, email: str, ttl: int) -> str:
    """Private browser cache; contest-hidden/admin responses get a short TTL."""
    if _is_admin(email) or pid in _hidden_pids():
        ttl = min(ttl, 30)
    return f"private, max-age={ttl}"


def _meta_or_404(pid: str) -> grader.Problem:
    """Metadata-only resolve: never downloads hidden tests (browsing path).

    Judging paths must keep using _problem_or_404 (full tests required).
    """
    slug = _resolve_pid(pid)
    p = PROBLEMS.get(slug)
    if p is not None:
        return p
    if PROBLEM_SOURCE != "supabase":
        raise HTTPException(status_code=404, detail=f"unknown problem: {pid}")
    if _sb is None:
        raise HTTPException(status_code=500, detail="problem store unavailable")
    try:
        meta, _row = problem_store.load_meta(slug, client=_sb)
    except problem_store.ProblemNotFound:
        raise HTTPException(status_code=404, detail=f"unknown problem: {pid}")
    except problem_store.ProblemUnavailable as e:
        print(f"[web] problem meta {slug} unavailable: {e}", file=sys.stderr)
        raise HTTPException(status_code=500, detail="problem store unavailable")
    PROBLEMS[meta.pid] = meta
    if meta.no > 0 and meta.no not in PROBLEMS_BY_NO:
        PROBLEMS_BY_NO[meta.no] = meta.pid
    return meta


def _resolve_pid(token: str) -> str:
    """Accept either the slug (`hanoi`) or its number (`19`) — students share
    links like /#/task/19 and the CLI shows numbers."""
    token = (token or "").strip()
    if token in PROBLEMS:
        return token
    if token.isdigit():
        pid = PROBLEMS_BY_NO.get(int(token), "")
        if pid:
            return pid
    return token


def _problem_or_404(pid: str) -> grader.Problem:
    slug = _resolve_pid(pid)
    p = PROBLEMS.get(slug)
    if p is not None:
        if PROBLEM_SOURCE == "supabase" and len(p.tests) <= len(p.samples):
            # โหลดเมตาดาต้าแค่ตัวอย่างตอน boot — ตอนเข้าถึงจริงต้องโหลด
            # เทสต์ลับเต็มก่อนตัดสิน (fail closed ถ้าโหลดไม่ได้)
            try:
                full_p = problem_store.get_problem(slug, source="supabase", client=_sb)
                PROBLEMS[slug] = full_p
                p = full_p
            except problem_store.ProblemNotFound:
                raise HTTPException(status_code=404, detail=f"unknown problem: {pid}")
            except problem_store.ProblemUnavailable as e:
                print(f"[web] problem {slug} unavailable: {e}", file=sys.stderr)
                raise HTTPException(status_code=500, detail="problem store unavailable")
        return p
    if PROBLEM_SOURCE == "supabase":
        # Cache miss: fetch exactly this problem (row + Storage, version-checked).
        # Missing assets / storage / DB errors are explicit 500s, never an
        # empty suite and never Accepted without judging.
        if _sb is None:
            raise HTTPException(status_code=500, detail="problem store unavailable")
        try:
            prob = problem_store.get_problem(slug, source="supabase", client=_sb)
        except problem_store.ProblemNotFound:
            raise HTTPException(status_code=404, detail=f"unknown problem: {pid}")
        except problem_store.ProblemUnavailable as e:
            print(f"[web] problem {slug} unavailable: {e}", file=sys.stderr)
            raise HTTPException(status_code=500, detail="problem store unavailable")
        PROBLEMS[prob.pid] = prob
        if prob.no > 0 and prob.no not in PROBLEMS_BY_NO:
            PROBLEMS_BY_NO[prob.no] = prob.pid
        _TESTS_HASH_CACHE[prob.pid] = grader.tests_hash(prob)
        return prob
        # (numeric miss falls through to 404 below)
    if PROBLEM_SOURCE == "supabase" and not PROBLEMS:
        raise HTTPException(status_code=500, detail="problem store unavailable")
    raise HTTPException(status_code=404, detail=f"unknown problem: {pid}")


# ---- problem visibility (contest-only sets) ----
# A problem can be kept out of the public Task list in three ways:
#   * config.json -> "hidden": true                (always private to contests/admin)
#   * Supabase problems.is_published = false        (admin-only, no contest bypass)
#   * listed in a contest with hide_from_tasks      (set from the admin form)
# Hidden problems stay reachable through their contest page (that is how students
# open them), through the admin panel, and are gated everywhere else.
_contest_pid_cache: Dict[str, object] = {"at": 0.0, "all": set(), "hide": set()}


def _refresh_contest_cache() -> None:
    """Refetch contest rows + derived visibility sets (single SELECT batch).

    Shares _contest_pid_cache so one timestamp busts everything (admin
    upsert/delete set ["at"]=0.0) and existing test resets keep working.
    Raises when the contests table is missing — policy callers degrade.
    """
    all_pids: set = set()
    hidden: set = set()
    rows: List[dict] = []
    if _sb is not None:
        rows = _all_contest_rows()
        for row in rows:
            pids = {_resolve_pid(str(x)) for x in (row.get("problems") or [])}
            all_pids |= pids
            if row.get("hide_from_tasks", True):
                hidden |= pids
    _contest_pid_cache.update({"at": time.monotonic(),
                               "all": all_pids, "hide": hidden, "rows": rows})


def _cached_contest_rows() -> List[dict]:
    """Contest rows with 20s TTL (same staleness the visibility policy
    already accepts). Raises on DB failure — callers degrade as before."""
    now = time.monotonic()
    if not (_contest_pid_cache.get("at")
            and now - float(_contest_pid_cache["at"]) < 20
            and isinstance(_contest_pid_cache.get("rows"), list)):
        _refresh_contest_cache()
    return _contest_pid_cache.get("rows") or []


def _contest_problem_sets() -> tuple:
    """(pids listed in any contest, pids a contest hides from Tasks)."""
    _contest_policy_rows()
    return (_contest_pid_cache.get("all") or set(),
            _contest_pid_cache.get("hide") or set())


def _contest_policy_rows(*, fresh: bool = False) -> List[dict]:
    """Transient failures cannot open policy; disclosure decisions bypass TTL."""
    try:
        if fresh:
            return _all_contest_rows() if _sb is not None else []
        return _cached_contest_rows()
    except Exception as exc:
        # A genuinely absent legacy table contains no contest policy. Other
        # failures (including partial pagination) must refuse access.
        if getattr(exc, "code", "") in ("42P01", "PGRST205") or \
                str(exc) == 'relation "public.contests" does not exist':
            return []
        print("[web] contest policy unavailable", file=sys.stderr)
        raise HTTPException(status_code=503, detail="contest policy unavailable")


def _hidden_pids() -> set:
    listed, hidden_by_contest = _contest_problem_sets()
    return hidden_by_contest | {
        pid for pid, p in PROBLEMS.items()
        if p.hidden or not getattr(p, "is_published", True)
    }


def _visible_to(pid: str, email: str) -> bool:
    """Public Task entry, or reachable because the problem belongs to a contest."""
    pid = _resolve_pid(pid)
    prob = PROBLEMS.get(pid)
    if prob is None:
        return False
    if _is_admin(email):
        return True
    # Unpublished problems are admin-only (no contest bypass).
    if not getattr(prob, "is_published", True):
        return False
    if pid not in _hidden_pids():
        return True
    # contest-only problem: readable once a contest listing it has STARTED
    for row in _contests_listing(pid):
        if _contest_started(row):
            return True
    return False


def _contests_listing(pid: str) -> List[dict]:
    """Contests that include this problem (empty when contests are unavailable)."""
    if _sb is None:
        return []
    try:
        return [row for row in _cached_contest_rows()
                if pid in {_resolve_pid(str(x)) for x in (row.get("problems") or [])}]
    except Exception as e:
        print(f"[web] contest lookup for {pid} failed: {e}", file=sys.stderr)
        return []


def _visible_problem_or_404(pid: str, email: str) -> grader.Problem:
    p = _problem_or_404(pid)
    if not _visible_to(pid, email):
        raise HTTPException(status_code=404, detail=f"unknown problem: {pid}")
    return p


def _host_limits() -> dict:
    return {"cpu_quota": CPU_QUOTA, "host_memory_mb": RAM_MB,
            "judge_workers": JUDGE_WORKERS, "judge_jobs": JUDGE_JOBS,
            "memory_cap_mb": MEMORY_CAP_MB, "queue_max": QUEUE_MAX,
            "overall_timeout_s": OVERALL_TIMEOUT,
            "sample_interval_ms": round(grader.SAMPLE_INTERVAL * 1000, 2),
            "compile_timeout_s": grader.COMPILE_TIMEOUT,
            "queue_mode": QUEUE_MODE, "role": GRADER_ROLE,
            "problem_source": PROBLEM_SOURCE,
        "problem_file_storage": problem_store.backend_problem_files.backend(),
            "problems_count": len(PROBLEMS),
            "reserve_memory_mb": _capacity["reserve_mb"],
            "memory_per_job_mb": _capacity["per_job_mb"],
            # memory policy: peak-RSS threshold = declared x allowance, enforced
            # RLIMIT_AS ceiling = threshold + virtual headroom (grader.py)
            "mem_allowance": grader.LANG_MEM_MULTIPLIER,
            "mem_headroom_mb": grader.LANG_MEM_HEADROOM_MB}


def _execution_settings() -> judge_execution.Settings:
    return judge_execution.Settings(MEMORY_CAP_MB, JUDGE_JOBS, CPU_QUOTA, RAM_MB)


def _effective(prob: Optional[grader.Problem], time_limit: float,
               memory_mb: int) -> tuple[float, int, dict]:
    return judge_execution._effective(prob, time_limit, memory_mb, _execution_settings())


def run_submission(prob: grader.Problem, ext: str, source: str,
                   reveal: str = REVEAL_SAMPLES) -> dict:
    return judge_execution.run_submission(prob, ext, source, reveal,
                                           settings=_execution_settings())


def _run_playground(prob: Optional[grader.Problem], ext: str, source: str,
                    stdin_data: str, time_limit: float, memory_mb: int) -> dict:
    return judge_execution._run_playground(prob, ext, source, stdin_data,
                                            time_limit, memory_mb,
                                            settings=_execution_settings())


@app.get("/api/health")
def health(request: Request, response: Response) -> dict:
    # Per-user body (admin sees hidden ids): private cache only, never shared.
    response.headers["Cache-Control"] = "private, max-age=20"
    response.headers["Vary"] = "Cookie, Authorization"
    _ensure_problems_fresh()
    email = (request.session.get("user") or {}).get("email", "")
    if _is_admin(email):
        ids = sorted(PROBLEMS.keys())
    else:
        hidden = _hidden_pids()
        ids = sorted(pid for pid in PROBLEMS if pid not in hidden)
    return {"ok": True, "problems": ids, "count": len(ids)}


@app.get("/api/languages")
def languages(response: Response) -> dict:
    # Constant payload; let browsers and the edge keep it instead of paying a
    # backend round trip for every page that asks for the language list.
    response.headers["Cache-Control"] = "public, max-age=3600"
    return {"languages": [
        {"id": lid, "name": info["name"], "ext": info["ext"]}
        for lid, info in LANGS.items()
    ]}


@app.get("/api/problems")
def list_problems(request: Request, response: Response, sort: str = "") -> dict:
    # Per-user body (admin sees hidden rows): private cache only, never shared.
    response.headers["Cache-Control"] = "private, max-age=20"
    response.headers["Vary"] = "Cookie, Authorization"
    email = (request.session.get("user") or {}).get("email", "")
    return {"problems": _listed_problems(email, sort)}


def _listed_problems(email: str, sort: str = "") -> list:
    _ensure_problems_fresh()
    order = {"easy": 0, "medium": 1, "hard": 2}
    sort_by_difficulty = (sort or "").strip().lower() == "difficulty"
    admin = _is_admin(email)
    hidden = _hidden_pids()
    listed, contest_hidden = _contest_problem_sets()
    ranked = grader.sort_problems(PROBLEMS.values()) if not sort_by_difficulty else sorted(
        PROBLEMS.values(), key=lambda p: (order.get((p.difficulty or "").lower(), 99),
                                           p.no if p.no > 0 else 0, p.pid))
    out = []
    for p in ranked:
        is_hidden = p.pid in hidden
        if is_hidden and not admin:
            continue  # contest-only problem: reachable from its contest page, not Tasks
        item = {
            "id": p.pid, "no": p.no, "title": p.title, "time_limit": p.time_limit,
            "memory_limit": p.memory_limit, "checker": p.checker,
            "difficulty": p.difficulty, "tags": p.tags,
            "tests": _problem_total(p.pid, p), "samples": len(p.samples),
            "has_pdf": bool((_PROBLEM_EXTRA.get(p.pid) or {}).get("has_pdf", False))
            if PROBLEM_SOURCE == "supabase"
            else (grader.PROBLEMS_DIR / p.pid / "statement.pdf").is_file(),
        }
        if admin:
            item["hidden"] = is_hidden
            item["contest_only"] = p.pid in listed
            item["config_hidden"] = p.hidden
            item["contest_hidden"] = p.pid in contest_hidden
        out.append(item)
    return out


@app.get("/api/home")
async def home_preview(response: Response) -> dict:
    """Only the public landing preview; never statements, samples or history."""
    response.headers["Cache-Control"] = "private, no-store"
    rows = await asyncio.to_thread(_listed_problems, "")
    allowed = ("id", "no", "title", "difficulty", "tags", "time_limit", "memory_limit")
    preview = []
    for difficulty in ("easy", "medium", "hard"):
        preview.extend({key: row[key] for key in allowed}
                       for row in [r for r in rows if r["difficulty"] == difficulty][:3])
    public_stats = await stats()
    totals = {"subs": 0, "accepted": 0}
    for row in public_stats.get("stats", {}).values():
        for key in totals:
            totals[key] += row.get(key) or 0
    return {"problems": preview, "problems_count": len(rows),
            "summary": totals, "configured": bool(public_stats.get("configured"))}


@app.get("/api/problems/{pid}")
def problem_detail(request: Request, pid: str, response: Response) -> dict:
    _ensure_problems_fresh()
    email = (request.session.get("user") or {}).get("email", "")
    if PROBLEM_SOURCE == "supabase":
        # Browsing path: metadata + row extras only, zero Storage downloads.
        # Hidden bytes load lazily on submit (judge worker, polled).
        meta = _meta_or_404(pid)
        if not _visible_to(meta.pid, email):
            raise HTTPException(status_code=404, detail=f"unknown problem: {pid}")
        total = _problem_total(meta.pid, meta)
        samples = list(meta.samples)
        has_pdf = bool((_PROBLEM_EXTRA.get(meta.pid) or {}).get("has_pdf", False))
        listed, contest_hidden = _contest_problem_sets()
        body = {"id": meta.pid, "no": meta.no, "title": meta.title,
                "time_limit": meta.time_limit,
                "memory_limit": meta.memory_limit, "checker": meta.checker,
                "difficulty": meta.difficulty, "tags": meta.tags,
                "tests": total, "statement": _problem_description(meta),
                "samples": samples,
                "tests_hash": _tests_hash(meta.pid),
                "has_pdf": has_pdf}
        if _is_admin(email):
            body["hidden"] = meta.pid in _hidden_pids()
            body["contest_only"] = meta.pid in listed
            body["config_hidden"] = meta.hidden
            body["contest_hidden"] = meta.pid in contest_hidden
        response.headers["Cache-Control"] = _browse_cache_control(meta.pid, email, 60)
        response.headers["Vary"] = "Cookie"
        return body
    p = _visible_problem_or_404(pid, email)
    listed, contest_hidden = _contest_problem_sets()
    has_pdf = (grader.PROBLEMS_DIR / p.pid / "statement.pdf").is_file()
    body = {"id": p.pid, "no": p.no, "title": p.title, "time_limit": p.time_limit,
            "memory_limit": p.memory_limit, "checker": p.checker,
            "difficulty": p.difficulty, "tags": p.tags,
            "tests": len(p.tests), "statement": p.description,
            "samples": list(p.samples),
            "tests_hash": _tests_hash(p.pid),
            "has_pdf": has_pdf}
    if _is_admin(email):
        body["hidden"] = p.pid in _hidden_pids()
        body["contest_only"] = p.pid in listed
        body["config_hidden"] = p.hidden
        body["contest_hidden"] = p.pid in contest_hidden
    response.headers["Cache-Control"] = _browse_cache_control(p.pid, email, 60)
    response.headers["Vary"] = "Cookie"
    return body


@app.get("/api/problems/{pid}/sample/{name}")
def problem_sample(request: Request, pid: str, name: str, response: Response) -> dict:
    """Public sample test data (statement examples) — hidden tests are never served."""
    _ensure_problems_fresh()
    email = (request.session.get("user") or {}).get("email", "")
    if PROBLEM_SOURCE == "supabase":
        # Sample bytes live in the row itself: one SELECT, zero Storage downloads.
        meta = _meta_or_404(pid)
        if not _visible_to(meta.pid, email):
            raise HTTPException(status_code=404, detail=f"unknown problem: {pid}")
        if name not in set(meta.samples):
            raise HTTPException(status_code=404,
                                detail="not a public sample (hidden tests are not served)")
        # Live contest with show_samples=false reveals nothing, even via samples.
        if not _is_admin(email) and meta.pid in _live_hidden_pids():
            raise HTTPException(status_code=404,
                                detail="not a public sample (hidden tests are not served)")
        if problem_store.backend_problem_files.backend() == "local":
            try:
                inp = problem_store._download(_sb, problem_store.problem_bucket(),
                                              f"{meta.pid}/tests/{name}.in").decode("utf-8")
                out = problem_store._download(_sb, problem_store.problem_bucket(),
                                              f"{meta.pid}/tests/{name}.out").decode("utf-8")
            except (problem_store.ProblemUnavailable, UnicodeDecodeError):
                raise HTTPException(status_code=503, detail="public sample file unavailable")
            response.headers["Cache-Control"] = _browse_cache_control(meta.pid, email, 300)
            response.headers["Vary"] = "Cookie"
            return {"problem": meta.pid, "test": name, "input": inp, "output": out}
        try:
            row = problem_store.fetch_row(meta.pid, client=_sb)
        except problem_store.ProblemNotFound:
            raise HTTPException(status_code=404, detail="sample not found")
        except problem_store.ProblemUnavailable as e:
            print(f"[web] sample {meta.pid}/{name} unavailable: {e}", file=sys.stderr)
            raise HTTPException(status_code=500, detail="problem store unavailable")
        for entry in (row.get("samples_data") or []):
            if str(entry.get("name")) == name:
                response.headers["Cache-Control"] = _browse_cache_control(meta.pid, email, 300)
                response.headers["Vary"] = "Cookie"
                return {"problem": meta.pid, "test": name,
                        "input": str(entry.get("input", "")),
                        "output": str(entry.get("expected", ""))}
        raise HTTPException(status_code=404, detail="sample not found")
    p = _visible_problem_or_404(pid, email)
    if name not in grader.sample_names(p):
        raise HTTPException(status_code=404,
                            detail="not a public sample (hidden tests are not served)")
    # Live contest with show_samples=false reveals nothing, even via samples.
    if not _is_admin(email) and p.pid in _live_hidden_pids():
        raise HTTPException(status_code=404,
                            detail="not a public sample (hidden tests are not served)")
    for tc in p.tests:
        if tc.name == name:
            response.headers["Cache-Control"] = _browse_cache_control(p.pid, email, 300)
            response.headers["Vary"] = "Cookie"
            return {"problem": p.pid, "test": tc.name,
                    "input": tc.input, "output": tc.expected}
    raise HTTPException(status_code=404, detail="sample not found")


@app.get("/api/problems/{pid}/pdf")
def problem_pdf(request: Request, pid: str):
    _ensure_problems_fresh()
    email = (request.session.get("user") or {}).get("email", "")
    # Browsing never needs hidden tests. Authorize the resolved slug before
    # reading Storage, including conditional requests carrying an old ETag.
    p = _meta_or_404(pid)
    if not _visible_to(p.pid, email):
        raise HTTPException(status_code=404, detail=f"unknown problem: {pid}")
    if PROBLEM_SOURCE == "supabase":
        # Private bucket, server-side credentials only: stream bytes, never a
        # public URL. Client-supplied paths are never trusted (resolved via row).
        try:
            data = problem_store.get_statement_pdf(p.pid, source="supabase", client=_sb)
        except problem_store.ProblemNotFound:
            raise HTTPException(status_code=404,
                                detail="no PDF statement for this problem")
        except problem_store.ProblemUnavailable as e:
            print(f"[web] pdf unavailable for {p.pid}: {e}", file=sys.stderr)
            raise HTTPException(status_code=500, detail="problem store unavailable")
        etag = '"' + hashlib.sha256(data).hexdigest() + '"'
        headers = {"Content-Disposition": f'inline; filename="{p.pid}.pdf"',
                   "Cache-Control": _browse_cache_control(p.pid, email, 600), "Vary": "Cookie",
                   "ETag": etag}
        candidates = [tag.strip().removeprefix("W/")
                      for tag in request.headers.get("if-none-match", "").split(",")]
        if etag in candidates or "*" in candidates:
            return Response(status_code=304, headers=headers)
        return Response(content=data, media_type="application/pdf", headers=headers)
    f = grader.PROBLEMS_DIR / p.pid / "statement.pdf"
    if not f.is_file():
        raise HTTPException(status_code=404, detail="no PDF statement for this problem")
    return FileResponse(str(f), media_type="application/pdf", filename=f"{p.pid}.pdf",
                        content_disposition_type="inline",
                        headers={"Cache-Control": _browse_cache_control(p.pid, email, 600),
                                 "Vary": "Cookie"})


async def _read_source(language: str, code: str,
                       file: Optional[UploadFile]) -> tuple[str, Optional[str]]:
    """Validate language + code/file input. Returns (source, filename)."""
    if language not in LANGS:
        raise HTTPException(status_code=400,
                            detail=f"unsupported language: {language} (choose python/c/cpp)")
    source: Optional[str] = None
    filename: Optional[str] = None
    if file is not None and file.filename:
        filename = Path(file.filename).name
        # Cap the read itself: don't buffer an unbounded upload into RAM.
        raw = await file.read(MAX_CODE_BYTES + 1)
        if len(raw) > MAX_CODE_BYTES:
            raise HTTPException(status_code=413,
                                detail=f"file too large (max {MAX_CODE_BYTES} bytes)")
        if not raw.strip():
            raise HTTPException(status_code=400, detail="uploaded file is empty")
        try:
            source = raw.decode("utf-8")
        except UnicodeDecodeError:
            raise HTTPException(status_code=400, detail="file is not valid UTF-8 text")
        ext = Path(filename).suffix.lower()
        if EXT2LANG.get(ext) != language:
            raise HTTPException(
                status_code=400,
                detail=f"file extension '{ext or '(none)'}' does not match language '{language}'")
    elif code and code.strip():
        raw = code.encode("utf-8")
        if len(raw) > MAX_CODE_BYTES:
            raise HTTPException(status_code=413,
                                detail=f"code too large ({len(raw)} bytes, max {MAX_CODE_BYTES})")
        source = code
    else:
        raise HTTPException(status_code=400,
                            detail="submit code as text (code field) or attach a file (file field)")
    return source, filename


@app.post("/api/submit", status_code=202)
async def submit(
    request: Request,
    problem: str = Form(...),
    language: str = Form(...),
    code: str = Form(""),
    file: Optional[UploadFile] = File(None),
    name: str = Form(""),
    contest: str = Form(""),
):
    user = request.session.get("user") or {}
    if not user.get("email"):
        raise HTTPException(status_code=401, detail="login first")
    email = user["email"]
    await asyncio.to_thread(_ensure_problems_fresh)
    # Admission needs only authorized metadata and its version. The worker,
    # not the API, resolves private test bytes after claiming the durable job.
    meta = await asyncio.to_thread(_meta_or_404, problem)
    if not await asyncio.to_thread(lambda: _visible_to(meta.pid, email)):
        raise HTTPException(status_code=404, detail=f"unknown problem: {problem}")
    prob = meta

    contest_obj: Optional[dict] = None
    if contest:
        contest_obj = await asyncio.to_thread(lambda: _contest_or_404(contest))
        listed = {_resolve_pid(str(x)) for x in (contest_obj.get("problems") or [])}
        if prob.pid not in listed:
            raise HTTPException(status_code=400, detail="problem is not part of this contest")
        if not _is_admin(email):
            if _contest_window(contest_obj) != "running":
                raise HTTPException(status_code=403,
                                    detail="contest is not running (starts later or ended)")
            if not await asyncio.to_thread(lambda: _is_registered(contest_obj["id"], email)):
                raise HTTPException(status_code=403,
                                    detail="ยังไม่ได้ลงทะเบียนคอนเทสต์นี้ — กด Register "
                                           "ในหน้าคอนเทสต์ก่อน")

    if QUEUE_MODE == "local":
        _quota_check("submit", email, SUBMIT_QUOTA)

    source, filename = await _read_source(language, code, file)
    # Two independent Supabase reads: overlap them instead of paying both
    # round trips serially on the submit path.
    display, reveal = await asyncio.gather(
        asyncio.to_thread(_profile_display_name, email, user.get("name") or email),
        asyncio.to_thread(_effective_reveal, contest_obj, email, prob.pid),
    )
    tests_hash = (_TESTS_HASH_CACHE.get(prob.pid) if PROBLEM_SOURCE == "supabase"
                  else _tests_hash(prob.pid))
    if not tests_hash:
        raise HTTPException(status_code=503, detail="problem version unavailable")

    job = await _create_job({
        "kind": "submit", "user_email": email, "name": display[:50],
        "problem": prob.pid, "language": language, "filename": filename,
        "contest_id": contest_obj["id"] if contest_obj else None,
        "source": source, "reveal": reveal,
        "tests_hash": tests_hash,
        "grader_version": grader.GRADER_VERSION,
    })
    return {
        "job_id": job["id"], "status": "queued", "problem": prob.pid,
        "samples": len(prob.samples),
        "queued_ahead": await _queue_position_hint(),
    }


@app.get("/api/jobs/{jid}")
async def job_status(request: Request, jid: str) -> dict:
    user = request.session.get("user") or {}
    if not user.get("email"):
        raise HTTPException(status_code=401, detail="login required")
    job = (await _load_job(jid) if QUEUE_MODE == "database" else
           _jobs.get(jid) or await _load_job(jid))
    if job is None:
        raise HTTPException(status_code=404, detail="unknown job")
    if job.get("user_email") != user["email"] and not _is_admin(user["email"]):
        # Same 404 as unknown: don't confirm other users' job ids exist.
        raise HTTPException(status_code=404, detail="unknown job")
    status = job.get("status", "queued")
    result = job.get("result") if status == "done" else None
    if result is not None and not isinstance(result, dict):
        # Legacy/corrupt JSON could bypass the field-level redactor. Return no
        # verdict payload rather than passing an unknown structure to a client.
        result = None
    if isinstance(result, dict) and job.get("kind") != "playground":
        # Re-evaluate the owner-facing live policy: the job's reveal is a
        # creation-time snapshot and may predate a contest switching to hidden.
        # Use the job owner, never the requester, so an admin poll cannot
        # declassify another user's submission.
        reveal = await asyncio.to_thread(
            _live_reveal, job.get("contest_id"), job.get("user_email", ""),
            job.get("problem", ""))
        result = _result_with_reveal(job, result, reveal)
    return {
        "id": jid, "status": status, "problem": job.get("problem", ""),
        "kind": job.get("kind", "submit"), "error": job.get("error", "") or "",
        "queued_ahead": await _queue_position_hint() if status == "queued" else 0,
        "result": result,
        "created_at": job.get("created_at"), "started_at": job.get("started_at"),
        "finished_at": job.get("finished_at"),
    }


@app.get("/api/version")
def version(request: Request, response: Response) -> dict:
    """Which judging semantics + test set a verdict comes from."""
    # Hashes are precomputed at boot (_TESTS_HASH_CACHE); this stays O(n) dict
    # build with no SHA256. Per-user body: private cache only, never shared.
    response.headers["Cache-Control"] = "private, max-age=20"
    response.headers["Vary"] = "Cookie, Authorization"
    _ensure_problems_fresh()
    email = (request.session.get("user") or {}).get("email", "")
    admin = _is_admin(email)
    hidden = _hidden_pids()
    tests = {pid: {"hash": _tests_hash(pid), "tests": _problem_total(pid, p),
                   "samples": list(p.samples)}
             for pid, p in sorted(PROBLEMS.items())
             if admin or pid not in hidden}
    body = {
        "version": grader.GRADER_VERSION,
        "build_sha256": BUILD_SHA256,
        "problems": len(tests),
        "languages": sorted(LANGS.keys()),
        "system": _host_limits(),
        "tests": tests,
        "problem_source": PROBLEM_SOURCE,
    }
    if admin:
        body["hidden_problems"] = sorted(hidden)
        body["problems_total"] = len(PROBLEMS)
    return body


@app.post("/api/run")
async def run_custom(
    request: Request,
    language: str = Form(...),
    code: str = Form(""),
    file: Optional[UploadFile] = File(None),
    input: str = Form(""),
    problem: str = Form(""),
    time_limit: float = Form(0.0),
):
    """Playground: run the code once against user-supplied stdin. Not recorded."""
    user = request.session.get("user") or {}
    if not user.get("email"):
        raise HTTPException(status_code=401, detail="login first")
    if QUEUE_MODE == "local":
        _quota_check("run", user["email"], RUN_QUOTA)
    await asyncio.to_thread(_ensure_problems_fresh)
    prob = None
    if problem:
        if PROBLEM_SOURCE == "supabase":
            # A custom-input run needs limits/visibility, never private test bytes.
            prob = await asyncio.to_thread(_meta_or_404, problem)
            if not await asyncio.to_thread(_visible_to, prob.pid, user["email"]):
                raise HTTPException(status_code=404, detail=f"unknown problem: {problem}")
        else:
            prob = await asyncio.to_thread(lambda: _visible_problem_or_404(problem, user["email"]))
    source, _filename = await _read_source(language, code, file)
    if len(input.encode("utf-8")) > RUN_INPUT_MAX:
        raise HTTPException(status_code=413,
                            detail=f"input too large (max {RUN_INPUT_MAX} bytes)")
    tl = time_limit if 0 < time_limit <= RUN_TIMEOUT_MAX else (
        min(prob.time_limit, RUN_TIMEOUT_MAX) if prob else min(2.0, RUN_TIMEOUT_MAX))
    mem = prob.memory_limit if prob else 256
    job = await _create_job({"kind": "playground", "user_email": user["email"],
                             "problem": prob.pid if prob else "", "language": language,
                             "source": source, "payload": {"stdin_data": input,
                                 "time_limit": tl, "memory_mb": mem}})
    # API-only processes await a durable worker result; they never launch code.
    deadline = time.monotonic() + OVERALL_TIMEOUT + RUN_BUDGET + tl
    delay = 0.2
    while time.monotonic() < deadline:
        current = await _load_job(job["id"]) if QUEUE_MODE == "database" else _jobs.get(job["id"])
        if current and current.get("status") == "done":
            return current["result"]
        if current and current.get("status") == "error":
            error = current.get("error") or "internal run error"
            raise HTTPException(status_code=504 if "timed out" in error else 500,
                                detail=_sanitize_compile_error(error))
        await asyncio.sleep(delay)
        # Backoff: 0.2s -> 1.5s cap cuts DB polls ~5x on slow judges,
        # same deadline and return contract.
        delay = min(1.5, delay * 1.3)
    raise HTTPException(status_code=504, detail="run timed out waiting for judge")


_CASE_VERDICTS = frozenset({"AC", "WA", "TLE", "MLE", "OLE", "CE", "RE"})
_COMPLETED_SUBMISSION_VERDICTS = _CASE_VERDICTS | {"REJECTED"}
_HISTORY_PUBLIC_FIELDS = (
    "id", "created_at", "problem", "language", "name", "filename", "verdict",
    "accepted", "passed", "total", "score", "counts", "contest_id",
)


def _completed_peer_submission(row: dict) -> bool:
    # submissions stores completed judge results, unlike judge_jobs. Explicit
    # non-completed status/kind evidence in imported rows must still fail closed.
    return (row.get("kind", "submit") in {"submit", "rejudge"}
            and row.get("status", "done") == "done"
            and row.get("verdict") in _COMPLETED_SUBMISSION_VERDICTS)


def _peer_problem_policy(pid: str, contest_id: Optional[str] = None) -> str:
    """Fresh visibility and contest decision; empty string means unprotected."""
    prob = PROBLEMS.get(pid)
    if prob is None:
        return "hidden"
    hidden_problem = bool(getattr(prob, "hidden", False))
    if PROBLEM_SOURCE == "supabase":
        # Peer source is a disclosure decision. Do not trust the TTL catalog
        # for publication state: a problem can be unpublished or hidden while a
        # history-only replica still has the old Problem object.
        if _sb is None:
            return "unavailable"
        try:
            res = (_sb.table("problems").select("slug,is_published,hidden")
                   .eq("slug", pid).limit(1).execute())
            fresh_rows = res.data or []
        except Exception:
            return "unavailable"
        if not fresh_rows:
            return "hidden"
        fresh = fresh_rows[0]
        if not _parse_bool(fresh.get("is_published"), False):
            return "hidden"
        hidden_problem = _parse_bool(fresh.get("hidden"), False)
    elif not getattr(prob, "is_published", True):
        return "hidden"
    try:
        # Do not use the TTL cache or the legacy missing-table=empty fallback:
        # unavailable contest policy cannot authorize new peer source access.
        rows = _all_contest_rows()
        if not isinstance(rows, list):
            return "unavailable"
        related = []
        now = dt.datetime.now(dt.timezone.utc)
        for row in rows:
            if not isinstance(row, dict) or not isinstance(row.get("problems"), list):
                return "unavailable"
            if pid not in {_resolve_pid(str(token)) for token in row["problems"]}:
                continue
            try:
                start = dt.datetime.fromisoformat(str(row.get("starts_at")).replace("Z", "+00:00"))
                end = dt.datetime.fromisoformat(str(row.get("ends_at")).replace("Z", "+00:00"))
                if start.utcoffset() is None or end.utcoffset() is None or end <= start:
                    return "contest"
            except (TypeError, ValueError, OverflowError):
                return "contest"
            if now <= end:
                return "contest"  # Both upcoming and running contests protect it.
            related.append(row)
        if contest_id and not any(row.get("id") == contest_id for row in related):
            return "contest"  # Missing/deleted target contest policy is unknown.
        # Every related contest is now known to have ended, so it grants the
        # same problem visibility as the existing started-contest policy.
        if hidden_problem and not related:
            return "hidden"
        return ""
    except Exception:
        return "unavailable"


def _peer_inspection_access(email: str, pid: str, contest_id: Optional[str] = None) -> dict:
    """Source access for learning: any signed-in student, no attempt gate.

    The completed-row check and the narrower peer serializer stay in the
    detail route. Hidden/unpublished problems and problems in upcoming or
    running contests remain protected by the fresh policy check.
    """
    if _is_admin(email):
        return {"allowed": True, "reason": ""}
    if not email:
        return {"allowed": False, "reason": "login"}
    reason = _peer_problem_policy(pid, contest_id)
    return {"allowed": not reason, "reason": reason}


def _public_history_row(row: dict) -> dict:
    """Allowlist even when a storage adapter returns more than SELECT asked for."""
    public = {key: row[key] for key in _HISTORY_PUBLIC_FIELDS if key in row
              and (row[key] is None or isinstance(row[key], (str, int, float, bool)))}
    counts = row.get("counts")
    public["counts"] = {key: value for key, value in counts.items()
                        if key in _CASE_VERDICTS and type(value) is int and value >= 0} \
        if isinstance(counts, dict) else {}
    # Old profiles/submissions sometimes fell back to the authentication email.
    name = row.get("name")
    public["name"] = name if isinstance(name, str) and name.strip() and "@" not in name else "นักเรียน"
    return public


def _peer_submission_detail(row: dict) -> dict:
    public = _public_history_row(row)
    public["code"] = row.get("code") if isinstance(row.get("code"), str) else ""
    public["peer_inspection"] = True
    public["results"] = []
    for test in row.get("results", []) if isinstance(row.get("results"), list) else []:
        if not isinstance(test, dict):
            continue
        verdict = test.get("verdict")
        safe = {"verdict": verdict if isinstance(verdict, str) and verdict in _CASE_VERDICTS else "UNKNOWN",
                "hidden": True}
        for key in ("wall_s", "cpu_s", "peak_mb"):
            value = test.get(key)
            safe[key] = value if type(value) in (int, float) and math.isfinite(value) and value >= 0 else None
        public["results"].append(safe)
    return public


@app.get("/api/history")
async def history(request: Request, problem: Optional[str] = None, limit: int = 20,
                  mine: bool = False) -> dict:
    if _sb is None:
        return {"configured": False, "submissions": []}
    limit = max(1, min(limit, 100))
    u = request.session.get("user") or {}
    email = u.get("email", "")
    # Filter values are resolved on the loop; only the blocking HTTP call
    # to Supabase runs in a thread so the event loop stays responsive.
    wanted_problem = _resolve_pid(problem) if problem else ""
    if mine and not email:
        raise HTTPException(status_code=401, detail="login required")

    def _fetch():
        q = _sb.table("submissions").select(
            ",".join(_HISTORY_PUBLIC_FIELDS)).order("created_at", desc=True).limit(limit)
        if wanted_problem:
            q = q.eq("problem", wanted_problem)
        if mine:
            q = q.eq("user_email", email)
        return q.execute()

    try:
        res = await asyncio.to_thread(_fetch)
    except HTTPException:
        raise
    except Exception as e:
        raise _db_err(e)
    rows = res.data or []
    if not mine and not _is_admin(email):
        # The public feed must not advertise contest-only problems.
        hidden = _hidden_pids()
        contests = {r.get("id"): r for r in _contest_policy_rows()}
        # A hidden problem reached through an ended contest still has its
        # per-problem feed. The global feed must not advertise hidden tasks.
        visible_problem = wanted_problem and _visible_to(wanted_problem, email)
        rows = [r for r in rows if ((r.get("problem") or "") not in hidden
                or (visible_problem and r.get("problem") == wanted_problem))
                and not _contest_result_frozen(contests.get(r.get("contest_id")), r)]
    payload = {"configured": True, "submissions": [_public_history_row(row) for row in rows]}
    if wanted_problem and not mine:
        payload["peer_access"] = await asyncio.to_thread(_peer_inspection_access, email, wanted_problem)
    return payload


@app.get("/api/history/{sid}")
async def history_detail(request: Request, sid: str) -> dict:
    user = request.session.get("user") or {}
    email = user.get("email") or ""
    if not email:
        raise HTTPException(status_code=401, detail="login required")
    if _sb is None:
        raise HTTPException(status_code=404, detail="history not configured")
    try:
        res = await asyncio.to_thread(
            lambda: _sb.table("submissions").select("*").eq("id", sid).limit(1).execute())
    except Exception as e:
        raise _db_err(e)
    rows = res.data or []
    if not rows:
        raise HTTPException(status_code=404, detail="submission not found")
    row = dict(rows[0])
    owner = row.get("user_email") or ""
    # Owners/admins retain their existing feedback. Peers receive a narrower,
    # explicitly serialized source/results view after fresh authorization.
    # A non-owner admin is authorized to inspect the row, but must still use
    # the submission owner's feedback policy below; admin access is not a
    # reason to declassify a live hidden contest or an old practice row.
    if owner != email and not _is_admin(email):
        access = await asyncio.to_thread(_peer_inspection_access, email,
                                         row.get("problem", ""), row.get("contest_id"))
        if not access["allowed"] or not _completed_peer_submission(row):
            reason = access["reason"] or "incomplete submission"
            raise HTTPException(status_code=503 if reason == "unavailable" else 403,
                                detail="peer inspection locked: " + reason)
        return _peer_submission_detail(row)
    row.pop("user_email", None)  # never expose login emails publicly
    # Re-evaluate the live owner-facing policy: stored rows can predate the
    # current contest window (or an old rejudge), so trusting their bytes would
    # serve samples during a show_samples=false contest. Use the submission
    # owner, not the viewer, so an admin looking at someone else's row cannot
    # declassify it.
    reveal = await asyncio.to_thread(
        _live_reveal, row.get("contest_id"), owner, row.get("problem", ""))
    row["results"] = _result_with_reveal(row, {"results": row.get("results", [])}, reveal)["results"]
    return row


@app.post("/api/report")
async def create_report(request: Request) -> dict:
    user = request.session.get("user") or {}
    if not user.get("email"):
        raise HTTPException(status_code=401, detail="login required")
    if _sb is None:
        raise HTTPException(status_code=404, detail="report not configured")
    body = await _json_object(request)
    problem = _resolve_pid(str(body.get("problem", "")).strip())
    if problem not in PROBLEMS:
        raise HTTPException(status_code=400, detail="unknown problem")
    kind = str(body.get("kind", "")).strip()
    if kind not in REPORT_KINDS:
        raise HTTPException(status_code=400, detail="bad kind")
    message = str(body.get("message", "")).strip()
    if not (1 <= len(message) <= 1000):
        raise HTTPException(status_code=400, detail="message must be 1-1000 characters")
    name = await asyncio.to_thread(
        _profile_display_name, user.get("email", ""),
        user.get("name") or user.get("email", ""))
    try:
        res = await asyncio.to_thread(lambda: _sb.table("reports").insert({
            "problem": problem, "kind": kind, "message": message,
            "user_email": user["email"], "name": name, "status": "open",
        }).execute())
    except Exception as e:
        raise _db_err(e)
    rows = res.data or []
    return {"ok": True, "id": rows[0]["id"] if rows else None}


@app.get("/api/reports")
def list_reports(request: Request, mine: bool = False, all: bool = False,
                 problem: Optional[str] = None, limit: int = 50) -> dict:
    user = request.session.get("user") or {}
    if not user.get("email"):
        raise HTTPException(status_code=401, detail="login required")
    if _sb is None:
        return {"configured": False, "reports": []}
    limit = max(1, min(limit, 100))
    want_all = bool(all) and _is_admin(user["email"])
    if all and not want_all:
        raise HTTPException(status_code=403, detail="admin only")
    try:
        cols = ("id,created_at,problem,kind,message,status,name" +
                (",user_email" if want_all else ""))
        q = _sb.table("reports").select(cols).order(
            "created_at", desc=True).limit(limit)
        if problem:
            q = q.eq("problem", _resolve_pid(problem))
        if not want_all:
            q = q.eq("user_email", user["email"])
        res = q.execute()
    except HTTPException:
        raise
    except Exception as e:
        raise _db_err(e)
    return {"configured": True, "reports": res.data or []}


@app.put("/api/reports/{rid}")
async def set_report_status(request: Request, rid: str) -> dict:
    user = request.session.get("user") or {}
    if not _is_admin(user.get("email", "")):
        raise HTTPException(status_code=403, detail="admin only")
    if _sb is None:
        raise HTTPException(status_code=404, detail="report not configured")
    body = await _json_object(request)
    status = str(body.get("status", "")).strip()
    if status not in REPORT_STATUS:
        raise HTTPException(status_code=400, detail="bad status")
    try:
        await asyncio.to_thread(lambda: _sb.table("reports").update({"status": status}).eq("id", rid).execute())
    except Exception as e:
        raise _db_err(e)
    return {"ok": True}


_boot_time = time.monotonic()


# ---- admin: realtime admin management ----
def _admin_rows_sync() -> list:
    res = _sb.table("admins").select(
        "email,created_at,created_by").order("created_at").execute()
    rows = []
    for row in res.data or []:
        email = _norm_email(row.get("email"))
        if email:
            rows.append({"email": email,
                         "created_at": row.get("created_at"),
                         "created_by": row.get("created_by") or ""})
    return rows


@app.get("/api/admin/admins")
async def admin_list_admins(request: Request) -> dict:
    global _db_admin_emails
    user = request.session.get("user") or {}
    if not _is_admin(user.get("email", "")):
        raise HTTPException(status_code=403, detail="admin only")
    you = _norm_email(user.get("email"))
    configured = _sb is not None
    db_rows: list = []
    if configured:
        try:
            db_rows = await asyncio.to_thread(_admin_rows_sync)
        except Exception as e:
            raise _db_err(e)
        _db_admin_emails = frozenset(row["email"] for row in db_rows)
    seen = set()
    admins = []
    for email in sorted(ADMIN_EMAILS):
        admins.append({"email": email, "source": "env", "removable": False,
                       "created_at": None, "created_by": "ADMIN_EMAILS"})
        seen.add(email)
    for row in db_rows:
        if row["email"] in seen:
            continue
        admins.append({"email": row["email"], "source": "database",
                       "removable": row["email"] != you,
                       "created_at": row.get("created_at"),
                       "created_by": row.get("created_by") or ""})
        seen.add(row["email"])
    candidates = []
    if configured:
        try:
            res = await asyncio.to_thread(
                lambda: _sb.table("users").select("email,name").order(
                    "created_at", desc=True).limit(100).execute())
            for row in res.data or []:
                email = _norm_email(row.get("email"))
                if email and email not in seen:
                    candidates.append({"email": email,
                                       "name": row.get("name") or ""})
        except Exception:
            candidates = []
    return {"configured": configured, "you": you, "admins": admins,
            "candidates": candidates}


@app.post("/api/admin/admins")
async def admin_add_admin(request: Request) -> dict:
    global _db_admin_emails
    user = request.session.get("user") or {}
    actor = _norm_email(user.get("email"))
    if not _is_admin(actor):
        raise HTTPException(status_code=403, detail="admin only")
    if _sb is None:
        raise HTTPException(status_code=404, detail="supabase not configured")
    body = await _json_object(request)
    email = _norm_email(body.get("email"))
    if not _admin_email_valid(email):
        raise HTTPException(status_code=400, detail="invalid email")
    if email in ADMIN_EMAILS:
        raise HTTPException(status_code=409,
                            detail="this email is a bootstrap admin (ADMIN_EMAILS)")
    if email in _db_admin_emails:
        raise HTTPException(status_code=409, detail="already an admin")
    try:
        await asyncio.to_thread(
            lambda: _sb.table("admins").insert(
                {"email": email, "created_by": actor}).execute())
    except Exception as e:
        low = str(e).lower()
        if "23505" in low or "duplicate" in low or "unique" in low:
            raise HTTPException(status_code=409, detail="already an admin")
        raise _db_err(e)
    _db_admin_emails = _db_admin_emails | frozenset({email})
    return {"ok": True, "email": email, "source": "database"}


@app.delete("/api/admin/admins/{email}")
async def admin_remove_admin(request: Request, email: str) -> dict:
    global _db_admin_emails
    user = request.session.get("user") or {}
    actor = _norm_email(user.get("email"))
    if not _is_admin(actor):
        raise HTTPException(status_code=403, detail="admin only")
    target = _norm_email(email)
    if not _admin_email_valid(target):
        raise HTTPException(status_code=400, detail="invalid email")
    if target in ADMIN_EMAILS:
        raise HTTPException(
            status_code=409,
            detail="bootstrap admins are fixed in ADMIN_EMAILS on the server")
    if target == actor:
        raise HTTPException(status_code=400,
                            detail="cannot remove your own admin access")
    if _sb is None:
        raise HTTPException(status_code=404, detail="supabase not configured")
    if target not in _db_admin_emails:
        await asyncio.to_thread(_load_db_admins_sync)
    if target not in _db_admin_emails:
        raise HTTPException(status_code=404, detail="admin not found")
    try:
        await asyncio.to_thread(
            lambda: _sb.table("admins").delete().eq("email", target).execute())
    except Exception as e:
        raise _db_err(e)
    _db_admin_emails = frozenset(e for e in _db_admin_emails if e != target)
    return {"ok": True, "email": target}


_ADMIN_HISTORY_FIELDS = (
    "id", "created_at", "user_email", "name", "problem", "language",
    "verdict", "accepted", "passed", "total", "score", "counts", "contest_id",
)


def _admin_history_row(row: dict) -> dict:
    """Allowlist metadata even when a storage adapter ignores the projection."""
    return {key: row.get(key) for key in _ADMIN_HISTORY_FIELDS}


@app.get("/api/admin/submissions")
async def admin_submissions(request: Request, page: int = 1, per_page: int = 20,
                            problem: Optional[str] = None) -> dict:
    """Paginated all-user submission history (metadata only, never source)."""
    user = request.session.get("user") or {}
    if not _is_admin(user.get("email", "")):
        raise HTTPException(status_code=403, detail="admin only")
    if _sb is None:
        raise HTTPException(status_code=404, detail="supabase not configured")
    if per_page not in (20, 50, 100):
        per_page = 20
    page = max(1, page)
    wanted_problem = _resolve_pid(problem) if problem else ""

    def _fetch():
        q = _sb.table("submissions").select(
            "id,created_at,user_email,name,problem,language,verdict,accepted,"
            "passed,total,score,counts,contest_id", count="exact")
        if wanted_problem:
            q = q.eq("problem", wanted_problem)
        return q.order("created_at", desc=True).order("id", desc=True).range(
            (page - 1) * per_page, page * per_page - 1).execute()

    try:
        res = await asyncio.to_thread(_fetch)
    except Exception as e:
        raise _db_err(e)
    total = res.count or 0
    return {
        "configured": True,
        "page": page,
        "per_page": per_page,
        "total": total,
        "pages": max(1, (total + per_page - 1) // per_page),
        "submissions": [_admin_history_row(row) for row in (res.data or [])],
    }


@app.get("/api/admin/overview")
async def admin_overview(request: Request) -> dict:
    user = request.session.get("user") or {}
    if not _is_admin(user.get("email", "")):
        raise HTTPException(status_code=403, detail="admin only")
    if _sb is None:
        raise HTTPException(status_code=404, detail="supabase not configured")
    await asyncio.to_thread(_ensure_problems_fresh)

    def _fetch():
        n_users = (_sb.table("users").select("email", count="exact").limit(1)
                   .execute().count) or 0
        subs = _sb.table("submissions").select(
            "id,created_at,problem,name,language,verdict,accepted,score,counts").order(
            "created_at", desc=True).limit(500).execute().data or []
        reps = _sb.table("reports").select("status", count="exact").limit(1).execute()
        rep_open = _sb.table("reports").select("id", count="exact").eq(
            "status", "open").limit(1).execute().count or 0
        return n_users, subs, reps, rep_open

    try:
        n_users, subs, reps, rep_open = await asyncio.to_thread(_fetch)
    except Exception as e:
        raise _db_err(e)
    per_problem: dict = {}
    for r in subs:
        p = per_problem.setdefault(r["problem"],
                                   {"subs": 0, "accepted": 0, "best": 0})
        p["subs"] += 1
        if r.get("accepted"):
            p["accepted"] += 1
        p["best"] = max(p["best"], r.get("score") or 0)
    hidden = sorted(_hidden_pids())
    listed, contest_hidden = _contest_problem_sets()
    if QUEUE_MODE == "database":
        try:
            counts = await asyncio.to_thread(
                lambda: _sb.rpc("judge_queue_diagnostics", {}).execute().data)
        except Exception as e:
            raise HTTPException(status_code=503, detail="judge database unavailable") from e
    else:
        counts = {status: sum(j.get("status") == status for j in _jobs.values())
                  for status in ("queued", "running")}
    queued = int(counts.get("queued", 0) or 0)
    return {
        "problems": len(PROBLEMS),
        "problems_public": len(PROBLEMS) - len(hidden),
        "problems_contest_only": [
            {"id": pid, "title": PROBLEMS[pid].title,
             "config_hidden": PROBLEMS[pid].hidden,
             "contest_hidden": pid in contest_hidden,
             "in_contest": pid in listed}
            for pid in hidden if pid in PROBLEMS
        ],
        "users": n_users,
        "submissions": len(subs),
        "submissions_truncated": len(subs) >= 500,
        "accepted": sum(1 for r in subs if r.get("accepted")),
        "reports_open": rep_open,
        "reports_total": (reps.count if reps else 0) or 0,
        "jobs": {
            **counts,
            "queue_depth": queued,
            "workers": JUDGE_WORKERS,
            "queue_max": QUEUE_MAX,
            # Ops monitor: frontend shows a banner when true.
            "queue_alert": queued >= 5,
            "queue_full": queued >= QUEUE_MAX,
        },
        "per_problem": per_problem,
        "recent": [{k: r.get(k) for k in
                    ("id", "created_at", "problem", "name", "language",
                     "verdict", "accepted", "score", "counts")}
                   for r in subs[:10]],
        "system": {"uptime_s": int(time.monotonic() - _boot_time),
                   "problems_loaded": sorted(PROBLEMS.keys()), **_host_limits()},
    }


@app.get("/api/stats")
async def stats() -> dict:
    if _sb is None:
        return {"configured": False, "stats": {}}
    await asyncio.to_thread(_ensure_problems_fresh)
    hidden = await asyncio.to_thread(_hidden_pids)
    visible = set(PROBLEMS) - hidden
    result = await _acached("stats", CACHE_TTL["stats"], _build_stats)
    # Cached aggregates are internal; reapply current visibility on every read.
    return {**result, "stats": {pid: row for pid, row in result["stats"].items()
                                if pid in visible}}


def _build_stats() -> dict:
    """Per-problem aggregates from the `problem_stats` view (all-time), with a
    bounded fallback scan when the view has not been created yet."""
    try:
        res = _sb.table("problem_stats").select(
            "problem,subs,accepted,best,total_score,solvers").limit(1000).execute()
        rows = res.data or []
    except Exception as e:
        print(f"[web] problem_stats view unavailable ({e}); falling back to a 500-row "
              "scan — run supabase/schema.sql", file=sys.stderr)
        try:
            res = _sb.table("submissions").select(
                "problem,accepted,score,user_email,contest_id").order(
                "created_at", desc=True).limit(500).execute()
        except Exception as e2:
            raise _db_err(e2)
        agg: dict = {}
        for r in [x for x in (res.data or []) if not x.get("contest_id")]:
            p = agg.setdefault(r["problem"], {"subs": 0, "accepted": 0, "best": 0,
                                              "total_score": 0, "solvers": set()})
            p["subs"] += 1
            if r["accepted"]:
                p["accepted"] += 1
                if r.get("user_email"):
                    p["solvers"].add(r["user_email"])
            p["best"] = max(p["best"], r["score"] or 0)
            p["total_score"] += r["score"] or 0
        rows = [{"problem": k, "subs": v["subs"], "accepted": v["accepted"],
                 "best": v["best"], "total_score": v["total_score"],
                 "solvers": len(v["solvers"])} for k, v in agg.items()]
    out = {}
    for r in rows:
        subs = r.get("subs") or 0
        out[r["problem"]] = {
            "subs": subs, "accepted": r.get("accepted") or 0,
            "best": r.get("best") or 0, "solvers": r.get("solvers") or 0,
            "avg": round((r.get("total_score") or 0) / subs, 1) if subs else 0.0,
        }
    return {"configured": True, "stats": out}


# ---- contests (B7) ----
CONTEST_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{1,39}$")


def _norm_ts(value: str, field: str) -> str:
    try:
        parsed = dt.datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
    except Exception:
        raise HTTPException(status_code=400, detail=f"{field} must be ISO-8601 (e.g. 2026-11-01T09:00:00Z)")
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.astimezone(dt.timezone.utc).isoformat()


def _contest_public(row: dict, email: str = "", counts: Dict[str, int] | None = None,
                    registered_ids: Optional[set] = None) -> dict:
    cid = row.get("id", "")
    status = _contest_window(row)
    upcoming = status == "upcoming"
    admin = _is_admin(email)
    # Before the start, non-admins see neither the problem slugs nor any detail:
    # the list opens when the contest does. Admins always see everything.
    show_problems = admin or not upcoming
    # Resolve each slug once; the old comprehension resolved it up to three
    # times per problem just to filter it.
    problems = []
    if show_problems:
        for raw in row.get("problems") or []:
            pid = _resolve_pid(str(raw))
            prob = PROBLEMS.get(pid)
            if admin or (prob is not None and getattr(prob, "is_published", True)):
                problems.append(pid)
    registered = (cid in registered_ids if registered_ids is not None else
                  (_is_registered(cid, email) if _registrations_supported() else False))
    return {
        "participants": (counts or {}).get(cid, 0),
        "registered": registered,
        "id": cid, "title": row.get("title", ""),
        "starts_at": row.get("starts_at"), "ends_at": row.get("ends_at"),
        "freeze_minutes": row.get("freeze_minutes") or 0,
        "show_samples": row.get("show_samples", True),
        "hide_from_tasks": row.get("hide_from_tasks", True),
        "status": status,
        "problems": problems,
    }


def _contest_rows() -> List[dict]:
    res = _sb.table("contests").select("*").order("starts_at", desc=True).limit(200).execute()
    return res.data or []


def _all_contest_rows() -> List[dict]:
    """Every contest, paginated: policy decisions (visibility/reveal) must not
    silently drop contests past the 200-row listing limit."""
    rows: List[dict] = []
    offset = 0
    page = 500
    for _ in range(1000):
        # One extra row can establish that the 10k safety bound was exceeded;
        # an empty response at exactly 10k still proves complete exhaustion.
        requested = min(page, 10001 - offset)
        q = (_sb.table("contests").select("*")
             .order("starts_at", desc=True).order("id", desc=True))
        if hasattr(q, "range"):
            res = q.range(offset, offset + requested - 1).execute()
        elif hasattr(q, "offset"):
            res = q.offset(offset).limit(requested).execute()
        else:
            raise RuntimeError("contest policy pagination unavailable")
        batch = res.data
        if (not isinstance(batch, list) or len(batch) > requested
                or any(not isinstance(row, dict) for row in batch)):
            raise RuntimeError("contest policy page malformed")
        if not batch:
            return rows
        rows.extend(batch)
        offset += len(batch)
        if offset > 10000:
            break
        # A configured PostgREST response cap can make every page short.
        # Only an empty page proves that all protective contests were read.
    raise RuntimeError("contest policy exceeds pagination budget")


@app.get("/api/contests")
def list_contests(request: Request, response: Response = None) -> dict:
    if response is not None:
        response.headers["Cache-Control"] = "private, no-store"
        response.headers["Vary"] = "Cookie, Authorization"
    if _sb is None:
        return {"configured": False, "contests": []}
    try:
        email = (request.session.get("user") or {}).get("email", "")
        rows = _contest_rows()
        if not rows:
            return {"configured": True, "contests": []}
        counts = _registration_counts()
        # Batched registration lookup for the whole listing, not one per contest.
        registered = _registered_contest_ids(
            email, [r.get("id", "") for r in rows if r.get("id")])
        return {"configured": True,
                "contests": [_contest_public(r, email, counts, registered)
                             for r in rows]}
    except Exception as e:
        # Read-only listing: degrade instead of 500 while the contests table is
        # missing (schema migration not run yet).
        print(f"[web] contests listing failed: {e}", file=sys.stderr)
        return {"configured": False, "contests": [],
                "hint": "contests table missing - run supabase/schema.sql"}


@app.post("/api/contests")
async def upsert_contest(request: Request) -> dict:
    user = request.session.get("user") or {}
    if not _is_admin(user.get("email", "")):
        raise HTTPException(status_code=403, detail="admin only")
    if _sb is None:
        raise HTTPException(status_code=404, detail="contests not configured")
    await asyncio.to_thread(_ensure_problems_fresh)
    body = await _json_object(request)
    cid = str(body.get("id", "")).strip().lower()
    if not CONTEST_ID_RE.match(cid):
        raise HTTPException(status_code=400,
                            detail="id must be 2-40 chars: a-z 0-9 _ -")
    title = str(body.get("title", "")).strip()[:120] or cid
    starts = _norm_ts(body.get("starts_at", ""), "starts_at")
    ends = _norm_ts(body.get("ends_at", ""), "ends_at")
    if ends <= starts:
        raise HTTPException(status_code=400, detail="ends_at must be after starts_at")
    problems = [_resolve_pid(str(p)) for p in (body.get("problems") or [])]
    unknown = [p for p in problems if p not in PROBLEMS]
    if not problems or unknown:
        raise HTTPException(status_code=400,
                            detail=f"problems must be a non-empty list of problem ids (unknown: {unknown})")
    try:
        freeze = max(0, min(int(body.get("freeze_minutes") or 0), 24 * 60))
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="freeze_minutes must be a number")
    row = {
        "id": cid, "title": title, "starts_at": starts, "ends_at": ends,
        "problems": problems, "freeze_minutes": freeze,
        "show_samples": _parse_bool(body.get("show_samples", True), True),
        "hide_from_tasks": _parse_bool(body.get("hide_from_tasks", True), True),
        "created_by": user["email"],
    }
    try:
        await asyncio.to_thread(lambda: _sb.table("contests").upsert(row, on_conflict="id").execute())
    except Exception as e:
        if "hide_from_tasks" in str(e):
            raise HTTPException(status_code=400,
                                detail="contests table is missing hide_from_tasks — run supabase/schema.sql")
        raise _db_err(e)
    _contest_pid_cache["at"] = 0.0   # visibility changed
    _reg_count_cache["at"] = 0.0
    counts = await asyncio.to_thread(_registration_counts)
    public = await asyncio.to_thread(_contest_public, row, user["email"], counts)
    return {"ok": True, "contest": public}


@app.delete("/api/contests/{cid}")
def delete_contest(request: Request, cid: str) -> dict:
    user = request.session.get("user") or {}
    if not _is_admin(user.get("email", "")):
        raise HTTPException(status_code=403, detail="admin only")
    if _sb is None:
        raise HTTPException(status_code=404, detail="contests not configured")
    try:
        _sb.table("contests").delete().eq("id", cid).execute()
    except Exception as e:
        raise _db_err(e)
    _contest_pid_cache["at"] = 0.0   # visibility changed
    return {"ok": True}


def _contest_result_frozen(contest: Optional[dict], submission: dict) -> bool:
    if not contest:
        return False
    if not _contest_times_valid(contest):
        return True  # malformed policy cannot disclose a contest result
    if _contest_window(contest) != "running":
        return False
    try:
        minutes = int(contest.get("freeze_minutes") or 0)
        if minutes <= 0:
            return False
        end = dt.datetime.fromisoformat(str(contest["ends_at"]).replace("Z", "+00:00"))
        created = dt.datetime.fromisoformat(str(submission["created_at"]).replace("Z", "+00:00"))
        return created > end - dt.timedelta(minutes=minutes)
    except (ValueError, TypeError, KeyError):
        return True  # malformed dates cannot disclose a frozen result


def _contest_scoreboard(contest: dict, live: bool, allowed_pids: Optional[set] = None) -> List[dict]:
    """Best score per problem per user inside one contest.

    Non-admins see a frozen board while the contest runs: submissions newer than
    ends_at - freeze_minutes are hidden until the contest is over.
    """
    if _sb is None:
        return []
    # Precompute the freeze cutoff once: parsing ends_at for every submission
    # (contest boards can hold tens of thousands of rows) is pure overhead.
    # freeze_all mirrors _contest_result_frozen's fail-closed behaviour when the
    # contest timestamps are malformed.
    freeze_cutoff = None
    freeze_all = False
    if not live and not _contest_times_valid(contest):
        freeze_all = True
    elif not live and _contest_window(contest) == "running":
        try:
            minutes = int(contest.get("freeze_minutes") or 0)
            if minutes > 0:
                end = dt.datetime.fromisoformat(
                    str(contest["ends_at"]).replace("Z", "+00:00"))
                freeze_cutoff = end - dt.timedelta(minutes=minutes)
        except (ValueError, TypeError, KeyError):
            freeze_all = True
    # Push the published-problem filter into the query: rows for removed or
    # unpublished problems are discarded below anyway, so fetching them wastes
    # bandwidth and memory on large boards. An empty allowed set means no row
    # can ever count, so skip the database entirely.
    wanted = list(allowed_pids) if allowed_pids is not None else None
    if wanted is not None and not wanted:
        return []
    rows = []
    offset = 0
    try:
        # PostgREST returns at most 1000 rows per request by default, so fetch
        # full pages. 500-row pages doubled the round trips on large boards.
        while offset < 50000:
            query = _sb.table("submissions").select(
                "user_email,name,problem,score,accepted,created_at", count="exact").eq(
                "contest_id", contest.get("id", ""))
            if wanted is not None:
                query = query.in_("problem", wanted)
            res = query.order("created_at").order("id").range(
                offset, offset + 999).execute()
            batch = res.data or []
            rows.extend(batch)
            offset += len(batch)
            total = res.count
            if total is not None and offset >= total:
                break
            if not batch:
                if total is None:
                    break
                raise HTTPException(status_code=503, detail="incomplete scoreboard data")
        else:
            raise HTTPException(status_code=503, detail="scoreboard exceeds safe query budget")
    except HTTPException:
        raise
    except Exception as e:
        raise _db_err(e)
    users: Dict[str, dict] = {}
    for r in rows:
        if allowed_pids is not None and r.get("problem") not in allowed_pids:
            continue
        if not live and (freeze_all or freeze_cutoff is not None):
            if freeze_all:
                continue
            try:
                created = dt.datetime.fromisoformat(
                    str(r["created_at"]).replace("Z", "+00:00"))
                frozen = created > freeze_cutoff
            except (ValueError, TypeError, KeyError):
                frozen = True  # malformed dates cannot disclose a frozen result
            if frozen:
                continue
        u = r.get("user_email") or ""
        if not u:
            continue
        agg = users.setdefault(u, {"name": r.get("name") or "?", "best": {}, "solved": set()})
        agg["name"] = r.get("name") or agg["name"]
        p = r.get("problem") or ""
        agg["best"][p] = max(agg["best"].get(p, 0), r.get("score") or 0)
        if r.get("accepted"):
            agg["solved"].add(p)
    board = [{"name": a["name"], "points": sum(a["best"].values()),
              "solved": len(a["solved"]), "per_problem": a["best"]}
             for a in users.values()]
    board.sort(key=lambda t: (-t["points"], -t["solved"], t["name"]))
    return board


@app.get("/api/contests/{cid}")
def contest_detail(request: Request, cid: str) -> dict:
    _ensure_problems_fresh()
    contest = _contest_or_404(cid)
    user = request.session.get("user") or {}
    email = user.get("email", "")
    listed = [_resolve_pid(str(x)) for x in (contest.get("problems") or [])]
    status = _contest_window(contest)
    live = _is_admin(email) or status != "running"
    admin = _is_admin(email)
    started = _contest_started(contest)
    problems = []
    # Upcoming contest: non-admins see no problem list at all before the start
    # (not even slugs) — it opens when the contest does. Editing the contest
    # mid-round (upsert with more problems) takes effect immediately.
    show_details = admin or started
    for pid in listed:
        p = PROBLEMS.get(pid)
        if p is None:
            continue
        if not show_details:
            continue
        if not admin and not getattr(p, "is_published", True):
            continue
        problems.append({"id": p.pid, "no": p.no, "title": p.title,
                         "difficulty": p.difficulty,
                         "tags": p.tags, "tests": _problem_total(pid, p),
                         "samples": len(p.samples),
                         "time_limit": p.time_limit, "memory_limit": p.memory_limit})
    public = _contest_public(contest, email, _registration_counts())
    body = {
        **public,
        "problem_details": problems,
        "frozen": not live and (contest.get("freeze_minutes") or 0) > 0,
        "scoreboard": _contest_scoreboard(contest, live, {p["id"] for p in problems}),
        # Codeforces-style lifecycle: register before the start, problems open at
        # the start, submissions only while running and only for participants.
        # Reuse the registration check already made by _contest_public above
        # instead of paying a second round trip for the same row.
        "problems_open": started or admin,
        "can_submit": (status == "running" and public["registered"]) or admin,
        "starts_in_s": _seconds_until(contest.get("starts_at")),
        "ends_in_s": _seconds_until(contest.get("ends_at")),
        "registration_open": status != "ended" or admin,
        "registrations_supported": _registrations_supported(),
    }
    if admin and _registrations_supported():
        try:
            res = _sb.table("contest_registrations").select(
                "user_email,name,registered_at").eq(
                "contest_id", contest.get("id", "")).order(
                "registered_at").limit(1000).execute()
            body["participants_list"] = [
                {"name": r.get("name") or r.get("user_email") or "?",
                 "email": r.get("user_email") or "",
                 "at": r.get("registered_at")}
                for r in (res.data or [])]
        except Exception as e:
            print(f"[web] participants lookup failed: {e}", file=sys.stderr)
    return body


def _seconds_until(ts: object) -> Optional[int]:
    """Seconds from now until an ISO timestamp (negative = already passed)."""
    try:
        when = dt.datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except Exception:
        return None
    return int((when - dt.datetime.now(dt.timezone.utc)).total_seconds())


@app.post("/api/contests/{cid}/register")
async def register_contest(request: Request, cid: str) -> dict:
    user = request.session.get("user") or {}
    email = user.get("email", "")
    if not email:
        raise HTTPException(status_code=401, detail="login first")
    contest = await asyncio.to_thread(lambda: _contest_or_404(cid))
    if not await asyncio.to_thread(lambda: _registrations_supported()):
        raise HTTPException(status_code=503,
                            detail="contest registration is off — run supabase/schema.sql")
    if _contest_window(contest) == "ended" and not _is_admin(email):
        raise HTTPException(status_code=400, detail="contest already ended")
    name = await asyncio.to_thread(
        _profile_display_name, email, user.get("name") or email)
    try:
        await asyncio.to_thread(lambda: _sb.table("contest_registrations").upsert(
            {"contest_id": cid, "user_email": email, "name": name[:50]},
            on_conflict="contest_id,user_email").execute())
    except Exception as e:
        _register_note_missing_table(e)
        raise _db_err(e)
    _reg_count_cache["at"] = 0.0
    return {"ok": True, "registered": True, "participants": await asyncio.to_thread(lambda: _registration_count(cid))}


@app.delete("/api/contests/{cid}/register")
async def unregister_contest(request: Request, cid: str) -> dict:
    user = request.session.get("user") or {}
    email = user.get("email", "")
    if not email:
        raise HTTPException(status_code=401, detail="login first")
    contest = await asyncio.to_thread(lambda: _contest_or_404(cid))
    if _contest_window(contest) == "running" and not _is_admin(email):
        raise HTTPException(status_code=400,
                            detail="contest is running — cannot unregister now")
    try:
        await asyncio.to_thread(lambda: _sb.table("contest_registrations").delete().eq("contest_id", cid).eq(
            "user_email", email).execute())
    except Exception as e:
        _register_note_missing_table(e)
        raise _db_err(e)
    _reg_count_cache["at"] = 0.0
    return {"ok": True, "registered": False, "participants": await asyncio.to_thread(lambda: _registration_count(cid))}


# ---- contest announcements (clarifications during a round) ----
# Admins post; everyone reads once the contest has started (running/ended).
# Before the start only admins see them — announcements may hint at problems.
_annc_table_ok: Dict[str, bool] = {"value": True}


def _announcements_supported() -> bool:
    return _sb is not None and _annc_table_ok["value"]


def _annc_note_missing_table(e: Exception) -> None:
    if "contest_announcements" in str(e) and _annc_table_ok["value"]:
        _annc_table_ok["value"] = False
        print("[web] contest_announcements table missing — announcements off; "
              "run supabase/schema.sql", file=sys.stderr)


@app.get("/api/contests/{cid}/announcements")
def contest_announcements(request: Request, cid: str) -> dict:
    contest = _contest_or_404(cid)
    user = request.session.get("user") or {}
    email = user.get("email", "")
    admin = _is_admin(email)
    if _sb is None:
        return {"configured": False, "announcements": []}
    if not admin and not _contest_started(contest):
        return {"configured": True, "announcements": [], "locked": True}
    if not _announcements_supported():
        return {"configured": True, "announcements": [], "unconfigured": True}
    try:
        res = _sb.table("contest_announcements").select(
            "id,body,created_at").eq("contest_id", cid).order(
            "created_at").limit(200).execute()
    except Exception as e:
        _annc_note_missing_table(e)
        if not _annc_table_ok["value"]:
            return {"configured": True, "announcements": [], "unconfigured": True}
        raise _db_err(e)
    return {"configured": True, "announcements": [
        {"id": r.get("id"), "body": r.get("body") or "",
         "created_at": r.get("created_at")} for r in (res.data or [])]}


@app.post("/api/contests/{cid}/announcements")
async def post_announcement(request: Request, cid: str) -> dict:
    user = request.session.get("user") or {}
    if not _is_admin(user.get("email", "")):
        raise HTTPException(status_code=403, detail="admin only")
    if _sb is None:
        raise HTTPException(status_code=404, detail="announcements not configured")
    await asyncio.to_thread(lambda: _contest_or_404(cid))
    body = await _json_object(request)
    text = str(body.get("body", "")).strip()
    if not (1 <= len(text) <= 2000):
        raise HTTPException(status_code=400, detail="body must be 1-2000 characters")
    row = {"id": str(uuid.uuid4()), "contest_id": cid, "body": text,
           "created_by": user.get("email", "")}
    try:
        await asyncio.to_thread(lambda: _sb.table("contest_announcements").insert(row).execute())
    except Exception as e:
        _annc_note_missing_table(e)
        raise _db_err(e)
    return {"ok": True, "id": row["id"]}


# ---- admin: rejudge (A4) + plagiarism (B8) ----
@app.post("/api/admin/rejudge")
async def admin_rejudge(request: Request) -> dict:
    """Re-run stored submissions against the current tests.

    Body: {"ids": [...]} or {"problem": "count_islands", "limit": 50}.
    """
    user = request.session.get("user") or {}
    if not _is_admin(user.get("email", "")):
        raise HTTPException(status_code=403, detail="admin only")
    if _sb is None:
        raise HTTPException(status_code=404, detail="history not configured")
    await asyncio.to_thread(_ensure_problems_fresh)
    body = await _json_object(request)
    ids = [str(i) for i in (body.get("ids") or [])][:REJUDGE_MAX]
    problem = _resolve_pid(str(body.get("problem", "")).strip())
    try:
        limit = max(1, min(int(body.get("limit") or 50), REJUDGE_MAX))
    except (TypeError, ValueError):
        limit = 50
    # One worker on a fractional core: a 200-row rejudge would block students for
    # hours. Keep the batch within the queue budget; the admin can call again.
    depth = ((await _queue_rpc("diagnostics"))["queued"] if QUEUE_MODE == "database"
             else _queue_depth())
    budget = min(REJUDGE_MAX, max(0, QUEUE_MAX - depth))
    if budget == 0:
        return {"queued": 0, "skipped": len(ids), "jobs": []}
    over_budget = 0
    if limit > budget:
        limit = budget
    if len(ids) > budget:
        over_budget = len(ids) - budget
        ids = ids[:budget]
    cols = "id,problem,language,filename,name,user_email,code,contest_id"
    try:
        if ids:
            res = await asyncio.to_thread(lambda: _sb.table("submissions").select(cols).in_("id", ids).execute())
        else:
            if not problem:
                raise HTTPException(status_code=400, detail="give ids[] or problem")
            res = await asyncio.to_thread(lambda: _sb.table("submissions").select(cols).eq("problem", problem).order(
                "created_at", desc=True).limit(limit).execute())
    except HTTPException:
        raise
    except Exception as e:
        raise _db_err(e)
    queued: List[str] = []
    skipped: List[str] = []
    # Cache contests once: rejudged rows keep their original contest's policy,
    # so a live show_samples=false contest stays REVEAL_NONE.
    _contest_cache: Dict[str, Optional[dict]] = {}
    _practice_reveal: Dict[str, str] = {}
    def _reveal_for_row(cid: object, owner: str, pid: str) -> str:
        if not cid:
            # Practice feedback follows the same live policy as submit: hidden
            # during any running show_samples=false contest, PRACTICE otherwise.
            if pid not in _practice_reveal:
                _practice_reveal[pid] = _effective_reveal(None, owner, pid)
            return _practice_reveal[pid]
        ckey = str(cid)
        if ckey not in _contest_cache:
            try:
                _contest_cache[ckey] = _contest_or_404(ckey)
            except HTTPException:
                # Unknown/unavailable contest: fail closed, never samples.
                _contest_cache[ckey] = None
        contest = _contest_cache[ckey]
        return _reveal_for(contest, owner) if contest is not None else REVEAL_NONE
    for row in res.data or []:
        pid = row.get("problem", "")
        lang = row.get("language", "")
        code = row.get("code") or ""
        if not code.strip() or pid not in PROBLEMS or lang not in LANGS:
            skipped.append(str(row.get("id")))
            continue
        try:
            job = await _create_job({
                "kind": "rejudge", "user_email": row.get("user_email") or "",
                "name": row.get("name") or "", "problem": pid, "language": lang,
                "filename": row.get("filename"), "source": code,
                "contest_id": row.get("contest_id"),
                "target_id": row.get("id"),
                # Owner-facing storage policy, never the requester's: an admin
                # rejudge must not declassify a live show_samples=false contest.
                "reveal": _reveal_for_row(
                    row.get("contest_id"), row.get("user_email") or "", pid),
                "tests_hash": _tests_hash(pid),
                "grader_version": grader.GRADER_VERSION,
            }, track_active=False)
        except HTTPException as e:
            if e.status_code != 503:
                raise
            skipped.append(str(row.get("id")))
            continue
        queued.append(job["id"])
    skipped_count = len(skipped) + over_budget
    return {"queued": len(queued), "skipped": skipped_count, "jobs": queued}


def _plagiarism_module():
    """tools/plagiarism.py is a standalone script: load it without packaging."""
    path = ROOT / "tools" / "plagiarism.py"
    if not path.is_file():
        return None
    import importlib.util
    spec = importlib.util.spec_from_file_location("ng_plagiarism", path)
    if spec is None or spec.loader is None:
        return None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@app.get("/api/admin/plagiarism")
def admin_plagiarism(request: Request, problem: str = "",
                     threshold: float = 0.6, limit: int = 300) -> dict:
    """Similar-code pairs among the most recent submissions of one problem."""
    _ensure_problems_fresh()
    user = request.session.get("user") or {}
    if not _is_admin(user.get("email", "")):
        raise HTTPException(status_code=403, detail="admin only")
    problem = _resolve_pid(problem)
    if problem not in PROBLEMS:
        raise HTTPException(status_code=400, detail="unknown problem")
    if _sb is None:
        raise HTTPException(status_code=404, detail="history not configured")
    mod = _plagiarism_module()
    if mod is None:
        raise HTTPException(status_code=501, detail="tools/plagiarism.py not available")
    try:
        threshold = min(max(float(threshold), 0.05), 1.0)
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="threshold must be a number 0.05..1.0")
    try:
        limit = max(2, min(int(limit), 600))
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="limit must be an integer 2..600")
    try:
        res = _sb.table("submissions").select(
            "id,name,user_email,language,code,created_at").eq(
            "problem", problem).order("created_at", desc=True).limit(limit).execute()
    except Exception as e:
        raise _db_err(e)
    rows = [{"id": r["id"], "name": r.get("name") or "?", "problem": problem,
             "language": r.get("language") or "", "code": r.get("code") or ""}
            for r in (res.data or []) if (r.get("code") or "").strip()]
    pairs = mod.similar_pairs(rows, threshold=threshold)
    return {"problem": problem, "threshold": threshold, "scanned": len(rows),
            "pairs": pairs[:200]}


@app.get("/api/{path:path}")
async def api_unknown(path: str) -> dict:
    """Unknown member /api/* returns JSON 404 after authentication, never SPA HTML.

    The static mount below (html=True) serves index.html for any unknown path;
    without this guard a mistyped API URL returns 200 HTML and clients fail
    with a JSON parse error instead of a clear 404.
    """
    raise HTTPException(status_code=404, detail="unknown api endpoint")


app.mount("/", StaticFiles(directory=str(STATIC_DIR), html=True), name="static")


if __name__ == "__main__":
    import argparse
    import uvicorn

    ap = argparse.ArgumentParser(description="Brutal grader web server")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8000)
    args = ap.parse_args()
    uvicorn.run(app, host=args.host, port=args.port)
